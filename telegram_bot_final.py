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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, notified_at TIMESTAMPTZ NOT NULL);''')
        await conn.close()
    except Exception as e: logger.error(f"Error al configurar la base de datos: {e}")

async def db_add_to_notified(token_address):
    conn = await asyncpg.connect(DATABASE_URL); await conn.execute("INSERT INTO watchlist (token_address, notified_at) VALUES ($1, NOW()) ON CONFLICT (token_address) DO NOTHING", token_address); await conn.close()

async def db_load_notified():
    conn = await asyncpg.connect(DATABASE_URL); rows = await conn.fetch("SELECT token_address FROM watchlist"); await conn.close()
    return {row['token_address'] for row in rows}

# --- FUNCIONES DE ANÁLISIS ---
async def search_dexscreener_for_momentum(client):
    """Busca en DexScreener pares de SOLANA con criterios de actividad específicos."""
    # --- ### BÚSQUEDA CORREGIDA Y MÁS ESPECÍFICA ### ---
    query = "SOLANA liquidity > 2000 and liquidity < 15000 and age < 24 hours and txns > 10"
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=45)
        if res.status_code == 200 and res.json().get('pairs'):
            # Filtramos de nuevo para estar 100% seguros de que son de Solana
            return [pair for pair in res.json()['pairs'] if pair.get('chainId') == 'solana']
    except Exception as e: logger.error(f"Error buscando en DexScreener: {e}")
    return []

async def get_rugcheck_data(client, token_address):
    """Obtiene un reporte de seguridad simple de RugCheck."""
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200: return res.json()
    except: pass
    return None

# --- TAREA ASÍNCRONA PRINCIPAL ---
async def momentum_explorer_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Explorador de Momentum...")
    while True:
        try:
            await asyncio.sleep(120) # Busca cada 2 minutos
            logger.info("[EXPLORADOR] Buscando tokens con momentum...")
            
            pairs = await search_dexscreener_for_momentum(client)
            if not pairs:
                logger.info("[EXPLORADOR] No se encontraron tokens que cumplan los criterios de búsqueda.")
                continue

            logger.info(f"[EXPLORADOR] Encontrados {len(pairs)} candidatos de Solana. Analizando uno por uno...")
            
            # --- ### PROCESAMIENTO SECUENCIAL PARA EVITAR RATE LIMIT ### ---
            for pair in pairs:
                token_address = pair.get('baseToken', {}).get('address')
                token_symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                
                if not token_address or token_address in notified_tokens:
                    continue
                
                logger.info(f"  -> Verificando seguridad de {token_symbol}...")
                rugcheck_data = await get_rugcheck_data(client, token_address)
                if not rugcheck_data:
                    logger.info(f"     - No se pudo obtener reporte de RugCheck para {token_symbol}.")
                    continue

                mint_renounced = any(risk['name'] == 'Mutable Metadata' and risk['level'] != 'high' for risk in rugcheck_data.get('risks', []))
                
                if mint_renounced:
                    logger.info(f"  - ✅ [APROBADO] ¡{token_symbol} ({token_address[:6]}...) pasa el filtro de seguridad!")
                    
                    liquidity = pair.get('liquidity', {}).get('usd', 0)
                    age_minutes = pair.get('pairAge', {}).get('m', 0)
                    
                    notified_tokens.add(token_address)
                    await db_add_to_notified(token_address)
                    
                    mensaje = (f"✅ **Alerta de Momentum**\n\n"
                               f"**Símbolo:** ${token_symbol}$\n"
                               f"**Mint:** `{token_address}`\n\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                               f"**Edad:** {age_minutes} minutos\n\n"
                               f"🚨 *Pasa el filtro de seguridad básico (Metadata no mutable). Investiga a fondo.*")
                    if TARGET_CHAT_ID: await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
                else:
                    logger.info(f"  - ❌ [RECHAZADO] {token_symbol} no pasa el filtro de seguridad (Metadata mutable).")

                await asyncio.sleep(2) # Pausa de 2 segundos entre cada token para no saturar RugCheck

        except asyncio.CancelledError: logger.info("Explorador de Momentum detenido."); break
        except Exception as e: logger.error(f"Error en Explorador de Momentum: {e}")

# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID; TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text("👋 v2.1 (Momentum Controlado). Usa /cazar y /parar.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('task'): await update.message.reply_text("🤔 El bot ya está buscando."); return
    await update.message.reply_text("🔎 ¡Iniciando Búsqueda de Momentum!")
    
    await setup_database()
    global notified_tokens; notified_tokens = await db_load_notified()
    
    client = httpx.AsyncClient()
    task = asyncio.create_task(momentum_explorer_task(client, context))
    context.bot_data.update({'client': client, 'task': task})

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('task'): await update.message.reply_text("🤔 El bot no está buscando."); return
    context.bot_data['task'].cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Búsqueda detenida!")

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    logger.info("--- El bot está escuchando a Telegram ---"); application.run_polling()

if __name__ == '__main__':
    main()
