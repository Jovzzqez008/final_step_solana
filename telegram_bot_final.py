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
async def get_new_pairs_from_dexscreener(client):
    url = "https://api.dexscreener.com/latest/dex/pairs/solana/raydium"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return res.json()['pairs']
    except Exception as e:
        logger.error(f"Error consultando nuevos pares en DexScreener: {e}")
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
    logger.info("Iniciando Explorador de Tokens (Screener)...")
    while True:
        try:
            await asyncio.sleep(300) # Se ejecuta cada 5 minutos
            logger.info("[EXPLORADOR] Buscando tokens que cumplan los criterios...")
            
            new_pairs = await get_new_pairs_from_dexscreener(client)
            if not new_pairs:
                logger.info("[EXPLORADOR] No se encontraron nuevos pares en este ciclo.")
                continue

            now_ts = time.time()
            SECONDS_IN_HOUR = 3600
            
            for pair in new_pairs:
                token_address = pair.get('baseToken', {}).get('address')
                if not token_address or token_address in watchlist:
                    continue

                creation_ts = pair.get('pairCreatedAt', 0) / 1000
                age_hours = (now_ts - creation_ts) / SECONDS_IN_HOUR
                liquidity = pair.get('liquidity', {}).get('usd', 0)
                holders = pair.get('txns', {}).get('h24', {}).get('buys', 0)

                ### --- INICIO DE LA MODIFICACIÓN --- ###
                # Criterios ajustados: >2h de edad, >$8k liquidez, >3 holders
                if age_hours > 2 and liquidity >= 8000 and holders >= 3:
                ### --- FIN DE LA MODIFICACIÓN --- ###
                    logger.info(f"  - ✅ [APROBADO POR EXPLORADOR] ¡{token_address[:10]} pasa todos los filtros!")
                    
                    new_watchlist_data = {'approved_at': time.time(), 'status': 'approved', 'last_notified': 'initial'}
                    watchlist[token_address] = new_watchlist_data
                    await db_add_to_watchlist(token_address, new_watchlist_data)
                    
                    mensaje = (f"✅ **Token Aprobado por Screener**\n\n"
                               f"**Mint:** `{token_address}`\n"
                               f"**Edad:** {age_hours:.1f} horas\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                               f"**Holders (aprox):** {holders}\n\n"
                               f"*Recuerda verificar la seguridad manualmente.*")
                    
                    if TARGET_CHAT_ID:
                        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        
        except asyncio.CancelledError:
            logger.info("Explorador de Tokens detenido.")
            break
        except Exception as e:
            logger.error(f"Error en Explorador de Tokens: {e}")

async def watchlist_monitor_task(client, context: ContextTypes.DEFAULT_TYPE):
    # (La lógica de esta tarea no cambia, sigue igual que la v12.1)
    logger.info("Iniciando Monitor de Watchlist con Filtro Progresivo...")
    while True:
        try:
            await asyncio.sleep(300)
            if not watchlist: continue

            logger.info(f"[WATCHLIST] Monitoreando {len(watchlist)} tokens...")
            now = time.time()
            SECONDS_IN_HOUR = 3600
            
            for token_address, data in list(watchlist.items()):
                approved_at = data.get('approved_at', 0)
                age_hours = (now - approved_at) / SECONDS_IN_HOUR
                status = data.get('status', 'approved')
                last_notified = data.get('last_notified', 'initial')

                # REGLA 1: Chequeo de 4 horas
                if status == 'approved' and age_hours > 4:
                    logger.info(f"  - [CHECK 4H] Verificando {token_address[:10]}...")
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

                # REGLA 2: Chequeo de 8 horas
                elif status == 'checked_4h' and age_hours > 8:
                    logger.info(f"  - [CHECK 8H] Verificando {token_address[:10]}...")
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

                # REGLA 3: El Gran Filtro de 24 horas
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
        
        except asyncio.CancelledError: logger.info("Monitor de Watchlist detenido."); break
        except Exception as e: logger.error(f"Error en Monitor de Watchlist: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v12.2 (Screener Rápido). Usa /cazar, /parar, /status.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está explorando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🔭 ¡Iniciando el explorador con filtro de 2 horas!")
    await setup_database(); global watchlist; watchlist = await db_load_all_watchlist()
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(screener_task(client, context)),
        asyncio.create_task(watchlist_monitor_task(client, context))
    ]})
# ... (El resto de comandos y la función main no cambian)
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
