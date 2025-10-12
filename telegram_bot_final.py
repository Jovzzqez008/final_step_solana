# telegram_bot_pump_fun_direct.py
# SOLANA PUMP.FUN BOT - API DIRECTA DE PUMP.FUN

import os
import re
import json
import asyncio
import logging
from datetime import datetime
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

# API REAL DE PUMP.FUN - ENDPOINTS QUE SÍ FUNCIONAN
PUMP_FUN_API_BASE = "https://api.pump.fun"
PUMP_FUN_COINS_URL = f"{PUMP_FUN_API_BASE}/coins"
PUMP_FUN_COIN_URL = f"{PUMP_FUN_API_BASE}/coin"

# Parámetros de trading
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))
PUMP_MC_MAX = float(os.getenv("PUMP_MC_MAX", "50000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    graduated BOOLEAN DEFAULT FALSE,
                    source TEXT DEFAULT 'pump.fun'
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT DEFAULT 'pump.fun'
                )
            """)

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pump_notifications WHERE mint = $1 AND alert_type = $2", 
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, market_cap: float = None, source: str = "pump.fun"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, alert_type, symbol, market_cap, source)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint) DO UPDATE SET
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    market_cap = EXCLUDED.market_cap,
                    source = EXCLUDED.source,
                    detected_at = NOW()
            """, mint, alert_type, symbol, market_cap, source)

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE pump_notifications SET graduated = TRUE WHERE mint = $1", mint)

    async def seen_recently(self, mint: str, minutes: int = 3) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "pump.fun"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, source
                FROM pump_notifications 
                WHERE graduated = FALSE AND market_cap BETWEEN $1 AND $2
                ORDER BY detected_at DESC
                LIMIT 50
            """, PUMP_MC_MIN, GRADUATION_MC_TARGET)
            return [dict(r) for r in rows]

    async def list_recent_notifications(self, limit: int = 10) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT mint, symbol, alert_type, market_cap, detected_at FROM pump_notifications ORDER BY detected_at DESC LIMIT $1", 
                limit
            )
            return [dict(r) for r in rows]

# ========== CLIENTE HTTP PARA PUMP.FUN ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/"
        })
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, timeout: int = 10) -> Any:
        try:
            async with self.session.get(url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logging.warning(f"HTTP {response.status} from {url}")
        except asyncio.TimeoutError:
            logging.warning(f"Timeout fetching {url}")
        except Exception as e:
            logging.error(f"Error fetching {url}: {str(e)}")
        return None

# ========== API DIRECTA DE PUMP.FUN ==========
class PumpFunAPI:
    def __init__(self, client: APIClient):
        self.client = client

    async def get_all_coins(self, limit: int = 100) -> List[Dict]:
        """Obtener TODOS los coins de Pump.fun - ENDPOINT REAL"""
        url = f"{PUMP_FUN_COINS_URL}"
        params = {
            "sort": "createdAt",  # Ordenar por más recientes
            "order": "desc",
            "limit": limit
        }
        
        data = await self.client.fetch_json(url, params=params)
        
        if data and isinstance(data, list):
            logging.info(f"✅ Pump.fun API: {len(data)} coins obtenidos")
            return data
        elif data and isinstance(data, dict) and 'coins' in data:
            return data['coins']
        else:
            logging.warning("⚠️ Formato inesperado de la API de Pump.fun")
            return []

    async def get_coin_by_mint(self, mint: str) -> Optional[Dict]:
        """Obtener coin específico por mint address"""
        url = f"{PUMP_FUN_COIN_URL}/{mint}"
        data = await self.client.fetch_json(url)
        
        if data and isinstance(data, dict):
            return data
        return None

    async def get_trending_coins(self, limit: int = 50) -> List[Dict]:
        """Obtener coins en tendencia"""
        url = f"{PUMP_FUN_COINS_URL}"
        params = {
            "sort": "volume",  # Ordenar por volumen (tendencia)
            "order": "desc",
            "limit": limit
        }
        
        data = await self.client.fetch_json(url, params=params)
        
        if data and isinstance(data, list):
            return data
        elif data and isinstance(data, dict) and 'coins' in data:
            return data['coins']
        return []

# ========== NOTIFICADOR TELEGRAM ==========
def build_buttons_for_mint(mint: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⚡ Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("📈 Pump.Fun", url=f"https://pump.fun/coin/{mint}")]
    ]
    return InlineKeyboardMarkup(kb)

class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_pump_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str, source: str = "pump.fun"):
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        msg_lines = []
        msg_lines.append(f"🚨 <b>{title}</b> 🚨")
        msg_lines.append("")
        msg_lines.append(f"<b>{symbol}</b>")
        
        if data.get('market_cap'):
            try:
                mc_str = f"• Market Cap: ${float(data['market_cap']):,.0f}"
                msg_lines.append(mc_str)
            except Exception:
                pass
        
        if data.get('price'):
            try:
                price_str = f"• Precio: ${float(data['price']):.8f}"
                msg_lines.append(price_str)
            except Exception:
                pass
        
        if data.get('liquidity'):
            try:
                liq_str = f"• Liquidez: ${float(data['liquidity']):,.0f}"
                msg_lines.append(liq_str)
            except Exception:
                pass
        
        if alert_type == "pump_early":
            msg_lines.append("• ⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>")
        elif alert_type == "pre_graduation":
            msg_lines.append("• 📈 <b>PRE-GRADUACIÓN INMINENTE</b>")
            if data.get('graduation_percent'):
                msg_lines.append(f"• Progreso: {data['graduation_percent']:.1f}%")
        elif alert_type == "post_graduation_pump":
            msg_lines.append("• 🔥 <b>EXPLOSIÓN POST-GRADUACIÓN</b>")
            if data.get('price_change_24h'):
                msg_lines.append(f"• Cambio 24h: {data['price_change_24h']:.2f}%")
        
        msg_lines.append(f"• Fuente: {source}")
        msg_lines.append("• 🎯 <b>DETECCIÓN DIRECTA PUMP.FUN</b>")
        msg_lines.append("")
        msg_lines.append("<b>Mint:</b>")
        msg_lines.append(f"<code>{mint}</code>")

        message = "\n".join(msg_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta {alert_type} enviada: {symbol} via {source}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

# ========== MONITOR PRINCIPAL CON API DIRECTA PUMP.FUN ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.running = False
        self.tasks = []
        self.scan_round = 0

    async def process_pumpfun_coin(self, coin: Dict) -> bool:
        """Procesar un coin de Pump.fun y enviar alertas si cumple criterios"""
        try:
            mint = coin.get('mint')
            symbol = coin.get('symbol', 'UNKNOWN')
            name = coin.get('name', '')
            
            if not mint:
                return False

            # Evitar duplicados recientes
            if await self.db.seen_recently(mint, minutes=2):
                return False

            await self.db.mark_seen(mint, "pump.fun")

            # Obtener métricas de Pump.fun
            market_cap = coin.get('marketCap')
            price = coin.get('price')
            liquidity = coin.get('liquidity')
            volume = coin.get('volume')
            created_at = coin.get('createdAt')
            price_change_24h = coin.get('priceChange24h', 0)

            # Convertir a float
            try:
                market_cap = float(market_cap) if market_cap else 0
                price = float(price) if price else 0
                liquidity = float(liquidity) if liquidity else 0
                price_change_24h = float(price_change_24h) if price_change_24h else 0
            except (TypeError, ValueError):
                return False

            # Filtro por market cap
            if market_cap < PUMP_MC_MIN or market_cap > PUMP_MC_MAX:
                return False

            # ALERTA EARLY - Detección inmediata
            if not await self.db.was_notified(mint, "pump_early"):
                await self.notifier.send_pump_alert(
                    title="PUMP.FUN EARLY DETECTION",
                    symbol=symbol,
                    mint=mint,
                    data={
                        "market_cap": market_cap,
                        "price": price,
                        "liquidity": liquidity
                    },
                    alert_type="pump_early",
                    source="pump.fun"
                )
                await self.db.mark_notified(mint, "pump_early", symbol, market_cap, "pump.fun")
                logging.info(f"🚨 Alerta EARLY enviada: {symbol} (${market_cap:,.0f})")
                return True

            # PRE-GRADUACIÓN - Si está cerca del objetivo
            if (market_cap >= GRADUATION_MC_TARGET * 0.7 and 
                market_cap <= GRADUATION_MC_TARGET and 
                not await self.db.was_notified(mint, "pre_graduation")):
                
                graduation_percent = (market_cap / GRADUATION_MC_TARGET) * 100
                
                await self.notifier.send_pump_alert(
                    title="PRE-GRADUACIÓN INMINENTE",
                    symbol=symbol,
                    mint=mint,
                    data={
                        'market_cap': market_cap,
                        'graduation_percent': graduation_percent
                    },
                    alert_type="pre_graduation",
                    source="pump.fun"
                )
                await self.db.mark_notified(mint, "pre_graduation", symbol, market_cap, "pump.fun")
                logging.info(f"🎯 Alerta PRE-GRAD enviada: {symbol} (${market_cap:,.0f})")
                return True

        except Exception as e:
            logging.error(f"❌ Error procesando coin: {str(e)}")
        
        return False

    async def pumpfun_scanner(self):
        """Scanner principal usando API directa de Pump.fun"""
        while self.running:
            try:
                self.scan_round += 1
                logging.info(f"🔄 Pump.fun Scan round #{self.scan_round}")
                
                async with APIClient() as client:
                    pump = PumpFunAPI(client)
                    
                    # Obtener coins más recientes
                    coins = await pump.get_all_coins(80)
                    
                    if not coins:
                        logging.warning("⚠️ No se pudieron obtener coins de Pump.fun")
                        # Intentar con trending como fallback
                        coins = await pump.get_trending_coins(50)
                        if not coins:
                            continue

                    processed = 0
                    for coin in coins:
                        if await self.process_pumpfun_coin(coin):
                            processed += 1

                    logging.info(f"✅ Scan round #{self.scan_round} completado. Alertas: {processed}")
                    
            except Exception as e:
                logging.error(f"❌ Error en Pump.fun scanner: {str(e)}")
            
            await asyncio.sleep(max(30, CHECK_INTERVAL * 60))

    async def start(self):
        """Iniciar monitor"""
        self.running = True
        
        # Iniciar scanner principal
        scanner_task = asyncio.create_task(self.pumpfun_scanner())
        self.tasks.append(scanner_task)
        
        logging.info("✅ PumpFunMonitor con API DIRECTA iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        
        for task in self.tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logging.info("⛔ PumpFunMonitor detenido")

# ========== POST-GRADUATION SCANNER ==========
class PostGraduationScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan_graduated_tokens(self):
        """Escanear tokens para detección post-graduación"""
        try:
            tokens = await self.db.get_pre_graduation_tokens()
            
            async with APIClient() as client:
                pump = PumpFunAPI(client)
                
                for token in tokens:
                    mint = token['mint']
                    symbol = token.get('symbol', 'UNKNOWN')
                    
                    # Obtener datos actualizados de Pump.fun
                    current_data = await pump.get_coin_by_mint(mint)
                    if not current_data:
                        continue
                    
                    current_mc = current_data.get('marketCap', 0)
                    price_change_24h = current_data.get('priceChange24h', 0)
                    
                    try:
                        current_mc = float(current_mc)
                        price_change_24h = float(price_change_24h)
                    except (TypeError, ValueError):
                        continue

                    # Marcar como graduado si supera el target
                    if current_mc > GRADUATION_MC_TARGET and not await self.db.was_notified(mint, "graduated"):
                        await self.db.mark_graduated(mint)
                        await self.db.mark_notified(mint, "graduated")
                        logging.info(f"🎓 Token graduado: {symbol} - MC: ${current_mc:,.0f}")

                    # Detectar explosión post-graduación
                    if (current_mc > GRADUATION_MC_TARGET and 
                        price_change_24h > 20 and 
                        not await self.db.was_notified(mint, "post_graduation_pump")):
                        
                        await self.notifier.send_pump_alert(
                            title="EXPLOSIÓN POST-GRADUACIÓN",
                            symbol=symbol,
                            mint=mint,
                            data={
                                "market_cap": current_mc,
                                "price_change_24h": price_change_24h
                            },
                            alert_type="post_graduation_pump",
                            source="pump.fun"
                        )
                        await self.db.mark_notified(mint, "post_graduation_pump")
                        logging.info(f"🔥 Explosión post-graduación: {symbol} (+{price_change_24h:.1f}%)")

        except Exception as e:
            logging.error(f"❌ Error en scan_graduated_tokens: {str(e)}")

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan_graduated_tokens()
            except Exception as e:
                logging.error(f"❌ Error en PostGraduationScanner: {str(e)}")
            await asyncio.sleep(2 * 60)  # Cada 2 minutos

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PostGraduationScanner iniciado")

    async def stop(self):
        """Detener scanner"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PostGraduationScanner detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - API Directa")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
post_graduation_scanner: Optional[PostGraduationScanner] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = f"""
🎯 <b>SOLANA PUMP.FUN BOT - API DIRECTA</b> 🚀

<b>Fuente principal:</b>
• 🎯 API Directa de Pump.fun (100% real)

<b>Comandos:</b>
/iniciar - Activar monitores
/detener - Pausar monitores  
/status - Estado del sistema
/silent on|off - Modo silencioso
/lista - Últimos tokens detectados

<b>Alertas:</b>
🚨 EARLY - MC bajo (${PUMP_MC_MIN:,.0f}-${PUMP_MC_MAX:,.0f})
🎯 PRE-GRAD - Cerca de graduación (${GRADUATION_MC_TARGET:,.0f})  
🔥 POST-GRAD - Explosión post-graduación

<code>Conectado directamente a la API de Pump.fun</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.start()
        if post_graduation_scanner:
            await post_graduation_scanner.start()

        await update.message.reply_text(
            "✅ <b>MONITOR PUMP.FUN ACTIVADO</b>\n\n"
            "🎯 API Directa Pump.fun → ESCANEANDO\n"
            "🔥 Post-Graduation Scanner → ACTIVO\n\n"
            "<i>Buscando oportunidades directamente en Pump.fun...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitores iniciados por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.stop()
        if post_graduation_scanner:
            await post_graduation_scanner.stop()

        await update.message.reply_text(
            "⛔ <b>MONITORES DETENIDOS</b>\n\n"
            "El bot ha dejado de escanear.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    post_status = "🟢 ACTIVO" if post_graduation_scanner and post_graduation_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA PUMP.FUN</b>\n\n"
        f"🎯 Pump.fun Monitor: {pump_status}\n"
        f"🔥 Post-Graduation: {post_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• MC Mínimo: ${PUMP_MC_MIN:,.0f}\n"
        f"• MC Máximo: ${PUMP_MC_MAX:,.0f}\n"
        f"• Objetivo Graduación: ${GRADUATION_MC_TARGET:,.0f}\n"
        f"• Intervalo: {CHECK_INTERVAL} minuto(s)\n"
        f"• API: {PUMP_FUN_API_BASE}\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def lista_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    rows = await db.list_recent_notifications(limit=10)
    if not rows:
        await update.message.reply_text("No hay notificaciones recientes.")
        return

    lines = ["<b>Últimas notificaciones:</b>\n"]
    for r in rows:
        sym = r.get('symbol', 'UNKNOWN')
        mint = r.get('mint', '')
        mc = r.get('market_cap', 0)
        alert_type = r.get('alert_type', '')
        
        lines.append(f"• <b>{sym}</b> [{alert_type}]")
        lines.append(f"  MC: ${float(mc):,.0f}")
        lines.append(f"  <code>{mint}</code>\n")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

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
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "post_graduation_scanner": post_graduation_scanner.running if post_graduation_scanner else False
        },
        "api": PUMP_FUN_API_BASE
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Pump.fun Bot - API Directa",
        "status": "operational",
        "api": PUMP_FUN_API_BASE
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor, post_graduation_scanner

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

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("lista", lista_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    post_graduation_scanner = PostGraduationScanner(db, notifier)

    logging.info("✅ Bot de Pump.fun con API DIRECTA inicializado")

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
            
        logging.info("🚀 Bot con API DIRECTA listo - Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot...")
    
    if pump_monitor:
        await pump_monitor.stop()
    if post_graduation_scanner:
        await post_graduation_scanner.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
