# sniper_bot_graduacion_inmediata.py
# SNIPER BOT - ALERTA INMEDIATA AL GRADUARSE

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# APIs
PUMP_FUN_API_BASE = "https://frontend-api.pump.fun"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

# Parámetros de graduación
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))  # Segundos entre checks
PORT = int(os.getenv("PORT", "8080"))

# ========== BASE DE DATOS POSTGRESQL ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL not set")
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            # Tabla de tokens graduados (solo para evitar duplicados)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS immediate_graduation_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    price DECIMAL DEFAULT 0,
                    liquidity DECIMAL DEFAULT 0,
                    volume_24h DECIMAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    graduated_at TIMESTAMP DEFAULT NOW(),
                    alert_sent BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMP,
                    dex_screener_url TEXT,
                    initial_dex TEXT
                )
            """)
            
            # Tabla de tokens en monitoreo pre-graduación
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pre_graduation_tokens (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    last_checked TIMESTAMP DEFAULT NOW(),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

    async def add_pre_graduation_token(self, mint: str, symbol: str = None, name: str = None, market_cap: float = 0):
        """Agregar token a monitoreo pre-graduación"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pre_graduation_tokens (mint, symbol, name, market_cap) 
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint) DO UPDATE SET 
                    market_cap = $4,
                    last_checked = NOW()
            """, mint, symbol, name, market_cap)

    async def mark_immediate_alert_sent(self, mint: str, data: Dict):
        """Marcar alerta inmediata enviada"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO immediate_graduation_alerts 
                (mint, symbol, name, market_cap, price, liquidity, volume_24h, alert_sent, alert_sent_at, dex_screener_url, initial_dex)
                VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, NOW(), $8, $9)
                ON CONFLICT (mint) DO NOTHING
            """, mint, data.get('symbol'), data.get('name'), data.get('market_cap'),
                data.get('price'), data.get('liquidity'), data.get('volume_24h'),
                data.get('dex_screener_url'), data.get('current_dex'))

    async def was_immediate_alert_sent(self, mint: str) -> bool:
        """Verificar si ya se envió alerta inmediata"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM immediate_graduation_alerts WHERE mint = $1", 
                mint
            )
            return bool(row)

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        """Obtener tokens en monitoreo pre-graduación"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, name, market_cap 
                FROM pre_graduation_tokens 
                WHERE last_checked > NOW() - INTERVAL '1 hour'
                ORDER BY market_cap DESC
                LIMIT 200
            """)
            return [dict(row) for row in rows]

    async def cleanup_old_pre_graduation(self):
        """Limpiar tokens pre-graduación antiguos"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM pre_graduation_tokens 
                WHERE last_checked < NOW() - INTERVAL '2 hours'
            """)

# ========== CLIENTE API PUMP.FUN ==========
class PumpFunAPI:
    def __init__(self):
        self.base_url = PUMP_FUN_API_BASE
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/"
        }

    async def get_all_recent_coins(self, limit: int = 200) -> List[Dict]:
        """Obtener TODOS los coins recientes de Pump.fun"""
        url = f"{self.base_url}/coins"
        params = {
            "limit": limit,
            "offset": 0,
            "sort": "createdAt",
            "order": "DESC",
            "includeNsfw": "true"
        }
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            coins = []
                            for coin in data:
                                parsed_coin = self._parse_coin_data(coin)
                                if parsed_coin:
                                    coins.append(parsed_coin)
                            return coins
        except Exception as e:
            logging.error(f"❌ Error obteniendo coins: {str(e)}")
        
        return []

    def _parse_coin_data(self, data: Dict) -> Optional[Dict]:
        """Parsear datos de un coin"""
        if not data:
            return None
            
        mint = data.get('mint') or data.get('id')
        if not mint:
            return None
            
        market_cap = data.get('marketCap') or data.get('mcap') or 0
        price = data.get('price') or data.get('usdPrice') or 0
        liquidity = data.get('liquidity', {}).get('usd', 0) if isinstance(data.get('liquidity'), dict) else data.get('liquidity', 0)

        return {
            'mint': mint,
            'symbol': data.get('symbol', 'UNKNOWN'),
            'name': data.get('name', ''),
            'market_cap': float(market_cap),
            'price': float(price),
            'liquidity': float(liquidity),
            'volume_24h': data.get('volume24h', 0),
            'created_at': data.get('createdAt')
        }

# ========== CLIENTE DEXSCREENER ==========
class DexScreenerAPI:
    def __init__(self):
        self.base_url = DEXSCREENER_API

    async def get_immediate_token_data(self, mint: str) -> Optional[Dict]:
        """Obtener datos inmediatos del token recién graduado"""
        url = f"{self.base_url}/tokens/{mint}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=8) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_immediate_data(data, mint)
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos inmediatos para {mint}: {str(e)}")
        
        return None

    def _parse_immediate_data(self, data: Dict, mint: str) -> Optional[Dict]:
        """Parsear datos inmediatos post-graduación"""
        pairs = data.get('pairs', [])
        if not pairs:
            return None

        # Tomar el primer pair disponible (más rápido)
        pair = pairs[0]

        price_change = pair.get('priceChange', {})
        if isinstance(price_change, dict):
            price_change_24h = price_change.get('h24', 0)
        else:
            price_change_24h = price_change

        return {
            'mint': mint,
            'market_cap': pair.get('marketCap', 0),
            'price': pair.get('priceUsd', 0),
            'liquidity': pair.get('liquidity', {}).get('usd', 0),
            'volume_24h': pair.get('volume', {}).get('h24', 0),
            'price_change_24h': float(price_change_24h),
            'dex_screener_url': pair.get('url'),
            'current_dex': pair.get('dexId', 'unknown')
        }

# ========== NOTIFICADOR TELEGRAM ==========
def build_immediate_buttons(mint: str, dex_url: str = None) -> InlineKeyboardMarkup:
    """Construir botones para alerta inmediata"""
    buttons = [
        [InlineKeyboardButton("⚡ Comprar Ahora", url=f"https://pump.fun/coin/{mint}")],
        [InlineKeyboardButton("📊 DexScreener", url=dex_url or f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🔄 Raydium", url=f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
    ]
    return InlineKeyboardMarkup(buttons)

class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_immediate_graduation_alert(self, symbol: str, mint: str, data: Dict):
        """Enviar alerta INMEDIATA de graduación"""
        if self.silent_mode:
            return

        market_cap = data.get('market_cap', 0)
        price = data.get('price', 0)
        dex = data.get('current_dex', 'unknown').upper()

        message_lines = [
            "🎓 <b>¡TOKEN GRADUADO AHORA MISMO!</b> 🎓",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Precio: <b>${price:.8f}</b>",
            f"• DEX: <b>{dex}</b>",
            f"• Estado: <b>GRADUACIÓN INMEDIATA</b>",
            "",
            "🚨 <b>ACABA DE GRADUARSE</b>",
            "• Token recién salido de Pump.fun",
            "• Oportunidad de entrada inmediata",
            "• Momento crítico de decisión",
            "",
            "<b>Mint:</b>",
            f"<code>{mint}</code>",
            "",
            "⚡ <i>¡Actúa rápido - Graduación en tiempo real!</i>"
        ]
        
        message = "\n".join(message_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_immediate_buttons(mint, data.get('dex_screener_url')),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta INMEDIATA enviada: {symbol} - ${market_cap:,.0f}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta inmediata: {str(e)}")

# ========== MONITOR GRADUACIÓN INMEDIATA ==========
class ImmediateGraduationMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.pump_api = PumpFunAPI()
        self.dex_api = DexScreenerAPI()
        
        self.running = False
        self.scan_round = 0
        self.stats = {
            'tokens_scanned': 0,
            'graduations_detected': 0,
            'immediate_alerts_sent': 0
        }

    async def scan_for_immediate_graduations(self):
        """Escanear para detectar graduaciones inmediatas"""
        self.scan_round += 1
        logging.info(f"🔍 Escaneo inmediato #{self.scan_round} - Buscando graduaciones...")
        
        try:
            # Obtener TODOS los tokens recientes
            all_coins = await self.pump_api.get_all_recent_coins(200)
            self.stats['tokens_scanned'] = len(all_coins)
            
            graduation_detected = False
            
            for coin in all_coins:
                mint = coin['mint']
                symbol = coin['symbol']
                market_cap = coin['market_cap']
                
                # Verificar si el token acaba de graduarse
                if market_cap >= GRADUATION_MC_TARGET:
                    # Verificar si ya alertamos sobre este token
                    if not await self.db.was_immediate_alert_sent(mint):
                        logging.info(f"🎓 GRADUACIÓN DETECTADA: {symbol} - ${market_cap:,.0f}")
                        
                        # Obtener datos inmediatos post-graduación
                        immediate_data = await self.dex_api.get_immediate_token_data(mint)
                        if not immediate_data:
                            immediate_data = coin  # Usar datos de Pump.fun como fallback
                        
                        # ENVIAR ALERTA INMEDIATA
                        await self.notifier.send_immediate_graduation_alert(
                            symbol=symbol,
                            mint=mint,
                            data=immediate_data
                        )
                        
                        # Marcar como alertado
                        await self.db.mark_immediate_alert_sent(mint, immediate_data)
                        
                        self.stats['graduations_detected'] += 1
                        self.stats['immediate_alerts_sent'] += 1
                        graduation_detected = True
                
                # También guardar tokens pre-graduación para monitoreo
                elif market_cap >= GRADUATION_MC_TARGET * 0.5:  # Monitorear desde 50% del target
                    await self.db.add_pre_graduation_token(
                        mint, symbol, coin['name'], market_cap
                    )
            
            if graduation_detected:
                logging.info("🚨 ¡Graduaciones detectadas y alertadas!")
            else:
                logging.info(f"✅ Escaneo #{self.scan_round} completado - {len(all_coins)} tokens escaneados")
                
        except Exception as e:
            logging.error(f"❌ Error en escaneo inmediato: {str(e)}")

    async def monitor_pre_graduation_tokens(self):
        """Monitorear tokens cercanos a graduación"""
        try:
            pre_graduation_tokens = await self.db.get_pre_graduation_tokens()
            
            for token in pre_graduation_tokens:
                mint = token['mint']
                symbol = token['symbol']
                previous_mc = token['market_cap']
                
                # Verificar si ya se graduó
                if not await self.db.was_immediate_alert_sent(mint):
                    # Obtener datos actualizados
                    immediate_data = await self.dex_api.get_immediate_token_data(mint)
                    if immediate_data:
                        current_mc = immediate_data.get('market_cap', 0)
                        
                        # Si se graduó, enviar alerta
                        if current_mc >= GRADUATION_MC_TARGET:
                            logging.info(f"🎓 GRADUACIÓN DETECTADA (pre-monitoreo): {symbol}")
                            
                            await self.notifier.send_immediate_graduation_alert(
                                symbol=symbol,
                                mint=mint,
                                data=immediate_data
                            )
                            
                            await self.db.mark_immediate_alert_sent(mint, immediate_data)
                            self.stats['graduations_detected'] += 1
                            self.stats['immediate_alerts_sent'] += 1
                        
                        # Actualizar en base de datos
                        await self.db.add_pre_graduation_token(
                            mint, symbol, token.get('name'), current_mc
                        )
            
            # Limpiar tokens antiguos
            await self.db.cleanup_old_pre_graduation()
            
        except Exception as e:
            logging.error(f"❌ Error monitoreando pre-graduación: {str(e)}")

    async def start(self):
        """Iniciar monitor de graduación inmediata"""
        self.running = True
        
        # Iniciar escaneo continuo
        asyncio.create_task(self._continuous_immediate_scan())
        
        # Iniciar monitoreo de tokens pre-graduación
        asyncio.create_task(self._continuous_pre_graduation_monitor())
        
        logging.info("✅ Monitor GRADUACIÓN INMEDIATA iniciado")

    async def _continuous_immediate_scan(self):
        """Escaneo continuo inmediato"""
        while self.running:
            try:
                await self.scan_for_immediate_graduations()
            except Exception as e:
                logging.error(f"❌ Error en escaneo continuo: {str(e)}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _continuous_pre_graduation_monitor(self):
        """Monitoreo continuo pre-graduación"""
        while self.running:
            try:
                await self.monitor_pre_graduation_tokens()
            except Exception as e:
                logging.error(f"❌ Error en monitoreo pre-graduación: {str(e)}")
            await asyncio.sleep(15)  # Cada 15 segundos para tokens cercanos

    async def stop(self):
        """Detener monitor"""
        self.running = False
        logging.info("⛔ Monitor GRADUACIÓN INMEDIATA detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Graduación Inmediata Sniper Bot")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
immediate_monitor: Optional[ImmediateGraduationMonitor] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = f"""
🎓 <b>SNIPER BOT - GRADUACIÓN INMEDIATA</b> ⚡

<b>Función Exclusiva:</b>
• 🚨 Alertar CUANDO SE GRADÚAN los tokens
• ⚡ Detección en TIEMPO REAL
• 📈 Market Cap: <b>${GRADUATION_MC_TARGET:,.0f}+</b>
• 🔄 Intervalo: <b>{CHECK_INTERVAL} segundos</b>

<b>Características:</b>
• Escaneo cada <b>{CHECK_INTERVAL}s</b> de tokens
• Alertas INSTANTÁNEAS al graduarse
• Enlaces directos a DEXs
• Sin retrasos, sin esperas

<b>Comandos:</b>
/graduacion_on - Activar detector
/graduacion_off - Pausar detector  
/status - Estado del sistema
/stats - Estadísticas de detección
/silent on|off - Modo silencioso

<code>Alertas en el momento exacto de graduación</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def graduacion_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activar detector de graduación inmediata"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if immediate_monitor:
            await immediate_monitor.start()

        await update.message.reply_text(
            "⚡ <b>DETECTOR DE GRADUACIÓN ACTIVADO</b>\n\n"
            "🎓 Objetivo: Tokens al graduarse\n"
            "⏱️  Velocidad: Escaneo cada 5 segundos\n"
            "📈 Trigger: MC ≥ $69,000\n"
            "🚨 Estado: <b>ALERTAS INMEDIATAS ACTIVAS</b>\n\n"
            "<i>Escaneando tokens en tiempo real...</i>",
            parse_mode="HTML"
        )
        logging.info(f"⚡ Detector de graduación activado por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error activando detector: {str(e)}")

async def graduacion_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Desactivar detector de graduación inmediata"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if immediate_monitor:
            await immediate_monitor.stop()

        await update.message.reply_text(
            "⛔ <b>DETECTOR DE GRADUACIÓN DESACTIVADO</b>\n\n"
            "Las alertas inmediatas han sido pausadas.\n"
            "Usa /graduacion_on para reactivar.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error desactivando detector: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del sistema"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    monitor_status = "🟢 ACTIVO" if immediate_monitor and immediate_monitor.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    
    stats = immediate_monitor.stats if immediate_monitor else {}
    current_time = datetime.utcnow().strftime("%H:%M:%S")

    status_text = (
        "📊 <b>ESTADO GRADUACIÓN INMEDIATA</b>\n\n"
        f"⚡ Detector: {monitor_status}\n"
        f"🔊 Silencioso: {silent_status}\n"
        f"🕐 Último escaneo: {current_time} UTC\n\n"
        
        "<b>📈 Estadísticas:</b>\n"
        f"• Escaneos realizados: {stats.get('scan_round', 0)}\n"
        f"• Tokens escaneados: {stats.get('tokens_scanned', 0)}\n"
        f"• Graduaciones detectadas: {stats.get('graduations_detected', 0)}\n"
        f"• Alertas enviadas: {stats.get('immediate_alerts_sent', 0)}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• MC Graduación: <b>${GRADUATION_MC_TARGET:,.0f}</b>\n"
        f"• Intervalo: <b>{CHECK_INTERVAL} segundos</b>\n"
        f"• Velocidad: <b>ALTA PRIORIDAD</b>\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estadísticas detalladas"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    stats = immediate_monitor.stats if immediate_monitor else {}
    
    efficiency = 0
    if stats.get('tokens_scanned', 0) > 0:
        efficiency = (stats.get('graduations_detected', 0) / stats.get('tokens_scanned', 0)) * 100

    stats_text = (
        "📊 <b>ESTADÍSTICAS DETALLADAS</b>\n\n"
        f"• Rondas de escaneo: <b>{stats.get('scan_round', 0)}</b>\n"
        f"• Total tokens analizados: <b>{stats.get('tokens_scanned', 0)}</b>\n"
        f"• Graduaciones detectadas: <b>{stats.get('graduations_detected', 0)}</b>\n"
        f"• Alertas inmediatas: <b>{stats.get('immediate_alerts_sent', 0)}</b>\n"
        f"• Eficiencia: <b>{efficiency:.2f}%</b>\n\n"
        
        "<b>🎯 Rendimiento:</b>\n"
        f"• Tokens/escaneo: <b>{stats.get('tokens_scanned', 0) / max(stats.get('scan_round', 1), 1):.1f}</b>\n"
        f"• Graduaciones/hora: <b>{stats.get('graduations_detected', 0) / max(stats.get('scan_round', 1) * CHECK_INTERVAL / 3600, 1):.1f}</b>\n"
        f"• Velocidad respuesta: <b>INMEDIATA</b>\n"
    )

    await update.message.reply_text(stats_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Modo silencioso"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    args = context.args
    if not args or args[0] not in ['on', 'off']:
        await update.message.reply_text("Uso: /silent on|off")
        return

    if notifier:
        notifier.silent_mode = (args[0] == 'on')
        status = "🔇 ACTIVADO" if notifier.silent_mode else "🔔 DESACTIVADO"
        await update.message.reply_text(f"Modo silencioso: {status}")

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
            
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"❌ Webhook error: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    stats = immediate_monitor.stats if immediate_monitor else {}
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "immediate_graduation": {
            "running": immediate_monitor.running if immediate_monitor else False,
            "stats": stats
        },
        "response_time": "immediate",
        "scan_interval_seconds": CHECK_INTERVAL
    }

@app.get("/")
async def root():
    return {
        "message": "Immediate Graduation Sniper Bot",
        "status": "operational",
        "function": "instant_graduation_alerts"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, immediate_monitor

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables: {', '.join(missing)}")

    await db.connect()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("graduacion_on", graduacion_on_command))
    telegram_app.add_handler(CommandHandler("graduacion_off", graduacion_off_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("stats", stats_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))

    notifier = TelegramNotifier(telegram_app)
    immediate_monitor = ImmediateGraduationMonitor(db, notifier)

    logging.info("✅ Graduación Inmediata Bot inicializado")

@app.on_event("startup")
async def startup_event():
    try:
        await initialize_app()
        await telegram_app.initialize()
        
        if os.getenv("RAILWAY_STATIC_URL"):
            webhook_url = f"{os.getenv('RAILWAY_STATIC_URL')}/webhook"
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            await telegram_app.start()
            logging.info("✅ Bot iniciado con polling")
            
        logging.info("🚀 Bot de Graduación Inmediata listo - Usa /graduacion_on para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot de graduación inmediata...")
    
    if immediate_monitor:
        await immediate_monitor.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot de graduación inmediata apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
