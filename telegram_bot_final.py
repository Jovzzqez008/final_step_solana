import asyncio
import json
import httpx
import time
import logging
import os
from dotenv import load_dotenv

import asyncpg
import importlib.metadata

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions

load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
# BIRDEYE_API_KEY ya no es necesario
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')

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

# --- ### FUNCIONES DE CONSULTA A APIS EXTERNAS (ACTUALIZADO) ### ---
async def get_dexscreener_data(client, token_address):
    """Consulta DexScreener para obtener datos de mercado (liquidez)."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200:
            data = res.json()
            # Buscamos el par con más liquidez, que suele ser el principal (vs SOL o USDC)
            if data.get('pairs'):
                main_pair = sorted(data['pairs'], key=lambda x: x.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                return main_pair
        return None
    except Exception as e:
        logger.error(f"Error consultando DexScreener para {token_address[:10]}: {e}")
        return None

async def get_rugcheck_data(client, token_address):
    """Consulta RugCheck para obtener datos de seguridad (holders)."""
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
    try:
        res = await client.get(url, timeout=30)
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        logger.error(f"Error consultando RugCheck para {token_address[:10]}: {e}")
        return None

# --- TAREAS ASÍNCRONAS ---
async def raydium_hunter_task():
    logger.info("Iniciando Cazador de Raydium...")
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                await websocket.recv()
                logger.info(f"Cazador de Raydium conectado.")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature)
                            await signature_queue.put(signature)
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(15)

async def processor_task(client):
    logger.info("Iniciando Procesador...")
    while True:
        try:
            signature = await signature_queue.get()
            # SIMULACIÓN: Aquí se llamaría a la función de parseo manual para encontrar el token_address
            token_address = Pubkey.new_unique().__str__() 
            
            if token_address and (token_address not in incubator and token_address not in watchlist):
                logger.info(f"  - [Candidato Encontrado] {token_address[:10]}... a la incubadora.")
                new_data = {'found_at': time.time(), 'source': "Raydium LPv4", 'status': 'incubating'}
                incubator[token_address] = new_data
                await db_add_to_incubator(token_address, new_data)

            signature_queue.task_done()
            await asyncio.sleep(1.2)
        except asyncio.CancelledError: logger.info("Procesador detenido."); break
        except Exception as e: logger.error(f"Error en Procesador: {e}")

# --- ### TAREA DE LA INCUBADORA (ACTUALIZADA A DEXSCREENER) ### ---
async def incubator_checker_task(client, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora (DexScreener + RugCheck)...")
    while True:
        try:
            await asyncio.sleep(60) # Revisa cada 60 segundos
            if not incubator: continue
            
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            for token_address, data in list(incubator.items()):
                
                # --- FILTRO 1: LIQUIDEZ (DEXSCREENER) ---
                dex_data = await get_dexscreener_data(client, token_address)
                
                if not dex_data:
                    if time.time() - data['found_at'] > 600: # Si tras 10 mins no hay datos, se descarta
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]} sin datos en DexScreener tras 10 mins.")
                        del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue

                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                if liquidity < 7500:
                    logger.info(f"  - ❌ [LIQUIDEZ BAJA] {token_address[:10]} descartado. Liquidez: ${liquidity:,.2f}")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    continue
                
                logger.info(f"  - ✅ [LIQUIDEZ OK] {token_address[:10]} pasa filtro de ${liquidity:,.2f}. Verificando seguridad...")

                # --- FILTRO 2: SEGURIDAD (RUGCHECK) ---
                rugcheck_data = await get_rugcheck_data(client, token_address)
                top_holders_ok = True
                
                if rugcheck_data:
                    top_holders = rugcheck_data.get('topHolders', [])
                    if top_holders and top_holders[0].get('pct', 0) * 100 >= 85:
                        top_holders_ok = False
                
                if top_holders_ok:
                    logger.info(f"  - ✅ [APROBADO] {token_address[:10]} pasa todos los filtros. ¡A la Watchlist!")
                    data.update({'status': 'approved', 'liquidity': liquidity})
                    watchlist[token_address] = data
                    await db_add_to_watchlist(token_address, data)
                    
                    del incubator[token_address]; await db_remove_from_incubator(token_address)
                    
                    mensaje = (f"✅ **Token Aprobado**\n\n"
                               f"**Mint:** `{token_address}`\n"
                               f"**Liquidez:** `${liquidity:,.2f}` USD\n\n"
                               f"Pasa tus filtros de liquidez y seguridad.")
                    await context.bot.send_message(chat_id=context.job.chat_id, text=mensaje, parse_mode='Markdown')
                else:
                    holder_pct = top_holders[0].get('pct', 0) * 100
                    logger.info(f"  - ❌ [RIESGO DE CENTRALIZACIÓN] {token_address[:10]} descartado. Top holder tiene {holder_pct:.2f}%.")
                    del incubator[token_address]; await db_remove_from_incubator(token_address)

        except asyncio.CancelledError: logger.info("Vigilante de la Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")


# --- COMANDOS Y EJECUCIÓN ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido! Usa /cazar, /parar, /status.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza con análisis combinado (DexScreener + RugCheck)!")
    await setup_database()
    global watchlist, incubator; watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    client = httpx.AsyncClient()
    context.bot_data['client'] = client
    
    task_raydium = asyncio.create_task(raydium_hunter_task())
    task_processor = asyncio.create_task(processor_task(client))
    task_incubator = asyncio.create_task(incubator_checker_task(client, context))
    context.bot_data['tasks'] = [task_raydium, task_processor, task_incubator]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando."); return
    for task in context.bot_data.get('tasks', []): task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 ¡Caza detenida!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (f"✅ El bot está **Activo**.\n"
                      f"🐣 **{len(incubator)}** tokens en incubadora.\n"
                      f"🕵️‍♂️ **{len(watchlist)}** en watchlist.\n"
                      f"⌛ **{signature_queue.qsize()}** transacciones en cola.")
    await update.message.reply_text(status_msg, parse_mode='Markdown')

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    logger.info("--- El bot está escuchando a Telegram ---")
    application.run_polling()

if __name__ == '__main__':
    main()
