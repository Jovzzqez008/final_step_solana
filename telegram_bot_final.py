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
        if res.status_code == 200 and 'result' in res.json() and res.json()['result']:
            return res.json()['result']['transaction'][0], "Éxito"
        return None, f"Error RPC o respuesta vacía: {res.text}"
    except Exception as e: logger.error(f"Excepción en get_raw_transaction: {e}"); return None, "Excepción"

def parse_manual_transaction(raw_tx_base64):
    try:
        tx_data = base64.b64decode(raw_tx_base64)
        tx = VersionedTransaction.from_bytes(tx_data)
        msg = tx.message
        account_keys = msg.account_keys
        for ix in msg.instructions:
            program_id_pubkey = account_keys[ix.program_id_index]
            if str(program_id_pubkey) == RAYDIUM_LP_V4_PROGRAM_ID and len(ix.accounts) > 9:
                mint_a_pubkey = account_keys[ix.accounts[8]]; mint_b_pubkey = account_keys[ix.accounts[9]]
                direcciones_conocidas = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                if str(mint_a_pubkey) not in direcciones_conocidas: return str(mint_a_pubkey), "Nuevo token encontrado"
                if str(mint_b_pubkey) not in direcciones_conocidas: return str(mint_b_pubkey), "Nuevo token encontrado"
        return None, "No se encontró instrucción de creación de pool"
    except Exception as e: logger.error(f"Error decodificando la transacción: {e}"); return None, "Error de decodificación"

async def get_dexscreener_data(client, token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return sorted(res.json()['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
        return None
    except Exception as e: logger.error(f"Error consultando DexScreener: {e}"); return None

async def get_rugcheck_data(client, token_address):
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200: return res.json()
        return None
    except Exception as e: logger.error(f"Error consultando RugCheck: {e}"); return None

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
            raw_tx, reason = await get_raw_transaction(client, signature)
            if raw_tx:
                token_address, reason_parse = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [TOKEN REAL ENCONTRADO] {token_address[:10]}... a la incubadora.")
                    new_data = {'found_at': time.time(), 'status': 'incubating'}; incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
                else: logger.info(f"  - フィルター [FILTRADO] {signature[:10]}. Razón: {reason_parse}")
            else: logger.info(f"  - フィルター [FILTRADO] {signature[:10]}. Sin TX. Razón: {reason}")
            signature_queue.task_done(); await asyncio.sleep(2)
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora (Secuencial)...")
    while True:
        try:
            await asyncio.sleep(5) 
            if not incubator: await asyncio.sleep(20); continue
            
            token_address, data = next(iter(incubator.items()))
            logger.info(f"[INCUBADORA] Verificando {token_address[:10]}... ({len(incubator)} pendientes)")
            
            dex_data = await get_dexscreener_data(client, token_address)
            if not dex_data:
                if time.time() - data['found_at'] > 600:
                    logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 10 mins.")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                continue 
            
            liquidity = dex_data.get('liquidity', {}).get('usd', 0)
            if liquidity < 5000:
                logger.info(f"  - ❌ [LIQUIDEZ BAJA] {token_address[:10]} descartado. Liquidez: ${liquidity:,.2f}")
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                continue
            
            logger.info(f"  - ✅ [LIQUIDEZ OK] {token_address[:10]} (${liquidity:,.2f}). Verificando seguridad...")

            rugcheck_data = await get_rugcheck_data(client, token_address)
