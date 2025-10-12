# telegram_bot_final.py
# PUMP.FUN GRADUATION SNIPER - HELIUS WSS + JUPITER API

import os
import re
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN_URL = os.getenv("DOMAIN_URL")

# Helius WebSocket (REEMPLAZA QUICKNODE)
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")

# Jupiter API
JUPITER_API_BASE = "https://api.jup.ag/tokens/v2"

# Parámetros de trading
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "50000"))
CHECK_INTERVAL_SECS = int(os.getenv("CHECK_INTERVAL_SECS", "5"))

PORT = int(os.getenv("PORT", "8080"))

# Regex para direcciones de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL"""
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL no configurada")
        
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas necesarias"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS graduation_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    price DECIMAL DEFAULT 0,
                    liquidity DECIMAL DEFAULT 0,
                    alert_type TEXT NOT NULL,
                    source TEXT DEFAULT 'helius',
                    detected_at TIMESTAMP DEFAULT NOW(),
                    tx_signature TEXT,
                    dex_name TEXT,
                    is_pump_token BOOLEAN DEFAULT FALSE,
                    organic_score DECIMAL DEFAULT 0
                )
            """)
            
            # Tabla de tokens vistos (cache)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_mints (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT
                )
            """)

    async def was_alerted(self, mint: str) -> bool:
        """Verificar si ya se alertó sobre este token"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM graduation_alerts WHERE mint = $1", 
                mint
            )
            return bool(row)

    async def mark_alerted(self, mint: str, symbol: str, market_cap: float, 
                          price: float, liquidity: float, tx_signature: str, 
                          dex_name: str, is_pump_token: bool, organic_score: float):
        """Marcar token como alertado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO graduation_alerts 
                (mint, symbol, market_cap, price, liquidity, alert_type, source, 
                 tx_signature, dex_name, is_pump_token, organic_score)
                VALUES ($1, $2, $3, $4, $5, 'graduated', 'helius_wss', 
                        $6, $7, $8, $9)
                ON CONFLICT (mint) DO NOTHING
            """, mint, symbol, market_cap, price, liquidity, 
                tx_signature, dex_name, is_pump_token, organic_score)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_mints 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "helius"):
        """Marcar token como visto"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_mints (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        """Obtener alertas recientes"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, price, liquidity, source, 
                       detected_at, dex_name, is_pump_token, organic_score
                FROM graduation_alerts 
                ORDER BY detected_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

# ========== CLIENTE JUPITER API ==========
class JupiterAPI:
    def __init__(self):
        self.base_url = JUPITER_API_BASE
        self.session = None

    async def get_session(self):
        """Obtener sesión aiohttp"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def search_token(self, mint: str) -> Optional[Dict]:
        """Buscar token por mint en Jupiter API"""
        try:
            session = await self.get_session()
            url = f"{self.base_url}/search"
            params = {"query": mint}
            
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0]  # Primer resultado
        except Exception as e:
            logging.error(f"❌ Error buscando token {mint} en Jupiter: {str(e)}")
        
        return None

    async def get_recent_tokens(self, limit: int = 50) -> List[Dict]:
        """Obtener tokens recientes de Jupiter API"""
        try:
            session = await self.get_session()
            url = f"{self.base_url}/recent"
            params = {"limit": limit}
            
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list):
                        return data
        except Exception as e:
            logging.error(f"❌ Error obteniendo tokens recientes: {str(e)}")
        
        return []

    async def close(self):
        """Cerrar sesión"""
        if self.session:
            await self.session.close()

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_graduation_alert(self, symbol: str, mint: str, data: Dict):
        """Enviar alerta de graduación inmediata"""
        if self.silent_mode:
            logging.info(f"🔇 Silent mode - Skipping alert for {symbol}")
            return

        market_cap = data.get('market_cap', 0)
        price = data.get('price', 0)
        liquidity = data.get('liquidity', 0)
        dex_name = data.get('dex_name', 'Unknown DEX')
        tx_signature = data.get('tx_signature', '')[:16] + '...' if data.get('tx_signature') else 'N/A'
        organic_score = data.get('organic_score', 0)
        is_pump_token = data.get('is_pump_token', False)

        # Emoji especial para tokens Pump.fun
        pump_emoji = "🎯" if is_pump_token else "🎓"

        message_lines = [
            f"{pump_emoji} <b>¡GRADUACIÓN DETECTADA EN TIEMPO REAL!</b> {pump_emoji}",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Precio: <b>${price:.8f}</b>",
            f"• Liquidez: <b>${liquidity:,.0f}</b>",
            f"• DEX: <b>{dex_name.upper()}</b>",
            f"• Organic Score: <b>{organic_score:.1f}</b>",
            f"• TX: <code>{tx_signature}</code>",
            "",
            "⚡ <b>ACCIÓN INMEDIATA REQUERIDA</b>",
            "• Token recién graduado de Pump.fun" if is_pump_token else "• Token recién listado en DEX",
            "• Pool creado en Raydium",
            "• Oportunidad de entrada temprana",
            "",
            "<b>Mint Address:</b>",
            f"<code>{mint}</code>",
            "",
            "🚀 <i>¡Bot activo con Helius WSS + Jupiter API!</i>"
        ]

        message = "\n".join(message_lines)

        # Botones de acción rápida
        keyboard = [
            [InlineKeyboardButton("⚡ Comprar Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 Ver en DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🔍 Raydium Swap", url=f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}")],
            [InlineKeyboardButton("🛡️ Rug Check", url=f"https://rugcheck.xyz/tokens/{mint}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta de graduación enviada: {symbol}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

# ========== CLIENTE HELIUS WSS ==========
class HeliusMonitor:
    def __init__(self, wss_url: str, db: Database, notifier: TelegramNotifier):
        self.wss_url = wss_url
        self.db = db
        self.notifier = notifier
        self.jupiter_api = JupiterAPI()
        self.websocket = None
        self.running = False
        self.reconnect_delay = 1
        self.task = None

        # Program IDs importantes
        self.raydium_program_id = "RVKd61ztZW9GUwhRbbLoYVRE5Xf1B2tVscKqwZqXgEr"
        self.pump_fun_program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

        # Patrones para detectar creación de pools
        self.pool_creation_keywords = [
            'initialize', 'create', 'new_pool', 'initialize2', 
            'create_pool', 'initialize_pool', 'init_pool',
            'add_liquidity', 'initialize_account', 'raydium',
            'open_book', 'create_amm', 'initialize_amm',
            'complete'  # Palabra clave de graduación en Pump.fun
        ]

    async def connect(self):
        """Conectar a Helius WebSocket"""
        try:
            self.websocket = await websockets.connect(
                self.wss_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10
            )
            
            # Suscribirse a logs de Raydium y Pump.fun
            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {
                        "mentions": [
                            self.raydium_program_id,
                            self.pump_fun_program_id
                        ]
                    },
                    {"commitment": "confirmed"}
                ]
            }
            
            await self.websocket.send(json.dumps(subscribe_message))
            response = await self.websocket.recv()
            logging.info("✅ Helius WebSocket conectado y suscrito a Raydium/Pump.fun")
            return True
            
        except Exception as e:
            logging.error(f"❌ Error conectando Helius: {str(e)}")
            return False

    async def listen(self):
        """Escuchar mensajes del WebSocket"""
        self.running = True
        
        while self.running:
            try:
                if not self.websocket:
                    success = await self.connect()
                    if not success:
                        await asyncio.sleep(self.reconnect_delay)
                        self.reconnect_delay = min(self.reconnect_delay * 2, 60)
                        continue
                    self.reconnect_delay = 1

                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                
                await self.process_message(data)
                
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                logging.warning(f"🔌 Conexión WebSocket cerrada, reconectando...")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)
            except Exception as e:
                logging.error(f"❌ Error en WebSocket: {str(e)}")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    async def process_message(self, data: Dict):
        """Procesar mensaje del WebSocket"""
        try:
            if data.get('method') == 'logsNotification':
                params = data.get('params', {})
                result = params.get('result', {})
                logs = result.get('logs', [])
                signature = result.get('signature', '')
                
                await self.analyze_logs(logs, signature)
                
        except Exception as e:
            logging.error(f"❌ Error procesando mensaje: {str(e)}")

    async def analyze_logs(self, logs: List[str], signature: str):
        """Analizar logs en busca de creación de pools"""
        try:
            logs_text = ' '.join(logs).lower()
            
            # Buscar patrones de creación de pool o graduación
            pool_created = any(keyword in logs_text for keyword in self.pool_creation_keywords)
            
            if pool_created:
                # Extraer mints de los logs
                mints_found = MINT_PATTERN.findall(' '.join(logs))
                unique_mints = list(set(mints_found))
                
                for mint in unique_mints:
                    if not await self.db.seen_recently(mint):
                        await self.db.mark_seen(mint)
                        await self.process_graduation(mint, signature, logs_text)
                        
        except Exception as e:
            logging.error(f"❌ Error analizando logs: {str(e)}")

    async def process_graduation(self, mint: str, signature: str, logs_text: str):
        """Procesar graduación detectada"""
        try:
            # Verificar si ya alertamos sobre este mint
            if await self.db.was_alerted(mint):
                return

            # Obtener datos del token desde Jupiter API
            token_data = await self.jupiter_api.search_token(mint)
            if not token_data:
                return

            symbol = token_data.get('symbol', 'UNKNOWN')
            market_cap = token_data.get('mcap', 0)
            price = token_data.get('usdPrice', 0)
            liquidity = token_data.get('liquidity', 0)
            organic_score = token_data.get('organicScore', 0)

            # DETECCIÓN MEJORADA DE TOKENS PUMP.FUN
            is_pump_token = await self.is_pump_fun_token(symbol, mint, token_data)

            # Determinar DEX basado en logs
            dex_name = "unknown"
            if 'raydium' in logs_text.lower():
                dex_name = "raydium"
            elif 'orca' in logs_text.lower():
                dex_name = "orca"
            elif 'jupiter' in logs_text.lower():
                dex_name = "jupiter"
            elif 'pump' in logs_text.lower():
                dex_name = "pump.fun"

            # Solo alertar si cumple criterios de graduación
            if (market_cap >= GRADUATION_MC_TARGET and 
                market_cap >= PUMP_MC_MIN and
                is_pump_token):  # Solo tokens de Pump.fun
                
                # Enviar alerta
                alert_data = {
                    'market_cap': market_cap,
                    'price': price,
                    'liquidity': liquidity,
                    'dex_name': dex_name,
                    'tx_signature': signature,
                    'organic_score': organic_score,
                    'is_pump_token': is_pump_token
                }
                
                await self.notifier.send_graduation_alert(symbol, mint, alert_data)
                
                # Guardar en base de datos
                await self.db.mark_alerted(
                    mint, symbol, market_cap, price, liquidity, 
                    signature, dex_name, is_pump_token, organic_score
                )
                
                logging.info(f"🎓 Graduación detectada: {symbol} en {dex_name} - MC: ${market_cap:,.0f}")

        except Exception as e:
            logging.error(f"❌ Error procesando graduación {mint}: {str(e)}")

    async def is_pump_fun_token(self, symbol: str, mint: str, token_data: Dict) -> bool:
        """Determinar si es un token de Pump.fun"""
        try:
            # 1. Verificar símbolo contiene "pump" (case insensitive)
            if 'pump' in symbol.lower():
                return True

            # 2. Verificar si es token reciente (primera pool en últimas 24h)
            first_pool = token_data.get('firstPool', {})
            if first_pool:
                created_at = first_pool.get('createdAt')
                if created_at:
                    pool_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if datetime.now(pool_time.tzinfo) - pool_time < timedelta(hours=24):
                        return True

            # 3. Verificar score orgánico (tokens Pump.fun suelen tener score bajo inicialmente)
            organic_score = token_data.get('organicScore', 100)
            if organic_score < 30:  # Score bajo indica token nuevo
                return True

            # 4. Verificar holder count (tokens Pump.fun tienen pocos holders inicialmente)
            holder_count = token_data.get('holderCount', 0)
            if holder_count < 1000:
                return True

            return False

        except Exception as e:
            logging.error(f"❌ Error verificando token Pump.fun {mint}: {str(e)}")
            return False

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
            
        self.task = asyncio.create_task(self.listen())
        logging.info("✅ Helius Monitor iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.jupiter_api.close()
        logging.info("⛔ Helius Monitor detenido")

# ========== FASTAPI APP CON LIFESPAN ==========
# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
helius_monitor: Optional[HeliusMonitor] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager para FastAPI"""
    # Startup
    await startup()
    yield
    # Shutdown
    await shutdown()

app = FastAPI(title="Pump.fun Graduation Sniper - Helius", lifespan=lifespan)

def is_authorized(update: Update) -> bool:
    """Verificar si usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎯 <b>PUMP.FUN GRADUATION SNIPER BOT</b> ⚡

<b>Tecnología Mejorada:</b>
• 🔗 Helius WebSocket (Tiempo real)
• 📊 Jupiter API V2 (Datos precisos)
• 🎯 Filtro exclusivo Pump.fun
• ⚡ Detección inmediata

<b>Detección Específica:</b>
• Solo tokens de Pump.fun
• Símbolos que contienen "pump"
• Tokens recientes (<24h)
• Market Cap > $69K

<b>Comandos:</b>
/iniciar - Activar monitor Helius
/detener - Pausar monitor
/status - Estado del sistema
/alertas - Últimas alertas
/silent on|off - Modo silencioso

<code>Detección especializada en tokens Pump.fun</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar - Activar monitor"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if helius_monitor:
            await helius_monitor.start()

        await update.message.reply_text(
            "✅ <b>MONITOR HELIUS ACTIVADO</b>\n\n"
            "🔗 WebSocket: CONECTADO\n"
            "🎯 Objetivo: Tokens Pump.fun\n"
            "⚡ Velocidad: TIEMPO REAL\n"
            "📊 Fuente: Helius + Jupiter API\n\n"
            "<i>Escaneando graduaciones de Pump.fun...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitor activado por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error activando monitor: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener - Pausar monitor"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if helius_monitor:
            await helius_monitor.stop()

        await update.message.reply_text(
            "⛔ <b>MONITOR HELIUS DETENIDO</b>\n\n"
            "La detección en tiempo real ha sido pausada.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitor: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status - Estado del sistema"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    monitor_status = "🟢 ACTIVO" if helius_monitor and helius_monitor.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    
    # Obtener estadísticas
    recent_alerts = await db.get_recent_alerts(5)
    alerts_count = len(recent_alerts)
    pump_tokens_count = sum(1 for alert in recent_alerts if alert.get('is_pump_token'))

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA</b>\n\n"
        f"🎯 Helius Monitor: {monitor_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n"
        f"📈 Alertas Recientes: <b>{alerts_count}</b>\n"
        f"🎯 Tokens Pump.fun: <b>{pump_tokens_count}</b>\n\n"
        
        "<b>🔗 Conexiones:</b>\n"
        f"• WebSocket: {'✅ CONECTADO' if helius_monitor and helius_monitor.websocket else '❌ DESCONECTADO'}\n"
        f"• Jupiter API: {'✅ ACTIVA' if helius_monitor else '❌ INACTIVA'}\n"
        f"• Base Datos: {'✅ CONECTADO' if db.pool else '❌ DESCONECTADO'}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• MC Graduación: ${GRADUATION_MC_TARGET:,.0f}\n"
        f"• MC Mínimo: ${PUMP_MC_MIN:,.0f}\n"
        f"• Helius: {'✅ CONFIGURADO' if HELIUS_WSS_URL else '❌ NO CONFIGURADO'}\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def alertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /alertas - Últimas alertas"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    alerts = await db.get_recent_alerts(8)
    if not alerts:
        await update.message.reply_text("No hay alertas recientes.")
        return

    lines = ["<b>🎯 Últimas Graduaciones Pump.fun:</b>\n"]
    for alert in alerts:
        symbol = alert.get('symbol', 'UNKNOWN')
        market_cap = alert.get('market_cap', 0)
        dex = alert.get('dex_name', 'unknown').upper()
        is_pump = alert.get('is_pump_token', False)
        detected_at = alert.get('detected_at')
        
        if isinstance(detected_at, datetime):
            time_str = detected_at.strftime("%H:%M:%S")
        else:
            time_str = "reciente"
        
        pump_emoji = "🎯" if is_pump else "📌"
        
        lines.append(f"• {pump_emoji} <b>{symbol}</b>")
        lines.append(f"  MC: ${float(market_cap):,.0f} | DEX: {dex}")
        lines.append(f"  Hora: {time_str}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /silent - Modo silencioso"""
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

# ========== ENDPOINTS WEB ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Webhook para Telegram"""
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
            
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"❌ Error en webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/last")
async def get_last_alerts(limit: int = 20):
    """Endpoint para últimas alertas"""
    alerts = await db.get_recent_alerts(limit)
    for alert in alerts:
        if isinstance(alert.get('detected_at'), datetime):
            alert['detected_at'] = alert['detected_at'].isoformat()
    return JSONResponse({"alerts": alerts})

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "database": bool(db.pool),
            "telegram_bot": bool(telegram_app),
            "helius_monitor": helius_monitor.running if helius_monitor else False,
            "websocket_connected": helius_monitor.websocket is not None if helius_monitor else False
        },
        "configuration": {
            "pump_fun_detection": True,
            "jupiter_api_integration": True,
            "graduation_mc_target": GRADUATION_MC_TARGET
        }
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Pump.fun Graduation Sniper Bot",
        "status": "operational",
        "technology": "Helius WSS + Jupiter API V2",
        "focus": "pump_fun_tokens_only",
        "version": "3.0"
    }

# ========== INICIALIZACIÓN ==========
async def startup():
    """Inicialización de la aplicación"""
    global telegram_app, notifier, helius_monitor

    # Verificar variables requeridas
    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "HELIUS_WSS_URL": HELIUS_WSS_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables: {', '.join(missing)}")

    # Conectar a base de datos
    await db.connect()

    # Inicializar aplicación de Telegram
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("alertas", alertas_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))

    # Inicializar notificador y monitor
    notifier = TelegramNotifier(telegram_app)
    helius_monitor = HeliusMonitor(HELIUS_WSS_URL, db, notifier)

    # Inicializar Telegram
    await telegram_app.initialize()
    
    # Configurar webhook si hay DOMAIN_URL
    if DOMAIN_URL:
        webhook_url = f"{DOMAIN_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        logging.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        await telegram_app.start()
        logging.info("✅ Bot iniciado con polling")

    logging.info("🚀 Bot de Graduación Pump.fun listo - Usa /iniciar para comenzar")

async def shutdown():
    """Apagado de la aplicación"""
    logging.info("🛑 Apagando bot...")
    
    if helius_monitor:
        await helius_monitor.stop()
    
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
