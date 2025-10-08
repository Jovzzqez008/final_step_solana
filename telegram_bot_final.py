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

# FUENTES CONFIABLES
JUPITER_V2_RECENT = "https://lite-api.jup.ag/tokens/v2/recent"
JUPITER_V2_TRENDING = "https://lite-api.jup.ag/tokens/v2/toptrending/1h"
JUPITER_V2_ORGANIC = "https://lite-api.jup.ag/tokens/v2/toporganicscore/1h"
GECKO_NEW_PAIRS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
BIRDEYE_API = "https://public-api.birdeye.so/public/token?address={}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS MEJORADOS BASADOS EN PANDU Y DORK
MIN_LIQUIDITY = 100000  # $100,000 mínimo (balance entre PANDU y DORK)
MIN_AGE_HOURS = 24      # Mínimo 24 horas (tokens más establecidos)
MAX_AGE_HOURS = 72      # Máximo 72 horas (3 días)

# CONFIG INCUBADORA
INCUBATION_DAYS = 3                    # 3 días de incubación
CHECK_INTERVAL_HOURS = 11              # Verificación cada 11 horas
MIN_LIQUIDITY_DROP_PERCENT = 70        # Máximo 70% de caída de liquidez permitida

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

# -------------------- FUNCIONES DE ENLACES --------------------
def get_token_links(token_address: str) -> str:
    """Genera enlaces de verificación para el token"""
    return (
        f"🔍 *Verificar:*\n"
        f"- DexScreener: https://dexscreener.com/solana/{token_address}\n"
        f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n"
        f"- Birdeye: https://birdeye.so/token/{token_address}?chain=solana\n"
        f"- GeckoTerminal: https://www.geckoterminal.com/solana/pools/{token_address}\n"
        f"- Jupiter: https://jup.ag/swap/SOL-{token_address}\n"
        f"- Raydium: https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token_address}"
    )

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
                data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS watchlist (
                token_address TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
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

# -------------------- FILTROS DE EDAD MEJORADOS --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Calcula la edad del token en horas de forma más robusta"""
    try:
        if not created_at_str:
            return None
            
        created_at_str = created_at_str.replace('Z', '+00:00')
        created_dt = datetime.fromisoformat(created_at_str)
        current_dt = datetime.utcnow().replace(tzinfo=created_dt.tzinfo)
        age_seconds = (current_dt - created_dt).total_seconds()
        age_hours = age_seconds / 3600
        
        return age_hours
    except Exception as e:
        logger.debug(f"Error calculando edad para {created_at_str}: {e}")
        return None

def is_token_in_age_range(age_hours: float) -> bool:
    """Verifica si el token está en el rango de edad deseado"""
    if age_hours is None:
        return False
    return MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS

# -------------------- OBTENER DATOS ACTUALIZADOS --------------------
async def get_updated_token_data(client: httpx.AsyncClient, token_address: str) -> Dict[str, Any]:
    """Obtiene datos actualizados del token desde Birdeye"""
    try:
        url = BIRDEYE_API.format(token_address)
        res = await client.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('data'):
                token_data = data['data']
                return {
                    'liquidity': token_data.get('liquidity', 0),
                    'price_usd': token_data.get('price', 0),
                    'market_cap': token_data.get('market_cap', 0),
                    'volume_24h': token_data.get('volume24h', 0),
                    'price_change_24h': token_data.get('priceChange24h', 0)
                }
    except Exception as e:
        logger.debug(f"Error obteniendo datos actualizados para {token_address}: {e}")
    return {}

# -------------------- FUENTES DE DATOS --------------------
async def get_geckoterminal_new_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pools nuevos de GeckoTerminal"""
    try:
        logger.info("Consultando GeckoTerminal /new_pools...")
        res = await client.get(GECKO_NEW_PAIRS, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            data = res.json()
            pools = data.get('data', [])
            logger.info(f"[GECKO TERMINAL] Pools obtenidos: {len(pools)}")
            
            processed_tokens = []
            for pool in pools:
                try:
                    attributes = pool.get('attributes', {})
                    token_address = attributes.get('base_token_address', '')
                    
                    if not token_address:
                        continue
                    
                    # Calcular edad del pool
                    created_at = attributes.get('pool_created_at')
                    age_hours = calculate_token_age(created_at) if created_at else None
                    
                    # FILTRO DE EDAD
                    if not is_token_in_age_range(age_hours):
                        continue
                    
                    # Obtener datos del token
                    base_token = attributes.get('base_token', {})
                    liquidity = float(attributes.get('reserve_in_usd', 0))
                    
                    # FILTRO DE LIQUIDEZ
                    if liquidity < MIN_LIQUIDITY:
                        continue
                    
                    processed_tokens.append({
                        'address': token_address,
                        'name': base_token.get('name', ''),
                        'symbol': base_token.get('symbol', ''),
                        'liquidity': liquidity,
                        'age_hours': age_hours,
                        'created_at': created_at,
                        'price_usd': float(base_token.get('price_usd', 0)),
                        'fdv_usd': float(attributes.get('fdv_usd', 0)),
                        'volume_24h': float(attributes.get('volume_usd', {}).get('h24', 0)),
                        'price_change_24h': float(attributes.get('price_change_percentage', {}).get('h24', 0)),
                        'source': 'geckoterminal_new'
                    })
                    
                except Exception as e:
                    logger.debug(f"Error procesando pool de GeckoTerminal: {e}")
                    continue
            
            logger.info(f"[GECKO TERMINAL] Tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h + ≥${MIN_LIQUIDITY:,}: {len(processed_tokens)}")
            return processed_tokens
            
    except Exception as e:
        logger.error(f"Error GeckoTerminal: {e}")
    return []

async def get_jupiter_recent_tokens_improved(client: httpx.AsyncClient) -> List[Dict]:
    """Jupiter V2 con filtros"""
    try:
        logger.info("Consultando Jupiter V2 /recent...")
        res = await client.get(JUPITER_V2_RECENT, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 RECENT] Tokens obtenidos: {len(tokens)}")
            
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    # Calcular edad del token
                    age_hours = None
                    first_pool = token.get('firstPool', {})
                    if first_pool and first_pool.get('createdAt'):
                        age_hours = calculate_token_age(first_pool['createdAt'])
                    
                    # FILTRO DE EDAD
                    if not is_token_in_age_range(age_hours):
                        continue
                    
                    liquidity = token.get('liquidity', 0)
                    
                    # FILTRO DE LIQUIDEZ
                    if liquidity < MIN_LIQUIDITY:
                        continue
                        
                    processed_tokens.append({
                        'address': token['id'],
                        'name': token.get('name', ''),
                        'symbol': token.get('symbol', ''),
                        'liquidity': liquidity,
                        'age_hours': age_hours,
                        'first_pool_created': first_pool.get('createdAt'),
                        'organic_score': token.get('organicScore', 0),
                        'is_verified': token.get('isVerified', False),
                        'holder_count': token.get('holderCount', 0),
                        'price_usd': token.get('usdPrice', 0),
                        'source': 'jupiter_v2_recent'
                    })
            
            logger.info(f"[JUPITER V2 RECENT] Tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h + ≥${MIN_LIQUIDITY:,}: {len(processed_tokens)}")
            return processed_tokens
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 recent: {e}")
    return []

async def get_all_tokens_combined(client: httpx.AsyncClient) -> List[Dict]:
    """Combina múltiples fuentes con filtros estrictos"""
    all_tokens = []
    
    tasks = [
        get_jupiter_recent_tokens_improved(client),
        get_geckoterminal_new_pairs(client)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    # Eliminar duplicados por address
    unique_tokens = {}
    for token in all_tokens:
        addr = token['address']
        if addr and addr not in unique_tokens:
            unique_tokens[addr] = token
    
    logger.info(f"🎯 TOTAL tokens únicos ({MIN_AGE_HOURS}-{MAX_AGE_HOURS}h, ≥${MIN_LIQUIDITY:,}): {len(unique_tokens)}")
    
    return list(unique_tokens.values())

# -------------------- SISTEMA DE INCUBADORA --------------------
async def add_to_incubator(token: Dict, context: ContextTypes.DEFAULT_TYPE):
    """Agrega un token a la incubadora"""
    address = token['address']
    
    incubator_data = {
        'token_info': token,
        'added_at': time.time(),
        'initial_liquidity': token.get('liquidity', 0),
        'checks': [],
        'next_check': time.time() + CHECK_INTERVAL_HOURS * 3600,
        'status': 'incubating'
    }
    
    incubator[address] = incubator_data
    await db_add_to_incubator(address, incubator_data)
    
    # NOTIFICACIÓN DE AGREGADO A INCUBADORA
    symbol = token.get('symbol', 'N/A')
    name = token.get('name', 'N/A')
    age_hours = token.get('age_hours', 'N/A')
    age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
    liquidity = token.get('liquidity', 0)
    source = token.get('source', 'N/A')
    
    message = (
        f"🥚 *TOKEN AGREGADO A INCUBADORA* 🥚\n\n"
        f"*Symbol:* {symbol}\n"
        f"*Name:* {name}\n"
        f"*Address:* `{address}`\n"
        f"*Edad:* {age_str}\n"
        f"*Liquidez inicial:* `${liquidity:,.2f}`\n"
        f"*Fuente:* {source}\n\n"
        f"🔍 *Próxima verificación en {CHECK_INTERVAL_HOURS} horas*\n"
        f"⏰ *Período de incubación: {INCUBATION_DAYS} días*\n\n"
        f"{get_token_links(address)}\n\n"
        f"📊 *Monitorizando rugpulls...*"
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
            logger.warning(f"No se pudo enviar notificación de incubadora: {e}")

    logger.info(f"🥚 AGREGADO A INCUBADORA: {symbol} - ${liquidity:,.0f} liquidez")

async def incubator_check_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica los tokens en la incubadora cada 11 horas"""
    logger.info("🔍 Iniciando verificación de incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)  # Esperar 11 horas entre verificaciones
                
                if not incubator:
                    continue
                    
                now = time.time()
                tokens_to_remove = []
                tokens_to_promote = []
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados del token
                    current_data = await get_updated_token_data(client, token_address)
                    if not current_data:
                        continue
                    
                    current_liquidity = current_data.get('liquidity', 0)
                    initial_liquidity = data.get('initial_liquidity', 0)
                    
                    # Calcular porcentaje de cambio
                    if initial_liquidity > 0:
                        liquidity_change_percent = ((current_liquidity - initial_liquidity) / initial_liquidity) * 100
                    else:
                        liquidity_change_percent = 0
                    
                    # Registrar verificación
                    check_data = {
                        'timestamp': now,
                        'liquidity': current_liquidity,
                        'liquidity_change_percent': liquidity_change_percent,
                        'price_usd': current_data.get('price_usd', 0),
                        'market_cap': current_data.get('market_cap', 0)
                    }
                    data['checks'].append(check_data)
                    
                    # Verificar si ha pasado el período de incubación
                    incubation_elapsed = (now - data['added_at']) / (24 * 3600)  # en días
                    
                    if incubation_elapsed >= INCUBATION_DAYS:
                        # TOKEN HA PASADO LA INCUBACIÓN
                        tokens_to_promote.append((token_address, data, current_data))
                    else:
                        # ENVIAR REPORTE DE ESTADO
                        await send_incubator_status(context, token_address, data, current_data, incubation_elapsed)
                    
                    # Programar próxima verificación
                    data['next_check'] = now + CHECK_INTERVAL_HOURS * 3600
                
                # Procesar tokens para promover
                for token_address, data, current_data in tokens_to_promote:
                    await promote_from_incubator(context, token_address, data, current_data)
                
                # Actualizar base de datos
                for token_address, data in incubator.items():
                    await db_add_to_incubator(token_address, data)
                    
            except Exception as e:
                logger.error(f"Error en incubator_check_task: {e}")
                await asyncio.sleep(3600)  # Esperar 1 hora antes de reintentar

async def send_incubator_status(context: ContextTypes.DEFAULT_TYPE, token_address: str, data: Dict, current_data: Dict, incubation_elapsed: float):
    """Envía reporte de estado de un token en incubadora"""
    token_info = data.get('token_info', {})
    symbol = token_info.get('symbol', 'N/A')
    name = token_info.get('name', 'N/A')
    
    current_liquidity = current_data.get('liquidity', 0)
    initial_liquidity = data.get('initial_liquidity', 0)
    liquidity_change_percent = ((current_liquidity - initial_liquidity) / initial_liquidity) * 100 if initial_liquidity > 0 else 0
    
    days_remaining = INCUBATION_DAYS - incubation_elapsed
    
    message = (
        f"📊 *REPORTE DE INCUBADORA* 📊\n\n"
        f"*Symbol:* {symbol}\n"
        f"*Name:* {name}\n"
        f"*Address:* `{token_address}`\n\n"
        f"💰 *Liquidez:* `${current_liquidity:,.2f}`\n"
        f"📈 *Cambio liquidez:* {liquidity_change_percent:+.1f}%\n"
        f"💵 *Precio:* `${current_data.get('price_usd', 0):.6f}`\n"
        f"🏢 *Market Cap:* `${current_data.get('market_cap', 0):,.2f}`\n\n"
        f"⏰ *Tiempo en incubadora:* {incubation_elapsed:.1f}/{INCUBATION_DAYS} días\n"
        f"🕐 *Días restantes:* {days_remaining:.1f}\n\n"
        f"✅ *Estado:* {'🟢 SALUDABLE' if liquidity_change_percent >= -MIN_LIQUIDITY_DROP_PERCENT else '🔴 PELIGRO'}\n\n"
        f"{get_token_links(token_address)}"
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
            logger.warning(f"No se pudo enviar reporte de incubadora: {e}")

async def promote_from_incubator(context: ContextTypes.DEFAULT_TYPE, token_address: str, data: Dict, current_data: Dict):
    """Promueve un token de incubadora a watchlist"""
    token_info = data.get('token_info', {})
    symbol = token_info.get('symbol', 'N/A')
    name = token_info.get('name', 'N/A')
    
    # Agregar a watchlist
    watch_data = {
        'approved_at': time.time(),
        'token_info': token_info,
        'source': token_info.get('source', 'incubator'),
        'incubator_checks': len(data.get('checks', [])),
        'final_liquidity': current_data.get('liquidity', 0)
    }
    watchlist[token_address] = watch_data
    await db_add_to_watchlist(token_address, watch_data)
    
    # Remover de incubadora
    del incubator[token_address]
    await db_remove_from_incubator(token_address)
    
    # NOTIFICACIÓN DE ÉXITO
    current_liquidity = current_data.get('liquidity', 0)
    initial_liquidity = data.get('initial_liquidity', 0)
    liquidity_change_percent = ((current_liquidity - initial_liquidity) / initial_liquidity) * 100 if initial_liquidity > 0 else 0
    
    message = (
        f"✅ *TOKEN PASÓ INCUBACIÓN* ✅\n\n"
        f"*Symbol:* {symbol}\n"
        f"*Name:* {name}\n"
        f"*Address:* `{token_address}`\n\n"
        f"💰 *Liquidez final:* `${current_liquidity:,.2f}`\n"
        f"📈 *Cambio total:* {liquidity_change_percent:+.1f}%\n"
        f"🔍 *Verificaciones realizadas:* {len(data.get('checks', []))}\n"
        f"⏰ *Días en incubadora:* {INCUBATION_DAYS}\n\n"
        f"{get_token_links(token_address)}\n\n"
        f"🎯 *Agregado a Watchlist*\n"
        f"⚠️ *Aún verificar seguridad antes de invertir*"
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
            logger.warning(f"No se pudo enviar notificación de promoción: {e}")

    logger.info(f"✅ PROMOVIDO A WATCHLIST: {symbol} - {len(data.get('checks', []))} verificaciones")

# -------------------- TAREAS PRINCIPALES --------------------
async def combined_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar combinado que agrega tokens a incubadora"""
    logger.info("🚀 Iniciando Radar Combinado...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_tokens_combined(client)
                
                if not tokens:
                    logger.info(f"[RADAR] No tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h con ≥${MIN_LIQUIDITY:,} liquidez")
                else:
                    added_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        # Evitar duplicados
                        if address in incubator or address in watchlist:
                            continue
                            
                        # ✅ AGREGAR A INCUBADORA
                        await add_to_incubator(token, context)
                        added_count += 1
                        
                    logger.info(f"  - 🥚 {added_count} tokens agregados a incubadora")
                    
                    # Notificar resumen
                    if added_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"📊 *Resumen radar:* {added_count} tokens nuevos agregados a incubadora",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(300)  # 5 minutos entre búsquedas
                
            except Exception as e:
                logger.error(f"Error en radar combinado: {e}")
                await asyncio.sleep(60)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Sistema de Incubadora de Tokens*\n\n"
        f"🎯 *Objetivo:* Tokens de {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h con ≥${MIN_LIQUIDITY:,} liquidez\n"
        f"🥚 *Incubadora:* {INCUBATION_DAYS} días con verificaciones cada {CHECK_INTERVAL_HOURS}h\n"
        f"📊 *Monitorización:* Liquidez, Market Cap, Precio\n"
        f"🔍 *Fuentes:* Jupiter V2 + GeckoTerminal\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual\n"
        "*/incubator* - Tokens en incubadora\n"
        "*/watchlist* - Tokens aprobados",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Sistema de Incubadora...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(combined_radar_task(context)),
        asyncio.create_task(incubator_check_task(context))
    ]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 No está cazando.")
        return
        
    for task in context.bot_data['tasks']:
        task.cancel()
    context.bot_data.clear()
    await update.message.reply_text("🛑 Sistema detenido.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 Sistema detenido"
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ *Sistema de Incubadora Activo*\n\n"
            f"🥚 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Buscando:* Tokens {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h + ≥${MIN_LIQUIDITY:,} liquidez\n"
            f"⏰ *Incubación:* {INCUBATION_DAYS} días\n"
            f"📊 *Verificaciones:* Cada {CHECK_INTERVAL_HOURS} horas\n"
            f"📡 *Fuentes:* Jupiter V2 + GeckoTerminal"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🥚 Incubadora vacía")
        return
    
    # Ordenar por más recientes
    sorted_incubator = sorted(incubator.items(), key=lambda x: x[1].get('added_at', 0), reverse=True)
    
    message = f"🥚 *Tokens en Incubadora ({len(incubator)}):*\n\n"
    for i, (addr, data) in enumerate(list(sorted_incubator)[:10], 1):
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        age_hours = token_info.get('age_hours', 'N/A')
        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
        liquidity = token_info.get('liquidity', 0)
        source = token_info.get('source', 'N/A')
        added_at = data.get('added_at', 0)
        elapsed_days = (time.time() - added_at) / (24 * 3600)
        checks_count = len(data.get('checks', []))
        
        message += (f"{i}. `{addr}`\n"
                   f"   📛 {symbol} | 💰 ${liquidity:,.0f} | ⏰ {age_str}\n"
                   f"   📡 {source} | 🔍 {checks_count} checks | 🕐 {elapsed_days:.1f}/{INCUBATION_DAYS}d\n\n")
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    
    # Ordenar por más recientes
    sorted_watchlist = sorted(watchlist.items(), key=lambda x: x[1].get('approved_at', 0), reverse=True)
    
    message = f"🏆 *Tokens Aprobados ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(sorted_watchlist)[:15], 1):
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        liquidity = data.get('final_liquidity', token_info.get('liquidity', 0))
        source = token_info.get('source', 'N/A')
        checks = data.get('incubator_checks', 0)
        
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liquidity:,.0f} | 🔍 {checks} checks | 📡 {source}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

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
    application.add_handler(CommandHandler("incubator", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))

    logger.info("--- Sistema de Incubadora de Tokens listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
