import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any, List
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# -------------------- CONFIG --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# JUPITER V2 API - MANTENEMOS JUPITER
JUPITER_V2_BASE = "https://lite-api.jup.ag/tokens/v2"
JUPITER_V2_RECENT = f"{JUPITER_V2_BASE}/recent"
JUPITER_V2_TRENDING = f"{JUPITER_V2_BASE}/toptrending/1h"
JUPITER_V2_TOP_ORGANIC = f"{JUPITER_V2_BASE}/toporganicscore/1h"

# DEXSCREENER COMO BACKUP
DEXSCREENER_SOLANA_TRENDING = "https://api.dexscreener.com/latest/dex/search?q=solana&limit=50"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS MEJORADOS
MIN_LIQUIDITY = 5000
MAX_AGE_HOURS = 24
MIN_AGE_HOURS = 1

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

async def db_load_all_watchlist() -> Dict[str, dict]:
    if not DATABASE_URL: return {}
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
        return {row['token_address']: json.loads(row['data']) for row in rows}
    finally: await conn.close()

# -------------------- JUPITER V2 API MEJORADA --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Calcula la edad del token en horas - CORREGIDA"""
    try:
        # Formato: "2024-01-15T10:30:45Z"
        created_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        current_dt = datetime.utcnow().replace(tzinfo=created_dt.tzinfo)
        age_seconds = (current_dt - created_dt).total_seconds()
        age_hours = age_seconds / 3600
        return age_hours
    except Exception as e:
        logger.debug(f"Error calculando edad para {created_at_str}: {e}")
        return None

async def get_jupiter_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens RECIENTES de Jupiter V2 - FILTRO MEJORADO"""
    try:
        logger.info("Consultando Jupiter V2 /recent...")
        res = await client.get(JUPITER_V2_RECENT, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 RECENT] Tokens brutos: {len(tokens)}")
            
            processed_tokens = []
            now = time.time()
            
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    # Calcular edad basada en firstPool.createdAt
                    first_pool = token.get('firstPool', {})
                    created_at = first_pool.get('createdAt')
                    
                    if created_at:
                        age_hours = calculate_token_age(created_at)
                        
                        # FILTRO CRÍTICO: Solo tokens de 1-24 horas
                        if age_hours and MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS:
                            processed_tokens.append({
                                'address': token['id'],
                                'name': token.get('name', ''),
                                'symbol': token.get('symbol', ''),
                                'liquidity': token.get('liquidity', 0),
                                'age_hours': age_hours,
                                'first_pool_created': created_at,
                                'organic_score': token.get('organicScore', 0),
                                'is_verified': token.get('isVerified', False),
                                'holder_count': token.get('holderCount', 0),
                                'price_usd': token.get('usdPrice', 0),
                                'source': 'jupiter_v2_recent'
                            })
            
            logger.info(f"[JUPITER V2 RECENT] Tokens filtrados (1-24h): {len(processed_tokens)}")
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 recent responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 recent: {e}")
    return []

async def get_jupiter_trending_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens TRENDING de Jupiter V2 con filtro de edad"""
    try:
        logger.info("Consultando Jupiter V2 /toptrending...")
        res = await client.get(JUPITER_V2_TRENDING, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 TRENDING] Tokens brutos: {len(tokens)}")
            
            processed_tokens = []
            
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    first_pool = token.get('firstPool', {})
                    created_at = first_pool.get('createdAt')
                    
                    if created_at:
                        age_hours = calculate_token_age(created_at)
                        
                        if age_hours and MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS:
                            processed_tokens.append({
                                'address': token['id'],
                                'name': token.get('name', ''),
                                'symbol': token.get('symbol', ''),
                                'liquidity': token.get('liquidity', 0),
                                'age_hours': age_hours,
                                'organic_score': token.get('organicScore', 0),
                                'is_verified': token.get('isVerified', False),
                                'price_change_1h': token.get('stats1h', {}).get('priceChange', 0),
                                'source': 'jupiter_v2_trending'
                            })
            
            logger.info(f"[JUPITER V2 TRENDING] Tokens filtrados (1-24h): {len(processed_tokens)}")
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 trending responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 trending: {e}")
    return []

async def get_dexscreener_trending_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Backup: Obtiene pares trending de DexScreener"""
    try:
        logger.info("Consultando DexScreener trending...")
        res = await client.get(DEXSCREENER_SOLANA_TRENDING, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            logger.info(f"[DEXSCREENER] Pares brutos: {len(pairs)}")
            
            now = time.time()
            recent_pairs = []
            
            for pair in pairs:
                if pair.get('chainId') != 'solana':
                    continue
                    
                created_at = pair.get('pairCreatedAt')
                if created_at:
                    age_hours = (now - (created_at / 1000)) / 3600
                    if MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS:
                        recent_pairs.append(pair)
            
            logger.info(f"[DEXSCREENER] Pares filtrados (1-24h): {len(recent_pairs)}")
            return recent_pairs
        else:
            logger.warning(f"DexScreener responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error DexScreener trending: {e}")
    return []

async def get_all_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina Jupiter V2 + DexScreener"""
    all_tokens = []
    
    # Ejecutar TODAS las fuentes en paralelo
    tasks = [
        get_jupiter_recent_tokens(client),
        get_jupiter_trending_tokens(client),
        get_dexscreener_trending_pairs(client)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    # Procesar tokens de diferentes fuentes
    processed_tokens = []
    
    for item in all_tokens:
        if isinstance(item, dict):
            address = None
            name = ""
            symbol = ""
            liquidity = 0
            age_hours = None
            
            # Para tokens de Jupiter
            if item.get('source', '').startswith('jupiter_v2'):
                address = item.get('address')
                name = item.get('name', '')
                symbol = item.get('symbol', '')
                liquidity = item.get('liquidity', 0)
                age_hours = item.get('age_hours')
            
            # Para pares de DexScreener
            elif "baseToken" in item:
                base_token = item.get("baseToken", {})
                address = base_token.get("address")
                name = base_token.get("name", "")
                symbol = base_token.get("symbol", "")
                liquidity = item.get('liquidity', {}).get('usd', 0)
                created_at = item.get('pairCreatedAt')
                if created_at:
                    age_hours = (time.time() - (created_at / 1000)) / 3600
            
            if address and age_hours and MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS:
                processed_tokens.append({
                    'address': address,
                    'name': name,
                    'symbol': symbol,
                    'liquidity': liquidity,
                    'age_hours': age_hours,
                    'raw_data': item,
                    'source': item.get('source', 'dexscreener')
                })
    
    # Eliminar duplicados
    unique_tokens = {}
    for token in processed_tokens:
        addr = token['address']
        if addr and addr not in unique_tokens:
            unique_tokens[addr] = token
    
    logger.info(f"🎯 TOTAL tokens únicos (1-24h): {len(unique_tokens)}")
    
    # DEBUG: Estadísticas detalladas
    if unique_tokens:
        jupiter_tokens = [t for t in unique_tokens.values() if 'jupiter' in t.get('source', '')]
        dexscreener_tokens = [t for t in unique_tokens.values() if 'dexscreener' in t.get('source', '')]
        tokens_with_liquidity = [t for t in unique_tokens.values() if t.get('liquidity', 0) >= MIN_LIQUIDITY]
        
        logger.info(f"📊 Fuentes: Jupiter={len(jupiter_tokens)}, DexScreener={len(dexscreener_tokens)}")
        logger.info(f"📊 Con liquidez ≥${MIN_LIQUIDITY}: {len(tokens_with_liquidity)}")
        
        # Mostrar ejemplos
        sample_tokens = list(unique_tokens.values())[:5]
        logger.info("🔍 Ejemplos de tokens encontrados:")
        for token in sample_tokens:
            age_info = f"{token.get('age_hours', 'N/A'):.1f}h"
            liq = token.get('liquidity', 0)
            source = token.get('source', 'unknown')
            logger.info(f"  - {token['symbol']} | {age_info} | ${liq:,.0f} | {source}")
    
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN CON DEXSCREENER --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos actualizados de DexScreener"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=8)
        if res.status_code == 200:
            data = res.json()
            if data.get('pairs'):
                pairs = data['pairs']
                if pairs:
                    best_pair = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                    return best_pair
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

# -------------------- FILTROS PRÁCTICOS --------------------
def check_liquidity(dex_data: dict) -> tuple:
    """Verifica liquidez mínima"""
    if not dex_data:
        return False, "❌ Sin datos"
    
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < MIN_LIQUIDITY:
        return False, f"❌ Liquidez: ${liquidity:,.0f}"
    
    return True, f"✅ Liquidez: ${liquidity:,.0f}"

def check_age(dex_data: dict) -> tuple:
    """Verifica edad del token"""
    created_at = dex_data.get('pairCreatedAt')
    if not created_at:
        return False, "❌ Sin timestamp"
    
    age_hours = (time.time() - (created_at / 1000)) / 3600
    if age_hours < MIN_AGE_HOURS:
        return False, f"❌ Muy nuevo: {age_hours:.1f}h"
    if age_hours > MAX_AGE_HOURS:
        return False, f"❌ Muy viejo: {age_hours:.1f}h"
    
    return True, f"✅ Edad: {age_hours:.1f}h"

def check_volume(dex_data: dict) -> tuple:
    """Verifica volumen mínimo"""
    volume_24h = dex_data.get('volume', {}).get('h24', 0)
    if volume_24h < 1000:
        return True, f"⚠️ Volumen bajo: ${volume_24h:,.0f}"
    
    return True, f"✅ Volumen: ${volume_24h:,.0f}"

# -------------------- TAREAS PRINCIPALES --------------------
async def hybrid_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar HÍBRIDO: Jupiter V2 + DexScreener"""
    logger.info("🚀 Iniciando Radar Híbrido (Jupiter V2 + DexScreener)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_recent_tokens(client)
                
                if not tokens:
                    logger.info("[RADAR] No se encontraron tokens recientes (1-24h).")
                else:
                    added_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        if address in incubator or address in watchlist:
                            continue
                            
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'token_info': token,
                            'source': token.get('source', 'hybrid')
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        added_count += 1
                        
                    logger.info(f"  - 🐣 {added_count} tokens nuevos añadidos a incubadora")
                    
                    if added_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"🔍 *Radar Híbrido activo:* {added_count} tokens recientes encontrados! Verificando...",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(120)  # 2 minutos
                
            except Exception as e:
                logger.error(f"Error en radar híbrido: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verificación PRÁCTICA"""
    logger.info("Iniciando Verificación de Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(60)
                
                if not incubator:
                    continue
                    
                now = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                approved_count = 0
                rejected_count = 0
                
                for token_address, data in list(incubator.items()):
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        if now - data['found_at'] > 1800:  # 30 minutos sin datos
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # APLICAR FILTROS
                    liquidity_ok, liquidity_reason = check_liquidity(dex_data)
                    age_ok, age_reason = check_age(dex_data)
                    volume_ok, volume_reason = check_volume(dex_data)
                    
                    if liquidity_ok and age_ok:
                        # ✅ APROBADO
                        approved_at = now
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        volume_24h = dex_data.get('volume', {}).get('h24', 0)
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'volume_24h': volume_24h,
                            'dex_data': dex_data,
                            'token_info': data['token_info'],
                            'checks': {
                                'liquidity': liquidity_reason,
                                'age': age_reason,
                                'volume': volume_reason
                            }
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        approved_count += 1

                        # NOTIFICACIÓN
                        token_info = data['token_info']
                        symbol = token_info.get('symbol', 'N/A')
                        name = token_info.get('name', 'N/A')
                        source = data.get('source', 'N/A')
                        
                        message = (
                            f"🎯 *TOKEN RECIENTE DETECTADO* 🎯\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Name:* {name}\n"
                            f"*Fuente:* {source}\n"
                            f"*Address:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Volumen 24h:* `${volume_24h:,.2f}`\n"
                            f"*Cambio 24h:* `{price_change}%`\n\n"
                            f"📊 *Verificaciones:*\n"
                            f"- {liquidity_reason}\n"
                            f"- {age_reason}\n"
                            f"- {volume_reason}\n\n"
                            f"🔍 *Verificar manualmente:*\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n\n"
                            f"⚠️ *Verifica seguridad en RugCheck antes de invertir*"
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
                                logger.warning(f"No se pudo enviar notificación: {e}")

                        logger.info(f"  - ✅ APROBADO: {symbol}")

                    else:
                        # ❌ RECHAZADO
                        rejected_count += 1
                        rejection_reasons = []
                        if not liquidity_ok: rejection_reasons.append(liquidity_reason)
                        if not age_ok: rejection_reasons.append(age_reason)
                        
                        reason_text = " | ".join(rejection_reasons)
                        logger.info(f"  - ❌ RECHAZADO: {token_address} - {reason_text}")
                        
                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(0.3)
                
                if approved_count > 0 or rejected_count > 0:
                    logger.info(f"📈 Ciclo: {approved_count} aprobados, {rejected_count} rechazados")
                    
            except Exception as e:
                logger.error(f"Error en verificador: {e}")
                await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Híbrido - Jupiter V2 + DexScreener*\n\n"
        "🎯 *Objetivo:* Tokens 1-24h + ≥$5,000 liquidez\n"
        "🔍 *Fuentes:* Jupiter V2 (recent/trending) + DexScreener\n"
        "🛡️ *Filtros:* Liquidez + Edad + Volumen\n"
        "⏰ *Búsqueda cada 2 minutos*\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Radar Híbrido...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(hybrid_radar_task(context)),
        asyncio.create_task(incubator_checker_task(context))
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
            f"✅ *Bot Híbrido Activo*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Fuentes:* Jupiter V2 + DexScreener\n"
            f"🎯 *Buscando:* Tokens 1-24h + ≥${MIN_LIQUIDITY:,}"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 Incubadora vacía")
        return
    message = f"🐣 *Tokens en Revisión ({len(incubator)}):*\n\n"
    for i, (addr, data) in enumerate(list(incubator.items())[-10:][::-1], 1):
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        age_hours = token_info.get('age_hours', 'N/A')
        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
        liquidity = token_info.get('liquidity', 0)
        source = data.get('source', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol} | ⏰ {age_str} | 💰 ${liquidity:,.0f} | 📡 {source}\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    message = f"🏆 *Tokens Aprobados ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(watchlist.items())[-15:][::-1], 1):
        liq = data.get('liquidity', 0)
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        age_hours = token_info.get('age_hours', 'N/A')
        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
        price_change = data.get('price_change_24h', 0)
        source = token_info.get('source', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f} | ⏰ {age_str} | 📈 {price_change}% | 📡 {source}\n"
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
    application.add_handler(CommandHandler("incubadora", incubator_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))

    logger.info("--- Bot Híbrido (Jupiter V2 + DexScreener) listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
