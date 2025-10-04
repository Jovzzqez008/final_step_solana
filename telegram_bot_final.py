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
from solders.pubkey import Pubkey
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions

load_dotenv()

# --- CONFIGURACIÓN GLOBAL CORREGIDA ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")       # Para la conexión WebSocket (cazador)
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")       # Para la API de análisis (analizador)
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')

# ... (El resto del código base no cambia)
watchlist = {}
incubator = {}
processed_signatures = set()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# --- FUNCIONES DE BASE DE DATOS (SIN CAMBIOS) ---
async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''CREATE TABLE IF NOT EXISTS incubator (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        logger.info("Base de datos y tablas verificadas/creadas correctamente.")
        await conn.close()
    except Exception as e:
        logger.error(f"Error al configurar la base de datos: {e}")
async def db_add_to_incubator(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data))
    await conn.close()
async def db_remove_from_incubator(token_address):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)
    await conn.close()
async def db_load_all_incubator():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT token_address, data FROM incubator")
    await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}
async def db_add_to_watchlist(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data))
    await conn.close()
async def db_update_watchlist_status(token_address, new_status):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE watchlist SET data = data || jsonb_build_object('status', $1::text) WHERE token_address = $2", new_status, token_address)
    await conn.close()
async def db_load_all_watchlist():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT token_address, data FROM watchlist")
    await conn.close()
    return {row['token_address']: json.loads(row['data']) for row in rows}

# --- FUNCIONES DEL BOT (CON LÓGICA CORREGIDA) ---
async def get_helius_security_report(client, signature):
    api_url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    payload = {"transactions": [signature]}
    try:
        res = await client.post(api_url, json=payload, timeout=20)
        if res.status_code != 200:
            logger.error(f"Error en API Helius para signature {signature[:10]}: {res.status_code} - {res.text}")
            return None, "Helius API Error"
        data = res.json()
        if not data or not data[0]:
            return None, "Respuesta de Helius vacía"
        tx_data = data[0]
        token_transfers = tx_data.get("tokenTransfers", [])
        for transfer in token_transfers:
            if transfer.get("mint") and transfer.get("toUserAccount"):
                token_address = transfer["mint"]
                is_safe = True; report = ["- ✅ Token detectado y parseado por Helius."]
                direcciones_a_ignorar = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}
                if token_address in direcciones_a_ignorar: continue
                if is_safe: return token_address, "\n".join(report)
        return None, "No se encontró un nuevo token mint en la transacción"
    except Exception as e:
        logger.error(f"Excepción en get_helius_security_report para {signature[:10]}: {e}")
        return None, "Excepción en análisis"

async def procesar_nueva_transaccion(client, signature, source, chat_id):
    logger.info(f"[CAZADOR] Transacción detectada: {signature[:10]}... Analizando con Helius.")
    token_address, report = await get_helius_security_report(client, signature)
    if token_address and (token_address not in incubator and token_address not in watchlist):
        logger.info(f"  - ✅ [OK SEGURIDAD] Token {token_address[:10]}... parece seguro. Añadiendo a incubadora.")
        new_data = {'found_at': time.time(), 'source': source, 'status': 'verified', 'security_report': report, 'symbol': 'N/A'}
        incubator[token_address] = new_data
        await db_add_to_incubator(token_address, new_data)
    else:
        if not token_address: logger.info(f"  - ❌ [RECHAZADO] Transacción {signature[:10]} no contenía un candidato válido. Razón: {report}")

async def incubator_checker_task(chat_id):
    logger.info("Iniciando Vigilante de la Incubadora (solo mercado)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(300)
                logger.info(f"Vigilante de Mercado despertando... {len(incubator)} tokens por revisar.")
                promoted = []; expired = []
                current_time = time.time()
                for token_address, data in list(incubator.items()):
                    if current_time - data.get('found_at', 0) > 7200:
                        expired.append(token_address); continue
                    birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"; headers = {"X-API-KEY": BIRDEYE_API_KEY}
                    try:
                        res = await client.get(birdeye_url, headers=headers, timeout=10); res.raise_for_status()
                        api_data = res.json()
                        if not api_data.get("success") or not api_data.get("data"): continue
                        token_data = api_data["data"]
                        symbol = token_data.get("symbol", "N/A"); liquidity = token_data.get("liquidity", 0); holders = token_data.get("holders", 0)
                        data['symbol'] = symbol
                        logger.info(f"  - [INCUBADORA] Chequeando {symbol}: Liquidez=${liquidity:,.2f} (Req: >7500), Holders={holders} (Req: >11)")
                        if liquidity > 7500 and holders > 11:
                            logger.info(f"  - 🔥 ¡PROMOCIÓN! {symbol} ({token_address})")
                            alerta = f"🕵️‍♂️ *NUEVO CANDIDATO*\n\n*{symbol}* ({token_address})\n\n{data.get('security_report', 'N/A')}"
                            await asyncio.to_thread(enviar_alerta_telegram_sync, alerta, chat_id)
                            watchlist[token_address] = {'found_at': data['found_at'], 'symbol': symbol, 'status': 'new', 'initial_liquidity': liquidity, 'initial_holders': holders, 'source': data['source']}
                            promoted.append(token_address)
                    except Exception as e: logger.error(f"  - Error en Birdeye para {token_address}: {e}")
                for token in promoted + expired:
                    if token in incubator: await db_remove_from_incubator(token); del incubator[token]
            except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
            except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

async def raydium_hunter_task(client, chat_id):
    logger.info("Iniciando Cazador Único de Raydium (LPv4)...");
    while True:
        try:
            # La conexión WebSocket usa la HELIUS_RPC_URL
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                first_resp = await websocket.recv(); logger.info(f"Cazador de Raydium conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        signature = str(log_message.result.value.signature)
                        if signature not in processed_signatures:
                            processed_signatures.add(signature)
                            asyncio.create_task(procesar_nueva_transaccion(client, signature, "Raydium LPv4", chat_id))
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(30)

def enviar_alerta_telegram_sync(mensaje, chat_id):
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True})
    except Exception as e: logger.error(f"Error enviando a Telegram: {e}")
async def watcher_task(chat_id):
    logger.info("Iniciando tarea del Vigía (Watcher)...");
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Error en el Vigía (Watcher): {e}")
async def analizar_superviviente(client, token_address, initial_data, hours, chat_id):
    pass

# --- COMANDOS DE TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO v8.1 (Helius Completo)!\n\nUsa /cazar, /parar, /status.")
async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza con el motor Helius! Agentes desplegados.")
    await setup_database()
    global watchlist, incubator
    watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados desde la DB. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    client = httpx.AsyncClient()
    context.bot_data['client'] = client
    task_raydium_lp4 = asyncio.create_task(raydium_hunter_task(client, chat_id))
    task_incubator = asyncio.create_task(incubator_checker_task(chat_id))
    task_watcher = asyncio.create_task(watcher_task(chat_id))
    context.bot_data['tasks'] = [task_raydium_lp4, task_incubator, task_watcher]
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando actualmente."); return
    tasks = context.bot_data.get('tasks', [])
    for task in tasks: task.cancel()
    client = context.bot_data.get('client')
    if client: await client.aclose()
    await asyncio.gather(*tasks, return_exceptions=True)
    context.bot_data['tasks'] = []
    context.bot_data['client'] = None
    await update.message.reply_text("🛑 ¡Caza detenida! Todos los agentes y conexiones han sido cerrados.")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks') and len(context.bot_data.get('tasks')) > 0:
        status_msg = (f"✅ El bot está **Activo**.\n🐣 Hay **{len(incubator)}** tokens en la incubadora (memoria viva).\n🕵️‍♂️ Hay **{len(watchlist)}** candidatos en la lista de vigilancia (memoria viva).")
    await update.message.reply_text(status_msg, parse_mode='Markdown')
async def diagnostic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía en este momento.")
        return
    total_tokens = len(incubator)
    limit = 20
    message = f"🐣 *Mostrando los primeros {min(total_tokens, limit)} de {total_tokens} tokens en la Incubadora:*\n\n"
    count = 0
    for addr, data in list(incubator.items()):
        if count >= limit: break
        symbol = data.get('symbol', 'N/A'); age_minutes = (time.time() - data.get('found_at', 0)) / 60
        status = data.get('status', 'verified')
        message += f"- `{symbol}` (`{addr[:4]}...{addr[-4:]}`)\n  - Edad: {age_minutes:.1f} mins\n  - Estado: {status}\n"
        count += 1
    if total_tokens > limit: message += f"\n...y {total_tokens - limit} más."
    await update.message.reply_text(message, parse_mode='Markdown')

def main():
    print("--- 🤖 Iniciando Bot de Telegram... ---")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("diagnostico", diagnostic_command))
    print("--- 🎧 El bot está escuchando a Telegram... ---")
    application.run_polling()

if __name__ == '__main__':
    main()
