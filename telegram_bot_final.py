import asyncio
import json
import time
import logging
import os
from dotenv import load_dotenv
import httpx
import asyncpg
from typing import Dict, Any, List

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# -------------------- CONFIG --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# ENDPOINTS 100% CONFIRMADOS Y FUNCIONALES
TOKEN_SOURCES = {
    "jupiter_strict": "https://cache.jup.ag/tokens",
    "jupiter_strict_v2": "https://token.jup.ag/all",  # Backup
    "dexscreener_solana": "https://api.dexscreener.com/latest/dex/search?q=solana",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS MÁS FLEXIBLES
MIN_LIQUIDITY = 3000  # Bajamos a $3K para más oportunidades
MAX_AGE_HOURS = 48    # Ampliamos a 48 horas
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

# -------------------- FUENTES 100% FUNCIONALES --------------------
async def get_jupiter_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens de Jupiter - ENDPOINT CONFIRMADO"""
    try:
        # Intentar con el endpoint principal primero
        res = await client.get(TOKEN_SOURCES["jupiter_strict"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER] Tokens obtenidos: {len(tokens)}")
            
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict) and token.get('address'):
                    processed_tokens.append({
                        'address': token['address'],
                        'name': token.get('name', ''),
                        'symbol': token.get('symbol', ''),
                        'source': 'jupiter'
                    })
            return processed_tokens
        else:
            # Fallback al endpoint alternativo
            logger.info("Intentando endpoint alternativo de Jupiter...")
            res = await client.get(TOKEN_SOURCES["jupiter_strict_v2"], headers=HEADERS, timeout=15)
            if res.status_code == 200:
                tokens = res.json()
                processed_tokens = []
                for token in tokens:
                    if isinstance(token, dict) and token.get('address'):
                        processed_tokens.append({
                            'address': token['address'],
                            'name': token.get('name', ''),
                            'symbol': token.get('symbol', ''),
                            'source': 'jupiter_v2'
                        })
                logger.info(f"[JUPITER V2] Tokens obtenidos: {len(processed_tokens)}")
                return processed_tokens
    except Exception as e:
        logger.error(f"Error Jupiter: {e}")
    return []

async def get_dexscreener_solana_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pares de Solana de DexScreener - ENDPOINT CONFIRMADO"""
    try:
        res = await client.get(TOKEN_SOURCES["dexscreener_solana"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            
            # Filtrar SOLO pares de Solana y con timestamp
            valid_pairs = []
            for pair in pairs:
                if pair.get('chainId') == 'solana':
                    valid_pairs.append(pair)
            
            logger.info(f"[DEXSCREENER] Pares Solana obtenidos: {len(valid_pairs)}")
            return valid_pairs
    except Exception as e:
        logger.error(f"Error DexScreener: {e}")
    return []

async def get_all_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina todas las fuentes - ESTRATEGIA AGRESIVA"""
    all_tokens = []
    
    # Ejecutar TODAS las fuentes en paralelo
    tasks = [
        get_jupiter_tokens(client),
        get_dexscreener_solana_pairs(client),
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    # Procesar tokens
    processed_tokens = []
    
    for item in all_tokens:
        if isinstance(item, dict):
            address = None
            name = ""
            symbol = ""
            
            # Para tokens de Jupiter
            if item.get('source', '').startswith('jupiter'):
                address = item.get('address')
                name = item.get('name', '')
                symbol = item.get('symbol', '')
            
            # Para pares de DexScreener
            elif "baseToken" in item:
                base_token = item.get("baseToken", {})
                address = base_token.get("address")
                name = base_token.get("name", "")
                symbol = base_token.get("symbol", "")
            
            if address:
                processed_tokens.append({
                    'address': address,
                    'name': name,
                    'symbol': symbol,
                    'raw_data': item
                })
    
    # Eliminar duplicados
    unique_tokens = {}
    for token in processed_tokens:
        addr = token['address']
        if addr and addr not in unique_tokens:
            unique_tokens[addr] = token
    
    logger.info(f"🎯 TOTAL tokens únicos para análisis: {len(unique_tokens)}")
    
    # DEBUG: Mostrar primeros tokens
    if unique_tokens:
        sample_tokens = list(unique_tokens.values())[:3]
        for token in sample_tokens:
            logger.info(f"  - Ejemplo: {token['symbol']} ({token['address'][:8]}...)")
    
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN MEJORADA --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos actualizados de DexScreener con reintentos"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    max_retries = 3
    
    for attempt in range(max_retries):
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
            elif res.status_code == 429:
                # Rate limit, esperar y reintentar
                await asyncio.sleep(2)
                continue
        except Exception as e:
            logger.debug(f"Intento {attempt + 1} fallido para {token_address}: {e}")
            await asyncio.sleep(1)
    
    return None

# -------------------- FILTROS FLEXIBLES --------------------
def passes_basic_filters(dex_data: dict) -> tuple:
    """Filtros flexibles - priorizar encontrar tokens"""
    if not dex_data:
        return False, "sin datos"
    
    # 1. Verificar liquidez
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < MIN_LIQUIDITY:
        return False, f"liquidez ${liquidity:,.0f}"
    
    # 2. Verificar edad si está disponible (pero no es crítico)
    created_at = dex_data.get('pairCreatedAt')
    if created_at:
        age_hours = (time.time() - (created_at / 1000)) / 3600
        if age_hours > MAX_AGE_HOURS:
            return False, f"viejo ({age_hours:.1f}h)"
        if age_hours < MIN_AGE_HOURS:
            return False, f"nuevo ({age_hours:.1f}h)"
        return True, f"${liquidity:,.0f} - {age_hours:.1f}h"
    
    # Si no hay timestamp, solo verificar liquidez
    return True, f"${liquidity:,.0f} - edad desconocida"

# -------------------- TAREAS PRINCIPALES --------------------
async def aggressive_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar AGRESIVO - Encontrar tokens a toda costa"""
    logger.info("🚀 Iniciando Radar AGRESIVO...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_tokens(client)
                
                if not tokens:
                    logger.warning("[RADAR] ⚠️  CERO tokens encontrados. Revisando fuentes...")
                    # Intentar diagnóstico
                    await diagnostic_check(client)
                else:
                    added_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        if address in incubator or address in watchlist:
                            continue
                            
                        # Agregar DIRECTAMENTE a incubadora
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'token_info': token,
                            'source': 'radar_agresivo'
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        added_count += 1
                        
                    logger.info(f"  - 🐣 {added_count} tokens añadidos a incubadora")
                    
                    # Notificar SIEMPRE que encontramos tokens
                    if added_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"🔍 *Radar activo:* {added_count} tokens encontrados! Verificando...",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(150)  # 2.5 minutos entre búsquedas
                
            except Exception as e:
                logger.error(f"Error en radar agresivo: {e}")
                await asyncio.sleep(30)

async def diagnostic_check(client: httpx.AsyncClient):
    """Diagnóstico para ver qué está fallando"""
    logger.info("🔧 Ejecutando diagnóstico...")
    
    try:
        # Probar Jupiter
        res1 = await client.get(TOKEN_SOURCES["jupiter_strict"], timeout=10)
        logger.info(f"Jupiter status: {res1.status_code}")
        
        # Probar DexScreener
        res2 = await client.get(TOKEN_SOURCES["dexscreener_solana"], timeout=10)
        logger.info(f"DexScreener status: {res2.status_code}")
        if res2.status_code == 200:
            data = res2.json()
            pairs = data.get('pairs', [])
            logger.info(f"DexScreener pairs: {len(pairs)}")
            if pairs:
                sample = pairs[0]
                logger.info(f"Sample pair: {sample.get('baseToken', {}).get('symbol', 'N/A')}")
                
    except Exception as e:
        logger.error(f"Error en diagnóstico: {e}")

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verificación RÁPIDA de incubadora"""
    logger.info("Iniciando Verificación RÁPIDA...")
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
                    # Obtener datos actualizados RÁPIDO
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        # Esperar más tiempo antes de eliminar
                        if now - data['found_at'] > 10800:  # 3 horas
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # APLICAR FILTROS FLEXIBLES
                    passes, reason = passes_basic_filters(dex_data)
                    
                    if passes:
                        # ✅ APROBAR INMEDIATAMENTE
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

                        # NOTIFICACIÓN URGENTE
                        symbol = data['token_info'].get('symbol', 'N/A')
                        
                        message = (
                            f"🎯 *TOKEN ENCONTRADO* 🎯\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Address:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Razón:* {reason}\n\n"
                            f"🔍 *Verificar rápido:*\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n\n"
                            f"⚡ *Oportunidad detectada - Verifica YA*"
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

                        logger.info(f"  - ✅ APROBADO: {token_address} - {reason}")

                    else:
                        # ❌ No cumple, pero mantener más tiempo
                        logger.debug(f"  - ⏳ Esperando: {token_address} - {reason}")
                        
                        if now - data['found_at'] > 14400:  # 4 horas
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(0.3)  # Delay mínimo
                
                if approved_count > 0:
                    logger.info(f"🎯 Tokens aprobados: {approved_count}")
                    
            except Exception as e:
                logger.error(f"Error en verificador: {e}")
                await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot AGRESIVO Activado*\n\n"
        "🎯 *Objetivo:* Encontrar tokens CUESTE LO QUE CUESTE\n"
        "🔍 *Estrategia:* Múltiples fuentes + Filtros flexibles\n"
        "💧 *Liquidez mínima:* $3,000\n"
        "⏰ *Búsqueda cada 2.5 minutos*\n\n"
        "*/cazar* - Iniciar modo AGRESIVO\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *INICIANDO MODO AGRESIVO...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(aggressive_radar_task(context)),
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
            f"✅ *Bot AGRESIVO Activo*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🔍 *Modo:* Búsqueda intensiva"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 Incubadora vacía")
        return
    message = f"🐣 *Tokens en Revisión ({len(incubator)}):*\n\n"
    for i, (addr, data) in enumerate(list(incubator.items())[-10:][::-1], 1):
        symbol = data.get('token_info', {}).get('symbol', 'N/A')
        source = data.get('source', 'desconocido')
        message += f"{i}. `{addr}`\n   📛 {symbol} | 📡 {source}\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    message = f"🏆 *Tokens Aprobados ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(watchlist.items())[-15:][::-1], 1):
        liq = data.get('liquidity', 0)
        symbol = data.get('token_info', {}).get('symbol', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f}\n"
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

    logger.info("--- Bot AGRESIVO listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
