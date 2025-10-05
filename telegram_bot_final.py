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
incubator = {}
latest_pairs_found = 0 # Para visibilidad en /status

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
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data)); await conn.close()
async def db_load_all_watchlist():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address, data FROM watchlist"); await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}

# --- FUNCIONES DE ANÁLISIS ---
async def get_new_pairs_from_dexscreener(client):
    """Obtiene los pares más nuevos de Solana en Raydium."""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana/raydium"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200 and res.json().get('pairs'):
            return res.json()['pairs']
    except Exception as e: logger.error(f"Error consultando nuevos pares: {e}")
    return []

# --- TAREAS ASÍNCRONAS ---
async def screener_explorer_task(client):
    logger.info("Iniciando Explorador de Oportunidades...")
    global latest_pairs_found
    while True:
        try:
            await asyncio.sleep(120) # Busca cada 2 minutos
            logger.info("[EXPLORADOR] Buscando nuevos pares en Raydium...")
            
            new_pairs = await get_new_pairs_from_dexscreener(client)
            latest_pairs_found = len(new_pairs)
            if not new_pairs: continue

            logger.info(f"[EXPLORADOR] Encontrados {len(new_pairs)} pares. Pre-filtrando para incubadora...")
            for pair in new_pairs:
                token_address = pair.get('baseToken', {}).get('address')
                token_symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                
                if not token_address or token_address in incubator or token_address in watchlist:
                    continue

                liquidity = pair.get('liquidity', {}).get('usd', 0)
                if liquidity >= 100: # Pre-filtro súper bajo
                    logger.info(f"  - 🌱 [NUEVO CANDIDATO] {token_symbol} ({token_address[:6]}...) entra a la incubadora.")
                    new_data = {'found_at': time.time(), 'symbol': token_symbol}
                    incubator[token_address] = new_data
                    await db_add_to_incubator(token_address, new_data)
        
        except asyncio.CancelledError: logger.info("Explorador detenido."); break
        except Exception as e: logger.error(f"Error en Explorador: {e}")

async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(60) # Revisa la incubadora cada minuto
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            for token_address, data in list(incubator.items()):
                
                pair_data = await get_new_pairs_from_dexscreener(client) # Re-usamos la función para obtener datos frescos
                if not pair_data: continue
                
                # Buscamos nuestro token en la lista de pares recientes
                dex_data = next((p for p in pair_data if p.get('baseToken', {}).get('address') == token_address), None)

                if not dex_data:
                    if time.time() - data['found_at'] > 3600: # Si tras 1 hora no aparece, lo descartamos
                        logger.info(f"  - 🗑️ [DESCARTADO] {data.get('symbol')} ({token_address[:6]}...) no encontrado tras 1h.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)

                # --- FILTRO ESTRICTO DE LIQUIDEZ ---
                if liquidity >= 7500:
                    logger.info(f"  - ✅ [APROBADO] ¡{data.get('symbol')} ({token_address[:6]}...) superó el filtro de liquidez!")
                    
                    new_watchlist_data = {'approved_at': time.time()}
                    watchlist[token_address] = new_watchlist_data
                    await db_add_to_watchlist(token_address, new_watchlist_data)
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    
                    mensaje = (f"✅ **Alerta de Liquidez**\n\n"
                               f"**Símbolo:** ${data.get('symbol')}$\n"
                               f"**Mint:** `{token_address}`\n\n"
                               f"**Liquidez Detectada:** `${liquidity:,.2f}` USD\n\n"
                               f"🚨 *Por favor, realiza tu análisis de seguridad manual en RugCheck.*")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                else:
                    logger.info(f"  - [EN ESPERA] {data.get('symbol')} - Liquidez actual: ${liquidity:,.0f} (Objetivo: $7,500)")

        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")


# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("👋 v3.0 (Explorador de Oportunidades). Usa /cazar y /parar.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está explorando."); return
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("🔭 ¡Iniciando Explorador de Oportunidades!")
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
        status_msg = (f"✅ El bot está **Activo** (Modo Oportunidad).\n\n"
                      f"**Última Búsqueda:**\n"
                      f"🔎 Pares Recientes Encontrados: **{latest_pairs_found}**\n\n"
                      f"**Análisis Actual:**\n"
                      f"🐣 Tokens en Incubadora: **{len(incubator)}**\n"
                      f"🏆 Tokens Aprobados: **{len(watchlist)}**")
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
