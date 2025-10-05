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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GOPLUS_API_KEY = os.getenv("GOPLUS_API_KEY")
TARGET_CHAT_ID = None

RAYDIUM_LP_V4_PROGRAM_ID = '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'

watchlist = {}
incubator = {}
processed_signatures = set()
signature_queue = asyncio.Queue()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
# (Sin cambios)
# ... (El código de las funciones de DB permanece igual)
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
# (Función de GoPlus mejorada)
async def get_goplus_security_data(client, token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={token_address}"
    headers = {"Authorization": f"Bearer {GOPLUS_API_KEY}"}
    try:
        res = await client.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return {'is_safe': False, 'reason': "Fallo en API de GoPlus"}
        
        data = res.json().get('result', {}).get(token_address.lower())
        if not data:
            return {'is_safe': None, 'reason': "Reporte no disponible aún"} # Reporte no listo

        is_honeypot = data.get('is_honeypot') == '1'
        if is_honeypot:
            return {'is_safe': False, 'reason': "Detectado como Honeypot"}

        locked_lp_pct = sum(float(lp.get('percent', 0)) for lp in data.get('lp_holders', []) if lp.get('is_locked') == 1) * 100
        
        return {'is_safe': True, 'locked_lp_pct': locked_lp_pct}
        
    except Exception as e:
        logger.error(f"Excepción en GoPlus: {e}")
        return {'is_safe': False, 'reason': "Excepción en GoPlus"}

# (El resto de funciones de análisis no cambian)
async def get_raw_transaction(client, signature):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [signature, {"encoding": "base64", "maxSupportedTransactionVersion": 0}]}
    try:
        res = await client.post(HELIUS_RPC_URL, json=payload, timeout=20)
        if res.status_code == 200 and 'result' in res.json() and res.json()['result']:
            return res.json()['result']['transaction'][0], "Éxito"
        return None, f"Error RPC: {res.text}"
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
                mint_a = str(account_keys[ix.accounts[8]]); mint_b = str(account_keys[ix.accounts[9]])
                known_mints = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                if mint_a not in known_mints: return mint_a
                if mint_b not in known_mints: return mint_b
        return None
    except Exception as e: logger.error(f"Error decodificando la transacción: {e}"); return None

async def get_dexscreener_data(client, token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if not pairs: return None
            return sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
        return None
    except Exception as e: logger.error(f"Error consultando DexScreener: {e}"); return None

# --- TAREAS ASÍNCRONAS ---
async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(30)
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            for token_address, data in list(incubator.items()):
                # 1. Chequeo de Seguridad con GoPlus (Honeypot y Liquidez Bloqueada)
                security_data = await get_goplus_security_data(client, token_address)
                
                if security_data.get('is_safe') is None: # Reporte no listo
                    logger.info(f"  - [GOPLUS ESPERANDO] {token_address[:10]}...")
                    continue
                
                if security_data.get('is_safe') is False:
                    logger.info(f"  - ❌ [RECHAZADO SEGURIDAD] {token_address[:10]} descartado. Razón: {security_data.get('reason')}")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                # NUEVO FILTRO DE LIQUIDEZ BLOQUEADA
                locked_lp = security_data.get('locked_lp_pct', 0)
                if locked_lp < 90:
                    logger.info(f"  - ❌ [LIQUIDEZ NO BLOQUEADA] {token_address[:10]} descartado. LP Bloqueado: {locked_lp:.2f}% (Req: >90%)")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue

                # 2. Chequeo de Mercado con DexScreener
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if time.time() - data['found_at'] > 600: # 10 minutos
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 10 mins.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0) # Proxy de holders
                
                if liquidity < 5000 or holders < 1:
                    logger.info(f"  - [MERCADO ESPERANDO] {token_address[:10]} - Liq: ${liquidity:,.0f}, Holders: {holders} (Req: >=1)")
                    continue

                # 3. Aprobación
                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]} pasa todos los filtros!")
                new_watchlist_data = {'approved_at': time.time(), 'last_notified': 'initial'}
                watchlist[token_address] = new_watchlist_data; await db_add_to_watchlist(token_address, new_watchlist_data)
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                
                mensaje = (f"✅ **Token Aprobado**\n\n**Mint:** `{token_address}`\n**LP Bloqueado:** {locked_lp:.2f}%\n**Liquidez:** `${liquidity:,.2f}` USD\n**Holders (aprox):** {holders}")
                if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError: logger.info("Vigilante de la Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

# ... (El resto del script, cazador, watchlist_monitor, comandos, etc. no cambia)
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
                token_address = parse_manual_transaction(raw_tx)
                if token_address and (token_address not in incubator and token_address not in watchlist):
                    logger.info(f"  - ✅ [TOKEN ENCONTRADO] {token_address[:10]}... a la incubadora.")
                    new_data = {'found_at': time.time(), 'status': 'incubating'}; incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
            signature_queue.task_done(); await asyncio.sleep(1)
        except asyncio.CancelledError: logger.info("Procesador Manual detenido."); break
        except Exception as e: logger.error(f"Error en Procesador Manual: {e}")

async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist...")
    while True:
        try:
            await asyncio.sleep(300)
            if not watchlist: continue
            logger.info(f"[WATCHLIST] Monitoreando {len(watchlist)} tokens aprobados...")
            now = time.time()
            SECONDS_IN_HOUR = 3600
            for token_address, data in list(watchlist.items()):
                approved_at = data.get('approved_at', 0)
                last_notified = data.get('last_notified', 'initial')
                age_hours = (now - approved_at) / SECONDS_IN_HOUR
                notify_periods = {'initial': 24, '24hr': 72, '72hr': 96}
                if last_notified in notify_periods and age_hours >= notify_periods[last_notified]:
                    logger.info(f"  - 🔔 Enviando reporte de {notify_periods[last_notified]}h para {token_address[:10]}...")
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change_24h = dex_data.get('priceChange', {}).get('h24', 0)
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0) 
                        mensaje = (f"🔔 **Reporte de Estado ({notify_periods[last_notified]}h)**\n\n"
                                   f"**Mint:** `{token_address}`\n"
                                   f"**Liquidez Actual:** `${liquidity:,.2f}` USD\n"
                                   f"**Holders (aprox 24h):** {holders}\n"
                                   f"**Cambio de Precio (24h):** `{price_change_24h}`%")
                        if TARGET_CHAT_ID:
                           await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                        new_status = f"{notify_periods[last_notified]}hr"
                        watchlist[token_address]['last_notified'] = new_status
                        await db_add_to_watchlist(token_address, watchlist[token_address])
        except asyncio.CancelledError: logger.info("Monitor de Watchlist detenido."); break
        except Exception as e: logger.error(f"Error en Monitor de Watchlist: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v9.3. Usa /cazar, /parar, /status.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🏹 ¡Iniciando caza con filtro de LP >90%!")
    await setup_database(); global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
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
                      f"🕵️‍♂️ **{len(watchlist)}** en watchlist (bajo monitoreo).\n"
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
