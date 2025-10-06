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

# MÚLTIPLES FUENTES DE TOKENS - Para máximo flujo
TOKEN_SOURCES = {
    "birdeye_trending": "https://public-api.birdeye.so/defi/token_trending?sort_by=volume&sort_type=desc&offset=0&limit=100",
    "pump_fun": "https://api.pump.fun/coins",  # Tokens recién lanzados
    "dexscreener_solana": "https://api.dexscreener.com/latest/dex/search?q=SOL&limit=50",
    "jupiter_strict": "https://api.jup.ag/tokens/v1/tokens"  # Intentaremos con headers mejorados
}

# Headers agresivos para evitar bloqueos
AGGRESSIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://birdeye.so/",
    "Origin": "https://birdeye.so",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site"
}

# Umbrales RELAJADOS para capturar más tokens
LIQUIDITY_THRESHOLD = 5000  # Bajamos a $5K para más oportunidades
MAX_TOKEN_AGE_HOURS = 24    # Ampliamos a 24 horas

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

# -------------------- DATABASE (MANTENER IGUAL) --------------------
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

# -------------------- MÚLTIPLES FUENTES DE TOKENS --------------------
async def get_birdeye_trending(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens trending de Birdeye - MUY CONFIABLE"""
    try:
        headers = {**AGGRESSIVE_HEADERS}
        headers["X-API-KEY"] = "public"  # Birdeye tiene API pública
        
        res = await client.get(TOKEN_SOURCES["birdeye_trending"], headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            tokens = data.get("data", {}).get("tokens", [])
            logger.info(f"[BIRDEYE] Tokens trending obtenidos: {len(tokens)}")
            return tokens
    except Exception as e:
        logger.error(f"Error Birdeye: {e}")
    return []

async def get_pump_fun_coins(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens recién lanzados de Pump.fun"""
    try:
        res = await client.get(TOKEN_SOURCES["pump_fun"], headers=AGGRESSIVE_HEADERS, timeout=15)
        if res.status_code == 200:
            coins = res.json()
            # Filtrar coins muy recientes (últimas 24 horas)
            now = time.time()
            recent_coins = []
            for coin in coins[:50]:  # Limitar a 50 más recientes
                created_at = coin.get("createdAt")
                if created_at:
                    age_hours = (now - created_at) / 3600
                    if age_hours <= MAX_TOKEN_AGE_HOURS:
                        recent_coins.append(coin)
            logger.info(f"[PUMP.FUN] Coins recientes: {len(recent_coins)}")
            return recent_coins
    except Exception as e:
        logger.error(f"Error Pump.fun: {e}")
    return []

async def get_dexscreener_solana(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pares de Solana de DexScreener"""
    try:
        res = await client.get(TOKEN_SOURCES["dexscreener_solana"], headers=AGGRESSIVE_HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            # Filtrar por antigüedad
            now = time.time()
            recent_pairs = []
            for pair in pairs:
                created_at = pair.get("pairCreatedAt")
                if created_at:
                    age_hours = (now - (created_at / 1000)) / 3600
                    if age_hours <= MAX_TOKEN_AGE_HOURS:
                        recent_pairs.append(pair)
            logger.info(f"[DEXSCREENER] Pares recientes: {len(recent_pairs)}")
            return recent_pairs
    except Exception as e:
        logger.error(f"Error DexScreener: {e}")
    return []

async def get_all_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina múltiples fuentes para máximo flujo de tokens"""
    all_tokens = []
    
    # Ejecutar todas las fuentes en paralelo
    tasks = [
        get_birdeye_trending(client),
        get_pump_fun_coins(client),
        get_dexscreener_solana(client)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for result in results:
        if isinstance(result, list):
            all_tokens.extend(result)
    
    # Eliminar duplicados por dirección
    unique_tokens = {}
    for token in all_tokens:
        address = None
        if isinstance(token, dict):
            address = (token.get("address") or token.get("mint") or 
                      (token.get("baseToken") or {}).get("address"))
        
        if address and address not in unique_tokens:
            unique_tokens[address] = token
    
    logger.info(f"🎯 TOTAL tokens únicos de todas las fuentes: {len(unique_tokens)}")
    return list(unique_tokens.values())

# -------------------- APIS DE VERIFICACIÓN --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos específicos de un token"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, headers=AGGRESSIVE_HEADERS, timeout=10)
        if res.status_code == 200 and res.json().get('pairs'):
            pairs = res.json()['pairs']
            if pairs:
                # Escoger el par con mayor liquidez
                best_pair = sorted(pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
                return best_pair
    except Exception as e:
        logger.debug(f"Error DexScreener para {token_address}: {e}")
    return None

async def get_rugcheck_data(client: httpx.AsyncClient, token_address: str):
    """Consulta RugCheck API"""
    url = f"https://api.rugcheck.xyz/api/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logger.debug(f"Error RugCheck para {token_address}: {e}")
    return None

# -------------------- FILTROS MEJORADOS --------------------
def passes_rugcheck_filters(rugcheck_data: dict) -> tuple:
    """Filtros ESSENCIALES de RugCheck"""
    if not rugcheck_data:
        return False, "sin datos de RugCheck"
    
    # FILTRO CRÍTICO: Liquidez bloqueada
    locked_liquidity = rugcheck_data.get('lockedLiquidity', False)
    if not locked_liquidity:
        return False, "liquidez NO bloqueada"
    
    # FILTRO CRÍTICO: Riesgo alto
    risk = rugcheck_data.get('risk', '').lower()
    if risk in ['high risk', 'rugpull', 'scam']:
        return False, f"riesgo alto: {risk}"
    
    return True, "OK"

def passes_dexscreener_filters(dex_data: dict) -> tuple:
    """Filtros de liquidez y antigüedad"""
    if not dex_data:
        return False, "sin datos de DexScreener"
    
    # Liquidez mínima
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < LIQUIDITY_THRESHOLD:
        return False, f"liquidez insuficiente (${liquidity:,.0f})"
    
    # Verificar que no sea demasiado antiguo
    pair_created_at = dex_data.get('pairCreatedAt')
    if pair_created_at:
        age_hours = (time.time() - (pair_created_at / 1000)) / 3600
        if age_hours > MAX_TOKEN_AGE_HOURS:
            return False, f"demasiado antiguo ({age_hours:.1f}h)"
    
    return True, "OK"

# -------------------- TAREAS PRINCIPALES --------------------
async def multi_source_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar que usa MÚLTIPLES fuentes para máximo flujo de tokens"""
    logger.info("🚀 Iniciando Radar Multi-Fuente...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Obtener tokens de todas las fuentes
                recent_tokens = await get_all_recent_tokens(client)
                
                if not recent_tokens:
                    logger.info("[RADAR] No se obtuvieron tokens en este ciclo.")
                else:
                    tokens_added = 0
                    for token_data in recent_tokens:
                        # Extraer dirección del token
                        address = (token_data.get("address") or token_data.get("mint") or 
                                  (token_data.get("baseToken") or {}).get("address"))
                        
                        if not address:
                            continue
                            
                        # Evitar duplicados
                        if address in incubator or address in watchlist:
                            continue
                            
                        # Agregar a incubadora
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'source': 'Multi-Fuente',
                            'raw_data': token_data
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        tokens_added += 1
                        
                    logger.info(f"  - 🐣 {tokens_added} nuevos tokens añadidos a incubadora")
                
                # Esperar 2 minutos entre ciclos (más frecuente)
                await asyncio.sleep(120)
                
            except asyncio.CancelledError:
                logger.info("Radar multi-fuente detenido.")
                break
            except Exception as e:
                logger.error(f"Error en radar multi-fuente: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica tokens en incubadora con filtros RugCheck"""
    logger.info("Iniciando Vigilante de la Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(60)  # Verificar cada 1 minuto
                
                if not incubator:
                    continue
                    
                now_ts = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                promoted_count = 0
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        # Si no hay datos después de 30 minutos, descartar
                        if now_ts - data['found_at'] > 1800:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # Obtener RugCheck
                    rugcheck_data = await get_rugcheck_data(client, token_address)
                    if not rugcheck_data:
                        continue

                    # APLICAR FILTROS
                    dex_passes, dex_reason = passes_dexscreener_filters(dex_data)
                    rugcheck_passes, rugcheck_reason = passes_rugcheck_filters(rugcheck_data)
                    
                    if dex_passes and rugcheck_passes:
                        # ✅ PROMOVER A WATCHLIST
                        approved_at = now_ts
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'last_notified': 'initial',
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'dex_data': dex_data,
                            'rugcheck': rugcheck_data
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        promoted_count += 1

                        # NOTIFICACIÓN URGENTE
                        risk = rugcheck_data.get('risk', 'N/A')
                        locked_liq = rugcheck_data.get('lockedLiquidity', False)
                        
                        message = (
                            f"🚨 *OPORTUNIDAD INMEDIATA* 🚨\n\n"
                            f"*Token:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Cambio 24h:* `{price_change}%`\n"
                            f"*Liquidez Bloqueada:* `{'✅ SÍ' if locked_liq else '❌ NO'}`\n"
                            f"*Riesgo:* `{risk}`\n\n"
                            f"🔍 *Verificar rápido:*\n"
                            f"- RugCheck: rugcheck.xyz/tokens/{token_address}\n"
                            f"- DexScreener: dexscreener.com/solana/{token_address}\n\n"
                            f"⚡ *Token recién detectado - Actúa rápido*"
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

                        logger.info(f"  - 🏆 PROMOVIDO: {token_address}")

                    else:
                        # ❌ No pasa filtros
                        rejection_reasons = []
                        if not dex_passes: rejection_reasons.append(dex_reason)
                        if not rugcheck_passes: rejection_reasons.append(rugcheck_reason)
                        
                        logger.info(f"  - ❌ Rechazado: {token_address} - {', '.join(rejection_reasons)}")
                        
                        # Remover después de 2 horas
                        if now_ts - data['found_at'] > 7200:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    # Pequeño delay entre tokens
                    await asyncio.sleep(0.5)
                
                if promoted_count > 0:
                    logger.info(f"🎯 Total promovidos en este ciclo: {promoted_count}")
                    
            except asyncio.CancelledError:
                logger.info("Vigilante de incubadora detenido.")
                break
            except Exception as e:
                logger.error(f"Error en vigilante: {e}")
                await asyncio.sleep(10)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo simple de watchlist"""
    logger.info("Iniciando Monitor de Watchlist...")
    while True:
        try:
            await asyncio.sleep(300)
            # Monitoreo básico - puedes expandir esto luego
            pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en monitor: {e}")
            await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM (SIMPLIFICADOS) --------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Multi-Fuente Activado*\n\n"
        "🔍 *Fuentes:* Birdeye + Pump.fun + DexScreener\n"
        "🛡️ *Filtros:* Liquidez bloqueada + Riesgo bajo\n"
        "💧 *Liquidez mínima:* $5,000\n"
        "⏰ *Antigüedad máxima:* 24 horas\n\n"
        "*/cazar* - Iniciar monitoreo agresivo\n"
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
        
    await update.message.reply_text("🏹 *Iniciando Caza Agresiva...*\n\n🎯 Múltiples fuentes activadas\n🛡️ Filtros RugCheck activos\n⚡ Buscando tokens recientes", parse_mode='Markdown')
    
    await setup_database()
    global incubator, watchlist
    incubator = await db_load_all_incubator()
    watchlist = await db_load_all_watchlist()

    context.bot_data['tasks'] = [
        asyncio.create_task(multi_source_radar_task(context)),
        asyncio.create_task(incubator_checker_task(context)),
        asyncio.create_task(watchlist_monitor_task(context))
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
            f"✅ *Bot Activo - Caza Agresiva*\n\n"
            f"🐣 *Incubadora:* {len(incubator)} tokens\n"
            f"🏆 *Watchlist:* {len(watchlist)} tokens\n"
            f"🎯 *Fuentes:* Birdeye + Pump.fun + DexScreener\n"
            f"🛡️ *Filtros:* Liquidez bloqueada + Riesgo bajo"
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 Incubadora vacía")
        return
    message = f"🐣 *Incubadora ({len(incubator)}):*\n\n"
    for i, addr in enumerate(list(incubator.keys())[-10:][::-1], 1):
        message += f"{i}. `{addr}`\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 Watchlist vacía")
        return
    message = f"🏆 *Watchlist ({len(watchlist)}):*\n\n"
    for i, (addr, data) in enumerate(list(watchlist.items())[-15:][::-1], 1):
        liq = data.get('liquidity', 0)
        risk = data.get('rugcheck', {}).get('risk', 'N/A')
        message += f"{i}. `{addr}`\n   💰 ${liq:,.0f} | 🛡️ {risk}\n"
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

    logger.info("--- Bot Multi-Fuente listo. Ejecutando... ---")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Error fatal: {e}")

if __name__ == '__main__':
    main()
