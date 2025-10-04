import asyncio
import json
import httpx
import time
import logging
import os
from dotenv import load_dotenv

import asyncpg
import importlib.metadata # <--- NUEVO IMPORTE PARA DEPURACIÓN

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions

load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')

watchlist = {}
incubator = {}
processed_signatures = set()
signature_queue = asyncio.Queue()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS incubator (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        logger.info("Base de datos y tablas verificadas/creadas correctamente.")
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")
async def db_add_to_incubator(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data)); await conn.close()
async def db_load_all_incubator():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM incubator"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}
async def db_load_all_watchlist():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM watchlist"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}

# --- FUNCIONES DEL BOT ---
async def get_helius_tx_details(client, signature):
    api_url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    try:
        res = await client.post(api_url, json={"transactions": [signature]}, timeout=20)
        if res.status_code != 200: return None, "Helius API Error"
        data = res.json()
        if not data or not data[0]: return None, "Respuesta de Helius vacía"
        
        tx_data = data[0]
        token_transfers = tx_data.get("tokenTransfers", [])
        for transfer in token_transfers:
            token_address = transfer.get("mint")
            direcciones_a_ignorar = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
            if token_address and token_address not in direcciones_a_ignorar:
                return token_address, "Token detectado"
        return None, "No se encontró un nuevo token mint"
    except Exception as e:
        logger.error(f"Excepción en get_helius_tx_details para {signature[:10]}: {e}")
        return None, "Excepción en análisis"

async def raydium_hunter_task():
    logger.info("Iniciando Cazador de Raydium...")
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                await websocket.recv()
                logger.info(f"Cazador de Raydium conectado.")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature)
                            await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(15)

async def helius_processor_task(client):
    logger.info("Iniciando Procesador de Helius...")
    while True:
        try:
            signature = await signature_queue.get()
            logger.info(f"[PROCESADOR] Analizando {signature[:10]}...")
            token_address, reason = await get_helius_tx_details(client, signature)
            if token_address and (token_address not in incubator and token_address not in watchlist):
                logger.info(f"  - ✅ [OK] Token {token_address[:10]}... válido. Añadiendo a incubadora.")
                new_data = {'found_at': time.time(), 'source': "Raydium LPv4", 'status': 'new'}
                incubator[token_address] = new_data
                await db_add_to_incubator(token_address, new_data)
            elif not token_address:
                logger.info(f"  - ❌ [RECHAZADO] {signature[:10]}. Razón: {reason}")
            signature_queue.task_done()
            await asyncio.sleep(1.2) # Pausa para no saturar la API
        except asyncio.CancelledError: logger.info("Procesador de Helius detenido."); break
        except Exception as e: logger.error(f"Error en Procesador de Helius: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- CÓDIGO DE DEPURACIÓN AÑADIDO ---
    try:
        solana_version = importlib.metadata.version("solana")
        solders_version = importlib.metadata.version("solders")
        mensaje_debug = (
            f"⚙️ **Bot Iniciado (Depuración)**\n\n"
            f"Versión de `solana`: <b>{solana_version}</b>\n"
            f"Versión de `solders`: <b>{solders_version}</b>"
        )
        await update.message.reply_text(mensaje_debug, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Error al obtener versiones: {e}")
    # --- FIN DEL CÓDIGO DE DEPURACIÓN ---
    await update.message.reply_text("👋 ¡Bienvenido! Usa /cazar, /parar, /status.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza!")
    await setup_database()
    global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    client = httpx.AsyncClient()
    context.bot_data['client'] = client
    
    task_raydium = asyncio.create_task(raydium_hunter_task())
    task_processor = asyncio.create_task(helius_processor_task(client))
    context.bot_data['tasks'] = [task_raydium, task_processor]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data.get('tasks', []): task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Caza detenida!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo**.\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🕵️‍♂️ **{len(watchlist)}** en watchlist.\n"
                      f"⌛ **{signature_queue.qsize()}** transacciones en cola.")
    await update.message.reply_text(status_msg, parse_mode='Markdown')

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    logger.info("--- El bot está escuchando a Telegram ---")
    application.run_polling()

if __name__ == '__main__':
    main()
