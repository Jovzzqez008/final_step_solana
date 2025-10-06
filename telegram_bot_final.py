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

# JUPITER V2 API
JUPITER_V2_BASE = "https://lite-api.jup.ag/tokens/v2"
JUPITER_V2_RECENT = f"{JUPITER_V2_BASE}/recent"
JUPITER_V2_TRENDING = f"{JUPITER_V2_BASE}/toptrending/1h"
JUPITER_V2_TOP_ORGANIC = f"{JUPITER_V2_BASE}/toporganicscore/1h"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# FILTROS
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

# -------------------- JUPITER V2 API --------------------
def calculate_token_age(created_at_str: str) -> float:
    """Calcula la edad del token en horas"""
    try:
        created_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        current_dt = datetime.utcnow().replace(tzinfo=created_dt.tzinfo)
        age_seconds = (current_dt - created_dt).total_seconds()
        return age_seconds / 3600
    except Exception as e:
        logger.debug(f"Error calculando edad: {e}")
        return None

async def get_jupiter_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens RECIENTES de Jupiter V2"""
    try:
        logger.info("Consultando Jupiter V2 /recent...")
        res = await client.get(JUPITER_V2_RECENT, headers=HEADERS, timeout=20)
        
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER V2 RECENT] Tokens obtenidos: {len(tokens)}")
            
            processed_tokens = []
            for token in tokens:
                if isinstance(token, dict) and token.get('id'):
                    age_hours = None
                    first_pool = token.get('firstPool', {})
                    if first_pool and first_pool.get('createdAt'):
                        age_hours = calculate_token_age(first_pool['createdAt'])
                    
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
            logger.warning(f"Jupiter V2 recent responded {res.status_code}")
            
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
    
    tasks = [
        get_jupiter_recent_tokens(client),
        get_jupiter_trending_tokens(client),
        get_jupiter_organic_tokens(client)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    unique_tokens = {}
    for token in all_tokens:
        addr = token['address']
        if addr and addr not in unique_tokens:
            unique_tokens[addr] = token
    
    logger.info(f"🎯 TOTAL tokens únicos Jupiter V2 (1-24h): {len(unique_tokens)}")
    
    if unique_tokens:
        sample_tokens = list(unique_tokens.values())[:3]
        logger.info("📋 Ejemplos de tokens encontrados:")
        for token in sample_tokens:
            age_info = f"{token.get('age_hours', 'N/A'):.1f}h" if token.get('age_hours') else 'edad N/A'
            liq = token.get('liquidity', 0)
            logger.info(f"  - {token['symbol']}: {age_info}, ${liq:,.0f} liquidez")
    
    return list(unique_tokens.values())

# -------------------- VERIFICACIÓN CON DEXSCREENER --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos actualizados de DexScreener"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
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

# -------------------- FILTROS DE SEGURIDAD (SIN RIESGO ALTO) --------------------
async def check_rugcheck_safety(client: httpx.AsyncClient, token_address: str) -> tuple:
    """Verifica seguridad con RugCheck - SIN FILTRO DE RIESGO ALTO"""
    try:
        url = f"https://api.rugcheck.xyz/api/tokens/{token_address}"
        res = await client.get(url, timeout=10)
        
        if res.status_code == 200:
            data = res.json()
            
            # FILTRO 1: Liquidez bloqueada (CRÍTICO)
            locked_liquidity = data.get('lockedLiquidity', False)
            if not locked_liquidity:
                return False, "❌ LIQUIDEZ NO BLOQUEADA"
            
            # FILTRO 2: Honeypot detection (CRÍTICO)
            is_honeypot = data.get('isHoneypot', False)
            if is_honeypot:
                return False, "❌ HONEYPOT DETECTADO"
            
            # ✅ NO APLICAMOS FILTRO DE RIESGO ALTO como solicitaste
            risk = data.get('risk', '')
            risk_info = f"Riesgo: {risk}" if risk else "Sin info riesgo"
                
            return True, f"✅ Seguridad OK - {risk_info}"
            
    except Exception as e:
        logger.debug(f"Error RugCheck {token_address}: {e}")
    
    return False, "❌ Sin datos de seguridad"

def check_holder_distribution(dex_data: dict) -> tuple:
    """Verifica distribución de holders para evitar whales"""
    try:
        holders = dex_data.get('holders', [])
        if holders:
            # Verificar top holder
            top_holder = holders[0] if isinstance(holders[0], dict) else None
            if top_holder:
                top_holder_percent = top_holder.get('percentage', 0)
                
                # FILTRO: Top holder no puede tener más del 25%
                if top_holder_percent > 25:
                    return False, f"❌ WHALE DOMINANTE: {top_holder_percent}%"
                
                # FILTRO: Top 5 holders no pueden tener más del 70%
                top5_percent = sum(h.get('percentage', 0) for h in holders[:5])
                if top5_percent > 70:
                    return False, f"❌ CONCENTRACIÓN ALTA: {top5_percent}% en top 5"
        
        return True, "✅ Distribución OK"
        
    except Exception:
        return True, "⚠️ No hay datos de holders"

def check_liquidity_quality(dex_data: dict) -> tuple:
    """Verifica calidad de la liquidez"""
    try:
        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
        
        # FILTRO 1: Liquidez mínima
        if liquidity < MIN_LIQUIDITY:
            return False, f"❌ LIQUIDEZ INSUFICIENTE: ${liquidity:,.0f}"
        
        # FILTRO 2: Verificar si la liquidez es real
        pairs = dex_data.get('pairs', [dex_data])
        valid_pools = sum(1 for p in pairs if p.get('liquidity', {}).get('usd', 0) > 1000)
        
        if valid_pools < 1:
            return False, "❌ SIN POOLS DE LIQUIDEZ"
        
        return True, f"✅ Liquidez: ${liquidity:,.0f}"
        
    except Exception:
        return True, "⚠️ Error verificación liquidez"

def check_token_age_and_activity(dex_data: dict) -> tuple:
    """Verifica edad y actividad del token"""
    try:
        pair_created_at = dex_data.get('pairCreatedAt')
        if pair_created_at:
            age_hours = (time.time() - (pair_created_at / 1000)) / 3600
            
            # FILTRO: Mínimo 1 hora, máximo 24 horas
            if age_hours < MIN_AGE_HOURS:
                return False, f"❌ DEMASIADO NUEVO: {age_hours:.1f}h"
            if age_hours > MAX_AGE_HOURS:
                return False, f"❌ DEMASIADO VIEJO: {age_hours:.1f}h"
            
            return True, f"✅ Edad: {age_hours:.1f}h"
        
        return True, "⚠️ Sin timestamp"
        
    except Exception:
        return True, "⚠️ Error verificación edad"

# -------------------- TAREAS PRINCIPALES CON SEGURIDAD --------------------
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
                            
                        if address in incubator or address in watchlist:
                            continue
                            
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
                    
                    if added_count > 0 and TARGET_CHAT_ID:
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"🔍 *Jupiter V2 activo:* {added_count} tokens recientes encontrados! Verificando seguridad...",
                            parse_mode='Markdown'
                        )
                
                await asyncio.sleep(120)
                
            except Exception as e:
                logger.error(f"Error en radar Jupiter V2: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verificación MEJORADA con filtros de seguridad (sin riesgo alto)"""
    logger.info("Iniciando Verificación de Incubadora CON SEGURIDAD...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(60)
                
                if not incubator:
                    continue
                    
                now = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                approved_count = 0
                security_rejects = 0
                
                for token_address, data in list(incubator.items()):
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        if now - data['found_at'] > 3600:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # 🔐 APLICAR FILTROS DE SEGURIDAD (SIN RIESGO ALTO)
                    security_checks = []
                    
                    # 1. Verificación RugCheck (sin riesgo alto)
                    rugcheck_safe, rugcheck_reason = await check_rugcheck_safety(client, token_address)
                    security_checks.append(rugcheck_safe)
                    
                    # 2. Distribución de holders
                    holder_safe, holder_reason = check_holder_distribution(dex_data)
                    security_checks.append(holder_safe)
                    
                    # 3. Calidad de liquidez
                    liquidity_safe, liquidity_reason = check_liquidity_quality(dex_data)
                    security_checks.append(liquidity_safe)
                    
                    # 4. Edad y actividad
                    age_safe, age_reason = check_token_age_and_activity(dex_data)
                    security_checks.append(age_safe)
                    
                    # 📊 EVALUAR RESULTADOS
                    all_security_passed = all(security_checks)
                    
                    if all_security_passed:
                        # ✅ TOKEN SEGURO - PASAR A WATCHLIST
                        approved_at = now
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'dex_data': dex_data,
                            'token_info': data['token_info'],
                            'security_checks': {
                                'rugcheck': rugcheck_reason,
                                'holders': holder_reason,
                                'liquidity': liquidity_reason,
                                'age': age_reason
                            }
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        approved_count += 1

                        # 📢 NOTIFICACIÓN CON INFO DE SEGURIDAD
                        token_info = data['token_info']
                        symbol = token_info.get('symbol', 'N/A')
                        name = token_info.get('name', 'N/A')
                        age_hours = token_info.get('age_hours', 'N/A')
                        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
                        organic_score = token_info.get('organic_score', 'N/A')
                        
                        message = (
                            f"🛡️ *TOKEN SEGURO DETECTADO* 🛡️\n\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Name:* {name}\n"
                            f"*Address:* `{token_address}`\n"
                            f"*Edad:* {age_str}\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Score Orgánico:* {organic_score}\n"
                            f"*Cambio 24h:* `{price_change}%`\n\n"
                            f"🔒 *Verificaciones de Seguridad:*\n"
                            f"- RugCheck: {rugcheck_reason}\n"
                            f"- Holders: {holder_reason}\n" 
                            f"- Liquidez: {liquidity_reason}\n"
                            f"- Edad: {age_reason}\n\n"
                            f"🔍 *Verificar manualmente:*\n"
                            f"- DexScreener: https://dexscreener.com/solana/{token_address}\n"
                            f"- RugCheck: https://rugcheck.xyz/tokens/{token_address}\n"
                            f"- BirdEye: https://birdeye.so/token/{token_address}?chain=solana\n\n"
                            f"✅ *Pasó todos los filtros de seguridad*"
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

                        logger.info(f"  - ✅ APROBADO: {symbol} - Seguridad verificada")

                    else:
                        # ❌ RECHAZADO POR SEGURIDAD
                        security_rejects += 1
                        rejection_reasons = [
                            f"RugCheck: {rugcheck_reason}",
                            f"Holders: {holder_reason}",
                            f"Liquidez: {liquidity_reason}", 
                            f"Edad: {age_reason}"
                        ]
                        
                        active_reasons = [r for r in rejection_reasons if '❌' in r]
                        reason_text = " | ".join(active_reasons) if active_reasons else "Varios filtros fallaron"
                        
                        logger.info(f"  - ❌ RECHAZADO: {token_address} - {reason_text}")
                        
                        # Eliminar inmediatamente por problemas de seguridad
                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                    
                    await asyncio.sleep(0.5)
                
                # 📈 REPORTE DEL CICLO
                if approved_count > 0 or security_rejects > 0:
                    logger.info(f"🎯 Ciclo completado: {approved_count} aprobados, {security_rejects} rechazados por seguridad")
                    
            except Exception as e:
                logger.error(f"Error en verificador de seguridad: {e}")
                await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Jupiter V2 - CON SEGURIDAD*\n\n"
        "🎯 *Objetivo:* Tokens 1-24h + ≥$5,000 liquidez\n"
        "🛡️ *Filtros de Seguridad:*\n"
        "  • Liquidez bloqueada (RugCheck)\n"
        "  • No honeypot (RugCheck)\n"
        "  • Distribución de holders\n"
        "  • Edad 1-24 horas\n"
        "  • Liquidez mínima $5,000\n\n"
        "⚠️ *NO se filtra por riesgo alto* (como solicitaste)\n\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener\n"
        "*/status* - Estado actual",
        parse_mode='Markdown'
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 Ya está cazando.")
        return
        
    await update.message.reply_text("🏹 *Iniciando Jupiter V2 Radar CON SEGURIDAD...*", parse_mode='Markdown')
    
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
            f"✅ *Jupiter V2 CON SEGURIDAD Activo*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🛡️ *Filtros:* Liquidez bloqueada + No honeypot + Holders"
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
    message = f"🏆 *Tokens SEGUROS Aprobados ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(watchlist.items())[-15:][::-1], 1):
        liq = data.get('liquidity', 0)
        token_info = data.get('token_info', {})
        symbol = token_info.get('symbol', 'N/A')
        age_hours = token_info.get('age_hours', 'N/A')
        age_str = f"{age_hours:.1f}h" if isinstance(age_hours, (int, float)) else age_hours
        security = data.get('security_checks', {})
        rugcheck_status = security.get('rugcheck', 'N/A')
        status_icon = "🛡️" if "✅" in rugcheck_status else "⚠️"
        message += f"{i}. `{addr}`\n   📛 {symbol} | 💰 ${liq:,.0f} | ⏰ {age_str} | {status_icon}\n"
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

    logger.info("--- Bot Jupiter V2 CON SEGURIDAD listo ---")
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == '__main__':
    main()
