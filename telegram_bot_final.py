import asyncio
import json
import httpx
import time
import logging
import os
from dotenv import load_dotenv
import base64

import asyncpg
import importlib.metadata

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions
from solders.transaction import VersionedTransaction

load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY") # Lo mantenemos por si se usa en el futuro
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

RAYDIUM_LP_V4_PROGRAM_ID = '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'

watchlist = {}
incubator = {}
processed_signatures = set()
signature_queue = asyncio.Queue()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
# (Sin cambios)
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS incubator (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
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

# --- ### NUEVAS FUNCIONES DE ANÁLISIS MANUAL ### ---
async def get_raw_transaction(client, signature):
    """Obtiene los datos crudos de la transacción usando una llamada RPC estándar."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "base64",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }
    try:
        res = await client.post(HELIUS_RPC_URL, json=payload, timeout=20)
        if res.status_code != 200:
            logger.error(f"Error RPC getTransaction para {signature[:10]}: {res.status_code}")
            return None, "Error RPC"
        
        data = res.json()
        if 'error' in data:
            logger.error(f"Error en respuesta RPC: {data['error']}")
            return None, "Error en respuesta RPC"
        
        # La transacción viene en un array, tomamos el primer elemento que es la data en base64
        raw_tx_base64 = data.get('result', {}).get('transaction', [None])[0]
        if raw_tx_base64:
            return raw_tx_base64, "Éxito"
        else:
            return None, "Respuesta sin datos de transacción"
            
    except Exception as e:
        logger.error(f"Excepción en get_raw_transaction: {e}")
        return None, "Excepción"

def parse_manual_transaction(raw_tx_base64):
    """Decodifica la transacción y busca la creación de un nuevo pool de Raydium."""
    try:
        tx_data = base64.b64decode(raw_tx_base64)
        tx = VersionedTransaction.from_bytes(tx_data)
        msg = tx.message
        account_keys = msg.account_keys

        # Buscamos la instrucción que llama al programa de Raydium
        for ix in msg.instructions:
            program_id_pubkey = account_keys[ix.program_id_index]
            if str(program_id_pubkey) == RAYDIUM_LP_V4_PROGRAM_ID:
                
                # ¡Pista Encontrada! Esta es una instrucción de Raydium.
                # Ahora extraemos los mints. Para la instrucción 'initialize2' de Raydium,
                # las direcciones de los tokens suelen estar en posiciones específicas
                # de la lista de cuentas de la instrucción.
                # (Indices 8 y 9 son comunes para los mints)
                if len(ix.accounts) > 9:
                    mint_a_index = ix.accounts[8]
                    mint_b_index = ix.accounts[9]
                    
                    mint_a_pubkey = account_keys[mint_a_index]
                    mint_b_pubkey = account_keys[mint_b_index]

                    direcciones_conocidas = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                    
                    if str(mint_a_pubkey) not in direcciones_conocidas:
                        return str(mint_a_pubkey), "Nuevo token encontrado"
                    elif str(mint_b_pubkey) not in direcciones_conocidas:
                        return str(mint_b_pubkey), "Nuevo token encontrado"
        
        return None, "No se encontró instrucción de creación de pool de Raydium"
    except Exception as e:
        logger.error(f"Error decodificando la transacción: {e}")
        return None, "Error de decodificación"

# --- TAREAS ASÍNCRONAS (CAZADOR Y PROCESADOR MODIFICADO) ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador de Raydium...")
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)))
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

# --- ### PROCESADOR MODIFICADO PARA USAR ANÁLISIS MANUAL ### ---
async def helius_processor_task(client):
    logger.info("Iniciando Procesador Manual...")
    while True:
        try:
            signature = await signature_queue.get()
            logger.info(f"[PROCESADOR] Obteniendo datos crudos de {signature[:10]}...")
            
            raw_tx, reason = await get_raw_transaction(client, signature)
            
            if raw_tx:
                token_address, reason_parse = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [OK] Token {token_address[:10]}... encontrado manualmente. Añadiendo a incubadora.")
                    new_data = {'found_at': time.time(), 'source': "Raydium LPv4 (Manual)", 'status': 'incubating'}
                    incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
                else:
                    logger.info(f"  - ❌ [RECHAZADO] {signature[:10]}. Razón: {reason_parse}")
            else:
                 logger.info(f"  - ❌ [RECHAZADO] {signature[:10]}. No se pudieron obtener datos. Razón: {reason}")

            signature_queue.task_done()
            await asyncio.sleep(2) # Pausa para no saturar el RPC gratuito
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")


# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido! Bot con motor de análisis manual. Usa /cazar, /parar, /status.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza con motor de análisis manual!")
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
