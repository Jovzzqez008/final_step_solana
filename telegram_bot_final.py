# solana_monitor_bot_pregrad_v1.py
# Bot de monitoreo Solana enfocado en PRE-GRAD y PRE-EXPLOSIÓN
# PostgreSQL + QuickNode WSS + Jupiter API + DexScreener + FastAPI Webhook
# Deployment: Railway (Puerto 8080, webhook control)

import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Variables de entorno críticas (Railway)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")

# Webhook domain (ej. https://tu-app.railway.app)
DOMAIN = os.getenv("DOMAIN", "")
PORT = int(os.getenv("PORT", "8080"))

# APIs
JUPITER_API_URL = os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/tokens/v2")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest")

# Helius opcional
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "")

# WSS principal
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")

# Parámetros de detección
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))  # alerta base de nuevo mint
PRE_GRAD_MIN_MC = float(os.getenv("PRE_GRAD_MIN_MC", "50000"))  # umbral inferior pre-grad
PRE_GRAD_MAX_MC = float(os.getenv("PRE_GRAD_MAX_MC", "80000"))  # umbral superior pre-grad

# Pre-explosión
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "100000"))
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL", "2"))

# Regex para detectar mints de Solana (base58)
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL ==========
class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    market_cap DECIMAL,
                    price DECIMAL,
                    volume_24h DECIMAL
                )
            """)

    async def was_notified(self, mint: str, alert_type: Optional[str] = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notifications WHERE mint = $1 AND alert_type = $2",
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: Optional[str] = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (mint, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    created_at = NOW()
            """, mint, alert_type, symbol)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint) VALUES ($1)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

    async def add_metric(self, mint: str, market_cap: Optional[float] = None,
                         price: Optional[float] = None, volume: Optional[float] = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO metrics (mint, market_cap, price, volume_24h)
                VALUES ($1, $2, $3, $4)
            """, mint, market_cap, price, volume)

# ========== CLIENTE HTTP ==========
class APIClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, timeout: int = 8) -> Any:
        try:
            async with self.session.get(url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()
                logging.warning(f"HTTP {response.status} from {url}")
                return None
        except asyncio.TimeoutError:
            logging.warning(f"Timeout fetching {url}")
            return None
        except Exception as e:
            logging.error(f"Error fetching {url}: {str(e)}")
            return None

# ========== INTEGRACIONES DE DATOS ==========
class JupiterAPI:
    def __init__(self, client: APIClient):
        self.client = client
        self.base_url = JUPITER_API_URL.rstrip("/")

    async def search_token(self, mint: str) -> List[Dict]:
        url = f"{self.base_url}/search"
        data = await self.client.fetch_json(url, params={"query": mint})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_recent_tokens(self, limit: int = 30) -> List[Dict]:
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_top_organic(self, interval: str = "5m", limit: int = 30) -> List[Dict]:
        url = f"{self.base_url}/toporganicscore/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tokens", [])
        return []

class DexScreenerAPI:
    def __init__(self, client: APIClient):
        self.client = client
        self.base_url = DEXSCREENER_API.rstrip("/")

    async def get_token_info(self, mint: str) -> Dict:
        url = f"{self.base_url}/tokens/{mint}"
        return await self.client.fetch_json(url)

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str):
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        message = f"🚀 <b>{title}</b> 🚀\n\n<b>{symbol}</b>\n"

        if alert_type == "pump_fun":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0) or 0:.8f}\n"
            if data.get('organic_score'):
                message += f"• Organic Score: {data['organic_score']}\n"
            message += "• 🆕 Nuevo Pool Detectado\n"

        elif alert_type == "pre_grad":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += "• 🚦 Cerca de graduarse en Pump.fun\n"
            message += "• Oportunidad de entrada temprana\n"

        elif alert_type == "pre_explosion":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Volumen 5m: +{data.get('volume_change_5m', 0):.0f}%\n"
            message += f"• Traders 5m: +{data.get('traders_change_5m', 0):.0f}%\n"
            message += f"• Cambio Precio 5m: {data.get('price_change_5m', 0):.2f}%\n"
            message += "• 📈 Acumulación silenciosa detectada\n"

        message += f"\n<b>Mint:</b>\n<code>{mint}</code>"

        keyboard = [
            [InlineKeyboardButton("🔁 Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("🔍 Go+ Security", url=f"https://gopluslabs.io/token-security/{mint}")]
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
            logging.info(f"✅ Alerta enviada: {symbol} - {alert_type}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

# ========== MONITORES ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def process_ws_message(self, message: str):
        # Detectar posibles mints en el stream
        mints = MINT_PATTERN.findall(message)
        for mint in set(mints):
            if await self.db.seen_recently(mint):
                continue

            await self.db.mark_seen(mint)
            logging.info(f"🆕 Mint detectado: {mint}")

            token_data = await self.get_token_data(mint)
            if not token_data:
                continue

            mc = float(token_data.get('market_cap') or 0)

            # Alerta base: nuevo mint con MC mínimo
            if mc >= PUMP_MC_MIN and not await self.db.was_notified(mint, "pump_fun"):
                await self.notifier.send_alert(
                    title="PUMP.FUN DETECTADO",
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    mint=mint,
                    data=token_data,
                    alert_type="pump_fun"
                )
                await self.db.mark_notified(mint, "pump_fun", token_data.get('symbol'))
                await self.db.add_metric(mint, market_cap=mc, price=token_data.get('price'), volume=token_data.get('volume_24h'))

            # Alerta crítica: PRE-GRAD en rango definido
            if (PRE_GRAD_MIN_MC <= mc <= PRE_GRAD_MAX_MC) and not await self.db.was_notified(mint, "pre_grad"):
                await self.notifier.send_alert(
                    title="PRE-GRADUACIÓN DETECTADA",
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    mint=mint,
                    data=token_data,
                    alert_type="pre_grad"
                )
                await self.db.mark_notified(mint, "pre_grad", token_data.get('symbol'))
                await self.db.add_metric(mint, market_cap=mc, price=token_data.get('price'), volume=token_data.get('volume_24h'))

    async def get_token_data(self, mint: str) -> Optional[Dict]:
        async with APIClient() as client:
            jup = JupiterAPI(client)
            tokens = await jup.search_token(mint)
            if tokens:
                t = tokens[0]
                return {
                    'symbol': t.get('symbol', 'UNKNOWN'),
                    'name': t.get('name', 'Unknown'),
                    'price': t.get('usdPrice'),
                    'market_cap': t.get('mcap'),
                    'volume_24h': t.get('volume24h'),
                    'organic_score': t.get('organicScore'),
                    'holders': t.get('holderCount')
                }

            # Fallback DexScreener
            ds = DexScreenerAPI(client)
            data = await ds.get_token_info(mint)
            if data and isinstance(data, dict):
                # DexScreener retorna estructuras diferentes, normalizamos si existe campo token/pairs
                token = data.get('token') or {}
                return {
                    'symbol': token.get('symbol', 'UNKNOWN'),
                    'name': token.get('name', 'Unknown'),
                    'price': token.get('priceUsd'),
                    'market_cap': token.get('marketCap'),
                    'volume_24h': (token.get('volume', {}) or {}).get('h24'),
                    'holders': token.get('holderCount')
                }
        return None

    async def start_websocket(self):
        # Preferencia por QuickNode WSS; si no, intenta Helius WSS si está configurado
        wss_url = QUICKNODE_WSS_URL or HELIUS_WSS_URL
        if not wss_url:
            logging.warning("⚠️ WSS URL no configurada (QUICKNODE_WSS_URL / HELIUS_WSS_URL)")
            return

        self.running = True
        reconnect_delay = 1
        while self.running:
            try:
                async with websockets.connect(wss_url) as ws:
                    logging.info(f"✅ Conectado a WSS: {wss_url}")
                    reconnect_delay = 1
                    async for message in ws:
                        if not self.running:
                            break
                        await self.process_ws_message(message)
            except Exception as e:
                logging.error(f"❌ WebSocket error: {str(e)}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.start_websocket())
        self.running = True
        logging.info("✅ PumpFunMonitor iniciado")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PumpFunMonitor detenido")

class PreExplosionScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def scan(self):
        async with APIClient() as client:
            jup = JupiterAPI(client)

            # Fuentes para candidatos (orgánico alto y recientes)
            organic = await jup.get_top_organic("5m", 40) or []
            recent = await jup.get_recent_tokens(30) or []
            candidates = organic + recent

            unique: Dict[str, Dict] = {}
            for token in candidates:
                mint = token.get('id')
                if mint:
                    unique[mint] = token

            for mint, token in unique.items():
                if await self.db.was_notified(mint, "pre_explosion"):
                    continue

                mc = float(token.get('mcap') or 0)
                if mc < PRE_EXPLOSION_MC_MIN:
                    continue

                stats5m = token.get('stats5m', {}) or {}
                volume_change = float(stats5m.get('volumeChange') or 0)
                traders_change = float(stats5m.get('numTraders') or 0)
                price_change = float(stats5m.get('priceChange') or 0)

                # Señal de acumulación silenciosa
                if (volume_change > 200 and traders_change > 30 and abs(price_change) < 15):
                    await self.notifier.send_alert(
                        title="PRE-EXPLOSIÓN DETECTADA",
                        symbol=token.get('symbol', 'UNKNOWN'),
                        mint=mint,
                        data={
                            'market_cap': mc,
                            'volume_change_5m': volume_change,
                            'traders_change_5m': traders_change,
                            'price_change_5m': price_change
                        },
                        alert_type="pre_explosion"
                    )
                    await self.db.mark_notified(mint, "pre_explosion", token.get('symbol'))
                    await self.db.add_metric(mint, market_cap=mc)

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(CHECK_INTERVAL_MIN * 60)
            except Exception as e:
                logging.error(f"❌ Error en PreExplosionScanner: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        self.running = True
        logging.info("✅ PreExplosionScanner iniciado")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PreExplosionScanner detenido")

# ========== APLICACIÓN FASTAPI + TELEGRAM ==========
app = FastAPI(title="Solana Monitor Bot - PreGrad/PreExplosion")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
pre_explosion_scanner: Optional[PreExplosionScanner] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    welcome_text = (
        "🤖 <b>Solana Monitor - PreGrad & PreExplosion</b>\n\n"
        "<b>Comandos:</b>\n"
        "/iniciar - Iniciar monitores\n"
        "/detener - Detener monitores\n"
        "/status - Estado\n"
        "/silent on|off - Modo silencioso\n\n"
        "<b>Monitores:</b>\n"
        "• 🎯 Pump.Fun (WSS QuickNode/Helius) con alerta PRE-GRAD\n"
        "• 📈 Scanner de PRE-EXPLOSIÓN (Jupiter)\n\n"
        "<code>Estado: Listo, esperando /iniciar</code>"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        if pump_monitor:
            await pump_monitor.start()
        if pre_explosion_scanner:
            await pre_explosion_scanner.start()

        await update.message.reply_text(
            "✅ <b>MONITORES INICIADOS</b>\n\n"
            "🎯 Pump.Fun Monitor → ACTIVO\n"
            "📈 Pre-Explosión Scanner → ACTIVO\n\n"
            "<i>Escaneando mercado en tiempo real...</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        if pump_monitor:
            await pump_monitor.stop()
        if pre_explosion_scanner:
            await pre_explosion_scanner.stop()
        await update.message.reply_text(
            "⛔ <b>MONITORES DETENIDOS</b>\nUsa /iniciar para reactivar.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    pre_status = "🟢 ACTIVO" if pre_explosion_scanner and pre_explosion_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    status_text = (
        "📊 <b>ESTADO</b>\n\n"
        f"🎯 Pump.Fun: {pump_status}\n"
        f"📈 Pre-Explosión: {pre_status}\n"
        f"🔊 Silencioso: {silent_status}\n\n"
        "<b>Parámetros:</b>\n"
        f"• Pump MC Min: ${PUMP_MC_MIN:,.0f}\n"
        f"• Pre-Grad MC: ${PRE_GRAD_MIN_MC:,.0f}–${PRE_GRAD_MAX_MC:,.0f}\n"
        f"• Pre-Explosión MC Min: ${PRE_EXPLOSION_MC_MIN:,.0f}\n"
        f"• Intervalo Scanner: {CHECK_INTERVAL_MIN}min\n\n"
        "<b>Config:</b>\n"
        f"• WSS: {'✅' if QUICKNODE_WSS_URL or HELIUS_WSS_URL else '❌'}\n"
        f"• DB: {'✅' if db.pool else '❌'}\n"
        f"• Webhook: {'✅' if DOMAIN else '❌'}\n"
    )
    await update.message.reply_text(status_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args = context.args
    if not args or args[0] not in ["on", "off"]:
        await update.message.reply_text("Uso: /silent on|off")
        return
    if notifier:
        notifier.silent_mode = (args[0] == "on")
        status = "🔇 ACTIVADO" if notifier.silent_mode else "🔔 DESACTIVADO"
        await update.message.reply_text(f"Modo silencioso: {status}")

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
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
        "version": "pregrad-v1",
        "database": "connected" if db.pool else "disconnected",
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "pre_explosion_scanner": pre_explosion_scanner.running if pre_explosion_scanner else False
        },
        "webhook": f"{DOMAIN}/webhook/{TELEGRAM_BOT_TOKEN}" if DOMAIN else None
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Monitor Bot - PreGrad & PreExplosion",
        "status": "operational",
        "docs": "/docs",
        "health": "/health"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor, pre_explosion_scanner

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables de entorno: {', '.join(missing)}")

    await db.connect()
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    pre_explosion_scanner = PreExplosionScanner(db, notifier)

    logging.info("✅ Aplicación inicializada")

@app.on_event("startup")
async def startup_event():
    try:
        await initialize_app()
        await telegram_app.initialize()
        await telegram_app.start()

        # Configurar webhook (no iniciamos monitores aquí)
        if DOMAIN:
            webhook_url = f"{DOMAIN}/webhook/{TELEGRAM_BOT_TOKEN}"
            await telegram_app.bot.set_webhook(url=webhook_url)
            logging.info(f"🔗 Webhook configurado: {webhook_url}")
        else:
            logging.warning("⚠️ DOMAIN no configurado; el webhook no se estableció")

        logging.info("🚀 Bot listo. Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("🛑 Apagando bot...")
    if pump_monitor:
        await pump_monitor.stop()
    if pre_explosion_scanner:
        await pre_explosion_scanner.stop()
    if telegram_app:
        await telegram_app.bot.delete_webhook()
        await telegram_app.stop()
        await telegram_app.shutdown()
    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
