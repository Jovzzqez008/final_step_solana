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
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL") # https://
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL") # wss://
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
        
        # El "discriminador" de 8 bytes de la instrucción 'initialize2' de Raydium.
        # Esta es su "huella digital" única y la forma más precisa de identificarla.
        INITIALIZE2_DISCRIMINATOR = bytes([242, 35, 198, 137, 82, 225, 242, 182])

        for ix in msg.instructions:
            program_id = str(account_keys[ix.program_id_index])
            
            if program_id == RAYDIUM_LP_V4_PROGRAM_ID:
                ix_data = ix.data
                is_initialize_ix = ix_data.startswith(INITIALIZE2_DISCRIMINATOR)
                
                if is_initialize_ix and len(ix.accounts) > 9:
                    mint_a = str(account_keys[ix.accounts[8]])
                    mint_b = str(account_keys[ix.accounts[9]])
                    known_mints = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                    
                    if mint_a not in known_mints: return mint_a
                    if mint_b not in known_mints: return mint_b
    except Exception as e:
        logger.error(f"Error decodificando la transacción: {e}")
    return None

async def get_dexscreener_data(client, token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            return sorted(res.json()['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except: pass
    return None

# --- TAREAS ASÍNCRONAS ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador en Tiempo Real..."); RAYDIUM_PUBKEY = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
    while True:
        try:
            async with connect(HELIUS_WSS_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_PUBKEY)); await websocket.recv()
                logger.info(f"Cazador conectado a Raydium.")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature); await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador detenido."); break
        except Exception as e: logger.error(f"Error en Cazador: {e}. Reiniciando..."); await asyncio.sleep(15)

async def processor_task(client):
    logger.info("Iniciando Procesador Manual Estricto...")
    while True:
        try:
            signature = await signature_queue.get()
            raw_tx = await get_raw_transaction(client, signature)
            if raw_tx:
                token_address = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [TOKEN REAL ENCONTRADO] {token_address[:10]}... a la incubadora.")
                    new_data = {'found_at': time.time()}; incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
            signature_queue.task_done();
            await asyncio.sleep(3)
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        # --- BLOQUE DE CÓDIGO CORREGIDO ---
        try:
            await asyncio.sleep(180) 
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            now_ts = time.time(); SECONDS_IN_HOUR = 3600
            for token_address, data in list(incubator.items()):
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if now_ts - data['found_at'] > SECONDS_IN_HOUR:
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 1h.")
                        del
