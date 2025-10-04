import asyncio
import json
import base64
import requests
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

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
GOPLUS_API_KEY = os.getenv("GOPLUS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Direcciones de los DEX a escuchar ---
RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')
METEORA_DLMM_PROGRAM = Pubkey.from_string('LBUZKhRxPF3XG2A2qRFFH2G2BgaR6f2x32a12p6c1J8')

# --- Diccionarios en memoria (se cargan desde la DB al iniciar) ---
watchlist = {}
incubator = {}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 💾 FUNCIONES PARA LA BASE DE DATOS (POSTGRESQL) 💾 ---
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

# --- FUNCIONES DEL BOT ---
def enviar_alerta_telegram_sync(mensaje, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"; payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try: requests.post(url, json=payload); logger.info(f"Alerta enviada al chat {chat_id}.")
    except Exception as e: logger.error(f"Error enviando a Telegram: {e}")

def get_security_report(token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={token_address}"; headers = {"Authorization": f"Bearer {GOPLUS_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10); res.raise_for_status()
        data = res.json().get('result', {}).get(token_address.lower());
        if not data: return "❓ Reporte de seguridad no disponible.", False, "N/A"
        report = []; is_safe = True
        if data.get('is_honeypot') == '1':
            report.append("- 🚨 ¡ALTO RIESGO! Posible Honeypot."); is_safe = False
        else: report.append("- ✅ No parece ser Honeypot.")
        total_lp_locked_pct = sum(float(lp.get('percent', 0)) for lp in data.get('lp_holders', []) if lp.get('is_locked') == 1)
        report_text = f"- 💧 Liquidez Bloqueada: {total_lp_locked_pct*100:.2f}%"; report.append(report_text)
        if is_safe and data.get('is_honeypot') == '0': return "\n".join(report), True, report_text
        else: return "\n".join(report), False, report_text
    except Exception as e:
        logger.error(f"Error en GoPlus para {token_address}: {e}"); return "❓ Error en GoPlus.", False, "N/A"

async def procesar_nuevo_token(token_address, source, chat_id):
    logger.info(f"[CAZADOR] Token detectado: {token_address}. Enviando a procesar...")
    reporte_seguridad, es_seguro, _ = get_security_report(token_address)
    if es_seguro:
        if token_address not in incubator and token_address not in watchlist:
            logger.info(f"  - ✅ [OK SEGURIDAD] Pasa filtro de seguridad. Añadiendo a incubadora y DB: {token_address}")
            new_data = {'found_at': time.time(), 'source': source, 'security_report': reporte_seguridad, 'symbol': 'Fetching...'}
            incubator[token_address] = new_data
            await db_add_to_incubator(token_address, new_data)
    else: logger.info(f"  - ❌ [RECHAZADO SEGURIDAD] No pasó el filtro de seguridad. Razón: {reporte_seguridad}")

async def incubator_checker_task(chat_id):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(300)
            logger.info("Vigilante de Incubadora despertando...")
            promoted_tokens = []; expired_tokens = []; current_time = time.time()
            for token_address, data in list(incubator.items()):
                if current_time - data.get('found_at', 0) > 7200:
                    expired_tokens.append(token_address); continue
                birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"; headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
                try:
                    res = requests.get(birdeye_url, headers=headers_birdeye, timeout=10); res.raise_for_status()
                    api_data = res.json()
                    if not api_data.get("success") or not api_data.get("data"): continue
                    token_data = api_data["data"]
                    symbol = token_data.get("symbol", "N/A"); liquidity = token_data.get("liquidity", 0); holders = token_data.get("holders", 0)
                    incubator[token_address]['symbol'] = symbol
                    logger.info(f"  - [INCUBADORA] Chequeando {symbol}: Liquidez=${liquidity:,.2f} (Req: >7500), Holders={holders} (Req: >50)")
                    if liquidity > 7500 and holders > 50:
                        logger.info(f"  - 🔥 ¡PROMOCIÓN! {symbol} ({token_address}) cumple los criterios.")
                        alerta = (f"🕵️‍♂️ *NUEVO CANDIDATO A VIGILAR* (Fuente: {data['source']})\n\n*{symbol}* ({token_address})\n\nHa madurado en la incubadora...\n\n*Reporte de Seguridad Inicial:*\n{data['security_report']}")
                        enviar_alerta_telegram_sync(alerta, chat_id)
                        watchlist[token_address] = {'found_at': data['found_at'], 'symbol': symbol, 'status': 'new', 'initial_liquidity': liquidity, 'initial_holders': holders, 'source': data['source']}
                        promoted_tokens.append(token_address)
                except Exception as e: logger.error(f"  - Error revisando {token_address} en Birdeye: {e}")
            for token in promoted_tokens:
                if token in incubator: await db_add_to_watchlist(token, watchlist[token]); await db_remove_from_incubator(token); del incubator[token]
            for token in expired_tokens:
                if token in incubator: await db_remove_from_incubator(token); del incubator[token]; logger.info(f"  - 🚮 Token expirado y eliminado de la incubadora y DB: {token}")
        except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

async def raydium_hunter_task(chat_id):
    logger.info("Iniciando Cazador de Raydium...");
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                first_resp = await websocket.recv(); logger.info(f"Cazador de Raydium conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        logs = log_message.result.value.logs
                        for log in logs:
                            if "initialize2" in log:
                                ### --- INICIO DE LA CORRECCIÓN --- ###
                                try:
                                    data = base64.b64decode(log.split()[-1])[8:]
                                    token_b = str(Pubkey(data[297:329]))
                                    if token_b:
                                        await procesar_nuevo_token(token_b, "Raydium", chat_id)
                                except:
                                    continue
                                ### --- FIN DE LA CORRECCIÓN --- ###
        except asyncio.CancelledError: logger.info("Cazador de Raydium detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium: {e}. Reiniciando..."); await asyncio.sleep(30)

async def meteora_hunter_task(chat_id):
    logger.info("Iniciando Cazador de Meteora...");
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(METEORA_DLMM_PROGRAM))
                first_resp = await websocket.recv(); logger.info(f"Cazador de Meteora conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        logs = log_message.result.value.logs
                        for log in logs:
                            if "Instruction: InitializePool" in log and "Program data: " in log:
                                ### --- INICIO DE LA CORRECCIÓN --- ###
                                try:
                                    data = base64.b64decode(log.split("Program data: ")[1])
                                    token_b = str(Pubkey(data[72:104]))
                                    if token_b:
                                        await procesar_nuevo_token(token_b, "Meteora", chat_id)
                                except:
                                    continue
                                ### --- FIN DE LA CORRECCIÓN --- ###
        except asyncio.CancelledError: logger.info("Cazador de Meteora detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Meteora: {e}. Reiniciando..."); await asyncio.sleep(30)

async def watcher_task(chat_id):
    logger.info("Iniciando tarea del Vigía (Watcher)...");
    while True:
        try:
            await asyncio.sleep(3600); logger.info("Vigía (Watcher) despertando...");
            current_time = time.time(); survivors_to_check = []
            for addr, data in list(watchlist.items()):
                age_seconds = current_time - data.get('found_at', 0); status = data.get('status', 'new')
                if status == 'new' and age_seconds > 86400: survivors_to_check.append((addr, data, 24))
                elif status == 'checked_24h' and age_seconds > 172800: survivors_to_check.append((addr, data, 48))
            if survivors_to_check:
                logger.info(f"  - {len(survivors_to_check)} superviviente(s) de la watchlist encontrado(s).")
                for addr, data, hours in survivors_to_check:
                    await analizar_superviviente(addr, data, hours, chat_id)
                    watchlist[addr]['status'] = f'checked_{hours}h'; await db_update_watchlist_status(addr, watchlist[addr]['status'])
            else: logger.info("  - Ningún candidato en watchlist cumple 24/48h todavía.")
        except asyncio.CancelledError: logger.info("Tarea del Vigía (Watcher) detenida."); break
        except Exception as e: logger.error(f"Error en el Vigía (Watcher): {e}")

async def analizar_superviviente(token_address, initial_data, hours, chat_id):
    logger.info(f"Fase 2: Analizando superviviente de {hours}h: {initial_data['symbol']}")
    birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"; headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
    try:
        res = requests.get(birdeye_url, headers=headers_birdeye, timeout=10); res.raise_for_status()
        data = res.json()
        if not data.get("success") or not data.get("data"): return
        token_data = data["data"]; current_liquidity = token_data.get("liquidity", 0); current_holders = token_data.get("holders", 0)
        liquidity_change = ((current_liquidity - initial_data['initial_liquidity']) / initial_data['initial_liquidity']) * 100 if initial_data['initial_liquidity'] > 0 else 0
        holders_change = ((current_holders - initial_data['initial_holders']) / initial_data['initial_holders']) * 100 if initial_data['initial_holders'] > 0 else 0
        if liquidity_change > -50 and holders_change > -10:
            alerta = (f"📈 *REPORTE DE SUPERVIVENCIA ({hours}H)*\n\n*{initial_data['symbol']}* ({token_address})\n\n*Progreso:*\n- Liquidez: `${current_liquidity:,.2f}` ({liquidity_change:+.2f}%)\n- Holders: *{current_holders:,}* ({holders_change:+.2f}%)\n\n[Ver en Birdeye](https://birdeye.so/token/{token_address}?chain=solana)")
            enviar_alerta_telegram_sync(alerta, chat_id)
    except Exception as e: logger.error(f"  - Error analizando superviviente: {e}")

# --- COMANDOS DE TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO v5.2 (Sintaxis Corregida)!\n\nUsa /cazar, /parar, /status, /diagnostico.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza con memoria persistente (DB)! Agentes desplegados.")
    await setup_database()
    global watchlist, incubator
    watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados desde la DB. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    task_raydium = asyncio.create_task(raydium_hunter_task(chat_id)); task_meteora = asyncio.create_task(meteora_hunter_task(chat_id))
    task_incubator = asyncio.create_task(incubator_checker_task(chat_id)); task_watcher = asyncio.create_task(watcher_task(chat_id))
    context.bot_data['tasks'] = [task_raydium, task_meteora, task_incubator, task_watcher]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando actualmente."); return
    for task in context.bot_data['tasks']: task.cancel()
    await asyncio.gather(*context.bot_data.get('tasks', []), return_exceptions=True)
    context.bot_data['tasks'] = []; await update.message.reply_text("🛑 ¡Caza detenida! Todos los agentes han vuelto a la base.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks') and len(context.bot_data.get('tasks')) > 0:
        status_msg = (f"✅ El bot está **Activo**.\n🐣 Hay **{len(incubator)}** tokens en la incubadora (memoria viva).\n🕵️‍♂️ Hay **{len(watchlist)}** candidatos en la lista de vigilancia (memoria viva).")
    await update.message.reply_text(status_msg, parse_mode='Markdown')
    
async def diagnostic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator: await update.message.reply_text("🐣 La incubadora está vacía en este momento."); return
    message = "🐣 *Tokens Actualmente en la Incubadora:*\n\n"
    for addr, data in incubator.items():
        symbol = data.get('symbol', 'N/A'); age_minutes = (time.time() - data.get('found_at', 0)) / 60
        message += f"- `{symbol}` (`{addr[:4]}...{addr[-4:]}`)\n  - Edad: {age_minutes:.1f} mins\n"
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
