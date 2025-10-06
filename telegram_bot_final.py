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

# FUENTES CONFIABLES - SIN DEXSCREENER
JUPITER_V2_RECENT = "https://lite-api.jup.ag/tokens/v2/recent"
JUPITER_V2_TRENDING = "https://lite-api.jup.ag/tokens/v2/toptrending/1h"
JUPITER_V2_ORGANIC = "https://lite-api.jup.ag/tokens/v2/toporganicscore/1h"

# GeckoTerminal para tokens nuevos
GECKO_NEW_PAIRS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"

# Birdeye para verificación rápida (opcional, solo si es necesario)
BIRDEYE_API = "https://public-api.birdeye.so/public/token?address={}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS MÁS ESTRICTOS
MIN_LIQUIDITY = 5000  # $5,000 mínimo
MAX_AGE_HOURS = 6     # Reducido a 6 horas máximo - MÁS FRESCOS
MIN_AGE_HOURS = 0.5   # Mínimo 30 minutos

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

# -------------------- FILTROS DE EDAD MEJORADOS --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Calcula la edad del token en horas de forma más robusta"""
    try:
        if not created_at_str:
            return None
            
        # Manejar diferentes formatos de fecha
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

# -------------------- GECKOTERMINAL - FUENTE PRINCIPAL --------------------
async def get_geckoterminal_new_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pools nuevos de GeckoTerminal - MÁS CONFIABLE"""
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
                    
                    # FILTRO ESTRICTO: solo tokens en rango de edad
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
            
        else:
            logger.warning(f"GeckoTerminal responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error GeckoTerminal: {e}")
    return []

# -------------------- JUPITER V2 MEJORADO --------------------
async def get_jupiter_recent_tokens_improved(client: httpx.AsyncClient) -> List[Dict]:
    """Jupiter V2 con filtros más estrictos"""
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
                    
                    # FILTRO ESTRICTO: solo tokens en rango de edad
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
        else:
            logger.warning(f"Jupiter V2 recent responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 recent: {e}")
    return []

async def get_jupiter_trending_tokens_improved(client: httpx.AsyncClient) -> List[Dict]:
    """Jupiter V2 trending con filtros"""
    try:
        logger.info("Consultando Jupiter V2 /toptrending...")
        res = await client.get(JUPITER_V2_TRENDING, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 TRENDING] Tokens obtenidos: {len(tokens)}")
            
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    # Para trending, también verificar edad
                    age_hours = None
                    first_pool = token.get('firstPool', {})
                    if first_pool and first_pool.get('createdAt'):
                        age_hours = calculate_token_age(first_pool['createdAt'])
                    
                    # FILTRO ESTRICTO: solo tokens en rango de edad
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
                        'organic_score': token.get('organicScore', 0),
                        'is_verified': token.get('isVerified', False),
                        'price_change_1h': token.get('stats1h', {}).get('priceChange', 0),
                        'source': 'jupiter_v2_trending'
                    })
            
            logger.info(f"[JUPITER V2 TRENDING] Tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h + ≥${MIN_LIQUIDITY:,}: {len(processed_tokens)}")
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 trending responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 trending: {e}")
    return []

async def get_all_tokens_combined(client: httpx.AsyncClient) -> List[Dict]:
    """Combina múltiples fuentes con filtros estrictos - SIN DEXSCREENER"""
    all_tokens = []
    
    # Ejecutar todas las fuentes en paralelo
    tasks = [
        get_jupiter_recent_tokens_improved(client),
        get_jupiter_trending_tokens_improved(client),
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
    
    # Mostrar ejemplos para debugging
    if unique_tokens:
        sample_tokens = list(unique_tokens.values())[:3]
        logger.info("📋 Ejemplos de tokens encontrados:")
        for token in sample_tokens:
            age_info = f"{token.get('age_hours', 'N/A'):.1f}h" if token.get('age_hours') else 'edad N/A'
            liq = token.get('liquidity', 0)
            source = token.get('source', 'desconocido')
            logger.info(f"  - {token['symbol']}: {age_info}, ${liq:,.0f} liquidez, {source}")
    
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN RÁPIDA CON BIRDEYE (OPCIONAL) --------------------
async def get_birdeye_token_data(client: httpx.AsyncClient, token_address: str):
    """Verificación opcional con Birdeye - SOLO SI ES NECESARIO"""
    try:
        url = BIRDEYE_API.format(token_address)
        res = await client.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('data'):
                return data['data']
    except Exception as e:
        logger.debug(f"Birdeye opcional falló para {token_address}: {e}")
    return None

# -------------------- TAREAS PRINCIPALES - SIN DEXSCREENER --------------------
async def combined_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar combinado SIN verificación externa"""
    logger.info("🚀 Iniciando Radar Combinado (SIN DexScreener)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_tokens_combined(client)
                
                if not tokens:
                    logger.info(f"[RADAR] No tokens en rango {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h con ≥${MIN_LIQUIDITY:,} liquidez")
                else:
                    approved_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        # Evitar duplicados
                        if address in watchlist:
                            continue
                            
                        # ✅ APROBAR DIRECTAMENTE - Ya pasó todos los filtros
                        approved_at = time.time()
                        watch_data = {
                            'approved_at': approved_at,
                            'token_info': token,
                            'source': token.get('source', 'combined')
                        }
                        watchlist[address] = watch_data
                        await db_add_to_watchlist(address, watch_data)
                        approved_count += 1

                        # NOTIFICACIÓN INMEDIATA
                        symbol = token.get('symbol', 'N/A')
                        name = token.get('name', 'N/A')
                        age_hours = token.get('age_hours', 'N/A')
                        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
                        liquidity = token.get('liquidity', 0)
                        source = token.get('source', 'N/A')
                        
                        message = (
                            f"🎯 *TOKEN RECIENTE DETECTADO* 🎯\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Name:* {name}\n"
                            f"*Address:* `{address}`\n"
                            f"*Edad:* {age_str}\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Fuente:* {source}\n\n"
                            f"🔍 *Verificar:*\n"
                            f"- DexScreener: https://dexscreener.com/solana/{address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{address}\n"
                            f"- Birdeye: https://birdeye.so/token/{address}?chain=solana\n\n"
                            f"⚠️ *Verifica seguridad manualmente antes de invertir*"
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

                        logger.info(f"  - ✅ APROBADO: {symbol} - ${liquidity:,.0f} liquidez, {age_str} edad")
                        
                    logger.info(f"  - 🎯 {approved_count} tokens aprobados directamente")
                    
                    # Notificar resumen
                    if approved_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"📊 *Resumen radar:* {approved_count} tokens nuevos ({MIN_AGE_HOURS}-{MAX_AGE_HOURS}h, ≥${MIN_LIQUIDITY:,})",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(60)  # 1 minuto entre búsquedas (más rápido)
                
            except Exception as e:
                logger.error(f"Error en radar combinado: {e}")
                await asyncio.sleep(30)

# -------------------- LIMPIEZA AUTOMÁTICA --------------------
async def cleanup_task(context: ContextTypes.DEFAULT_TYPE):
    """Limpia tokens viejos de la watchlist"""
    logger.info("Iniciando tarea de limpieza...")
    while True:
        try:
            await asyncio.sleep(3600)  # Revisar cada 1 hora
            
            if not watchlist:
                continue
                
            now = time.time()
            removed_count = 0
            
            for token_address, data in list(watchlist.items()):
                approved_at = data.get('approved_at', 0)
                # Eliminar tokens con más de 24 horas en watchlist
                if now - approved_at > 86400:  # 24 horas
                    del watchlist[token_address]
                    # No eliminamos de DB para mantener historial
                    removed_count += 1
            
            if removed_count > 0:
                logger.info(f"🧹 Limpiados {removed_count} tokens viejos de watchlist")
                
        except Exception as e:
            logger.error(f"Error en limpieza: {e}")

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Mejorado - Tokens Recientes*\n\n"
        f"🎯 *Objetivo:* Tokens de {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h con ≥${MIN_LIQUIDITY:,} liquidez\n"
        "🔍 *Fuentes:* Jupiter V2 + GeckoTerminal\n"
        "⚡ *Detección directa sin verificaciones externas*\n"
        "⏰ *Búsqueda cada 1 minuto*\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual\n"
        "*/watchlist* - Tokens aprobados",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Radar Combinado...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(combined_radar_task(context)),
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
            f"✅ *Radar Combinado Activo*\n\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Buscando:* Tokens {MIN_AGE_HOURS}-{MAX_AGE_HOURS}h + ≥${MIN_LIQUIDITY:,} liquidez\n"
            f"📡 *Fuentes:* Jupiter V2 + GeckoTerminal\n"
            f"⚡ *Sin DexScreener*"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    
    # Ordenar por más recientes
    sorted_watchlist = sorted(watchlist.items(), key=lambda x: x[1].get('approved_at', 0), reverse=True)
    
    message = f"🏆 *Tokens Aprobados ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(sorted_watchlist)[:15], 1):
        liq = data.get('token_info', {}).get('liquidity', 0)
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        age_hours = token_info.get('age_hours', 'N/A')
        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
        source = token_info.get('source', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f} | ⏰ {age_str} | 📡 {source}\n"
    
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
    application.add_handler(CommandHandler("watchlist", watchlist_command))

    logger.info("--- Bot Mejorado (SIN DexScreener) - Tokens Recientes listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
