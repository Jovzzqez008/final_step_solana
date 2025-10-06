# Copia y pega este script reemplazando el anterior.
# He mantenido la estructura y comandos que ya tenías.

import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# -------------------- CONFIG --------------------
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID: Optional[int] = None

# Endpoints
JUPITER_TOKENS_ENDPOINT = "https://api.jup.ag/tokens/v1/tokens"
DEXSCREENER_TOKEN_INFO = "https://api.dexscreener.com/latest/dex/tokens/{}"
RUGCHECK_API = "https://api.rugcheck.xyz/api/tokens/{}"

# Headers y timeouts
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BotRadar/1.0; +https://railway.app)",
    "Accept": "application/json"
}
JUPITER_HEADERS = COMMON_HEADERS
DEXSCREENER_HEADERS = COMMON_HEADERS
RUGCHECK_HEADERS = COMMON_HEADERS

# Umbrales
LIQUIDITY_THRESHOLD = 7500
NEW_WINDOW_SECONDS = 12 * 3600  # 12 horas

# Estructuras en memoria
incubator: Dict[str, Dict[str, Any]] = {}
watchlist: Dict[str, Dict[str, Any]] = {}

# DB pool global (se inicializa en setup_database)
db_pool: Optional[asyncpg.pool.Pool] = None

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE HELPERS (Pool) --------------------
async def setup_database():
    global db_pool
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL configurada — la persistencia no funcionará.")
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as conn:
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
        logger.info("Base de datos inicializada (pool).")
    except Exception as e:
        logger.error(f"Error inicializando DB: {e}")

async def db_add_to_incubator(token_address: str, data: dict):
    global db_pool
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2",
            token_address, json.dumps(data)
        )

async def db_remove_from_incubator(token_address: str):
    global db_pool
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)

async def db_load_all_incubator() -> Dict[str, dict]:
    global db_pool
    if not db_pool:
        return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM incubator")
        return {row['token_address']: json.loads(row['data']) for row in rows}

async def db_add_to_watchlist(token_address: str, data: dict):
    global db_pool
    if not db_pool:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2",
            token_address, json.dumps(data)
        )

async def db_load_all_watchlist() -> Dict[str, dict]:
    global db_pool
    if not db_pool:
        return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
        return {row['token_address']: json.loads(row['data']) for row in rows}

# -------------------- EXTERNAL API HELPERS --------------------
async def get_jupiter_tokens(client: httpx.AsyncClient):
    """Consulta tokens desde la API de Jupiter con headers y tolerancia a formato."""
    try:
        res = await client.get(JUPITER_TOKENS_ENDPOINT, headers=JUPITER_HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"Jupiter responded {res.status_code}: {res.text[:200]}")
            return []
        data = res.json()
        # Soportar varias claves posibles
        tokens = []
        if isinstance(data, list):
            tokens = data
        elif isinstance(data, dict):
            # distintos endpoints usan 'data', 'tokens' o 'results'
            tokens = data.get('data') or data.get('tokens') or data.get('results') or []
        now_ts = time.time()
        recent = []
        for t in tokens:
            if not isinstance(t, dict):
                continue
            created_ts = t.get('createdAt') or t.get('listedAt') or t.get('timestamp') or t.get('listed_at')
            if created_ts:
                # detectar ms vs s
                try:
                    created_ts = float(created_ts)
                    if created_ts > 1e12:
                        created_ts = created_ts / 1000.0
                except Exception:
                    # si no se puede parsear, ignorar la fecha
                    created_ts = None
            if created_ts:
                age = now_ts - created_ts
                if 0 <= age <= NEW_WINDOW_SECONDS:
                    recent.append(t)
            else:
                # si no hay timestamp, incluir (puede generar más falsos positivos)
                recent.append(t)
        logger.info(f"[JUPITER] Tokens obtenidos: {len(recent)} (filtrados por ventana)")
        return recent
    except Exception as e:
        logger.error(f"Error consultando Jupiter: {e}")
        return []

async def get_dexscreener_data(client: httpx.AsyncClient, token_address: str):
    url = DEXSCREENER_TOKEN_INFO.format(token_address)
    try:
        res = await client.get(url, headers=DEXSCREENER_HEADERS, timeout=12)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if pairs:
                best = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                return best
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

async def get_rugcheck_data(client: httpx.AsyncClient, token_address: str):
    url = RUGCHECK_API.format(token_address)
    try:
        res = await client.get(url, headers=RUGCHECK_HEADERS, timeout=15)
        if res.status_code == 200:
            return res.json()
        else:
            logger.debug(f"RugCheck responded {res.status_code} for {token_address}")
    except Exception as e:
        logger.debug(f"Error RugCheck para {token_address}: {e}")
    return None

# -------------------- FILTERING LOGIC --------------------
def passes_rugcheck_filters(rugcheck_data: dict) -> tuple:
    """
    - lockedLiquidity debe ser True (si la API usa otro nombre, ajustar)
    - risk no debe ser 'High Risk' ni 'Rugpull'
    """
    if not rugcheck_data:
        return False, "sin datos de RugCheck"
    # Intentar leer distintos nombres
    locked_liquidity = rugcheck_data.get('lockedLiquidity')
    if locked_liquidity is None:
        locked_liquidity = rugcheck_data.get('liquidityLocked') or rugcheck_data.get('locked_liquidity') or False
    if not locked_liquidity:
        return False, "liquidez NO bloqueada"

    risk = str(rugcheck_data.get('risk') or rugcheck_data.get('scoreTag') or '').lower()
    if 'high' in risk or 'rug' in risk:
        return False, f"riesgo alto: {risk}"
    return True, "OK"

def passes_dexscreener_filters(dex_data: dict) -> tuple:
    if not dex_data:
        return False, "sin datos de DexScreener"
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < LIQUIDITY_THRESHOLD:
        return False, f"liquidez insuficiente (${liquidity:,.0f})"
    # pairCreatedAt puede venir en ms o s
    pair_created_at = dex_data.get('pairCreatedAt') or dex_data.get('createdAt') or dex_data.get('pair_created_at')
    if pair_created_at:
        try:
            ts = float(pair_created_at)
            if ts > 1e12:
                ts = ts / 1000.0
            age_days = (time.time() - ts) / 86400
            if age_days > 7:
                return False, f"demasiado antiguo ({age_days:.1f} días)"
        except Exception:
            pass
    return True, "OK"

# -------------------- TAREAS ASÍNCRONAS PRINCIPALES --------------------
async def jupiter_radar_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Radar de Jupiter...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                recent_tokens = await get_jupiter_tokens(client)
                if not recent_tokens:
                    logger.info("[RADAR] No se obtuvieron tokens en este ciclo.")
                else:
                    for entry in recent_tokens:
                        token_address = None
                        if isinstance(entry, dict):
                            token_address = entry.get('mint') or entry.get('address') or (entry.get('baseToken') or {}).get('address')
                        if not token_address:
                            continue
                        if token_address in incubator or token_address in watchlist:
                            continue
                        found_at = time.time()
                        data = {'found_at': found_at, 'source': 'Jupiter Radar', 'meta': entry}
                        incubator[token_address] = data
                        await db_add_to_incubator(token_address, data)
                        logger.info(f"  - 🐣 Nuevo en incubadora: {token_address}")
                        await asyncio.sleep(0.5)  # tiny spacing
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                logger.info("Radar de Jupiter detenido.")
                break
            except Exception as e:
                logger.error(f"Error en Radar de Jupiter: {e}")
                await asyncio.sleep(15)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
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
                    dex_data = await get_dexscreener_data(client, token_address)
                    if not dex_data:
                        if now_ts - data['found_at'] > 3600:
                            logger.info(f"  - 🗑️ Descartado (sin datos DexScreener): {token_address}")
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    rugcheck_data = await get_rugcheck_data(client, token_address)
                    if not rugcheck_data:
                        logger.info(f"  - ⚠️ Sin datos RugCheck: {token_address}")
                        # si quieres forzar descarte tras X tiempo, puedes hacerlo aquí
                        await asyncio.sleep(1)
                        continue

                    dex_passes, dex_reason = passes_dexscreener_filters(dex_data)
                    rugcheck_passes, rugcheck_reason = passes_rugcheck_filters(rugcheck_data)

                    if dex_passes and rugcheck_passes:
                        approved_at = now_ts
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        watch_data = {
                            'approved_at': approved_at,
                            'last_notified': 'initial',
                            'meta': dex_data,
                            'rugcheck': rugcheck_data,
                            'liquidity': liquidity
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)
                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)

                        # mensaje enriquecido
                        risk = rugcheck_data.get('risk') or rugcheck_data.get('scoreTag') or 'N/A'
                        locked_liq = rugcheck_data.get('lockedLiquidity') or rugcheck_data.get('liquidityLocked') or rugcheck_data.get('locked_liquidity', False)
                        pair_created_at = dex_data.get('pairCreatedAt') or dex_data.get('createdAt')
                        age_days = "N/A"
                        if pair_created_at:
                            try:
                                ts = float(pair_created_at)
                                if ts > 1e12:
                                    ts = ts / 1000.0
                                age_days = f"{(now_ts - ts) / 86400:.1f}"
                            except Exception:
                                pass
                        message = (
                            f"✅ *OPORTUNIDAD CONFIRMADA - Jupiter Radar*\n\n"
                            f"*Token:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}` USD\n"
                            f"*Edad:* `{age_days}` días\n"
                            f"*Liquidez Bloqueada:* `{'✅ SÍ' if locked_liq else '❌ NO'}`\n"
                            f"*Riesgo RugCheck:* `{risk}`\n\n"
                            f"🔍 Verificación rápida:\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n\n"
                            f"⚠️ *Realiza tu debido análisis antes de invertir*"
                        )
                        if TARGET_CHAT_ID:
                            try:
                                await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode='Markdown', disable_web_page_preview=True)
                            except Exception as e:
                                logger.warning(f"No se pudo enviar notificación Telegram: {e}")
                        logger.info(f"  - 🏆 PROMOVIDO a watchlist: {token_address}")
                    else:
                        reasons = []
                        if not dex_passes:
                            reasons.append(f"Dex: {dex_reason}")
                        if not rugcheck_passes:
                            reasons.append(f"RugCheck: {rugcheck_reason}")
                            # Loguear el objeto rugcheck para inspección si es inesperado
                            logger.debug(f"RugCheck data (debug) for {token_address}: {json.dumps(rugcheck_data)[:800]}")
                        logger.info(f"  - ❌ Rechazado: {token_address} - {' | '.join(reasons)}")
                        if now_ts - data['found_at'] > 7200:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Vigilante de incubadora detenido.")
                break
            except Exception as e:
                logger.error(f"Error en vigilante de incubadora: {e}")
                await asyncio.sleep(10)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(300)
                if not watchlist:
                    continue
                now = time.time()
                for token_address, data in list(watchlist.items()):
                    approved_at = data.get('approved_at', 0)
                    last_notified = data.get('last_notified', 'initial')
                    age_hours = (now - approved_at) / 3600
                    notify_periods = {'initial': 24, '24hr': 72, '72hr': 96}
                    if last_notified in notify_periods and age_hours >= notify_periods[last_notified]:
                        dex_data = await get_dexscreener_data(client, token_address)
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
                                await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode='Markdown')
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

# -------------------- TELEGRAM COMMANDS --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "👋 *Bot Jupiter Radar Activado*\n\n"
        "🔍 *Comandos disponibles:*\n"
        "/cazar - Iniciar monitoreo\n"
        "/parar - Detener monitoreo\n"
        "/status - Estado actual\n"
        "/incubadora - Ver tokens en incubación\n"
        "/watchlist - Ver tokens aprobados\n\n"
        f"🚀 *Filtros activos:* Liquidez bloqueada + Riesgo bajo + Liquidez ≥ ${LIQUIDITY_THRESHOLD:,}\n"
        f"⏱ *Ventana de nuevo:* {NEW_WINDOW_SECONDS/3600:.0f} horas",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot ya está cazando.")
        return
    await update.message.reply_text("🏹 *Iniciando Radar de Jupiter...*", parse_mode='Markdown')
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()
    context.bot_data['tasks'] = [
        asyncio.create_task(jupiter_radar_task(context)),
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
    await update.message.reply_text("🛑 *Caza detenida.*", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está *Detenido*."
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ *Bot Activo - Jupiter Radar*\n\n"
            f"🐣 *Incubadora:* `{len(incubator)}` tokens\n"
            f"🏆 *Watchlist:* `{len(watchlist)}` tokens\n"
            f"🔍 *Filtros activos:*\n"
            f"   • Liquidez bloqueada (RugCheck)\n"
            f"   • Riesgo bajo\n"
            f"   • Liquidez ≥ ${LIQUIDITY_THRESHOLD:,}\n"
            f"   • Ventana: {NEW_WINDOW_SECONDS/3600:.0f} horas"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía.")
        return
    message = f"🐣 *Tokens en Incubadora ({len(incubator)}):*\n\n"
    token_addresses = list(incubator.keys())
    tokens_to_show = token_addresses[-10:]
    tokens_to_show.reverse()
    for i, token_address in enumerate(tokens_to_show, 1):
        message += f"{i}. `{token_address}`\n"
    if len(incubator) > 10:
        message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 La watchlist está vacía.")
        return
    message = f"🏆 *Tokens en Watchlist ({len(watchlist)}):*\n\n"
    token_addresses = list(watchlist.keys())
    tokens_to_show = token_addresses[-15:]
    tokens_to_show.reverse()
    for i, token_address in enumerate(tokens_to_show, 1):
        data = watchlist[token_address]
        liquidity = data.get('liquidity', 0)
        risk = data.get('rugcheck', {}).get('risk', 'N/A')
        message += f"{i}. `{token_address}`\n   💰 ${liquidity:,.0f} | 🛡️ {risk}\n"
    if len(watchlist) > 15:
        message += f"\n... y {len(watchlist) - 15} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

# -------------------- BOOT --------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado.")
        return
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("incubadora", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    logger.info("--- Bot Jupiter Radar listo. Ejecutando polling... ---")
    application.run_polling()

if __name__ == '__main__':
    main()
