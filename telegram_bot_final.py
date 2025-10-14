import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any, List
from datetime import datetime
import websockets

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# -------------------- CONFIG PUMP.FUN --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# PARÁMETROS DE LA ESTRATEGIA
MIN_GAIN_PERCENT = 300    # +300% mínimo para alerta
MAX_TIME_WINDOW = 15      # 15 minutos máximo para alcanzar +300%
STOP_LOSS_PERCENT = 50    # -50% stop loss
MAX_MONITOR_TIME = 30     # 30 minutos máximo de monitoreo por token

# PUMP PORTAL WEBSOCKET
PUMP_PORTAL_WSS = "wss://pumpportal.fun/api/data"

# Estructuras en memoria
monitored_tokens: Dict[str, Dict[str, Any]] = {}  # Tokens siendo monitoreados

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE --------------------
async def setup_database():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL configurada.")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS token_monitoring (
                token_address TEXT PRIMARY KEY,
                initial_price DECIMAL,
                initial_market_cap DECIMAL,
                created_at TIMESTAMP DEFAULT NOW(),
                status TEXT DEFAULT 'monitoring'
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS token_alerts (
                id SERIAL PRIMARY KEY,
                token_address TEXT NOT NULL,
                gain_percent DECIMAL,
                time_elapsed DECIMAL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        await conn.close()
        logger.info("Base de datos inicializada.")
    except Exception as e:
        logger.error(f"Error inicializando DB: {e}")

async def db_add_monitored_token(token_address: str, initial_price: float, initial_market_cap: float):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO token_monitoring (token_address, initial_price, initial_market_cap) VALUES ($1, $2, $3) ON CONFLICT (token_address) DO UPDATE SET initial_price = $2, initial_market_cap = $3",
            token_address, initial_price, initial_market_cap
        )
    finally: await conn.close()

async def db_remove_monitored_token(token_address: str):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DELETE FROM token_monitoring WHERE token_address = $1", token_address)
    finally: await conn.close()

async def db_record_alert(token_address: str, gain_percent: float, time_elapsed: float):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO token_alerts (token_address, gain_percent, time_elapsed) VALUES ($1, $2, $3)",
            token_address, gain_percent, time_elapsed
        )
    finally: await conn.close()

# -------------------- DETECCIÓN PUMP.FUN --------------------
async def pump_portal_websocket_task(context: ContextTypes.DEFAULT_TYPE):
    """Conexión WebSocket a PumpPortal para detectar nuevos tokens"""
    logger.info("🚀 Conectando a PumpPortal WebSocket...")
    
    while True:
        try:
            async with websockets.connect(PUMP_PORTAL_WSS) as websocket:
                # Suscribirse a nuevos tokens
                subscribe_msg = {
                    "method": "subscribeNewToken"
                }
                await websocket.send(json.dumps(subscribe_msg))
                logger.info("✅ Suscrito a nuevos tokens en PumpPortal")

                async for message in websocket:
                    try:
                        data = json.loads(message)
                        await handle_new_token(data, context)
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Error decodificando mensaje: {e}")
                    except Exception as e:
                        logger.error(f"❌ Error procesando mensaje: {e}")

        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}, reconectando en 5 segundos...")
            await asyncio.sleep(5)

async def handle_new_token(token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Manejar nuevo token detectado desde Pump.fun"""
    mint = token_data.get('mint')
    if not mint:
        logger.warning("❌ Token sin mint, ignorando")
        return

    # Evitar duplicados
    if mint in monitored_tokens:
        logger.info(f"❌ Token {mint} ya está siendo monitoreado")
        return

    logger.info(f"🎯 Nuevo token detectado: {mint}")

    # Obtener datos iniciales del token
    initial_data = await get_token_initial_data(mint)
    if not initial_data:
        logger.warning(f"❌ No se pudieron obtener datos iniciales para {mint}")
        return

    initial_price = initial_data.get('price', 0)
    initial_market_cap = initial_data.get('market_cap', 0)

    if initial_price <= 0:
        logger.warning(f"❌ Precio inicial inválido para {mint}")
        return

    # Agregar a monitoreo
    monitored_tokens[mint] = {
        'symbol': token_data.get('symbol', f"TOKEN_{mint[:6]}"),
        'name': token_data.get('name', 'Unknown'),
        'initial_price': initial_price,
        'initial_market_cap': initial_market_cap,
        'start_time': time.time(),
        'max_price': initial_price,
        'monitoring_task': None
    }

    # Guardar en base de datos
    await db_add_monitored_token(mint, initial_price, initial_market_cap)

    # Iniciar tarea de monitoreo individual
    task = asyncio.create_task(monitor_single_token(mint, context))
    monitored_tokens[mint]['monitoring_task'] = task

    logger.info(f"✅ Token {mint} agregado a monitoreo")

# -------------------- MONITOREO INDIVIDUAL --------------------
async def monitor_single_token(mint: str, context: ContextTypes.DEFAULT_TYPE):
    """Monitorear un token individualmente por 30 minutos máximo"""
    token_data = monitored_tokens[mint]
    start_time = token_data['start_time']
    initial_price = token_data['initial_price']
    max_price = initial_price
    
    logger.info(f"🔍 Iniciando monitoreo para {token_data['symbol']}")

    while True:
        try:
            current_time = time.time()
            elapsed_minutes = (current_time - start_time) / 60

            # Verificar tiempo máximo de monitoreo (30 minutos)
            if elapsed_minutes > MAX_MONITOR_TIME:
                logger.info(f"🕒 Token {mint} alcanzó 30 minutos, eliminando monitoreo")
                await cleanup_token(mint, "timeout")
                return

            # Obtener datos actualizados
            current_data = await get_token_current_data(mint)
            if not current_data:
                await asyncio.sleep(10)
                continue

            current_price = current_data.get('price', 0)
            current_market_cap = current_data.get('market_cap', 0)

            # Actualizar precio máximo
            if current_price > max_price:
                max_price = current_price
                monitored_tokens[mint]['max_price'] = max_price

            # Calcular ganancia porcentual
            gain_percent = ((current_price - initial_price) / initial_price) * 100

            # ⚡ REGLA PRINCIPAL: +300% en 15 minutos
            if gain_percent >= MIN_GAIN_PERCENT and elapsed_minutes <= MAX_TIME_WINDOW:
                logger.info(f"🎯 ALERTA! {token_data['symbol']} +{gain_percent:.1f}% en {elapsed_minutes:.1f}min")
                
                # Enviar alerta
                await send_pump_alert(mint, token_data, gain_percent, elapsed_minutes, current_data, context)
                
                # Registrar en base de datos
                await db_record_alert(mint, gain_percent, elapsed_minutes)
                
                # Limpiar token
                await cleanup_token(mint, "alert_sent")
                return

            # 🛑 STOP LOSS: -50% desde precio máximo
            loss_from_max = ((current_price - max_price) / max_price) * 100
            if loss_from_max <= -STOP_LOSS_PERCENT:
                logger.info(f"🛑 STOP LOSS: {token_data['symbol']} -{abs(loss_from_max):.1f}% desde máximo")
                await cleanup_token(mint, "stop_loss")
                return

            await asyncio.sleep(10)  # Verificar cada 10 segundos

        except Exception as e:
            logger.error(f"❌ Error monitoreando {mint}: {e}")
            await asyncio.sleep(30)

async def cleanup_token(mint: str, reason: str):
    """Limpiar token del monitoreo"""
    if mint in monitored_tokens:
        # Cancelar tarea de monitoreo si existe
        if monitored_tokens[mint]['monitoring_task']:
            monitored_tokens[mint]['monitoring_task'].cancel()
        
        # Remover de memoria
        del monitored_tokens[mint]
        
        # Remover de base de datos
        await db_remove_monitored_token(mint)
        
        logger.info(f"✅ Token {mint} removido: {reason}")

# -------------------- OBTENER DATOS TOKEN --------------------
async def get_token_initial_data(mint: str) -> Dict:
    """Obtener datos iniciales del token"""
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            response = await client.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('pairs') and len(data['pairs']) > 0:
                    pair = data['pairs'][0]
                    return {
                        'price': float(pair.get('priceUsd', 0)),
                        'market_cap': float(pair.get('marketCap', 0)),
                        'liquidity': float(pair.get('liquidity', {}).get('usd', 0))
                    }
    except Exception as e:
        logger.error(f"❌ Error obteniendo datos iniciales: {e}")
    return {}

async def get_token_current_data(mint: str) -> Dict:
    """Obtener datos actualizados del token"""
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            response = await client.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('pairs') and len(data['pairs']) > 0:
                    pair = data['pairs'][0]
                    return {
                        'price': float(pair.get('priceUsd', 0)),
                        'market_cap': float(pair.get('marketCap', 0)),
                        'volume_24h': float(pair.get('volume', {}).get('h24', 0)),
                        'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0))
                    }
    except Exception as e:
        logger.error(f"❌ Error obteniendo datos actualizados: {e}")
    return {}

# -------------------- ALERTAS TELEGRAM --------------------
async def send_pump_alert(mint: str, token_data: Dict, gain_percent: float, elapsed_minutes: float, current_data: Dict, context: ContextTypes.DEFAULT_TYPE):
    """Enviar alerta de pump detectado"""
    symbol = token_data['symbol']
    name = token_data['name']
    market_cap = current_data.get('market_cap', 0)
    current_price = current_data.get('price', 0)

    message = (
        f"🚀 *ALERTA DE PUMP DETECTADA!* 🚀\n\n"
        f"*Token:* {name} ({symbol})\n"
        f"*Ganancia:* +{gain_percent:.1f}%\n"
        f"*Tiempo:* {elapsed_minutes:.1f} minutos\n"
        f"*Market Cap:* ${market_cap:,.0f}\n"
        f"*Precio Actual:* ${current_price:.6f}\n\n"
        f"*Address:* `{mint}`\n\n"
        f"🔗 *Enlaces Rápidos:*"
    )

    keyboard = [
        [InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
    ]

    if TARGET_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=message,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True
            )
            logger.info(f"✅ Alerta enviada para {symbol}")
        except Exception as e:
            logger.error(f"❌ Error enviando alerta: {e}")

# -------------------- TAREAS PRINCIPALES --------------------
async def pump_fun_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Tarea principal de monitoreo Pump.fun"""
    logger.info("🚀 Iniciando Monitor Pump.fun...")
    await pump_portal_websocket_task(context)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Pump.fun - Alerta de Pumps*\n\n"
        "🎯 *Objetivo:* Detectar tokens que alcanzan +300% en 15 minutos\n"
        "⏰ *Monitoreo:* 30 minutos máximo por token\n"
        "🛑 *Stop Loss:* -50% desde máximo\n"
        "📡 *Fuente:* PumpPortal WebSocket en tiempo real\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener monitoreo\n"
        "*/status* - Estado actual\n"
        "*/tokens* - Tokens siendo monitoreados",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('monitor_task'):
        await update.message.reply_text("🤔 Ya está monitoreando Pump.fun")
        return
        
    await update.message.reply_text("🏹 *Iniciando Monitor Pump.fun...*", parse_mode='Markdown')
    
    await setup_database()

    # Iniciar tarea de monitoreo
    context.bot_data['monitor_task'] = asyncio.create_task(
        pump_fun_monitor_task(context)
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('monitor_task'):
        await update.message.reply_text("🤔 No está monitoreando")
        return
        
    # Cancelar tarea de monitoreo
    context.bot_data['monitor_task'].cancel()
    context.bot_data.clear()
    
    # Limpiar todos los tokens monitoreados
    for mint in list(monitored_tokens.keys()):
        await cleanup_token(mint, "monitor_stopped")
        
    await update.message.reply_text("🛑 Monitor detenido")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 Monitor detenido"
    if context.bot_data.get('monitor_task'):
        status_msg = (
            f"✅ *Monitor Pump.fun Activo*\n\n"
            f"🔍 *Tokens Monitoreados:* {len(monitored_tokens)}\n"
            f"🎯 *Regla:* +{MIN_GAIN_PERCENT}% en {MAX_TIME_WINDOW}min\n"
            f"🛑 *Stop Loss:* {STOP_LOSS_PERCENT}%\n"
            f"⏰ *Duración Máxima:* {MAX_MONITOR_TIME}min\n\n"
            f"📡 *Conectado a PumpPortal WebSocket*"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitored_tokens:
        await update.message.reply_text("🔍 No hay tokens siendo monitoreados")
        return
        
    message = f"🔍 *Tokens en Monitoreo ({len(monitored_tokens)}):*\n\n"
    for i, (mint, data) in enumerate(monitored_tokens.items(), 1):
        elapsed = (time.time() - data['start_time']) / 60
        message += f"{i}. `{mint}`\n   📛 {data['symbol']} | ⏰ {elapsed:.1f}min\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# -------------------- MAIN --------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado.")
        return
        
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("tokens", tokens_command))

    logger.info("--- Bot Pump.fun - Listo para monitoreo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
