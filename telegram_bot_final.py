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

load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

watchlist = {}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
# (Sin cambios)
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")
async def db_add_to_watchlist(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data)); await conn.close()
async def db_load_all_watchlist():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM watchlist"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}
async def db_remove_from_watchlist(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("DELETE FROM watchlist WHERE token_address = $1", token_address); await conn.close()

# --- FUNCIONES DE ANÁLISIS ---
# (Sin cambios)
async def search_dexscreener(client):
    url = "https://api.dexscreener.com/latest/dex/search?q=SOL"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return res.json()['pairs']
    except Exception as e: logger.error(f"Error buscando en DexScreener: {e}")
    return []
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
async def screener_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Explorador de Tokens (Búsqueda Inteligente)...")
    while True:
        try:
            await asyncio.sleep(120)
            logger.info("[EXPLORADOR] Buscando tokens que cumplan los criterios...")
            
            candidate_pairs = await search_dexscreener(client)
            if not candidate_pairs:
                logger.info("[EXPLORADOR] La búsqueda no devolvió candidatos en este ciclo.")
                continue

            ### --- INICIO DE LA MODIFICACIÓN --- ###
            # Filtramos para quedarnos solo con pares de la red Solana
            solana_pairs = [p for p in candidate_pairs if p.get('chainId') == 'solana']
            logger.info(f"[EXPLORADOR] Se encontraron {len(candidate_pairs)} pares en total, {len(solana_pairs)} son de Solana.")
            ### --- FIN DE LA MODIFICACIÓN --- ###

            now_ts = time.time()
            SECONDS_IN_HOUR = 3600
            
            found_count = 0
            for pair in solana_pairs: # Iteramos sobre la lista ya filtrada
                token_address = pair.get('baseToken', {}).get('address')
                if not token_address or token_address in watchlist:
                    continue

                creation_ts = pair.get('pairCreatedAt', 0) / 1000
                age_hours = (now_ts - creation_ts) / SECONDS_IN_HOUR
                liquidity = pair.get('liquidity', {}).get('usd', 0)
                holders = pair.get('txns', {}).get('h24', {}).get('buys', 0)

                if (1 <= age_hours <= 24) and (liquidity >= 8000) and (holders >= 3):
                    found_count += 1
                    logger.info(f"  - ✅ [APROBADO POR EXPLORADOR] ¡{token_address[:10]} pasa todos los filtros!")
                    new_watchlist_data = {'approved_at': time.time(), 'status': 'approved', 'last_notified': 'initial'}
                    watchlist[token_address] = new_watchlist_data
                    await db_add_to_watchlist(token_address, new_watchlist_data)
                    mensaje = (f"✅ **Token Aprobado por Screener**\n\n**Mint:** `{token_address}`\n...")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
            
            if found_count == 0:
                logger.info(f"[EXPLORADOR] Se analizaron {len(solana_pairs)} pares de Solana, pero ninguno cumplió los criterios.")

        except asyncio.CancelledError: logger.info("Explorador de Tokens detenido."); break
        except Exception as e: logger.error(f"Error en Explorador de Tokens: {e}")

# ... (El resto del script, watchlist_monitor_task, comandos y main no cambia) ...
async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist con Filtro Progresivo...")
    while True:
        try:
            await asyncio.sleep(300);
            if not watchlist: continue
            logger.info(f"[WATCHLIST] Monitoreando {len(watchlist)} tokens...")
            now = time.time(); SECONDS_IN_HOUR = 3600
            for token_address, data in list(watchlist.items()):
                approved_at = data.get('approved_at', 0); age_hours = (now - approved_at) / SECONDS_IN_HOUR
                status = data.get('status', 'approved'); last_notified = data.get('last_notified', 'initial')
                if status == 'approved' and age_hours > 4:
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        mensaje = f"📈 **Progreso (4h)**\n\n**Mint:** `{token_address}`\nAlcanzó **{holders}** holders." if holders >= 50 else f"📉 **Crecimiento Lento (4h)**\n\n**Mint:** `{token_address}`\nTiene **{holders}** holders."
                        if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                        watchlist[token_address]['status'] = 'checked_4h'
                        await db_add_to_watchlist(token_address, watchlist[token_address])
                elif status == 'checked_4h' and age_hours > 8:
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        mensaje = f"📈 **Progreso (8h)**\n\n**Mint:** `{token_address}`\nAlcanzó **{holders}** holders." if holders >= 70 else f"📉 **Crecimiento Lento (8h)**\n\n**Mint:** `{token_address}`\nTiene **{holders}** holders."
                        if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                        watchlist[token_address]['status'] = 'checked_8h'
                        await db_add_to_watchlist(token_address, watchlist[token_address])
                elif status == 'checked_8h' and age_hours > 24 and last_notified == 'initial':
                    dex_data = await get_dexscreener_data(client, token_address)
                    if dex_data:
                        holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)
                        if holders < 111:
                            mensaje = f"🗑️ **Token Descartado (24h)**\n\n**Mint:** `{token_address}`\nNo alcanzó 111 holders."
                            if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                            del watchlist[token_address]; await db_remove_from_watchlist(token_address)
                            continue
                        else:
                            liquidity = dex_data.get('liquidity', {}).get('usd', 0); price_change_24h = dex_data.get('priceChange', {}).get('h24', 0)
                            mensaje = (f"🔔 **Reporte de Estado (24h)**\n\n**Mint:** `{token_address}`\n**Liquidez:** `${liquidity:,.2f}`\n**Holders:** {holders}\n**Precio 24h:** {price_change_24h}%")
                            if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                            watchlist[token_address]['last_notified'] = '24hr'
                            await db_add_to_watchlist(token_address, watchlist[token_address])
        except asyncio.CancelledError: logger.info("Monitor de Watchlist detenido."); break
        except Exception as e: logger.error(f"Error en Monitor de Watchlist: {e}")
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v12.6 (Filtro Solana). Usa /cazar, /parar, /status.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está explorando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🔭 ¡Iniciando Búsqueda Inteligente en Solana!")
    await setup_database(); global watchlist; watchlist = await db_load_all_watchlist()
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(screener_task(client, context)),
        asyncio.create_task(watchlist_monitor_task(client, context))
    ]})
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está explorando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    global TARGET_CHAT_ID; TARGET_CHAT_ID = None
    await update.message.reply_text("🛑 ¡Exploración detenida!")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = f"✅ El bot está **Activo** (Modo Explorador).\n🕵️‍♂️ **{len(watchlist)}** tokens en watchlist."
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
