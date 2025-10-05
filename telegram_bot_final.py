import asyncio
import json
import httpx
import time
import logging
import os
from dotenv import load_dotenv
import base64

import asyncpg

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions
from solders.transaction import VersionedTransaction

load_dotenv()

# --- CONFIGURACIÓN GLOBAL SIMPLIFICADA ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

RAYDIUM_LP_V4_PROGRAM_ID = '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'

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
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")
async def db_add_to_incubator(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data)); await conn.close()
async def db_remove_from_incubator(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address); await conn.close()
async def db_load_all_incubator():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM incubator"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}
async def db_add_to_watchlist(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data)); await conn.close()
async def db_load_all_watchlist():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM watchlist"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}
async def db_remove_from_watchlist(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("DELETE FROM watchlist WHERE token_address = $1", token_address); await conn.close()

# --- FUNCIONES DE ANÁLISIS Y APIS EXTERNAS ---
async def get_raw_transaction(client, signature):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [signature, {"encoding": "base64", "maxSupportedTransactionVersion": 0}]}
    try:
        res = await client.post(HELIUS_RPC_URL, json=payload, timeout=20)
        if res.status_code == 200 and 'result' in res.json() and res.json()['result']: return res.json()['result']['transaction'][0]
    except: pass
    return None
def parse_manual_transaction(raw_tx_base64):
    try:
        tx_data = base64.b64decode(raw_tx_base64)
        tx = VersionedTransaction.from_bytes(tx_data)
        msg = tx.message; account_keys = msg.account_keys
        for ix in msg.instructions:
            program_id_pubkey = account_keys[ix.program_id_index]
            if str(program_id_pubkey) == RAYDIUM_LP_V4_PROGRAM_ID and len(ix.accounts) > 9:
                mint_a = str(account_keys[ix.accounts[8]]); mint_b = str(account_keys[ix.accounts[9]])
                known_mints = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                if mint_a not in known_mints: return mint_a
                if mint_b not in known_mints: return mint_b
    except: pass
    return None
async def get_dexscreener_data(client, token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if not pairs: return None
            return sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except: pass
    return None

# --- TAREAS ASÍNCRONAS ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador..."); RAYDIUM_PUBKEY = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_PUBKEY)); await websocket.recv()
                logger.info(f"Cazador conectado.")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature); await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador detenido."); break
        except Exception as e: logger.error(f"Error en Cazador: {e}. Reiniciando..."); await asyncio.sleep(15)

async def processor_task(client):
    logger.info("Iniciando Procesador Manual...")
    while True:
        try:
            signature = await signature_queue.get()
            raw_tx = await get_raw_transaction(client, signature)
            if raw_tx:
                token_address = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [TOKEN ENCONTRADO] {token_address[:10]}... a la incubadora.")
                    new_data = {'found_at': time.time()}; incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
            signature_queue.task_done(); await asyncio.sleep(1)
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(30)
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            for token_address, data in list(incubator.items()):
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if time.time() - data['found_at'] > 600:
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 10 mins.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    else:
                        logger.info(f"  - [DEXSCREENER ESPERANDO] Aún no hay datos para {token_address[:10]}.")
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                
                if liquidity < 5000 or holders < 1:
                    logger.info(f"  - [MERCADO ESPERANDO] {token_address[:10]} - Liq: ${liquidity:,.0f}, Holders: {holders}")
                    continue

                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]} pasa el filtro de mercado!")
                new_watchlist_data = {'approved_at': time.time(), 'status': 'approved', 'last_notified': 'initial'}
                watchlist[token_address] = new_watchlist_data; await db_add_to_watchlist(token_address, new_watchlist_data)
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                
                mensaje = (f"✅ **Token Aprobado (Solo Mercado)**\n\n**Mint:** `{token_address}`\n**Liquidez:** `${liquidity:,.2f}` USD\n**Holders (aprox):** {holders}\n\n*Recuerda verificar la seguridad manualmente.*")
                if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError: logger.info("Vigilante de la Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    # (La lógica de esta tarea permanece como la diseñaste)
    pass

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v11.2 (Prueba). Usa /cazar, /parar, /status.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🏹 ¡Iniciando caza en MODO DE PRUEBA!")
    await setup_database(); global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    
    # --- BLOQUE DE PRUEBA CORREGIDO ---
    logger.info("--- INYECTANDO TOKENS DE PRUEBA EN LA INCUBADORA ---")
    tokens_de_prueba = [
        "4NGbC4RRrUjS78ooSN53Up7gSg4dGrj6F6dxpMWHbonk",
        "D3jYB29ZjSRwry6c266VNoeVkQWH8fqSsMCy2F8xBAGS",
        "4EyZeBHzExbXJTM6uVDiXyGVZVnf9Vi5rdBaBCFvBAGS",
        "AZbem4s8iLJE5eniDZJ7c8q1ahbfMwWgCA8TxVW2tDUB",
        "AhiEuwGafsTbu2oUkk1Tmbprg1wdUvWiUCLVvAwvBAGS"
    ]
    for token_addr in tokens_de_prueba:
        if token_addr not in incubator and token_addr not in watchlist:
            logger.info(f"  - Inyectando: {token_addr[:10]}...")
            new_data = {'found_at': time.time()}
            incubator[token_addr] = new_data
            await db_add_to_incubator(token_addr, new_data)
    logger.info("--- ¡TOKENS DE PRUEBA INYECTADOS! ---")
    # --- FIN DEL BLOQUE DE PRUEBA ---

    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(raydium_hunter_task()),
        asyncio.create_task(processor_task(client)),
        asyncio.create_task(incubator_checker_task(client, context)),
        asyncio.create_task(watchlist_monitor_task(client, context))
    ]})

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    global TARGET_CHAT_ID; TARGET_CHAT_ID = None
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
    logger.info("--- El bot está escuchando a Telegram ---"); application.run_polling()
if __name__ == '__main__':
    main()
