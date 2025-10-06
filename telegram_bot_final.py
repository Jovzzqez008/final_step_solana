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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS notified_tokens (token_address TEXT PRIMARY KEY, notified_at TIMESTAMPTZ NOT NULL);''')
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")

async def db_add_to_notified(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO notified_tokens (token_address, notified_at) VALUES ($1, NOW()) ON CONFLICT (token_address) DO NOTHING", token_address); await conn.close()

async def db_load_notified():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address FROM notified_tokens"); await conn.close()
    return {row['token_address'] for row in rows}

# --- FUNCIONES DE ANÁLISIS ---
async def search_for_opportunities(client):
    """Busca en DexScreener pares con criterios de oportunidad específicos."""
    query = "solana liquidity > 5000 and liquidity < 25000 and age > 10 minutes and age < 6 hours and txns.h1 > 15"
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=45)
        if res.status_code == 200 and res.json().get('pairs'):
            return [pair for pair in res.json()['pairs'] if pair.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error buscando oportunidades en DexScreener: {e}")
    return []

async def get_rugcheck_data(client, token_address):
    """Obtiene un reporte de seguridad de RugCheck."""
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
            await asyncio.sleep(120) # Busca cada 2 minutos
            logger.info("[RADAR] Buscando tokens que cumplan los criterios...")
            
            pairs = await search_for_opportunities(client)
            latest_pairs_found = len(pairs)
            if not pairs:
                logger.info("[RADAR] No se encontraron tokens que cumplan los criterios de búsqueda.")
                continue

            logger.info(f"[RADAR] Encontrados {len(pairs)} candidatos. Analizando seguridad uno por uno...")
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
                
                if mint_renounced and top_holder_pct < 40:
                    logger.info(f"  - ✅ [APROBADO] ¡{token_symbol} ({token_address[:6]}...) pasa todos los filtros!")
                    
                    liquidity = pair.get('liquidity', {}).get('usd', 0)
                    age_minutes = pair.get('pairAge', {}).get('m', 0)
                    
                    notified_tokens.add(token_address)
                    await db_add_to_notified(token_address)
                    
                    mensaje = (f"✅ **Alerta de Oportunidad**\n\n"
                               f"**Símbolo:** ${token_symbol}$\n"
                               f"**Mint:** `{token_address}`\n\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                               f"**Edad:** {age_minutes} minutos\n"
                               f"**Top Holder:** {top_holder_pct:.2f}%\n\n"
                               f"🚨 *Pasa los filtros de mercado y seguridad. Investiga a fondo.*")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                else:
                    logger.info(f"  - ❌ [RECHAZADO] {token_symbol} no pasa el filtro de seguridad (Mint Renunciado: {mint_renounced}, Top Holder: {top_holder_pct:.1f}%).")

                await asyncio.sleep(2)

        except asyncio.CancelledError: logger.info("Radar de Oportunidades detenido."); break
        except Exception as e: logger.error(f"Error en Radar de Oportunidades: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v13.1 (Radar Corregido). Usa /cazar y /parar.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('task'): await update.message.reply_text("🤔 El bot ya está buscando."); return
    await update.message.reply_text("📡 ¡Iniciando Radar de Oportunidades!")
    
    await setup_database()
    global notified_tokens; notified_tokens = await db_load_notified()
    
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
    # --- ### LÍNEA CORREGIDA ### ---
    # Cambiamos 'tasks' (plural) a 'task' (singular) para que coincida con cómo guardamos la tarea
    if context.bot_data.get('task'):
        status_msg = (f"✅ El bot está **Activo** (Modo Radar).\n\n"
                      f"**Última Búsqueda:**\n"
                      f"🔎 Pares Encontrados: **{latest_pairs_found}**\n\n"
                      f"**Resultados:**\n"
                      f"🏆 Tokens Aprobados (historial): **{len(notified_tokens)}**")
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
