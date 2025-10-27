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

# JUPITER V2 API - CONFIRMADA Y FUNCIONAL
JUPITER_V2_BASE = "https://lite-api.jup.ag/tokens/v2"
JUPITER_V2_RECENT = f"{JUPITER_V2_BASE}/recent"  # Tokens recién creados
JUPITER_V2_TRENDING = f"{JUPITER_V2_BASE}/toptrending/1h"  # Tokens trending
JUPITER_V2_TOP_ORGANIC = f"{JUPITER_V2_BASE}/toporganicscore/1h"  # Tokens con score orgánico

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS EXACTOS COMO LOS QUIERES
MIN_LIQUIDITY = 5000  # $5,000 mínimo
MAX_AGE_HOURS = 24    # Máximo 24 horas
MIN_AGE_HOURS = 1     # Mínimo 1 hora

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

# -------------------- JUPITER V2 API FUNCTIONS --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Calcula la edad del token en horas basado en firstPool.createdAt"""
    try:
        # Formato: "2024-01-15T10:30:45Z"
        created_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        current_dt = datetime.utcnow().replace(tzinfo=created_dt.tzinfo)
        age_seconds = (current_dt - created_dt).total_seconds()
        return age_seconds / 3600  # Convertir a horas
    except Exception as e:
        logger.debug(f"Error calculando edad para {created_at_str}: {e}")
        return None

async def get_jupiter_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens RECIENTES de Jupiter V2 - ENDPOINT PERFECTO"""
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
                    
                    # Solo incluir tokens en el rango de edad deseado
                    if age_hours and MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS:
                        processed_tokens.append({
                            'address': token['id'],
                            'name': token.get('name', ''),
                            'symbol': token.get('symbol', ''),
                            'liquidity': token.get('liquidity', 0),
                            'age_hours': age_hours,
                            'first_pool_created': first_pool.get('createdAt'),
                            'organic_score': token.get('organicScore', 0),
                            'is_verified': token.get('isVerified', False),
                            'holder_count': token.get('holderCount', 0),
                            'price_usd': token.get('usdPrice', 0),
                            'source': 'jupiter_v2_recent'
                        })
            
            logger.info(f"[JUPITER V2 RECENT] Tokens en rango 1-24h: {len(processed_tokens)}")
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 recent responded {res.status_code}: {res.text}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 recent: {e}")
    return []

async def get_jupiter_trending_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens TRENDING de Jupiter V2"""
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
                    
                    # Incluir trending tokens que estén en nuestro rango de edad
                    if not age_hours or (MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS):
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
            
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 trending responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 trending: {e}")
    return []

async def get_jupiter_organic_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens con alto score orgánico"""
    try:
        logger.info("Consultando Jupiter V2 /toporganicscore...")
        res = await client.get(JUPITER_V2_TOP_ORGANIC, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 ORGANIC] Tokens obtenidos: {len(tokens)}")
            
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    # Verificar edad para organic tokens también
                    age_hours = None
                    first_pool = token.get('firstPool', {})
                    if first_pool and first_pool.get('createdAt'):
                        age_hours = calculate_token_age(first_pool['createdAt'])
                    
                    if not age_hours or (MIN_AGE_HOURS <= age_hours <= MAX_AGE_HOURS):
                        processed_tokens.append({
                            'address': token['id'],
                            'name': token.get('name', ''),
                            'symbol': token.get('symbol', ''),
                            'liquidity': token.get('liquidity', 0),
                            'age_hours': age_hours,
                            'organic_score': token.get('organicScore', 0),
                            'organic_score_label': token.get('organicScoreLabel', ''),
                            'is_verified': token.get('isVerified', False),
                            'source': 'jupiter_v2_organic'
                        })
            
            return processed_tokens
        else:
            logger.warning(f"Jupiter V2 organic responded {res.status_code}")
            
    except Exception as e:
        logger.error(f"Error Jupiter V2 organic: {e}")
    return []

async def get_all_jupiter_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina TODAS las fuentes de Jupiter V2"""
    all_tokens = []
    
    # Ejecutar todas las fuentes en paralelo
    tasks = [
        get_jupiter_recent_tokens(client),
        get_jupiter_trending_tokens(client),
        get_jupiter_organic_tokens(client)
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
    
    logger.info(f"🎯 TOTAL tokens únicos Jupiter V2 (1-24h): {len(unique_tokens)}")
    
    # Mostrar ejemplos para debugging
    if unique_tokens:
        sample_tokens = list(unique_tokens.values())[:5]
        logger.info("📋 Ejemplos de tokens encontrados:")
        for token in sample_tokens:
            age_info = f"{token.get('age_hours', 'N/A'):.1f}h" if token.get('age_hours') else 'edad N/A'
            liq = token.get('liquidity', 0)
            logger.info(f"  - {token['symbol']}: {age_info}, ${liq:,.0f} liquidez")
    
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN CON DEXSCREENER --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos actualizados de DexScreener para verificación final"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('pairs'):
                pairs = data['pairs']
                if pairs:
                    # Escoger el par con mayor liquidez
                    best_pair = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                    return best_pair
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

# -------------------- FILTROS SIMPLIFICADOS --------------------
def passes_basic_filters(dex_data: dict) -> tuple:
    """Solo verifica liquidez - SIN SEGURIDAD AUTOMÁTICA"""
    if not dex_data:
        return False, "sin datos"
    
    # Verificar liquidez mínima
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < MIN_LIQUIDITY:
        return False, f"liquidez ${liquidity:,.0f}"
    
    return True, f"${liquidity:,.0f} liquidez"

# -------------------- TAREAS PRINCIPALES --------------------
async def jupiter_v2_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar PRINCIPAL usando Jupiter V2 API"""
    logger.info("🚀 Iniciando Radar Jupiter V2...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_jupiter_tokens(client)
                
                if not tokens:
                    logger.info("[RADAR] No se encontraron tokens en el rango 1-24 horas.")
                else:
                    added_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        # Evitar duplicados
                        if address in incubator or address in watchlist:
                            continue
                            
                        # Agregar a incubadora
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'token_info': token,
                            'source': token.get('source', 'jupiter_v2')
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        added_count += 1
                        
                    logger.info(f"  - 🐣 {added_count} tokens nuevos añadidos a incubadora")
                    
                    # Notificar si encontramos tokens
                    if added_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"🔍 *Jupiter V2 activo:* {added_count} tokens recientes (1-24h) encontrados! Verificando liquidez...",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(120)  # 2 minutos entre búsquedas
                
            except Exception as e:
                logger.error(f"Error en radar Jupiter V2: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verificación RÁPIDA de tokens en incubadora"""
    logger.info("Iniciando Verificación de Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(60)  # Revisar cada 1 minuto
                
                if not incubator:
                    continue
                    
                now = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                approved_count = 0
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados de DexScreener
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        # Esperar antes de eliminar
                        if now - data['found_at'] > 3600:  # 1 hora
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # APLICAR FILTROS BÁSICOS (solo liquidez)
                    passes, reason = passes_basic_filters(dex_data)
                    
                    if passes:
                        # ✅ APROBAR - Cumple liquidez mínima
                        approved_at = now
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'dex_data': dex_data,
                            'token_info': data['token_info']
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        approved_count += 1

                        # NOTIFICACIÓN INMEDIATA
                        token_info = data['token_info']
                        symbol = token_info.get('symbol', 'N/A')
                        name = token_info.get('name', 'N/A')
                        age_hours = token_info.get('age_hours', 'N/A')
                        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
                        organic_score = token_info.get('organic_score', 'N/A')
                        
                        message = (
                            f"🎯 *TOKEN RECIENTE DETECTADO* 🎯\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Name:* {name}\n"
                            f"*Address:* `{token_address}`\n"
                            f"*Edad:* {age_str}\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Score Orgánico:* {organic_score}\n"
                            f"*Cambio 24h:* `{price_change}%`\n\n"
                            f"🔍 *Verificar manualmente:*\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n"
                            f"- BirdEye: https://birdeye.so/token/{token_address}?chain=solana\n\n"
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

                        logger.info(f"  - ✅ APROBADO: {symbol} - {reason}")

                    else:
                        # ❌ No cumple liquidez mínima
                        logger.info(f"  - ❌ Rechazado: {token_address} - {reason}")
                        
                        # Eliminar después de 2 horas
                        if now - data['found_at'] > 7200:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(0.3)  # Pequeño delay
                
                if approved_count > 0:
                    logger.info(f"🎯 Tokens aprobados este ciclo: {approved_count}")
                    
            except Exception as e:
                logger.error(f"Error en verificador: {e}")
                await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Jupiter V2 - Tokens Recientes*\n\n"
        "🎯 *Objetivo:* Tokens de 1-24 horas con ≥$5,000 liquidez\n"
        "🔍 *Fuente:* Jupiter V2 API Oficial\n"
        "📊 *Endpoints:* /recent + /toptrending + /toporganicscore\n"
        "⏰ *Búsqueda cada 2 minutos*\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual\n"
        "*/incubadora* - Tokens en revisión\n"
        "*/watchlist* - Tokens aprobados",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Jupiter V2 Radar...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(jupiter_v2_radar_task(context)),
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
            f"✅ *Jupiter V2 Activo*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Buscando:* Tokens 1-24h + ≥${MIN_LIQUIDITY:,} liquidez"
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
        source = data.get('source', 'desconocido')
        message += f"{i}. `{addr}`\n   📛 {symbol} | ⏰ {age_str} | 📡 {source}\n"
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
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f} | ⏰ {age_str}\n"
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

    logger.info("--- Bot Jupiter V2 - Tokens Recientes listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
