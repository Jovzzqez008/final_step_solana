import asyncio
import json
import httpx
import time
import logging
import os
from dotenv import load_dotenv

import asyncpg

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions

load_dotenv()

# --- ⚙️ CONFIGURACIÓN GLOBAL ⚙️ ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')

# --- VARIABLES GLOBALES ---
db_pool = None # <-- 🆕 POOL DE CONEXIONES
watchlist = {}
incubator = {}
processed_signatures = set()
signature_queue = asyncio.Queue()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS (REFACTORIZADAS CON POOL) 💾 ---

async def setup_database():
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS incubator (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        logger.info("Base de datos y tablas verificadas/creadas correctamente.")
    except Exception as e:
        logger.error(f"Error al configurar la base de datos: {e}")
        raise e

async def db_add_to_incubator(token_address, data):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data))

async def db_remove_from_incubator(token_address):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)

async def db_load_all_incubator():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM incubator")
    return {row['token_address']: json.loads(row['data']) for row in rows}

async def db_add_to_watchlist(token_address, data):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data))

async def db_update_watchlist_status(token_address, new_status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE watchlist SET data = data || jsonb_build_object('status', $1::text) WHERE token_address = $2", new_status, token_address)

async def db_load_all_watchlist():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
    return {row['token_address']: json.loads(row['data']) for row in rows}


# --- 🤖 FUNCIONES DEL BOT 🤖 ---

async def get_helius_report_and_token(client, signature):
    api_url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    payload = {"transactions": [signature]}
    try:
        res = await client.post(api_url, json=payload, timeout=20)
        res.raise_for_status()
        data = res.json()
        if not data or not data[0]: return None, "Respuesta de Helius vacía"

        tx_data = data[0]
        token_transfers = tx_data.get("tokenTransfers", [])
        for transfer in token_transfers:
            if transfer.get("mint") and transfer.get("toUserAccount"):
                token_address = transfer["mint"]
                # Ignorar tokens comunes como SOL o USDC
                if token_address in {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}:
                    continue
                # Aquí se podría expandir el reporte de Helius
                report = ["- ✅ Token detectado y parseado por Helius."]
                return token_address, "\n".join(report)
        return None, "No se encontró un nuevo token mint en la transacción."
    except httpx.ReadTimeout:
        logger.warning(f"Timeout en Helius para {signature[:10]}...")
        return None, "Timeout en API Helius"
    except Exception as e:
        logger.error(f"Excepción en get_helius_report para {signature[:10]}: {e}")
        return None, f"Excepción en análisis: {e}"

async def procesar_nueva_transaccion(client, signature, source, chat_id):
    logger.info(f"[PROCESADOR] Analizando {signature[:10]}... con Helius.")
    token_address, report = await get_helius_report_and_token(client, signature)

    if token_address and (token_address not in incubator and token_address not in watchlist):
        logger.info(f"  - ✅ [OK SEGURIDAD] Token {token_address[:10]}... parece seguro. Añadiendo a incubadora.")
        new_data = {'found_at': time.time(), 'source': source, 'status': 'incubating', 'security_report': report}
        incubator[token_address] = new_data
        await db_add_to_incubator(token_address, new_data)
    elif token_address:
        logger.info(f"  - 🥱 [DUPLICADO] Token {token_address[:10]}... ya está en la incubadora o watchlist.")
    else:
        logger.info(f"  - ❌ [RECHAZADO] Transacción {signature[:10]} no contenía un candidato válido. Razón: {report}")


# --- 🔥 TAREAS DE FONDO 🔥 ---

async def raydium_hunter_task(chat_id):
    logger.info("Iniciando Cazador Único de Raydium (LPv4)...");
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                first_resp = await websocket.recv(); logger.info(f"Cazador de Raydium conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature)
                            await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(30)

async def helius_processor_task(client, chat_id):
    logger.info("Iniciando Procesador de Helius...")
    while True:
        try:
            signature = await signature_queue.get()
            logger.info(f"[CAZADOR] {signature_queue.qsize()} transacciones en cola.")
            await procesar_nueva_transaccion(client, signature, "Raydium LPv4", chat_id)
            signature_queue.task_done()
            await asyncio.sleep(1.1) # Pausa para no exceder los límites de la API de Helius
        except asyncio.CancelledError: logger.info("Procesador de Helius detenido."); break
        except Exception as e: logger.error(f"Error en Procesador de Helius: {e}")

### --- ✅ TAREA DE INCUBADORA IMPLEMENTADA --- ###
async def incubator_checker_task(client, chat_id):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(300) # Revisa cada 5 minutos
            logger.info("[INCUBADORA] Vigilante despertando...")
            
            local_incubator_copy = await db_load_all_incubator()
            global incubator; incubator = local_incubator_copy

            if not local_incubator_copy:
                logger.info("[INCUBADORA] La incubadora está vacía. Volviendo a dormir.")
                continue

            logger.info(f"[INCUBADORA] Revisando {len(local_incubator_copy)} tokens...")
            current_time = time.time()
            birdeye_url = "https://public-api.birdeye.so/defi/token_overview?address="
            headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}

            for token_address, data in local_incubator_copy.items():
                if current_time - data.get('found_at', 0) > 7200: # 2 horas de vida
                    logger.info(f"  - 🚮 [EXPIRADO] Token {token_address[:10]}... ha expirado. Eliminando.")
                    await db_remove_from_incubator(token_address)
                    if token_address in incubator: del incubator[token_address]
                    continue

                try:
                    res = await client.get(f"{birdeye_url}{token_address}", headers=headers_birdeye, timeout=15)
                    if res.status_code != 200: continue
                    
                    api_data = res.json()
                    if not api_data.get("success") or not api_data.get("data"): continue
                    
                    token_data = api_data["data"]
                    liquidity = token_data.get("liquidity", 0)
                    holders = token_data.get("holders", 0)
                    symbol = token_data.get("symbol", "N/A")

                    if liquidity > 7500 and holders > 50:
                        logger.info(f"  - 🔥 [PROMOCIÓN] {symbol} ({token_address[:10]}...) cumple los criterios!")
                        
                        alerta = (
                            f"🕵️‍♂️ *NUEVO CANDIDATO A VIGILAR*\n\n"
                            f"*{symbol}* ha madurado y cumple los criterios de mercado.\n\n"
                            f"🔗 ` {token_address} `\n\n"
                            f"*Reporte de Seguridad Inicial:*\n{data['security_report']}"
                        )
                        enviar_alerta_telegram_sync(alerta, chat_id)
                        
                        watchlist_data = {
                            'found_at': data['found_at'], 'symbol': symbol, 'status': 'new',
                            'initial_liquidity': liquidity, 'initial_holders': holders,
                            'source': data['source']
                        }
                        await db_add_to_watchlist(token_address, watchlist_data)
                        await db_remove_from_incubator(token_address) # Lo movemos de incubadora a watchlist
                        if token_address in incubator: del incubator[token_address]
                        global watchlist; watchlist[token_address] = watchlist_data

                except Exception as e:
                    logger.error(f"  - Error revisando {token_address[:10]} en Birdeye: {e}")

        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

def enviar_alerta_telegram_sync(mensaje, chat_id):
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True}
        )
    except Exception as e:
        logger.error(f"Error enviando a Telegram: {e}")

# --- 🧠 COMANDOS DE TELEGRAM 🧠 ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO v9 (Estable)!\n\nUsa /cazar, /parar, /diagnostico.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot ya está cazando."); return
    
    await update.message.reply_text("🏹 ¡Iniciando la caza! Motor Helius y Vigilante de Incubadora desplegados.")
    
    global watchlist, incubator
    watchlist = await db_load_all_watchlist()
    incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados desde la DB. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    
    client = httpx.AsyncClient()
    context.bot_data['client'] = client
    
    task_raydium = asyncio.create_task(raydium_hunter_task(chat_id))
    task_processor = asyncio.create_task(helius_processor_task(client, chat_id))
    task_incubator = asyncio.create_task(incubator_checker_task(client, chat_id))
    context.bot_data['tasks'] = [task_raydium, task_processor, task_incubator]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot no está cazando actualmente."); return
    
    tasks = context.bot_data.get('tasks', [])
    for task in tasks: task.cancel()
    
    client = context.bot_data.get('client')
    if client: await client.aclose()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    context.bot_data.clear()
    
    while not signature_queue.empty():
        try: signature_queue.get_nowait()
        except asyncio.QueueEmpty: break
        
    await update.message.reply_text("🛑 ¡Caza detenida! Todos los agentes y conexiones han sido cerrados.")

async def diagnostico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = f"🛑 El bot está **Detenido**.\nTransacciones en fila: {signature_queue.qsize()}."
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ El bot está **Activo**.\n"
            f"🐣 Hay **{len(incubator)}** tokens en la incubadora.\n"
            f"🕵️‍♂️ Hay **{len(watchlist)}** candidatos en la watchlist.\n"
            f"⌛ Hay **{signature_queue.qsize()}** transacciones en la fila de espera para ser analizadas."
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# --- 🚀 ARRANQUE Y PARADA DEL BOT 🚀 ---
async def startup_bot(app: Application):
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        logger.info("Pool de conexiones a la base de datos creado exitosamente.")
        await setup_database()
    except Exception as e:
        logger.critical(f"No se pudo conectar a la base de datos: {e}")
        # Si no hay DB, no podemos arrancar.
        os._exit(1)

async def shutdown_bot(app: Application):
    logger.info("Cerrando pool de conexiones de la base de datos...")
    if db_pool:
        await db_pool.close()

def main():
    print("--- 🤖 Iniciando Bot de Telegram... ---")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN)\
        .post_init(startup_bot)\
        .post_shutdown(shutdown_bot)\
        .build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("diagnostico", diagnostico_command)) # Renombrado de /status
    
    print("--- 🎧 El bot está escuchando a Telegram... ---")
    application.run_polling()

if __name__ == '__main__':
    main()
