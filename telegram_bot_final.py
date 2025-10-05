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

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
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
        INITIALIZE2_DISCRIMINATOR = bytes([242, 35, 198, 137, 82, 225, 242, 182])
        for ix in msg.instructions:
            program_id = str(account_keys[ix.program_id_index])
            if program_id == RAYDIUM_LP_V4_PROGRAM_ID:
                if ix.data.startswith(INITIALIZE2_DISCRIMINATOR) and len(ix.accounts) > 9:
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
            return sorted(res.json()['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except: pass
    return None

# --- ### NUEVA FUNCIÓN DE BÚSQUEDA DE MOMENTUM ### ---
async def search_dexscreener_for_momentum(client):
    query = "solana liquidity > 2000 and liquidity < 25000 and age > 1 hours and age < 24 hours and txns > 25"
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=45)
        if res.status_code == 200 and res.json().get('pairs'):
            return [pair for pair in res.json()['pairs'] if pair.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error buscando momentum en DexScreener: {e}")
    return []


# --- TAREAS ASÍNCRONAS ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador en Tiempo Real..."); RAYDIUM_PUBKEY = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
    while True:
        try:
            async with connect(HELIUS_WSS_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_PUBKEY)); await websocket.recv()
                logger.info(f"Cazador de Raydium conectado.")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature); await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador detenido."); break
        except Exception as e: logger.error(f"Error en Cazador: {e}. Reiniciando..."); await asyncio.sleep(15)

# --- ### NUEVA TAREA RADAR DE MOMENTUM ### ---
async def momentum_radar_task():
    logger.info("Iniciando Radar de Momentum...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(600) # Revisa cada 10 minutos
                logger.info("[RADAR] Buscando tokens con momentum...")
                
                pairs = await search_dexscreener_for_momentum(client)
                if not pairs:
                    logger.info("[RADAR] No se encontraron tokens que cumplan los criterios de momentum.")
                    continue

                logger.info(f"[RADAR] Encontrados {len(pairs)} candidatos. Enviando a incubadora...")
                for pair in pairs:
                    token_address = pair.get('baseToken', {}).get('address')
                    if token_address and token_address not in incubator and token_address not in watchlist:
                        logger.info(f"  - 📡 [RADAR] {pair.get('baseToken',{}).get('symbol','')} ({token_address[:6]}...) detectado. A la incubadora.")
                        new_data = {'found_at': time.time(), 'source': 'Momentum Radar'}
                        incubator[token_address] = new_data
                        await db_add_to_incubator(token_address, new_data)
            except asyncio.CancelledError: logger.info("Radar de Momentum detenido."); break
            except Exception as e: logger.error(f"Error en Radar de Momentum: {e}")

async def processor_task(client):
    logger.info("Iniciando Procesador Manual...")
    while True:
        try:
            signature = await signature_queue.get()
            raw_tx = await get_raw_transaction(client, signature)
            if raw_tx:
                token_address = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [CAZADOR] {token_address[:10]}... a la incubadora.")
                    new_data = {'found_at': time.time(), 'source': 'Cazador'}; incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
            signature_queue.task_done();
            await asyncio.sleep(3)
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(180) 
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            now_ts = time.time(); SECONDS_IN_HOUR = 3600
            for token_address, data in list(incubator.items()):
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if now_ts - data['found_at'] > 3 * SECONDS_IN_HOUR: # Aumentamos a 3h el tiempo de espera por datos
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 3h.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                if liquidity < 7500:
                    logger.info(f"  - [EN ESPERA] {token_address[:10]}... liquidez baja (${liquidity:,.0f}). Objetivo: >$7.5k")
                    continue

                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]} pasa el filtro de liquidez!")
                new_watchlist_data = {'approved_at': now_ts}; watchlist[token_address] = new_watchlist_data
                await db_add_to_watchlist(token_address, new_watchlist_data)
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                
                mensaje = (f"✅ **Alerta de Oportunidad ({data.get('source')})**\n\n**Mint:** `{token_address}`\n\n**Liquidez:** `${liquidity:,.2f}` USD\n\n🚨 *Realiza tu análisis de seguridad manual en RugCheck.*")
                if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v8.0 (Doble Motor). Usa /cazar y /parar.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando Doble Motor (Cazador en Tiempo Real + Radar de Momentum)!")
    await setup_database(); global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(raydium_hunter_task()),
        asyncio.create_task(momentum_radar_task()),
        asyncio.create_task(processor_task(client)),
        asyncio.create_task(incubator_checker_task(client, context))
    ]})
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Caza detenida!")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo** (Doble Motor).\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🏆 **{len(watchlist)}** tokens aprobados.\n"
                      f"⌛ **{signature_queue.qsize()}** transacciones en cola.")
    await update.message.reply_text(status_msg, parse_mode='Markdown')
    
async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator: await update.message.reply_text("🐣 La incubadora está vacía."); return
    message = f"🐣 **Últimos Tokens en la Incubadora ({len(incubator)}):**\n\n"
    token_addresses = list(incubator.keys())
    tokens_to_show = token_addresses[-10:]; tokens_to_show.reverse()
    for token_address in tokens_to_show:
        message += f"- `{token_address}`\n"
    if len(incubator) > 10: message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("incubadora", incubator_command))
    
    logger.info("--- El bot está escuchando a Telegram ---"); application.run_polling()
if __name__ == '__main__':
    main()
