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

# FUENTES CONFIRMADAS QUE SÍ FUNCIONAN
TOKEN_SOURCES = {
    "jupiter_tokens": "https://token.jup.ag/strict",
    "dexscreener_solana": "https://api.dexscreener.com/latest/dex/search?q=SOL",
    "dexscreener_new": "https://api.dexscreener.com/latest/dex/pairs/new",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS EXACTOS COMO LOS QUIERES
MIN_LIQUIDITY = 5000  # $5,000 mínimo
MIN_AGE_HOURS = 1     # Mínimo 1 hora
MAX_AGE_HOURS = 24    # Máximo 24 horas

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

# -------------------- FUENTES MEJORADAS --------------------
async def get_jupiter_strict_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens de Jupiter Strict - ESTA SÍ FUNCIONA"""
    try:
        res = await client.get(TOKEN_SOURCES["jupiter_tokens"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER] Tokens obtenidos: {len(tokens)}")
            
            # Procesar tokens de Jupiter
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict):
                    processed_tokens.append({
                        'address': token.get('address'),
                        'name': token.get('name', ''),
                        'symbol': token.get('symbol', ''),
                        'source': 'jupiter'
                    })
            return processed_tokens
    except Exception as e:
        logger.error(f"Error Jupiter: {e}")
    return []

async def get_dexscreener_active_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pares activos de Solana de DexScreener"""
    try:
        res = await client.get(TOKEN_SOURCES["dexscreener_solana"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            
            # Filtrar por antigüedad y Solana
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
            
            logger.info(f"[DEXSCREENER] Pares recientes (1-24h): {len(recent_pairs)}")
            return recent_pairs
    except Exception as e:
        logger.error(f"Error DexScreener: {e}")
    return []

async def get_dexscreener_new_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pares NUEVOS de DexScreener"""
    try:
        res = await client.get(TOKEN_SOURCES["dexscreener_new"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            
            # Filtrar SOLO Solana
            solana_pairs = []
            for pair in pairs:
                if pair.get('chainId') == 'solana':
                    solana_pairs.append(pair)
            
            logger.info(f"[DEXSCREENER] Pares nuevos Solana: {len(solana_pairs)}")
            return solana_pairs
    except Exception as e:
        logger.error(f"Error DexScreener nuevos: {e}")
    return []

async def get_all_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina todas las fuentes confiables"""
    all_tokens = []
    
    # Ejecutar en paralelo
    tasks = [
        get_jupiter_strict_tokens(client),
        get_dexscreener_active_pairs(client),
        get_dexscreener_new_pairs(client)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    # Procesar y normalizar tokens
    processed_tokens = []
    
    for item in all_tokens:
        if isinstance(item, dict):
            address = None
            name = ""
            symbol = ""
            
            # Para tokens de Jupiter
            if "address" in item and item.get("source") == "jupiter":
                address = item.get("address")
                name = item.get("name", "")
                symbol = item.get("symbol", "")
            
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
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN DE DATOS --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos actualizados de DexScreener para un token específico"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if pairs:
                # Escoger el par con mayor liquidez
                best_pair = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                return best_pair
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

# -------------------- FILTROS SIMPLIFICADOS --------------------
def passes_basic_filters(dex_data: dict) -> tuple:
    """Solo verifica liquidez y edad - SIN SEGURIDAD AUTOMÁTICA"""
    if not dex_data:
        return False, "sin datos"
    
    # 1. Verificar liquidez
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < MIN_LIQUIDITY:
        return False, f"liquidez ${liquidity:,.0f} < ${MIN_LIQUIDITY:,}"
    
    # 2. Verificar edad si está disponible
    created_at = dex_data.get('pairCreatedAt')
    if created_at:
        age_hours = (time.time() - (created_at / 1000)) / 3600
        if age_hours > MAX_AGE_HOURS:
            return False, f"demasiado viejo ({age_hours:.1f}h)"
        if age_hours < MIN_AGE_HOURS:
            return False, f"demasiado nuevo ({age_hours:.1f}h)"
        return True, f"OK - ${liquidity:,.0f} - {age_hours:.1f}h"
    
    # Si no hay timestamp, solo verificar liquidez
    return True, f"OK - ${liquidity:,.0f} - edad desconocida"

# -------------------- TAREAS PRINCIPALES --------------------
async def token_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Busca tokens nuevos constantemente"""
    logger.info("🚀 Iniciando Radar Mejorado...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                tokens = await get_all_recent_tokens(client)
                
                if not tokens:
                    logger.info("[RADAR] No se encontraron tokens en este ciclo.")
                else:
                    added_count = 0
                    for token in tokens:
                        address = token['address']
                        
                        if not address:
                            continue
                            
                        if address in incubator or address in watchlist:
                            continue
                            
                        # Agregar a incubadora
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'token_info': token,
                            'source': 'radar_mejorado'
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        added_count += 1
                        
                    logger.info(f"  - 🐣 {added_count} tokens nuevos añadidos a incubadora")
                    
                    # Notificar si encontramos muchos tokens
                    if added_count >= 3:
                        if TARGET_CHAT_ID:
                            await context.bot.send_message(
                                chat_id=TARGET_CHAT_ID,
                                text=f"🔍 *Radar activo:* {added_count} nuevos tokens encontrados y en revisión...",
                                parse_mode='Markdown'
                            )
                
                await asyncio.sleep(180)  # 3 minutos entre búsquedas
                
            except Exception as e:
                logger.error(f"Error en radar: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica tokens en incubadora - SOLO LIQUIDEZ Y EDAD"""
    logger.info("Iniciando Verificación de Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(90)  # Revisar cada 1.5 minutos
                
                if not incubator:
                    continue
                    
                now = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                approved_count = 0
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        # Eliminar después de 2 hora sin datos
                        if now - data['found_at'] > 7200:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # APLICAR FILTROS BÁSICOS
                    passes, reason = passes_basic_filters(dex_data)
                    
                    if passes:
                        # ✅ APROBAR - Cumple liquidez y edad
                        approved_at = now
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        created_at = dex_data.get('pairCreatedAt')
                        
                        age_info = "edad desconocida"
                        if created_at:
                            age_hours = (now - (created_at / 1000)) / 3600
                            age_info = f"{age_hours:.1f}h"
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'age_info': age_info,
                            'dex_data': dex_data,
                            'token_info': data['token_info']
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        # Eliminar de incubadora
                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        approved_count += 1

                        # NOTIFICACIÓN INMEDIATA
                        symbol = data['token_info'].get('symbol', 'N/A')
                        name = data['token_info'].get('name', 'N/A')
                        
                        message = (
                            f"🎯 *TOKEN CUMPLE CRITERIOS* 🎯\n\n"
                            f"*Token:* {symbol} ({name})\n"
                            f"*Address:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Edad:* `{age_info}`\n"
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

                        logger.info(f"  - ✅ APROBADO: {token_address}")

                    else:
                        # ❌ No cumple filtros básicos
                        logger.info(f"  - ❌ Rechazado: {token_address} - {reason}")
                        
                        # Eliminar después de 4 horas
                        if now - data['found_at'] > 14400:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(0.5)  # Pequeño delay
                
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
        "🚀 *Bot de Tokens Nuevos - VERSION MEJORADA*\n\n"
        "🎯 *Objetivo:* Encontrar tokens con ≥$5,000 liquidez\n"
        "🔍 *Fuentes Confiables:* Jupiter Strict + DexScreener\n"
        "⏰ *Búsqueda cada 3 minutos*\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual\n"
        "*/incubadora* - Tokens en revisión\n"
        "*/watchlist* - Tokens que cumplen criterios",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando búsqueda mejorada...*", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(token_radar_task(context)),
        asyncio.create_task(incubator_checker_task(context))
    ]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 No está cazando.")
        return
        
    for task in context.bot_data['tasks']:
        task.cancel()
    context.bot_data.clear()
    await update.message.reply_text("🛑 Búsqueda detenida.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 Bot detenido"
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ *Bot Activo - Buscando Tokens*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🎯 *Criterios:* ≥${MIN_LIQUIDITY:,} liquidez"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 Incubadora vacía")
        return
    message = f"🐣 *Tokens en Revisión ({len(incubator)}):*\n\n"
    for i, addr in enumerate(list(incubator.keys())[-15:][::-1], 1):
        token_data = incubator[addr].get('token_info', {})
        symbol = token_data.get('symbol', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol}\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    message = f"🏆 *Tokens que Cumplen ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(watchlist.items())[-20:][::-1], 1):
        liq = data.get('liquidity', 0)
        age = data.get('age_info', 'N/A')
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f} | ⏰ {age}\n"
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

    logger.info("--- Bot de Tokens Nuevos MEJORADO listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
