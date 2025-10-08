# telegram_bot_final8.py  (modificado con Birdeye y delay de 8 segundos)
import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any, List
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# -------------------- CONFIG --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# FUENTES CONFIABLES - JUPITER V2 + GECKOTERMINAL + BIRDEYE
# Jupiter Token API v2 (lite)
JUPITER_BASE_LITE = "https://lite-api.jup.ag/tokens/v2"
JUPITER_V2_RECENT = f"{JUPITER_BASE_LITE}/recent"
JUPITER_V2_TRENDING = f"{JUPITER_BASE_LITE}/toptrending/1h"
JUPITER_V2_ORGANIC = f"{JUPITER_BASE_LITE}/toporganicscore/1h"

# GeckoTerminal API base
GECKO_API_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_NEW_POOLS = f"{GECKO_API_BASE}/networks/solana/new_pools"
GECKO_TOKEN_INFO = f"{GECKO_API_BASE}/networks/solana/tokens/{{}}/info"

# Birdeye API
BIRDEYE_API = "https://public-api.birdeye.so/public/token?address={}"
BIRDEYE_PRICE_API = "https://public-api.birdeye.so/public/price?address={}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# -------------------- FILTROS (ajustables) --------------------
# Edad en horas — ahora 6 a 120 horas según tu pedido
MIN_AGE_HOURS = 6
MAX_AGE_HOURS = 120

# Liquidez mínima para entrada a incubadora (puedes bajarlo)
MIN_LIQUIDITY = 10000  # $10,000 mínimo para considerar
# Requisitos para mover de incubadora -> watchlist
WATCHLIST_LIQUIDITY_THRESHOLD = 100000  # $100k para pasar a watchlist
WATCHLIST_MARKETCAP_THRESHOLD = 200000  # $200k marketcap (si decides usar marketcap)
# Al final de 5 días, si liquidez < LIQUIDITY_DISCARD_THRESHOLD se borra
LIQUIDITY_DISCARD_THRESHOLD = 35000

# Monitoreo periódicos
INCUBATOR_CHECK_INTERVAL = 60 * 60 * 4  # 4 horas
WATCHLIST_CHECK_INTERVAL = 60 * 60 * 4  # 4 horas
RADAR_LOOP_INTERVAL = 60  # 1 minuto entre búsquedas combinadas (ajustable)

# Tiempo entre revisión de cada token (8 segundos para Birdeye)
TOKEN_CHECK_DELAY = 8

# Elección de criterio: usar liquidity, marketcap, o ambos (combinación lógica)
# Opciones:
#   "liquidity_only", "marketcap_only", "either" (liquidity OR marketcap),
#   "both" (liquidity AND marketcap)
CRITERIA_MODE = "either"

# Estructuras en memoria
incubator: Dict[str, Dict[str, Any]] = {}
watchlist: Dict[str, Dict[str, Any]] = {}

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE --------------------
async def setup_database():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL configurada.")
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

async def db_remove_from_watchlist(token_address: str):
    if not DATABASE_URL: return
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DELETE FROM watchlist WHERE token_address = $1", token_address)
    finally: await conn.close()

async def db_load_all_watchlist() -> Dict[str, dict]:
    if not DATABASE_URL: return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally: await conn.close()

# -------------------- UTILIDADES DE EDAD --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Devuelve edad en horas (float). Soporta ISO timestamps con Z."""
    try:
        if not created_at_str:
            return None
        created_at_str = created_at_str.replace('Z', '+00:00')
        created_dt = datetime.fromisoformat(created_at_str)
        current_dt = datetime.utcnow().replace(tzinfo=created_dt.tzinfo)
        age_seconds = (current_dt - created_dt).total_seconds()
        return age_seconds / 3600.0
    except Exception as e:
        logger.debug(f"Error calculando edad para {created_at_str}: {e}")
        return None

def is_token_in_age_range(age_hours: float) -> bool:
    if age_hours is None:
        return False
    return MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS

def format_age_display(age_hours: float) -> str:
    if age_hours is None:
        return "N/A"
    if age_hours < 1:
        minutes = age_hours * 60
        return f"{minutes:.0f}min"
    else:
        return f"{age_hours:.1f}h"

# -------------------- FUENTES: GeckoTerminal & Jupiter & Birdeye --------------------
async def get_geckoterminal_new_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pools nuevos de GeckoTerminal."""
    try:
        logger.info("Consultando GeckoTerminal new_pools...")
        res = await client.get(GECKO_NEW_POOLS, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"GeckoTerminal new_pools responded {res.status_code}")
            return []
        data = res.json()
        pools = data.get('data', [])
        res_tokens = []
        for pool in pools:
            try:
                attributes = pool.get('attributes', {})
                token_address = attributes.get('base_token_address') or attributes.get('base_token', {}).get('address')
                if not token_address:
                    continue
                created_at = attributes.get('pool_created_at') or attributes.get('created_at') or attributes.get('createdAt')
                age_hours = calculate_token_age(created_at) if created_at else None
                if not is_token_in_age_range(age_hours):
                    continue
                liquidity = float(attributes.get('reserve_in_usd', 0) or 0)
                # omitimos tokens con liquidez 0 o muy baja
                if liquidity < MIN_LIQUIDITY:
                    continue
                base_token = attributes.get('base_token', {})
                res_tokens.append({
                    'address': token_address,
                    'name': base_token.get('name',''),
                    'symbol': base_token.get('symbol',''),
                    'liquidity': liquidity,
                    'age_hours': age_hours,
                    'created_at': created_at,
                    'marketcap': float(base_token.get('market_cap_usd', 0) or 0),
                    'holders': int(base_token.get('holders', 0) or 0),
                    'source': 'geckoterminal'
                })
            except Exception as e:
                logger.debug(f"Error procesando pool Gecko: {e}")
                continue
        logger.info(f"[GECKO] Encontrados {len(res_tokens)} tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h")
        return res_tokens
    except Exception as e:
        logger.error(f"Error GeckoTerminal: {e}")
        return []

async def get_jupiter_recent_tokens_improved(client: httpx.AsyncClient) -> List[Dict]:
    """Consulta Jupiter lite /recent y filtra por edad y liquidez."""
    try:
        logger.info("Consultando Jupiter V2 recent...")
        res = await client.get(JUPITER_V2_RECENT, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            logger.warning(f"Jupiter recent responded {res.status_code}")
            return []
        tokens = res.json()
        processed = []
        for token in tokens:
            try:
                # Jupiter puede devolver 'firstPool' u otros campos
                first_pool = token.get('firstPool') or token.get('first_pool') or {}
                created_at = first_pool.get('createdAt') or first_pool.get('created_at')
                age_hours = calculate_token_age(created_at) if created_at else None
                if not is_token_in_age_range(age_hours):
                    continue
                liquidity = float(token.get('liquidity') or 0)
                if liquidity < MIN_LIQUIDITY:
                    continue
                processed.append({
                    'address': token.get('id'),
                    'name': token.get('name',''),
                    'symbol': token.get('symbol',''),
                    'liquidity': liquidity,
                    'age_hours': age_hours,
                    'created_at': created_at,
                    'marketcap': float(token.get('marketCapUsd') or token.get('fdvUsd') or 0),
                    'holders': int(token.get('holderCount') or 0),
                    'source': 'jupiter'
                })
            except Exception as e:
                logger.debug(f"Error procesando token Jupiter: {e}")
                continue
        logger.info(f"[JUPITER] Encontrados {len(processed)} tokens en rango")
        return processed
    except Exception as e:
        logger.error(f"Error Jupiter recent: {e}")
        return []

async def get_token_info_gecko(client: httpx.AsyncClient, address: str) -> Dict:
    """Pide info detallada de token a GeckoTerminal (marketcap, holders, liquidez de pools)."""
    try:
        url = GECKO_TOKEN_INFO.format(address)
        res = await client.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            logger.debug(f"Gecko token info {address} responded {res.status_code}")
            return {}
        data = res.json().get('data', {})
        attributes = data.get('attributes', {})
        token_info = attributes.get('token', {})
        # Se retorna un dict con campos útiles
        return {
            'liquidity': float(attributes.get('reserve_in_usd', 0) or 0),
            'marketcap': float(token_info.get('market_cap_usd', 0) or 0),
            'holders': int(token_info.get('holders', 0) or 0),
            'symbol': token_info.get('symbol',''),
            'name': token_info.get('name',''),
            'price_usd': float(token_info.get('price_usd', 0) or 0)
        }
    except Exception as e:
        logger.debug(f"Error get_token_info_gecko {address}: {e}")
        return {}

async def get_token_info_birdeye(client: httpx.AsyncClient, address: str) -> Dict:
    """Obtiene información del token desde Birdeye API con delay de 8 segundos."""
    try:
        # Delay de 8 segundos antes de cada consulta a Birdeye
        await asyncio.sleep(TOKEN_CHECK_DELAY)
        
        url = BIRDEYE_API.format(address)
        res = await client.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            logger.debug(f"Birdeye token info {address} responded {res.status_code}")
            return {}
        
        data = res.json().get('data', {})
        return {
            'liquidity': float(data.get('liquidity', 0) or 0),
            'marketcap': float(data.get('market_cap', 0) or 0),
            'holders': int(data.get('holders', 0) or 0),
            'symbol': data.get('symbol', ''),
            'name': data.get('name', ''),
            'price_usd': float(data.get('price', 0) or 0),
            'volume_24h': float(data.get('volume24h', 0) or 0)
        }
    except Exception as e:
        logger.debug(f"Error get_token_info_birdeye {address}: {e}")
        return {}

# -------------------- LÓGICA: INCUBADORA -> WATCHLIST --------------------
def qualifies_for_watchlist(metrics: Dict[str, Any]) -> bool:
    """
    Decide si un token califica para pasar a watchlist según CRITERIA_MODE:
    - liquidity_only: líquido >= WATCHLIST_LIQUIDITY_THRESHOLD
    - marketcap_only: marketcap >= WATCHLIST_MARKETCAP_THRESHOLD
    - either: liquidity >= threshold OR marketcap >= threshold
    - both: liquidity >= threshold AND marketcap >= threshold
    """
    liq = metrics.get('liquidity', 0) or 0
    mcap = metrics.get('marketcap', 0) or 0

    if CRITERIA_MODE == "liquidity_only":
        return liq >= WATCHLIST_LIQUIDITY_THRESHOLD
    if CRITERIA_MODE == "marketcap_only":
        return mcap >= WATCHLIST_MARKETCAP_THRESHOLD
    if CRITERIA_MODE == "both":
        return (liq >= WATCHLIST_LIQUIDITY_THRESHOLD) and (mcap >= WATCHLIST_MARKETCAP_THRESHOLD)
    # default either
    return (liq >= WATCHLIST_LIQUIDITY_THRESHOLD) or (mcap >= WATCHLIST_MARKETCAP_THRESHOLD)

# -------------------- TAREAS PRINCIPALES --------------------
async def get_all_tokens_combined(client: httpx.AsyncClient) -> List[Dict]:
    """Combina Jupiter + GeckoTerminal para buscar tokens nuevos en rango de edad."""
    tasks = [
        get_jupiter_recent_tokens_improved(client),
        get_geckoterminal_new_pairs(client)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_tokens = []
    for r in results:
        if isinstance(r, list):
            all_tokens.extend(r)
    # dedupe por address
    unique = {}
    for t in all_tokens:
        addr = t.get('address')
        if addr and addr not in unique:
            unique[addr] = t
    logger.info(f"🎯 TOTAL tokens únicos ({MIN_AGE_HOURS}h-{MAX_AGE_HOURS}h, ≥${MIN_LIQUIDITY:,}): {len(unique)}")
    return list(unique.values())

async def combined_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar combinado: detecta tokens y los mete en incubadora si cumplen edad+liquidez."""
    logger.info("🚀 Iniciando Radar Combinado (incubadora)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_tokens_combined(client)
                if not tokens:
                    logger.info("No tokens encontrados en este ciclo.")
                else:
                    added = 0
                    for token in tokens:
                        address = token.get('address')
                        if not address: continue
                        # Si ya está en watchlist, ignorar
                        if address in watchlist:
                            continue
                        # Si ya está en incubator, actualizar timestamp y seguir
                        if address in incubator:
                            # update basic token_info
                            incubator[address]['token_info'] = token
                            await db_add_to_incubator(address, incubator[address])
                            continue
                        # Crear entrada en incubadora
                        created = time.time()
                        incub_data = {
                            'added_at': created,
                            'token_info': token,
                            'source': token.get('source','combined')
                        }
                        incubator[address] = incub_data
                        await db_add_to_incubator(address, incub_data)
                        added += 1

                        # Notificar que entró a incubadora (igual que antes, con enlaces)
                        symbol = token.get('symbol','N/A')
                        name = token.get('name','N/A')
                        age_str = format_age_display(token.get('age_hours'))
                        liquidity = token.get('liquidity', 0)
                        message = (
                            f"🟡 *INCUBADORA:* Token en rango (6-120h)\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Name:* {name}\n"
                            f"*Address:* `{address}`\n"
                            f"*Edad:* {age_str}\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n\n"
                            f"🔗 Verificar:\n"
                            f"- DexScreener: https://dexscreener.com/solana/{address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{address}\n"
                            f"- Birdeye: https://birdeye.so/token/{address}?chain=solana\n\n"
                            f"⏳ Se monitoreará cada {INCUBATOR_CHECK_INTERVAL//3600} horas para ver si pasa a watchlist."
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
                                logger.warning(f"No se pudo enviar notificación incubadora: {e}")

                    if added > 0:
                        logger.info(f"  - Añadidos a incubadora: {added}")

                await asyncio.sleep(RADAR_LOOP_INTERVAL)
            except Exception as e:
                logger.error(f"Error en combined_radar_task: {e}")
                await asyncio.sleep(30)

# -------------------- MONITOREO INCUBADORA -> WATCHLIST --------------------
async def incubator_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Revisa incubadora cada 4 horas y mueve a watchlist si cumple thresholds usando Birdeye."""
    logger.info("Iniciando monitor de incubadora (cada 4h) con Birdeye...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                now = time.time()
                moved = 0
                for addr, data in list(incubator.items()):
                    try:
                        # obtener metrics en tiempo real desde Birdeye
                        birdeye_metrics = await get_token_info_birdeye(client, addr)
                        
                        # fallback a datos existentes si Birdeye falla
                        token_info = data.get('token_info', {})
                        metrics = {
                            'liquidity': birdeye_metrics.get('liquidity') or token_info.get('liquidity') or 0,
                            'marketcap': birdeye_metrics.get('marketcap') or token_info.get('marketcap') or 0,
                            'holders': birdeye_metrics.get('holders') or token_info.get('holders') or 0
                        }
                        
                        # Si califica
                        if qualifies_for_watchlist(metrics):
                            approved_at = time.time()
                            watch_data = {
                                'approved_at': approved_at,
                                'token_info': {
                                    **token_info,
                                    **metrics,
                                    **birdeye_metrics
                                },
                                'source': data.get('source', 'incubator')
                            }
                            watchlist[addr] = watch_data
                            await db_add_to_watchlist(addr, watch_data)
                            # eliminar de incubadora
                            del incubator[addr]
                            await db_remove_from_incubator(addr)
                            moved += 1

                            # Notificar ingreso a watchlist con métricas actuales de Birdeye
                            message = (
                                f"🟢 *TOKEN A WATCHLIST* 🟢\n\n"
                                f"*Address:* `{addr}`\n"
                                f"*Liquidez:* `${metrics['liquidity']:,.2f}`\n"
                                f"*MarketCap:* `${metrics['marketcap']:,.2f}`\n"
                                f"*Holders:* {metrics['holders']}\n"
                                f"*Volumen 24h:* `${birdeye_metrics.get('volume_24h', 0):,.2f}`\n\n"
                                f"🔗 Revisa:\n"
                                f"- DexScreener: https://dexscreener.com/solana/{addr}\n"
                                f"- RugCheck: https://rugcheck.xyz/tokens/{addr}\n"
                                f"- Birdeye: https://birdeye.so/token/{addr}?chain=solana\n"
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
                                    logger.warning(f"No se pudo notificar watchlist: {e}")

                        else:
                            # actualizar datos en incubator con info de Birdeye
                            incubator[addr]['last_checked'] = now
                            incubator[addr]['last_metrics'] = metrics
                            incubator[addr]['birdeye_data'] = birdeye_metrics
                            await db_add_to_incubator(addr, incubator[addr])
                            
                    except Exception as e:
                        logger.debug(f"Error procesando incubator token {addr}: {e}")
                        continue

                if moved > 0:
                    logger.info(f"🟢 Movidos a watchlist: {moved}")

                await asyncio.sleep(INCUBATOR_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error incubator_monitor_task: {e}")
                await asyncio.sleep(60)

# -------------------- MONITOREO WATCHLIST --------------------
async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Monitorea watchlist cada 4 horas usando Birdeye API."""
    logger.info("Iniciando monitor de watchlist (cada 4h) con Birdeye...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                now = time.time()
                removals = 0
                for addr, data in list(watchlist.items()):
                    try:
                        # Obtener métricas actualizadas desde Birdeye
                        birdeye_metrics = await get_token_info_birdeye(client, addr)
                        
                        liq = birdeye_metrics.get('liquidity') or data.get('token_info', {}).get('liquidity') or 0
                        mcap = birdeye_metrics.get('marketcap') or data.get('token_info', {}).get('marketcap') or 0
                        holders = birdeye_metrics.get('holders') or data.get('token_info', {}).get('holders') or 0
                        volume_24h = birdeye_metrics.get('volume_24h', 0)

                        # actualizar en memoria y DB con datos de Birdeye
                        watchlist[addr]['token_info'].update({
                            'liquidity': liq,
                            'marketcap': mcap,
                            'holders': holders,
                            'volume_24h': volume_24h,
                            'last_checked': now,
                            'birdeye_data': birdeye_metrics
                        })
                        await db_add_to_watchlist(addr, watchlist[addr])

                        # enviar resumen breve al canal con datos de Birdeye
                        age_days = (now - data.get('approved_at', now)) / 86400.0
                        message = (
                            f"📈 *WATCHLIST UPDATE (Birdeye)*\n\n"
                            f"`{addr}`\n"
                            f"- Liquidez: `${liq:,.2f}`\n"
                            f"- MarketCap: `${mcap:,.2f}`\n"
                            f"- Holders: {holders}\n"
                            f"- Volumen 24h: `${volume_24h:,.2f}`\n"
                            f"- Tiempo en watchlist: {age_days:.2f} días\n"
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
                                logger.debug(f"No se envió update watchlist: {e}")

                        # regla de eliminación post-5-días
                        if now - data.get('approved_at', 0) > 5 * 86400:
                            if liq < LIQUIDITY_DISCARD_THRESHOLD:
                                # eliminar
                                del watchlist[addr]
                                await db_remove_from_watchlist(addr)
                                removals += 1
                                if TARGET_CHAT_ID:
                                    try:
                                        await context.bot.send_message(
                                            chat_id=TARGET_CHAT_ID,
                                            text=f"🗑️ *Eliminado de watchlist* `{addr}` — liquidez ${liq:,.0f} < ${LIQUIDITY_DISCARD_THRESHOLD:,}",
                                            parse_mode='Markdown'
                                        )
                                    except:
                                        pass
                    except Exception as e:
                        logger.debug(f"Error watchlist monitor {addr}: {e}")
                        continue

                if removals > 0:
                    logger.info(f"🧹 Remove from watchlist: {removals}")

                await asyncio.sleep(WATCHLIST_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error watchlist_monitor_task: {e}")
                await asyncio.sleep(60)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Mejorado - Incubadora & Watchlist*\n\n"
        f"🎯 *Edad buscada:* {MIN_AGE_HOURS} a {MAX_AGE_HOURS} horas\n"
        f"💧 *Liquidez mínima para incubadora:* ${MIN_LIQUIDITY:,}\n"
        f"🟡 *Incubadora -> Watchlist:* Liquidez ≥ ${WATCHLIST_LIQUIDITY_THRESHOLD:,} (o marketcap según configuración)\n"
        "🔁 *Monitoreo incubadora/watchlist cada 4 horas*\n"
        f"🔍 *Fuente monitoreo:* Birdeye API\n"
        f"⏰ *Delay Birdeye:* {TOKEN_CHECK_DELAY} segundos\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual\n"
        "*/incubadora* - Ver tokens en incubadora\n"
        "*/watchlist* - Ver tokens en watchlist",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return

    await update.message.reply_text("🏹 *Iniciando Radar Combinado + Incubadora...*", parse_mode='Markdown')

    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    # lanzar tareas: radar, incubator monitor, watchlist monitor, cleanup
    context.bot_data['tasks'] = [
        asyncio.create_task(combined_radar_task(context)),
        asyncio.create_task(incubator_monitor_task(context)),
        asyncio.create_task(watchlist_monitor_task(context)),
        asyncio.create_task(cleanup_task(context))
    ]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 No está cazando.")
        return

    for task in context.bot_data['tasks']:
        task.cancel()
    context.bot_data.clear()
    await update.message.reply_text("🛑 Caza detenida.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 Bot detenido"
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ *Radar Activo*\n\n"
            f"🟡 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Edad buscada:* {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h\n"
            f"⚖️ *Criterio:* {CRITERIA_MODE}\n"
            f"🔗 *Fuente monitoreo:* Birdeye API\n"
            f"⏰ *Delay Birdeye:* {TOKEN_CHECK_DELAY} segundos\n"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🟡 Incubadora vacía")
        return
    sorted_inc = sorted(incubator.items(), key=lambda x: x[1].get('added_at',0), reverse=True)
    message = f"🟡 *Incubadora ({len(incubator)}):*\n\n"
    for i, (addr, data) in enumerate(list(sorted_inc)[:20], 1):
        token_info = data.get('token_info', {})
        liq = token_info.get('liquidity', 0)
        age = token_info.get('age_hours', 'N/A')
        age_str = format_age_display(age)
        birdeye_liq = data.get('birdeye_data', {}).get('liquidity', 0)
        message += f"{i}. `{addr}`\n   📛 {token_info.get('symbol','N/A')} | 💰 ${liq:,.0f} | 🕒 {age_str}"
        if birdeye_liq:
            message += f" | 🔄 ${birdeye_liq:,.0f}\n"
        else:
            message += "\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    sorted_watch = sorted(watchlist.items(), key=lambda x: x[1].get('approved_at', 0), reverse=True)
    message = f"🏆 *Tokens en Watchlist ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(sorted_watch)[:20], 1):
        ti = data.get('token_info', {})
        liq = ti.get('liquidity', 0)
        mcap = ti.get('marketcap', 0)
        holders = ti.get('holders', 0)
        volume_24h = ti.get('volume_24h', 0)
        age_days = (time.time() - data.get('approved_at', time.time())) / 86400.0
        message += (f"{i}. `{addr}`\n   📛 {ti.get('symbol','N/A')} | 💰 ${liq:,.0f} | 🏷 MC ${mcap:,.0f} | 👥 {holders} | 📊 ${volume_24h:,.0f} | ⏳ {age_days:.2f}d\n")
    await update.message.reply_text(message, parse_mode='Markdown')

# -------------------- LIMPIEZA AUTOMÁTICA (ajustada) --------------------
async def cleanup_task(context: ContextTypes.DEFAULT_TYPE):
    """Limpia tokens viejos en incubadora y watchlist según reglas."""
    logger.info("Iniciando tarea de limpieza...")
    while True:
        try:
            await asyncio.sleep(3600)  # Revisar cada 1 hora
            now = time.time()
            # Incubadora: eliminar entradas > 7 días (histórico)
            for token_address, data in list(incubator.items()):
                if now - data.get('added_at', 0) > 7 * 86400:
                    del incubator[token_address]
                    await db_remove_from_incubator(token_address)
            # Watchlist: no eliminamos automáticamente aquí (watchlist_monitor_task se encarga)
        except Exception as e:
            logger.error(f"Error en limpieza: {e}")

# -------------------- MAIN --------------------
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

    logger.info("--- Bot Incubadora/Watchlist listo ---")

    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
