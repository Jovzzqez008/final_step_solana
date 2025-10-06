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
async def db_remove_from_watchlist(token_address): # <-- Función añadida para la watchlist inteligente
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

async def get_jupiter_momentum_tokens(client):
    query = "solana age > 12 hours and age < 24 hours"
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return [p for p in res.json()['pairs'] if p.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error consultando DexScreener para Júpiter: {e}")
    return []

# --- TAREAS ASÍNCRONAS ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador de Raydium..."); RAYDIUM_PUBKEY = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
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
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(15)

async def jupiter_momentum_task():
    logger.info("Iniciando Radar de Momentum (Júpiter)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(300) 
                logger.info("[RADAR JÚPITER] Buscando en la 'Zona Dorada' (12-24 horas)...")
                momentum_tokens = await get_jupiter_momentum_tokens(client)
                if not momentum_tokens: 
                    logger.info("[RADAR JÚPITER] No se encontraron tokens en la ventana de 12-24 horas.")
                    continue
                for pair in momentum_tokens:
                    token_address = pair.get('baseToken', {}).get('address')
                    if token_address and token_address not in incubator and token_address not in watchlist:
                        logger.info(f"  - 📡 [RADAR] Júpiter detectó {pair.get('baseToken', {}).get('symbol','')} con momentum. A la incubadora.")
                        new_data = {'found_at': time.time(), 'source': 'Júpiter Radar'}; incubator[token_address] = new_data
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
            now_ts = time.time()
            for token_address, data in list(incubator.items()):
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if now_ts - data['found_at'] > 3600:
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 1h.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                if liquidity < 7500:
                    logger.info(f"  - [EN ESPERA] {token_address[:10]}... liquidez baja (${liquidity:,.0f}). Objetivo: >$7.5k")
                    continue

                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]} pasa el filtro de liquidez!")
                new_watchlist_data = {'approved_at': now_ts, 'status': 'approved', 'last_notified': 'initial'}
                watchlist[token_address] = new_watchlist_data
                await db_add_to_watchlist(token_address, new_watchlist_data)
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                
                mensaje = (f"✅ **Alerta de Oportunidad ({data.get('source')})**\n\n**Mint:** `{token_address}`\n\n**Liquidez:** `${liquidity:,.2f}` USD\n\n🚨 *Realiza tu análisis de seguridad manual en RugCheck.*")
                if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

### --- INICIO DE LA MODIFICACIÓN --- ###
async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist con Filtro Progresivo...")
    while True:
        try:
            await asyncio.sleep(300) # Revisa cada 5 minutos
            if not watchlist: continue

            logger.info(f"[WATCHLIST] Monitoreando {len(watchlist)} tokens aprobados...")
            now = time.time()
            SECONDS_IN_HOUR = 3600
            
            for token_address, data in list(watchlist.items()):
                approved_at = data.get('approved_at', 0)
                age_hours = (now - approved_at) / SECONDS_IN_HOUR
                status = data.get('status', 'approved')
                last_notified = data.get('last_notified', 'initial')

                # REGLA 1: Chequeo de 4 horas (informativo)
                if status == 'approved' and age_hours > 4:
                    logger.info(f"  - [CHECK 4H] Verificando crecimiento de {token_address[:10]}...")
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        if holders >= 50:
                            mensaje = f"📈 **Progreso (4h)**\n\n**Mint:** `{token_address}`\nAlcanzó los **{holders}** holders."
                        else:
                            mensaje = f"📉 **Crecimiento Lento (4h)**\n\n**Mint:** `{token_address}`\nTiene **{holders}** holders (objetivo: 50)."
                        if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                        
                        watchlist[token_address]['status'] = 'checked_4h'
                        await db_add_to_watchlist(token_address, watchlist[token_address])

                # REGLA 2: Chequeo de 8 horas (informativo)
                elif status == 'checked_4h' and age_hours > 8:
                    logger.info(f"  - [CHECK 8H] Verificando crecimiento de {token_address[:10]}...")
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        if holders >= 70:
                             mensaje = f"📈 **Progreso (8h)**\n\n**Mint:** `{token_address}`\nAlcanzó los **{holders}** holders."
                        else:
                            mensaje = f"📉 **Crecimiento Lento (8h)**\n\n**Mint:** `{token_address}`\nTiene **{holders}** holders (objetivo: 70)."
                        if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                        
                        watchlist[token_address]['status'] = 'checked_8h'
                        await db_add_to_watchlist(token_address, watchlist[token_address])

                # REGLA 3: El Gran Filtro de 24 horas (Decisivo)
                elif status == 'checked_8h' and age_hours > 24 and last_notified == 'initial':
                    logger.info(f"  - [GRAN FILTRO 24H] Verificando {token_address[:10]}...")
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        if holders < 111:
                            logger.info(f"  - 🗑️ [DESCARTADO 24H] {token_address[:10]} no alcanzó 111 holders (tiene {holders}).")
                            mensaje = f"🗑️ **Token Descartado (24h)**\n\n**Mint:** `{token_address}`\nNo alcanzó los 111 holders."
                            if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                            del watchlist[token_address]; await db_remove_from_watchlist(token_address)
                            continue
                        else:
                            logger.info(f"  - 🏆 [SUPERVIVIENTE 24H] {token_address[:10]} superó el filtro con {holders} holders.")
                            liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                            price_change_24h = dex_data.get('priceChange', {}).get('h24', 0)
                            mensaje = (f"🔔 **Reporte de Estado (24h)**\n\n**Mint:** `{token_address}`\n**Liquidez:** `${liquidity:,.2f}`\n**Holders:** {holders}\n**Precio 24h:** {price_change_24h}%")
                            if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                            watchlist[token_address]['last_notified'] = '24hr'
                            await db_add_to_watchlist(token_address, watchlist[token_address])

                # REGLA 4: Reportes de 72 y 96 horas
                elif age_hours > 72 and last_notified == '24hr':
                    # Aquí iría la lógica completa para el reporte de 72h
                    watchlist[token_address]['last_notified'] = '72hr'
                    await db_add_to_watchlist(token_address, watchlist[token_address])
                elif age_hours > 96 and last_notified == '72hr':
                    # Aquí iría la lógica completa para el reporte de 96h
                    watchlist[token_address]['last_notified'] = '96hr'
                    await db_add_to_watchlist(token_address, watchlist[token_address])

        except asyncio.CancelledError: logger.info("Monitor de Watchlist detenido."); break
        except Exception as e: logger.error(f"Error en Monitor de Watchlist: {e}")
### --- FIN DE LA MODIFICACIÓN --- ###


# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v14.0 (Híbrido Final). Usa /cazar, /parar, /status, /incubadora, /watchlist.")
# ... (El resto de comandos y la función main no cambian)
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando Doble Motor con Watchlist Inteligente!")
    await setup_database(); global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(raydium_hunter_task()),
        asyncio.create_task(jupiter_momentum_task()),
        asyncio.create_task(processor_task(client)),
        asyncio.create_task(incubator_checker_task(client, context)),
        asyncio.create_task(watchlist_monitor_task(client, context))
    ]})
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear(); await update.message.reply_text("🛑 ¡Caza detenida!")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo** (Doble Motor).\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🏆 **{len(watchlist)}** tokens en watchlist.\n"
                      f"⌛ **{signature_queue.qsize()}** transacciones en cola.")
    await update.message.reply_text(status_msg, parse_mode='Markdown')
async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator: await update.message.reply_text("🐣 La incubadora está vacía."); return
    message = f"🐣 **Últimos Tokens en Incubadora ({len(incubator)}):**\n\n"
    token_addresses = list(incubator.keys()); tokens_to_show = token_addresses[-10:]; tokens_to_show.reverse()
    for token_address in tokens_to_show: message += f"- `{token_address}`\n"
    if len(incubator) > 10: message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')
async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist: await update.message.reply_text("🏆 La watchlist está vacía."); return
    message = f"🏆 **Últimos Tokens en Watchlist ({len(watchlist)}):**\n\n"
    token_addresses = list(watchlist.keys()); tokens_to_show = token_addresses[-15:]; tokens_to_show.reverse()
    for token_address in tokens_to_show: message += f"- `{token_address}`\n"
    if len(watchlist) > 15: message += f"\n... y {len(watchlist) - 15} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("incubadora", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    logger.info("--- El bot está escuchando a Telegram ---"); application.run_polling()
if __name__ == '__main__':
    main()
