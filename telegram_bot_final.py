# telegram_bot_final.py
# BOT ESENCIAL PUMP.FUN - FastAPI + Webhook

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

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

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OWNER_ID = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))

# Parámetros de trading
MIN_GAIN_PERCENT = float(os.getenv("MIN_GAIN_PERCENT", "300"))    # +300%
MAX_TIME_WINDOW = int(os.getenv("MAX_TIME_WINDOW", "15"))         # 15 minutos
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "50"))   # -50%
MAX_MONITOR_TIME = int(os.getenv("MAX_MONITOR_TIME", "30"))       # 30 minutos máximo

# WebSocket PumpPortal
PUMP_PORTAL_WSS = "wss://pumpportal.fun/api/data"

# ========== BASE DE DATOS SIMPLE ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas si no existen"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas esenciales"""
        async with self.pool.acquire() as conn:
            # Tabla de tokens monitoreados
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS token_monitoring (
                    mint TEXT PRIMARY KEY,
                    symbol TEXT,
                    initial_price DECIMAL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'monitoring'
                )
            """)

    async def add_token(self, mint: str, symbol: str, initial_price: float):
        """Agregar token a monitoreo"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO token_monitoring (mint, symbol, initial_price)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    initial_price = EXCLUDED.initial_price,
                    created_at = NOW(),
                    status = 'monitoring'
            """, mint, symbol, initial_price)

    async def update_token_status(self, mint: str, status: str):
        """Actualizar estado del token"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE token_monitoring SET status = $1 WHERE mint = $2
            """, status, mint)

    async def get_monitoring_tokens(self) -> List[Dict]:
        """Obtener tokens en monitoreo"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM token_monitoring WHERE status = 'monitoring'")

# ========== CLIENTE API SIMPLE ==========
class DexScreenerAPI:
    @staticmethod
    async def get_token_data(mint: str) -> Dict:
        """Obtener datos del token desde DexScreener"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('pairs') and len(data['pairs']) > 0:
                            pair = data['pairs'][0]
                            return {
                                'price': float(pair['priceUsd']),
                                'market_cap': float(pair.get('marketCap', 0)),
                                'liquidity': float(pair.get('liquidity', {}).get('usd', 0)),
                                'fdv': float(pair.get('fdv', 0))
                            }
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos de DexScreener: {e}")
        return {}

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_alert(self, mint: str, symbol: str, gain_percent: float, elapsed_min: float, token_data: Dict):
        """Enviar alerta de pump"""
        if self.silent_mode:
            return

        message = (
            f"🚀 <b>ALERTA DE PUMP DETECTADA</b> 🚀\n\n"
            f"<b>{symbol}</b>\n"
            f"• Ganancia: <b>+{gain_percent:.1f}%</b>\n"
            f"• Tiempo: {elapsed_min:.1f} minutos\n"
            f"• Market Cap: ${token_data.get('market_cap', 0):,.0f}\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>"
        )

        keyboard = [
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}")]
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
            logging.info(f"✅ Alerta enviada: {symbol} +{gain_percent:.1f}%")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {e}")

# ========== MONITOR PRINCIPAL ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.monitoring_tasks = {}
        self.websocket_task = None
        self.running = False

    async def start_websocket(self):
        """Conectar a PumpPortal WebSocket y suscribirse a nuevos tokens"""
        self.running = True
        reconnect_delay = 1

        while self.running:
            try:
                async with websockets.connect(PUMP_PORTAL_WSS) as websocket:
                    logging.info("✅ Conectado a PumpPortal WebSocket")
                    reconnect_delay = 1

                    # Suscribirse a nuevos tokens
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    logging.info("✅ Suscrito a nuevos tokens")

                    async for message in websocket:
                        if not self.running:
                            break
                        await self.process_new_token(message)

            except Exception as e:
                logging.error(f"❌ WebSocket error: {e}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def process_new_token(self, message: str):
        """Procesar nuevo token detectado"""
        try:
            data = json.loads(message)
            mint = data.get('mint')
            if not mint:
                return

            # Evitar duplicados
            if mint in self.monitoring_tasks:
                return

            # Obtener datos iniciales
            token_data = await DexScreenerAPI.get_token_data(mint)
            if not token_data or token_data.get('price', 0) == 0:
                return

            initial_price = token_data['price']
            symbol = f"TOKEN_{mint[:6]}"

            # Agregar a la base de datos
            await self.db.add_token(mint, symbol, initial_price)

            # Iniciar monitoreo individual
            task = asyncio.create_task(self.monitor_token(mint, symbol, initial_price))
            self.monitoring_tasks[mint] = task
            logging.info(f"🎯 Monitoreando nuevo token: {symbol}")

        except Exception as e:
            logging.error(f"❌ Error procesando nuevo token: {e}")

    async def monitor_token(self, mint: str, symbol: str, initial_price: float):
        """Monitorear un token individualmente"""
        start_time = datetime.now()
        max_price = initial_price

        while self.running:
            try:
                # Verificar tiempo máximo (30 minutos)
                elapsed = (datetime.now() - start_time).total_seconds() / 60
                if elapsed > MAX_MONITOR_TIME:
                    await self.cleanup_token(mint, "timeout")
                    return

                # Obtener precio actual
                current_data = await DexScreenerAPI.get_token_data(mint)
                if not current_data:
                    await asyncio.sleep(10)
                    continue

                current_price = current_data.get('price', 0)
                if current_price == 0:
                    await asyncio.sleep(10)
                    continue

                # Actualizar precio máximo
                if current_price > max_price:
                    max_price = current_price

                # Calcular ganancia
                gain_percent = ((current_price - initial_price) / initial_price) * 100

                # ⚡ REGLA PRINCIPAL: +X% en Y minutos
                if gain_percent >= MIN_GAIN_PERCENT and elapsed <= MAX_TIME_WINDOW:
                    await self.notifier.send_alert(mint, symbol, gain_percent, elapsed, current_data)
                    await self.cleanup_token(mint, "alert_sent")
                    return

                # 🛑 STOP LOSS: -Z% desde el máximo
                current_loss = ((current_price - max_price) / max_price) * 100
                if current_loss <= -STOP_LOSS_PERCENT:
                    await self.cleanup_token(mint, "stop_loss")
                    return

                await asyncio.sleep(10)  # Esperar 10 segundos entre checks

            except Exception as e:
                logging.error(f"❌ Error monitoreando {symbol}: {e}")
                await asyncio.sleep(30)

    async def cleanup_token(self, mint: str, reason: str):
        """Limpiar token del monitoreo"""
        if mint in self.monitoring_tasks:
            self.monitoring_tasks[mint].cancel()
            del self.monitoring_tasks[mint]

        await self.db.update_token_status(mint, reason)
        logging.info(f"🛑 Token {mint} eliminado: {reason}")

    async def start(self):
        """Iniciar monitor"""
        if self.websocket_task and not self.websocket_task.done():
            return

        self.websocket_task = asyncio.create_task(self.start_websocket())
        logging.info("✅ PumpFunMonitor iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False

        # Cancelar todas las tareas de monitoreo
        for mint, task in self.monitoring_tasks.items():
            task.cancel()

        if self.websocket_task:
            self.websocket_task.cancel()
            try:
                await self.websocket_task
            except asyncio.CancelledError:
                pass

        logging.info("⛔ PumpFunMonitor detenido")

# ========== APLICACIÓN FASTAPI ==========
app = FastAPI(title="Pump.fun Monitor Bot")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None

# ========== COMANDOS TELEGRAM ==========
def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    if not update.effective_user:
        return False
    return str(update.effective_user.id) == OWNER_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "🤖 <b>PUMP.FUN MONITOR BOT</b>\n\n"
        "• Detección en tiempo real via PumpPortal\n"
        f"• Alerta: +{MIN_GAIN_PERCENT}% en {MAX_TIME_WINDOW}min\n"
        f"• Stop Loss: {STOP_LOSS_PERCENT}%\n"
        f"• Duración máxima: {MAX_MONITOR_TIME}min\n\n"
        "Usa /iniciar para comenzar el monitoreo",
        parse_mode="HTML"
    )

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar"""
    if not is_authorized(update):
        return

    try:
        await pump_monitor.start()
        await update.message.reply_text("✅ Monitor iniciado")
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando monitor: {e}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener"""
    if not is_authorized(update):
        return

    try:
        await pump_monitor.stop()
        await update.message.reply_text("⛔ Monitor detenido")
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitor: {e}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status"""
    if not is_authorized(update):
        return

    status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    tokens_count = len(pump_monitor.monitoring_tasks) if pump_monitor else 0

    await update.message.reply_text(
        f"📊 <b>Estado del Monitor</b>\n\n"
        f"• Estado: {status}\n"
        f"• Tokens monitoreados: {tokens_count}\n",
        parse_mode="HTML"
    )

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Endpoint para webhooks de Telegram"""
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        if update.effective_user and str(update.effective_user.id) == OWNER_ID:
            await telegram_app.process_update(update)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"❌ Webhook error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitor_running": pump_monitor.running if pump_monitor else False,
        "tokens_monitored": len(pump_monitor.monitoring_tasks) if pump_monitor else 0
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {"message": "Pump.fun Monitor Bot", "status": "operational"}

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    """Inicializar aplicación"""
    global telegram_app, notifier, pump_monitor

    # Verificar variables críticas
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OWNER_ID, DATABASE_URL]):
        raise RuntimeError("❌ Faltan variables de entorno críticas")

    # Conectar base de datos
    await db.connect()

    # Inicializar Telegram
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))

    # Inicializar notificador y monitor
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)

    logging.info("✅ Aplicación inicializada")

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de FastAPI"""
    try:
        await initialize_app()
        await telegram_app.initialize()
        await telegram_app.start()
        logging.info("🚀 Bot iniciado - Usa /iniciar para comenzar el monitoreo")
    except Exception as e:
        logging.error(f"❌ Error durante el startup: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Evento de apagado de FastAPI"""
    logging.info("🛑 Apagando bot...")
    if pump_monitor:
        await pump_monitor.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
