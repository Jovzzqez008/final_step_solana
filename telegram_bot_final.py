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
incubator = {} # Vuelve la incubadora

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

# --- FUNCIONES DE ANÁLISIS ---
async def get_new_pairs_from_dexscreener(client):
    """Obtiene los pares más nuevos directamente de Solana en Raydium."""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana/raydium"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return res.json()['pairs']
    except Exception as e: logger.error(f"Error consultando nuevos pares: {e}")
    return []

async def get_dexscreener_data(client, token_address):
    """Obtiene datos detallados de un token específico."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            return sorted(res.json()['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except: pass
    return None

# --- TAREAS ASÍNCRONAS ---
async def screener_explorer_task(client):
    logger.info("Iniciando Explorador para Incubadora...")
    while True:
        try:
            await asyncio.sleep(120) # Busca cada 2 minutos
            logger.info("[EXPLORADOR] Buscando nuevos candidatos para la incubadora...")
            
            new_pairs = await get_new_pairs_from_dexscreener(client)
            if not new_pairs: continue

            logger.info(f"[EXPLORADOR] Encontrados {len(new_pairs)} pares recientes. Aplicando pre-filtro...")
            for pair in new_pairs:
                token_address = pair.get('baseToken', {}).get('address')
                token_symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                
                if not token_address or token_address in incubator or token_address in watchlist:
                    continue

                # Pre-filtro suave para entrar a la incubadora
                liquidity = pair.get('liquidity', {}).get('usd', 0)
                if liquidity >= 1000: # Si tiene al menos $1000 de liquidez
                    logger.info(f"  - 🌱 [NUEVO CANDIDATO] {token_symbol} ({token_address[:6]}...) entra a la incubadora con ${liquidity:,.0f} de liquidez.")
                    new_data = {'found_at': time.time(), 'symbol': token_symbol}
                    incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
        
        except asyncio.CancelledError: logger.info("Explorador detenido."); break
        except Exception as e: logger.error(f"Error en Explorador: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(300) # Revisa la incubadora cada 5 minutos
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            now_ts = time.time(); SECONDS_IN_HOUR = 3600
            
            for token_address, data in list(incubator.items()):
                # Regla de descarte: más de 2 horas en la incubadora sin madurar
                if now_ts - data['found_at'] > (2 * SECONDS_IN_HOUR):
                    logger.info(f"  - 🗑️ [DESCARTADO] {data.get('symbol')} ({token_address[:6]}...) no maduró en 2h.")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue

                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data: continue
                
                creation_ts = dex_data.get('pairCreatedAt', 0) / 1000
                age_hours = (now_ts - creation_ts) / SECONDS_IN_HOUR
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                holders = dex_data.get('txns', {}).get('h24', {}).get('buys', 0)

                # --- TUS CRITERIOS ESTRICTOS PARA SER PROMOVIDO ---
                if (age_hours >= 1) and (liquidity >= 5000) and (holders >= 5):
                    logger.info(f"  - ✅ [APROBADO] ¡{data.get('symbol')} ({token_address[:6]}...) pasa los filtros!")
                    
                    new_watchlist_data = {'approved_at': now_ts, 'status': 'monitoring'}
                    watchlist[token_address] = new_watchlist_data
                    await db_add_to_watchlist(token_address, new_watchlist_data)
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    
                    mensaje = (f"✅ **Token Aprobado por Incubadora**\n\n"
                               f"**Símbolo:** ${data.get('symbol')}$\n"
                               f"**Mint:** `{token_address}`\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                               f"**Edad:** {age_hours:.1f} horas\n"
                               f"**Holders (aprox):** {holders}\n\n"
                               f"🚨 *Recuerda verificar la seguridad manualmente en RugCheck.*")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                else:
                    logger.info(f"  - [MADURANDO] {data.get('symbol')} - Edad: {age_hours:.1f}h, Liq: ${liquidity:,.0f}, Holders: {holders}")

        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v1.0 (Incubadora Inteligente). Usa /cazar, /parar, /status.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está explorando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🔭 ¡Iniciando el explorador con Incubadora!")
    await setup_database(); global watchlist, incubator; 
    watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(screener_explorer_task(client)),
        asyncio.create_task(incubator_checker_task(client, context))
    ]})
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está explorando."); return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear(); global TARGET_CHAT_ID; TARGET_CHAT_ID = None
    await update.message.reply_text("🛑 ¡Exploración detenida!")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo** (Modo Explorador).\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🕵️‍♂️ **{len(watchlist)}** tokens en watchlist.")
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
