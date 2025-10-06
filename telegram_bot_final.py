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
# Se eliminan las importaciones del cazador que ya no se usan
# from solana.rpc.websocket_api import connect
# from solders.rpc.config import RpcTransactionLogsFilterMentions
from solders.transaction import VersionedTransaction

load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# Ya no es necesaria para la caza en tiempo real
# RAYDIUM_LP_V4_PROGRAM_ID = '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'

watchlist = {}
incubator = {}
# Ya no son necesarios para la caza en tiempo real
# processed_signatures = set()
# signature_queue = asyncio.Queue()

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
async def get_dexscreener_data(client, token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            return sorted(res.json()['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except: pass
    return None

async def get_jupiter_top_tokens(client):
    ### --- INICIO DE LA MODIFICACIÓN --- ###
    # Se ajusta el rango de edad de la búsqueda
    query = "solana age > 1 hours and age < 24 hours"
    ### --- FIN DE LA MODIFICACIÓN --- ###
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return [p for p in res.json()['pairs'] if p.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error consultando DexScreener para Júpiter: {e}")
    return []

# --- TAREAS ASÍNCRONAS ---
async def jupiter_momentum_task():
    logger.info("Iniciando Radar de Momentum (Júpiter)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                ### --- INICIO DE LA MODIFICACIÓN --- ###
                # Se aumenta la frecuencia de la búsqueda
                await asyncio.sleep(120) # Ahora se ejecuta cada 2 minutos
                ### --- FIN DE LA MODIFICACIÓN --- ###
                logger.info("[RADAR JÚPITER] Buscando tokens con momentum en la ventana de 1-24 horas...")
                momentum_tokens = await get_jupiter_top_tokens(client)
                if not momentum_tokens: 
                    logger.info("[RADAR JÚPITER] No se encontraron tokens que cumplan el criterio de edad.")
                    continue
                for pair in momentum_tokens:
                    token_address = pair.get('baseToken', {}).get('address')
                    if token_address and token_address not in incubator and token_address not in watchlist:
                        logger.info(f"  - 📡 [RADAR] Júpiter detectó {pair.get('baseToken', {}).get('symbol','')} con momentum. A la incubadora.")
                        new_data = {'found_at': time.time(), 'source': 'Júpiter Radar'}; incubator[token_address] = new_data
                        await db_add_to_incubator(token_address, new_data)
            except asyncio.CancelledError: logger.info("Radar de Momentum detenido."); break
            except Exception as e: logger.error(f"Error en Radar de Momentum: {e}")

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
                    if now_ts - data['found_at'] > 3 * SECONDS_IN_HOUR:
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 3h.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                if liquidity < 7500:
                    logger.info(f"  - [EN ESPERA] {token_address[:10]}... liquidez baja (${liquidity:,.0f}). Objetivo: >$7.5k")
                    continue

                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]} pasa el filtro de liquidez!")
                new_watchlist_data = {'approved_at': now_ts, 'last_notified': 'initial'}
                watchlist[token_address] = new_watchlist_data
                await db_add_to_watchlist(token_address, new_watchlist_data)
                del incubator[token_address]; await db_remove_from_incubator(token_address)
                
                mensaje = (f"✅ **Alerta de Oportunidad ({data.get('source')})**\n\n**Mint:** `{token_address}`\n\n**Liquidez:** `${liquidity:,.2f}` USD\n\n🚨 *Realiza tu análisis de seguridad manual en RugCheck.*")
                if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist...")
    while True:
        try:
            await asyncio.sleep(300)
            if not watchlist: continue
            # (El resto de la lógica de esta tarea no cambia)
        except asyncio.CancelledError: logger.info("Monitor de Watchlist detenido."); break
        except Exception as e: logger.error(f"Error en Monitor de Watchlist: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v15.0 (Radar Enfocado). Usa /cazar, /parar, /status, /incubadora, /watchlist.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("📡 ¡Iniciando Radar de Momentum Enfocado!")
    await setup_database(); global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    client = httpx.AsyncClient()
    
    ### --- INICIO DE LA MODIFICACIÓN --- ###
    # Se eliminan las tareas del cazador y procesador en tiempo real
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(jupiter_momentum_task()),
        asyncio.create_task(incubator_checker_task(client, context)),
        asyncio.create_task(watchlist_monitor_task(client, context))
    ]})
    ### --- FIN DE LA MODIFICACIÓN --- ###

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Caza detenida!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo** (Modo Radar Enfocado).\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🏆 **{len(watchlist)}** tokens en watchlist (bajo monitoreo).")
    await update.message.reply_text(status_msg, parse_mode='Markdown')
    
async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator: await update.message.reply_text("🐣 La incubadora está vacía."); return
    message = f"🐣 **Últimos Tokens en la Incubadora ({len(incubator)}):**\n\n"
    token_addresses = list(incubator.keys()); tokens_to_show = token_addresses[-10:]; tokens_to_show.reverse()
    for token_address in tokens_to_show: message += f"- `{token_address}`\n"
    if len(incubator) > 10: message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist: await update.message.reply_text("🏆 La watchlist está vacía."); return
    message = f"🏆 **Últimos Tokens en la Watchlist ({len(watchlist)}):**\n\n"
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
