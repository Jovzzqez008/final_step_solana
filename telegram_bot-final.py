import asyncio
import json
import base64
import httpx # <-- Usaremos httpx en lugar de requests
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

RAYDIUM_LP_V4 = Pubkey.from_string('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8')

watchlist = {}
incubator = {}
processed_signatures = set()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
# (Sin cambios)
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
    
async def db_update_incubator(token_address, data):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE incubator SET data = $2 WHERE token_address = $1", token_address, json.dumps(data))
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

### --- INICIO DE LA MODIFICACIÓN --- ###
async def get_security_report(client, token_address): # Ahora recibe un cliente httpx
    url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={token_address}"; headers = {"Authorization": f"Bearer {GOPLUS_API_KEY}"}
    try:
        res = await client.get(url, headers=headers, timeout=10); res.raise_for_status()
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

async def incubator_checker_task(chat_id):
    logger.info("Iniciando Vigilante de la Incubadora...")
    async with httpx.AsyncClient() as client: # Creamos un cliente que se reutiliza
        while True:
            try:
                await asyncio.sleep(300)
                logger.info(f"Vigilante de Incubadora despertando... {len(incubator)} tokens por revisar.")
                promoted = []; expired = []; to_remove = []
                current_time = time.time()
                
                # Hacemos una copia para iterar de forma segura
                for token_address, data in list(incubator.items()):
                    status = data.get('status', 'unverified')
                    if status == 'unverified':
                        logger.info(f"  - [VERIFICANDO] Chequeando seguridad de {token_address[:10]}...")
                        reporte, es_seguro, _ = await get_security_report(client, token_address)
                        if reporte == "❓ Reporte de seguridad no disponible.":
                            logger.info(f"  - [ESPERANDO] GoPlus aún no tiene reporte para {token_address[:10]}.")
                        elif es_seguro:
                            logger.info(f"  - ✅ [OK SEGURIDAD] {token_address[:10]} es seguro.")
                            data['status'] = 'verified'; data['security_report'] = reporte
                            await db_update_incubator(token_address, data)
                        else:
                            logger.info(f"  - ❌ [RECHAZADO SEGURIDAD] {token_address[:10]} no es seguro.")
                            to_remove.append(token_address)
                        
                        await asyncio.sleep(0.5) # Pausa de medio segundo para no saturar la API
                        continue

                    if data.get('status') == 'verified':
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
                                # La función de enviar alerta no es async, la ejecutamos en un thread para no bloquear
                                await asyncio.to_thread(enviar_alerta_telegram_sync, alerta, chat_id)
                                watchlist[token_address] = {'found_at': data['found_at'], 'symbol': symbol, 'status': 'new', 'initial_liquidity': liquidity, 'initial_holders': holders, 'source': data['source']}
                                promoted.append(token_address)
                        except Exception as e: logger.error(f"  - Error en Birdeye para {token_address}: {e}")
                
                # Limpieza
                for token in promoted + expired + to_remove:
                    if token in incubator: await db_remove_from_incubator(token); del incubator[token]

            except asyncio.CancelledError: logger.info("Vigilante de Incubadora detenido."); break
            except Exception as e: logger.error(f"Error en Vigilante de Incubadora: {e}")

# La función para enviar a telegram no cambia, pero es importante que no sea async
def enviar_alerta_telegram_sync(mensaje, chat_id):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown", "disable_web_page_preview": True})
        logger.info(f"Alerta enviada al chat {chat_id}.")
    except Exception as e:
        logger.error(f"Error enviando a Telegram: {e}")
### --- FIN DE LA MODIFICACIÓN --- ###


async def procesar_nuevo_token(token_address, source, chat_id):
    # (El resto del script, cazadores, watcher, comandos, etc. no necesita cambios)
    direcciones_a_ignorar = {
        '11111111111111111111111111111111', 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
        'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'ComputeBudget111111111111111111111111111111',
        '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8', 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
    }
    if token_address in direcciones_a_ignorar:
        # logger.info(f"[FILTRADO] Dirección conocida ignorada: {token_address[:10]}...")
        return

    if len(token_address) < 32 or len(token_address) > 44: return

    if token_address not in incubator and token_address not in watchlist:
        logger.info(f"[CAZADOR] Token detectado: {token_address}. Añadiendo a incubadora para verificación.")
        new_data = {'found_at': time.time(), 'source': source, 'status': 'unverified', 'symbol': 'N/A'}
        incubator[token_address] = new_data
        await db_add_to_incubator(token_address, new_data)

async def raydium_hunter_task(chat_id):
    logger.info("Iniciando Cazador Único de Raydium (LPv4)...");
    while True:
        try:
            async with connect(HELIUS_RPC_URL) as websocket:
                await websocket.logs_subscribe(RpcTransactionLogsFilterMentions(RAYDIUM_LP_V4))
                first_resp = await websocket.recv(); logger.info(f"Cazador de Raydium (LPv4) conectado. ID: {first_resp[0].result}")
                async for msg in websocket:
                    for log_message in msg:
                        signature = log_message.result.value.signature
                        if signature in processed_signatures: continue
                        processed_signatures.add(signature)
                        logs = log_message.result.value.logs
                        posibles_tokens = set()
                        for log in logs:
                            for word in log.split():
                                if 32 <= len(word) <= 44:
                                    try: Pubkey.from_string(word); posibles_tokens.add(word)
                                    except: continue
                        for token_b in posibles_tokens:
                            await procesar_nuevo_token(token_b, "Raydium LPv4", chat_id)
        except asyncio.CancelledError: logger.info("Cazador de Raydium (LPv4) detenido."); break
        except Exception as e: logger.error(f"Error en Cazador de Raydium (LPv4): {e}. Reiniciando..."); await asyncio.sleep(30)


async def watcher_task(chat_id):
    logger.info("Iniciando tarea del Vigía (Watcher)...");
    async with httpx.AsyncClient() as client:
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
                        await analizar_superviviente(client, addr, data, hours, chat_id)
                        watchlist[addr]['status'] = f'checked_{hours}h'; await db_update_watchlist_status(addr, watchlist[addr]['status'])
                else: logger.info("  - Ningún candidato en watchlist cumple 24/48h todavía.")
            except asyncio.CancelledError: logger.info("Tarea del Vigía (Watcher) detenida."); break
            except Exception as e: logger.error(f"Error en el Vigía (Watcher): {e}")

async def analizar_superviviente(client, token_address, initial_data, hours, chat_id):
    logger.info(f"Fase 2: Analizando superviviente de {hours}h: {initial_data['symbol']}")
    birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"; headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
    try:
        res = await client.get(birdeye_url, headers=headers_birdeye, timeout=10); res.raise_for_status()
        data = res.json()
        if not data.get("success") or not data.get("data"): return
        token_data = data["data"]; current_liquidity = token_data.get("liquidity", 0); current_holders = token_data.get("holders", 0)
        liquidity_change = ((current_liquidity - initial_data['initial_liquidity']) / initial_data['initial_liquidity']) * 100 if initial_data['initial_liquidity'] > 0 else 0
        holders_change = ((current_holders - initial_data['initial_holders']) / initial_data['initial_holders']) * 100 if initial_data['initial_holders'] > 0 else 0
        if liquidity_change > -50 and holders_change > -10:
            alerta = (f"📈 *REPORTE DE SUPERVIVENCIA ({hours}H)*\n\n*{initial_data['symbol']}* ({token_address})\n\n*Progreso:*\n- Liquidez: `${current_liquidity:,.2f}` ({liquidity_change:+.2f}%)\n- Holders: *{current_holders:,}* ({holders_change:+.2f}%)\n\n[Ver en Birdeye](https://birdeye.so/token/{token_address}?chain=solana)")
            await asyncio.to_thread(enviar_alerta_telegram_sync, alerta, chat_id)
    except Exception as e: logger.error(f"  - Error analizando superviviente: {e}")


# --- COMANDOS DE TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO v6.3 (Asíncrono)!\n\nUsa /cazar, /parar, /status, /diagnostico.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot ya está cazando."); return
    await update.message.reply_text("🏹 ¡Iniciando la caza con enfoque total en Raydium LPv4! Agentes desplegados.")
    await setup_database()
    global watchlist, incubator
    watchlist = await db_load_all_watchlist(); incubator = await db_load_all_incubator()
    logger.info(f"Datos cargados desde la DB. Incubadora: {len(incubator)}, Watchlist: {len(watchlist)}")
    
    task_raydium_lp4 = asyncio.create_task(raydium_hunter_task(chat_id))
    task_incubator = asyncio.create_task(incubator_checker_task(chat_id))
    task_watcher = asyncio.create_task(watcher_task(chat_id))
    context.bot_data['tasks'] = [task_raydium_lp4, task_incubator, task_watcher]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'): await update.message.reply_text("🤔 El bot no está cazando actualmente."); return
    for task in context.bot_data.get('tasks'):
        task.cancel()
    await asyncio.gather(*context.bot_data.get('tasks', []), return_exceptions=True)
    context.bot_data['tasks'] = []; await update.message.reply_text("🛑 ¡Caza detenida! Todos los agentes han vuelto a la base.")

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
        if count >= limit:
            break
        symbol = data.get('symbol', 'N/A'); age_minutes = (time.time() - data.get('found_at', 0)) / 60
        status = data.get('status', 'unverified')
        message += f"- `{symbol}` (`{addr[:4]}...{addr[-4:]}`)\n  - Edad: {age_minutes:.1f} mins\n  - Estado: {status}\n"
        count += 1
    if total_tokens > limit:
        message += f"\n...y {total_tokens - limit} más."
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
