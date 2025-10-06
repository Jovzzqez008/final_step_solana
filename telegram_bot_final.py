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

load_dotenv()

# -------------------- CONFIG --------------------
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")  # conservado por compatibilidad
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# Ajustes operativos
JUPITER_TOKENS_ENDPOINT = "https://api.jup.ag/markets/v1/tokens"
DEXSCREENER_TOKEN_INFO = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

# Umbral de liquidez para pasar a watchlist (USD)
LIQUIDITY_THRESHOLD = 7500
# Ventana de "nuevo" en segundos (12 horas)
NEW_WINDOW_SECONDS = 12 * 3600

# Estructuras en memoria (se sincronizan con la DB al iniciar)
incubator: Dict[str, Dict[str, Any]] = {}
watchlist: Dict[str, Dict[str, Any]] = {}

# Logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- DATABASE HELPERS --------------------
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
    if not DATABASE_URL:
        return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data))
    finally:
        await conn.close()

async def db_remove_from_incubator(token_address: str):
    if not DATABASE_URL:
        return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)
    finally:
        await conn.close()

async def db_load_all_incubator() -> Dict[str, dict]:
    if not DATABASE_URL:
        return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM incubator")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally:
        await conn.close()

async def db_add_to_watchlist(token_address: str, data: dict):
    if not DATABASE_URL:
        return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data))
    finally:
        await conn.close()

async def db_load_all_watchlist() -> Dict[str, dict]:
    if not DATABASE_URL:
        return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally:
        await conn.close()

# -------------------- EXTERNAL API HELPERS --------------------
async def get_jupiter_tokens(client: httpx.AsyncClient):
    """
    Consulta tokens desde la API de Jupiter y devuelve aquellos que tengan fecha de listing
    dentro de la ventana NEW_WINDOW_SECONDS. La API de jup.ag devuelve objetos con timestamps
    en algunos campos; si no vienen, hacemos lo mejor posible con la información disponible.
    """
    try:
        res = await client.get(JUPITER_TOKENS_ENDPOINT, timeout=20)
        if res.status_code != 200:
            logger.warning(f"Jupiter responded {res.status_code}")
            return []
        data = res.json()
        tokens = data.get('data') or data.get('tokens') or data.get('results') or []
        now_ts = time.time()
        recent = []
        for t in tokens:
            # La estructura exacta puede variar; intentamos obtener un timestamp razonable
            created_ts = None
            # Common fields to check (depends on endpoint version)
            if isinstance(t, dict):
                created_ts = t.get('listedAt') or t.get('createdAt') or t.get('pairCreatedAt')
                # algunos endpoints traen timestamps en ms
                if created_ts and created_ts > 1e12:
                    created_ts = created_ts / 1000.0
            if created_ts:
                age = now_ts - float(created_ts)
                if 0 <= age <= NEW_WINDOW_SECONDS:
                    recent.append(t)
            else:
                # Si no hay timestamp, aplicar heurística: incluir tokens con liquidez baja/no listados
                # (esto reduce falsos negativos; igualmente el filtro de incubadora sigue vigente)
                recent.append(t)
        logger.info(f"[JUPITER] Tokens obtenidos: {len(recent)} (filtrados por ventana)")
        return recent
    except Exception as e:
        logger.error(f"Error consultando Jupiter: {e}")
        return []

async def get_dexscreener_data(client: httpx.AsyncClient, token_address: str):
    url = DEXSCREENER_TOKEN_INFO.format(mint=token_address)
    try:
        res = await client.get(url, timeout=12)
        if res.status_code == 200 and res.json().get('pairs'):
            # Escogemos el par con mayor liquidez reportada
            pairs = res.json()['pairs']
            best = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
            return best
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

# -------------------- TAREAS ASÍNCRONAS PRINCIPALES --------------------
async def jupiter_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Tarea principal: consulta periódica a Jupiter y agrega tokens nuevos a la incubadora."""
    logger.info("Iniciando Radar de Jupiter...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                recent_tokens = await get_jupiter_tokens(client)
                if not recent_tokens:
                    logger.info("[RADAR] No se obtuvieron tokens en este ciclo.")
                else:
                    for entry in recent_tokens:
                        # intentamos normalizar una dirección de mint/base token
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
                await asyncio.sleep(300)  # 5 minutos entre consultas
            except asyncio.CancelledError:
                logger.info("Radar de Jupiter detenido.")
                break
            except Exception as e:
                logger.error(f"Error en Radar de Jupiter: {e}")
                await asyncio.sleep(15)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica tokens en incubadora: consulta DexScreener y promueve a watchlist si cumplen liquidez."""
    logger.info("Iniciando Vigilante de la Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(180)  # cada 3 minutos
                if not incubator:
                    continue
                now_ts = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                for token_address, data in list(incubator.items()):
                    dex_data = await get_dexscreener_data(client, token_address)
                    if not dex_data:
                        # Si no hay datos y pasó 1 hora desde encontrado, descartamos
                        if now_ts - data['found_at'] > 3600:
                            logger.info(f"  - 🗑️ Descartado (sin datos): {token_address}")
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                    logger.info(f"  - {token_address[:8]}... Liquidez: ${liquidity:,.2f}")
                    if liquidity >= LIQUIDITY_THRESHOLD:
                        # Promover a watchlist
                        approved_at = now_ts
                        watch_data = {'approved_at': approved_at, 'last_notified': 'initial', 'meta': dex_data}
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        # eliminar de incubadora
                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)

                        # notificar a Telegram
                        message = (
                            f"✅ *Alerta de Oportunidad (Jupiter Radar)*\n\n"
                            f"*Mint:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}` USD\n\n"
                            "🚨 Realiza tu análisis de seguridad manual en RugCheck."
                        )
                        if TARGET_CHAT_ID:
                            try:
                                await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode='Markdown')
                            except Exception as e:
                                logger.warning(f"No se pudo enviar notificación Telegram: {e}")

                # fin for incubator
            except asyncio.CancelledError:
                logger.info("Vigilante de incubadora detenido.")
                break
            except Exception as e:
                logger.error(f"Error en vigilante de incubadora: {e}")
                await asyncio.sleep(10)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Monitorea tokens aprobados y envía reportes periódicos (24h,72h,96h)."""
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
                        # Obtener datos frescos de DexScreener
                        dex_data = await get_dexscreener_data(client, token_address)
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0) if dex_data else 0
                        price_change_24h = dex_data.get('priceChange', {}).get('h24', 0) if dex_data else 0
                        message = (
                            f"🔔 *Reporte ({notify_periods[last_notified]}h)*\n\n"
                            f"*Mint:* `{token_address}`\n"
                            f"*Liq. Actual:* `${liquidity:,.2f}` USD\n"
                            f"*Cambio 24h:* `{price_change_24h}%`"
                        )
                        if TARGET_CHAT_ID:
                            try:
                                await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode='Markdown')
                            except Exception as e:
                                logger.warning(f"No se pudo enviar reporte Telegram: {e}")

                        # actualizar estado
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
    await update.message.reply_text("👋 Bot listo. Usa /cazar, /parar, /status, /incubadora, /watchlist.")

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot ya está cazando.")
        return
    await update.message.reply_text("🏹 Iniciando Radar de Jupiter y monitores...")
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    # Crear client HTTP compartido
    context.bot_data['client'] = httpx.AsyncClient()
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
    if client := context.bot_data.get('client'):
        await client.aclose()
    context.bot_data.clear()
    await update.message.reply_text("🛑 Caza detenida.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está *Detenido*."
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ El bot está *Activo* (Radar Jupiter).\n"
            f"🐣 *{len(incubator)}* tokens en incubadora.\n"
            f"🏆 *{len(watchlist)}* tokens en watchlist.\n"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía.")
        return
    message = f"🐣 *Últimos Tokens en Incubadora ({len(incubator)}):*\n\n"
    token_addresses = list(incubator.keys())
    tokens_to_show = token_addresses[-10:]
    tokens_to_show.reverse()
    for token_address in tokens_to_show:
        message += f"- `{token_address}`\n"
    if len(incubator) > 10:
        message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 La watchlist está vacía.")
        return
    message = f"🏆 *Últimos Tokens en Watchlist ({len(watchlist)}):*\n\n"
    token_addresses = list(watchlist.keys())
    tokens_to_show = token_addresses[-15:]
    tokens_to_show.reverse()
    for token_address in tokens_to_show:
        message += f"- `{token_address}`\n"
    if len(watchlist) > 15:
        message += f"\n... y {len(watchlist) - 15} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

# -------------------- BOOT --------------------

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado en las variables de entorno.")
        return
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("incubadora", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))

    logger.info("--- Bot listo. Ejecutando polling... ---")
    application.run_polling()

if __name__ == '__main__':
    main()
