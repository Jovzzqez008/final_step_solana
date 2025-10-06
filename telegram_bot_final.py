import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

load_dotenv()

# -------------------- CONFIG --------------------
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# APIs - Vamos a usar DexScreener como fuente principal
DEXSCREENER_NEW_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/new"
DEXSCREENER_TOKEN_INFO = "https://api.dexscreener.com/latest/dex/tokens/{}"
RUGCHECK_API = "https://api.rugcheck.xyz/api/tokens/{}"

# Headers mejorados
DEXSCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TokenRadar/1.0; +https://github.com)",
    "Accept": "application/json",
    "Referer": "https://dexscreener.com/"
}

# Umbrales
LIQUIDITY_THRESHOLD = 7500
MAX_TOKEN_AGE_HOURS = 12  # Máximo 12 horas de antigüedad

# Estructuras en memoria
incubator: Dict[str, Dict[str, Any]] = {}
watchlist: Dict[str, Dict[str, Any]] = {}

# Logging mejorado
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE HELPERS (MANTENER IGUAL) --------------------
async def setup_database():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL configurada — la persistencia no funcionará.")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS incubator (
                token_address TEXT PRIMARY KEY,
                data JSONB NOT NULL
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS watchlist (
                token_address TEXT PRIMARY KEY,
                data JSONB NOT NULL
            );
        ''')
        await conn.close()
        logger.info("Base de datos inicializada.")
    except Exception as e:
        logger.error(f"Error inicializando DB: {e}")

async def db_add_to_incubator(token_address: str, data: dict):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2",
            token_address, json.dumps(data)
        )
    finally: await conn.close()

async def db_remove_from_incubator(token_address: str):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)
    finally: await conn.close()

async def db_load_all_incubator() -> Dict[str, dict]:
    if not DATABASE_URL: return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM incubator")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally: await conn.close()

async def db_add_to_watchlist(token_address: str, data: dict):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2",
            token_address, json.dumps(data)
        )
    finally: await conn.close()

async def db_load_all_watchlist() -> Dict[str, dict]:
    if not DATABASE_URL: return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally: await conn.close()

# -------------------- EXTERNAL API HELPERS ACTUALIZADAS --------------------
async def get_dexscreener_new_pairs(client: httpx.AsyncClient):
    """Obtiene pares nuevos de DexScreener - MÁS CONFIABLE QUE JUPITER"""
    try:
        res = await client.get(DEXSCREENER_NEW_PAIRS, headers=DEXSCREENER_HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"DexScreener responded {res.status_code}")
            return []
        
        data = res.json()
        pairs = data.get('pairs', [])
        
        # Filtrar solo Solana y tokens recientes
        now_ts = time.time()
        recent_solana_pairs = []
        
        for pair in pairs:
            # Solo tokens de Solana
            if pair.get('chainId') != 'solana':
                continue
                
            # Verificar antigüedad (usando pairCreatedAt)
            pair_created_at = pair.get('pairCreatedAt')
            if pair_created_at:
                age_hours = (now_ts - (pair_created_at / 1000)) / 3600
                if age_hours <= MAX_TOKEN_AGE_HOURS:
                    recent_solana_pairs.append(pair)
            else:
                # Si no hay timestamp, incluir de todos modos
                recent_solana_pairs.append(pair)
        
        logger.info(f"[DEXSCREENER] Pares nuevos obtenidos: {len(recent_solana_pairs)}")
        return recent_solana_pairs
        
    except Exception as e:
        logger.error(f"Error consultando DexScreener: {e}")
        return []

async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos específicos de un token desde DexScreener"""
    url = DEXSCREENER_TOKEN_INFO.format(token_address)
    try:
        res = await client.get(url, headers=DEXSCREENER_HEADERS, timeout=12)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if pairs:
                # Escoger el par con mayor liquidez
                best_pair = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                return best_pair
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

async def get_rugcheck_data(client: httpx.AsyncClient, token_address: str):
    """Consulta RugCheck API para verificar liquidez bloqueada y riesgo"""
    url = RUGCHECK_API.format(token_address)
    try:
        res = await client.get(url, timeout=15)
        if res.status_code == 200:
            return res.json()
        else:
            logger.debug(f"RugCheck responded {res.status_code} for {token_address}")
    except Exception as e:
        logger.debug(f"Error RugCheck para {token_address}: {e}")
    return None

# -------------------- FILTERING LOGIC (MANTENER IGUAL) --------------------
def passes_rugcheck_filters(rugcheck_data: dict) -> tuple:
    """Verifica filtros de RugCheck"""
    if not rugcheck_data:
        return False, "sin datos de RugCheck"
    
    locked_liquidity = rugcheck_data.get('lockedLiquidity', False)
    if not locked_liquidity:
        return False, "liquidez NO bloqueada"
    
    risk = rugcheck_data.get('risk', '').lower()
    if risk in ['high risk', 'rugpull']:
        return False, f"riesgo alto: {risk}"
    
    return True, "OK"

def passes_dexscreener_filters(dex_data: dict) -> tuple:
    """Verifica filtros de DexScreener"""
    if not dex_data:
        return False, "sin datos de DexScreener"
    
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < LIQUIDITY_THRESHOLD:
        return False, f"liquidez insuficiente (${liquidity:,.0f})"
    
    return True, "OK"

# -------------------- TAREAS ASÍNCRONAS ACTUALIZADAS --------------------
async def dexscreener_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Tarea principal: consulta pares nuevos de DexScreener"""
    logger.info("Iniciando Radar de DexScreener...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                new_pairs = await get_dexscreener_new_pairs(client)
                if not new_pairs:
                    logger.info("[RADAR] No se obtuvieron nuevos pares en este ciclo.")
                else:
                    for pair in new_pairs:
                        # Extraer dirección del token
                        base_token = pair.get('baseToken', {})
                        token_address = base_token.get('address')
                        
                        if not token_address:
                            continue

                        # Evitar duplicados
                        if token_address in incubator or token_address in watchlist:
                            continue

                        found_at = time.time()
                        data = {
                            'found_at': found_at, 
                            'source': 'DexScreener Radar', 
                            'meta': pair,
                            'pair_data': pair
                        }
                        incubator[token_address] = data
                        await db_add_to_incubator(token_address, data)
                        logger.info(f"  - 🐣 Nuevo en incubadora: {token_address}")
                
                await asyncio.sleep(300)  # 5 minutos entre consultas
                
            except asyncio.CancelledError:
                logger.info("Radar de DexScreener detenido.")
                break
            except Exception as e:
                logger.error(f"Error en Radar de DexScreener: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica tokens en incubadora con filtros RugCheck + DexScreener"""
    logger.info("Iniciando Vigilante de la Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(180)
                if not incubator:
                    continue
                    
                now_ts = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados del token
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        if now_ts - data['found_at'] > 3600:
                            logger.info(f"  - 🗑️ Descartado (sin datos): {token_address}")
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # Obtener datos de RugCheck
                    rugcheck_data = await get_rugcheck_data(client, token_address)
                    if not rugcheck_data:
                        logger.info(f"  - ⚠️ Sin datos RugCheck: {token_address}")
                        continue

                    # APLICAR FILTROS
                    dex_passes, dex_reason = passes_dexscreener_filters(dex_data)
                    rugcheck_passes, rugcheck_reason = passes_rugcheck_filters(rugcheck_data)
                    
                    if dex_passes and rugcheck_passes:
                        # ✅ PROMOVER A WATCHLIST
                        approved_at = now_ts
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'last_notified': 'initial',
                            'meta': dex_data,
                            'rugcheck': rugcheck_data,
                            'liquidity': liquidity,
                            'dex_data': dex_data
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)

                        # NOTIFICACIÓN MEJORADA
                        risk = rugcheck_data.get('risk', 'N/A')
                        locked_liq = rugcheck_data.get('lockedLiquidity', False)
                        
                        message = (
                            f"✅ *OPORTUNIDAD CONFIRMADA - DexScreener Radar*\n\n"
                            f"*Token:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}` USD\n"
                            f"*Liquidez Bloqueada:* `{'✅ SÍ' if locked_liq else '❌ NO'}`\n"
                            f"*Riesgo RugCheck:* `{risk}`\n\n"
                            f"🔍 *Verificación Rápida:*\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n\n"
                            f"⚠️ *Realiza tu debido análisis antes de invertir*"
                        )
                        
                        if TARGET_CHAT_ID:
                            try:
                                await context.bot.send_message(
                                    chat_id=TARGET_CHAT_ID,
                                    text=message,
                                    parse_mode='Markdown',
                                    disable_web_page_preview=True
                                )
                            except Exception as e:
                                logger.warning(f"No se pudo enviar notificación Telegram: {e}")

                        logger.info(f"  - 🏆 PROMOVIDO a watchlist: {token_address}")

                    else:
                        # ❌ FILTROS FALLIDOS
                        rejection_reasons = []
                        if not dex_passes: rejection_reasons.append(f"DexScreener: {dex_reason}")
                        if not rugcheck_passes: rejection_reasons.append(f"RugCheck: {rugcheck_reason}")
                        
                        logger.info(f"  - ❌ Rechazado: {token_address} - {' | '.join(rejection_reasons)}")
                        
                        if now_ts - data['found_at'] > 7200:  # 2 horas
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(1)  # Delay para no saturar APIs
                    
            except asyncio.CancelledError:
                logger.info("Vigilante de incubadora detenido.")
                break
            except Exception as e:
                logger.error(f"Error en vigilante de incubadora: {e}")
                await asyncio.sleep(10)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Monitorea tokens aprobados (MANTENER IGUAL)"""
    logger.info("Iniciando Monitor de Watchlist...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(300)
                if not watchlist: continue
                    
                now = time.time()
                for token_address, data in list(watchlist.items()):
                    approved_at = data.get('approved_at', 0)
                    last_notified = data.get('last_notified', 'initial')
                    age_hours = (now - approved_at) / 3600
                    
                    notify_periods = {'initial': 24, '24hr': 72, '72hr': 96}
                    if last_notified in notify_periods and age_hours >= notify_periods[last_notified]:
                        dex_data = await get_dexscreener_token_data(client, token_address)
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0) if dex_data else 0
                        price_change_24h = dex_data.get('priceChange', {}).get('h24', 0) if dex_data else 0
                        
                        message = (
                            f"🔔 *Reporte ({notify_periods[last_notified]}h)*\n\n"
                            f"*Token:* `{token_address}`\n"
                            f"*Liq. Actual:* `${liquidity:,.2f}` USD\n"
                            f"*Cambio 24h:* `{price_change_24h}%`\n\n"
                            f"📊 https://dexscreener.com/solana/{token_address}"
                        )
                        
                        if TARGET_CHAT_ID:
                            try:
                                await context.bot.send_message(
                                    chat_id=TARGET_CHAT_ID,
                                    text=message,
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.warning(f"No se pudo enviar reporte Telegram: {e}")

                        new_state = f"{notify_periods[last_notified]}hr"
                        watchlist[token_address]['last_notified'] = new_state
                        await db_add_to_watchlist(token_address, watchlist[token_address])
                        
            except asyncio.CancelledError:
                logger.info("Monitor de watchlist detenido.")
                break
            except Exception as e:
                logger.error(f"Error en monitor de watchlist: {e}")
                await asyncio.sleep(10)

# -------------------- TELEGRAM COMMANDS (MANTENER IGUAL) --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "👋 *Bot DexScreener Radar Activado*\n\n"
        "🔍 *Comandos disponibles:*\n"
        "/cazar - Iniciar monitoreo\n"
        "/parar - Detener monitoreo\n" 
        "/status - Estado actual\n"
        "/incubadora - Ver tokens en incubación\n"
        "/watchlist - Ver tokens aprobados\n\n"
        "🚀 *Filtros activos:* Liquidez bloqueada + Riesgo bajo + Liquidez ≥$7.5K",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Radar de DexScreener...*\n\n🔍 Filtros activados:\n- Liquidez bloqueada (RugCheck)\n- Riesgo bajo\n- Liquidez ≥$7,500\n- Tokens ≤12 horas", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(dexscreener_radar_task(context)),
        asyncio.create_task(incubator_checker_task(context)),
        asyncio.create_task(watchlist_monitor_task(context))
    ]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot no está cazando.")
        return
        
    for task in context.bot_data['tasks']:
        task.cancel()
        
    context.bot_data.clear()
    await update.message.reply_text("🛑 *Caza detenida.* Todos los monitores apagados.", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está *Detenido*."
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ *Bot Activo - DexScreener Radar*\n\n"
            f"🐣 *Incubadora:* `{len(incubator)}` tokens\n"
            f"🏆 *Watchlist:* `{len(watchlist)}` tokens\n"
            f"🔍 *Filtros activos:*\n"
            f"   • Liquidez bloqueada (RugCheck)\n"
            f"   • Riesgo bajo\n" 
            f"   • Liquidez ≥${LIQUIDITY_THRESHOLD:,}\n"
            f"   • Tokens ≤{MAX_TOKEN_AGE_HOURS} horas"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía.")
        return
        
    message = f"🐣 *Tokens en Incubadora ({len(incubator)}):*\n\n"
    for i, token_address in enumerate(list(incubator.keys())[-10:][::-1], 1):
        message += f"{i}. `{token_address}`\n"
        
    if len(incubator) > 10:
        message += f"\n... y {len(incubator) - 10} más antiguos."
        
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 La watchlist está vacía.")
        return
        
    message = f"🏆 *Tokens en Watchlist ({len(watchlist)}):*\n\n"
    for i, token_address in enumerate(list(watchlist.keys())[-15:][::-1], 1):
        data = watchlist[token_address]
        liquidity = data.get('liquidity', 0)
        risk = data.get('rugcheck', {}).get('risk', 'N/A')
        message += f"{i}. `{token_address}`\n   💰 ${liquidity:,.0f} | 🛡️ {risk}\n"
        
    if len(watchlist) > 15:
        message += f"\n... y {len(watchlist) - 15} más antiguos."
        
    await update.message.reply_text(message, parse_mode='Markdown')

# -------------------- BOOT CON MANEJO DE CONFLICT --------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado.")
        return
        
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers de comandos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("incubadora", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))

    logger.info("--- Bot DexScreener Radar listo. Ejecutando polling... ---")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # Importante: elimina updates pendientes al iniciar
        )
    except Conflict as e:
        logger.error(f"🚨 ERROR DE CONFLICTO: {e}")
        logger.error("💡 SOLUCIÓN: Detén todas las otras instancias del bot y reinicia.")
    except Exception as e:
        logger.error(f"Error fatal: {e}")

if __name__ == '__main__':
    main()
