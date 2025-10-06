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

notified_tokens = set()
latest_pairs_found = 0
current_mode = "estricto"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS (CORREGIDA) 💾 ---
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS notified_tokens (token_address TEXT PRIMARY KEY, notified_at TIMESTAMPTZ NOT NULL);''')
        # --- ### LÍNEA AÑADIDA PARA CREAR LA TABLA DE CONFIGURACIÓN ### ---
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);''')
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")

async def db_add_to_notified(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO notified_tokens (token_address, notified_at) VALUES ($1, NOW()) ON CONFLICT (token_address) DO NOTHING", token_address); await conn.close()

async def db_load_notified():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address FROM notified_tokens"); await conn.close()
    return {row['token_address'] for row in rows}

async def db_save_mode(mode):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO settings (key, value) VALUES ('search_mode', $1) ON CONFLICT (key) DO UPDATE SET value = $1", mode); await conn.close()

async def db_load_mode():
    conn = await asyncpg.connect(DATABASE_URL); row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'search_mode'"); await conn.close()
    return row['value'] if row else "estricto"


# --- FUNCIONES DE ANÁLISIS ---
async def search_for_opportunities(client, mode):
    if mode == "estricto":
        query = "solana liquidity > 5000 and liquidity < 25000 and age > 10 minutes and age < 6 hours and txns.h1 > 15"
    else: # Modo flexible
        query = "solana liquidity > 1000 and age < 12 hours"
        
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=45)
        if res.status_code == 200 and res.json().get('pairs'):
            return [pair for pair in res.json()['pairs'] if pair.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error buscando oportunidades en DexScreener: {e}")
    return []

async def get_rugcheck_data(client, token_address):
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
    try:
        res = await client.get(url, timeout=15)
        if res.status_code == 200: return res.json()
    except: pass
    return None

# --- TAREA ASÍNCRONA PRINCIPAL ---
async def opportunity_radar_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Radar de Oportunidades...")
    global latest_pairs_found
    while True:
        try:
            await asyncio.sleep(120)
            logger.info(f"[RADAR] Buscando tokens en modo '{current_mode.upper()}'...")
            
            pairs = await search_for_opportunities(client, current_mode)
            latest_pairs_found = len(pairs)
            if not pairs:
                logger.info("[RADAR] No se encontraron tokens que cumplan los criterios.")
                continue

            logger.info(f"[RADAR] Encontrados {len(pairs)} candidatos. Analizando uno por uno...")
            for pair in pairs:
                token_address = pair.get('baseToken', {}).get('address')
                token_symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                
                if not token_address or token_address in notified_tokens:
                    continue
                
                logger.info(f"  -> Verificando seguridad de {token_symbol}...")
                rugcheck_data = await get_rugcheck_data(client, token_address)
                if not rugcheck_data: continue

                mint_renounced = not any(risk['name'] == 'Mint Authority' for risk in rugcheck_data.get('risks', []))
                
                top_holder_pct = rugcheck_data.get('topHolders', [{}])[0].get('pct', 1) * 100
                holder_limit = 40 if current_mode == "estricto" else 90
                
                if mint_renounced and top_holder_pct < holder_limit:
                    logger.info(f"  - ✅ [APROBADO] ¡{token_symbol} ({token_address[:6]}...) pasa los filtros!")
                    
                    liquidity = pair.get('liquidity', {}).get('usd', 0)
                    age_minutes = pair.get('pairAge', {}).get('m', 0)
                    
                    notified_tokens.add(token_address)
                    await db_add_to_notified(token_address)
                    
                    mensaje = (f"✅ **Alerta de Oportunidad ({current_mode.capitalize()})**\n\n"
                               f"**Símbolo:** ${token_symbol}$\n"
                               f"**Mint:** `{token_address}`\n\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                               f"**Edad:** {age_minutes} minutos\n"
                               f"**Top Holder:** {top_holder_pct:.2f}%\n\n"
                               f"🚨 *Investiga a fondo antes de invertir.*")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                
                await asyncio.sleep(2)

        except asyncio.CancelledError: logger.info("Radar de Oportunidades detenido."); break
        except Exception as e: logger.error(f"Error en Radar de Oportunidades: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v14.1 (DB Corregida). Usa /cazar, /parar, /status, /modo_estricto, /modo_flexible.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('task'): await update.message.reply_text("🤔 El bot ya está buscando."); return
    
    global current_mode, notified_tokens
    await setup_database()
    current_mode = await db_load_mode()
    notified_tokens = await db_load_notified()
    
    await update.message.reply_text(f"📡 ¡Iniciando Radar en modo **{current_mode.upper()}**!")
    
    client = httpx.AsyncClient()
    task = asyncio.create_task(opportunity_radar_task(client, context))
    context.bot_data.update({'client': client, 'task': task})

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('task'): await update.message.reply_text("🤔 El bot no está buscando."); return
    context.bot_data['task'].cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Búsqueda detenida!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('task'):
        status_msg = (f"✅ El bot está **Activo** (Modo: **{current_mode.upper()}**).\n\n"
                      f"🔎 Pares encontrados en la última búsqueda: **{latest_pairs_found}**\n"
                      f"🏆 Tokens aprobados en el historial: **{len(notified_tokens)}**")
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def set_strict_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_mode
    current_mode = "estricto"
    await db_save_mode(current_mode)
    await update.message.reply_text("✅ Modo de búsqueda cambiado a **ESTRICTO**.\nEl cambio se aplicará en el próximo ciclo de búsqueda.")

async def set_flexible_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_mode
    current_mode = "flexible"
    await db_save_mode(current_mode)
    await update.message.reply_text("✅ Modo de búsqueda cambiado a **FLEXIBLE**.\nEl cambio se aplicará en el próximo ciclo de búsqueda.")

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("modo_estricto", set_strict_mode))
    application.add_handler(CommandHandler("modo_flexible", set_flexible_mode))
    
    logger.info("--- El bot está escuchando a Telegram ---"); application.run_polling()

if __name__ == '__main__':
    main()
