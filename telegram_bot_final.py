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

# FUENTES CONFIABLES
TOKEN_SOURCES = {
    "dexscreener_new_pairs": "https://api.dexscreener.com/latest/dex/pairs/new",
    "jupiter_strict": "https://cache.jup.ag/strict-tokens"
}

# Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://jup.ag/"
}

# Umbrales
LIQUIDITY_THRESHOLD = 5000  # $5,000
MIN_TOKEN_AGE_HOURS = 1     # Mínimo 1 hora
MAX_TOKEN_AGE_HOURS = 24    # Máximo 24 horas

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
# [ ... mismo código de database ... ]

# -------------------- FUENTES CONFIABLES --------------------
async def get_jupiter_strict_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene tokens de Jupiter Strict List"""
    try:
        res = await client.get(TOKEN_SOURCES["jupiter_strict"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            tokens = res.json()
            logger.info(f"[JUPITER] Tokens strict obtenidos: {len(tokens)}")
            return tokens
        else:
            logger.warning(f"Jupiter responded {res.status_code}")
    except Exception as e:
        logger.error(f"Error Jupiter strict: {e}")
    return []

async def get_dexscreener_new_pairs(client: httpx.AsyncClient) -> List[Dict]:
    """Obtiene pares NUEVOS de DexScreener"""
    try:
        res = await client.get(TOKEN_SOURCES["dexscreener_new_pairs"], headers=HEADERS, timeout=15)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            # Filtrar solo Solana
            solana_pairs = [pair for pair in pairs if pair.get('chainId') == 'solana']
            logger.info(f"[DEXSCREENER] Pares nuevos Solana: {len(solana_pairs)}")
            return solana_pairs
        else:
            logger.warning(f"DexScreener new pairs responded {res.status_code}")
    except Exception as e:
        logger.error(f"Error DexScreener new pairs: {e}")
    return []

async def get_all_recent_tokens(client: httpx.AsyncClient) -> List[Dict]:
    """Combina fuentes CONFIABLES"""
    all_tokens = []
    
    # Ejecutar fuentes en paralelo
    tasks = [
        get_jupiter_strict_tokens(client),
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
            # Para tokens de Jupiter strict
            if 'address' in item and 'name' in item:
                processed_tokens.append({
                    'type': 'jupiter_token',
                    'address': item['address'],
                    'name': item.get('name', ''),
                    'symbol': item.get('symbol', ''),
                    'source': 'jupiter_strict'
                })
            # Para pares de DexScreener
            elif 'baseToken' in item:
                base_token = item.get('baseToken', {})
                address = base_token.get('address')
                if address:
                    processed_tokens.append({
                        'type': 'dex_pair',
                        'address': address,
                        'name': base_token.get('name', ''),
                        'symbol': base_token.get('symbol', ''),
                        'pair_data': item,
                        'source': 'dexscreener'
                    })
    
    # Eliminar duplicados
    unique_tokens = {}
    for token in processed_tokens:
        address = token.get('address')
        if address and address not in unique_tokens:
            unique_tokens[address] = token
    
    logger.info(f"🎯 TOTAL tokens únicos: {len(unique_tokens)}")
    return list(unique_tokens.values())

# -------------------- APIS DE VERIFICACIÓN --------------------
async def get_dexscreener_token_data(client: httpx.AsyncClient, token_address: str):
    """Obtiene datos específicos de un token"""
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

# -------------------- FILTROS (SOLO LIQUIDEZ Y EDAD) --------------------
def passes_filters(dex_data: dict) -> tuple:
    """Filtros de liquidez y edad"""
    if not dex_data:
        return False, "sin datos de DexScreener"
    
    # Liquidez mínima
    liquidity = dex_data.get('liquidity', {}).get('usd', 0)
    if liquidity < LIQUIDITY_THRESHOLD:
        return False, f"liquidez insuficiente (${liquidity:,.0f})"
    
    # Edad del token
    pair_created_at = dex_data.get('pairCreatedAt')
    if not pair_created_at:
        return False, "no hay datos de creación"
    
    now = time.time()
    age_hours = (now - (pair_created_at / 1000)) / 3600
    
    if age_hours < MIN_TOKEN_AGE_HOURS:
        return False, f"demasiado nuevo ({age_hours:.2f} horas)"
    if age_hours > MAX_TOKEN_AGE_HOURS:
        return False, f"demasiado antiguo ({age_hours:.2f} horas)"
    
    return True, "OK"

# -------------------- TAREAS PRINCIPALES --------------------
async def multi_source_radar_task(context: ContextTypes.DEFAULT_TYPE):
    """Radar con fuentes CONFIABLES"""
    logger.info("🚀 Iniciando Radar...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Obtener tokens de fuentes confiables
                recent_tokens = await get_all_recent_tokens(client)
                
                if not recent_tokens:
                    logger.info("[RADAR] No se obtuvieron tokens en este ciclo.")
                else:
                    tokens_added = 0
                    for token_data in recent_tokens:
                        address = token_data.get('address')
                        if not address:
                            continue
                            
                        # Evitar duplicados
                        if address in incubator or address in watchlist:
                            continue
                            
                        # Agregar a incubadora
                        found_at = time.time()
                        data = {
                            'found_at': found_at,
                            'source': token_data.get('source', 'unknown'),
                            'token_data': token_data
                        }
                        incubator[address] = data
                        await db_add_to_incubator(address, data)
                        tokens_added += 1
                        
                    logger.info(f"  - 🐣 {tokens_added} nuevos tokens añadidos a incubadora")
                
                # Esperar 3 minutos entre ciclos
                await asyncio.sleep(180)
                
            except asyncio.CancelledError:
                logger.info("Radar detenido.")
                break
            except Exception as e:
                logger.error(f"Error en radar: {e}")
                await asyncio.sleep(30)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    """Verifica tokens en incubadora por liquidez y edad"""
    logger.info("Iniciando Vigilante de la Incubadora...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(120)  # Verificar cada 2 minutos
                
                if not incubator:
                    continue
                    
                now_ts = time.time()
                logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
                promoted_count = 0
                
                for token_address, data in list(incubator.items()):
                    # Obtener datos actualizados
                    dex_data = await get_dexscreener_token_data(client, token_address)
                    if not dex_data:
                        # Si no hay datos después de 1 hora, descartar
                        if now_ts - data['found_at'] > 3600:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                        continue

                    # APLICAR FILTROS (solo liquidez y edad)
                    passes, reason = passes_filters(dex_data)
                    
                    if passes:
                        # ✅ PROMOVER A WATCHLIST
                        approved_at = now_ts
                        liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                        price_change = dex_data.get('priceChange', {}).get('h24', 0)
                        pair_created_at = dex_data.get('pairCreatedAt')
                        age_hours = (now_ts - (pair_created_at / 1000)) / 3600 if pair_created_at else 0
                        
                        watch_data = {
                            'approved_at': approved_at,
                            'last_notified': 'initial',
                            'liquidity': liquidity,
                            'price_change_24h': price_change,
                            'age_hours': age_hours,
                            'dex_data': dex_data
                        }
                        watchlist[token_address] = watch_data
                        await db_add_to_watchlist(token_address, watch_data)

                        del incubator[token_address]
                        await db_remove_from_incubator(token_address)
                        promoted_count += 1

                        # NOTIFICACIÓN
                        message = (
                            f"🚨 *TOKEN ENCONTRADO* 🚨\n\n"
                            f"*Token:* `{token_address}`\n"
                            f"*Liquidez:* `${liquidity:,.2f}`\n"
                            f"*Cambio 24h:* `{price_change}%`\n"
                            f"*Edad:* `{age_hours:.2f} horas`\n\n"
                            f"🔍 *Verificar:*\n"
                            f"- DexScreener: dexscreener.com/solana/{token_address}\n"
                            f"- RugCheck: rugcheck.xyz/tokens/{token_address}"
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
                        logger.debug(f"  - ❌ Rechazado: {token_address} - {reason}")
                        
                        # Remover después de 3 horas
                        if now_ts - data['found_at'] > 10800:
                            del incubator[token_address]
                            await db_remove_from_incubator(token_address)
                    
                    # Pequeño delay entre tokens
                    await asyncio.sleep(0.5)
                
                if promoted_count > 0:
                    logger.info(f"🎯 Total promovidos: {promoted_count}")
                    
            except asyncio.CancelledError:
                logger.info("Vigilante detenido.")
                break
            except Exception as e:
                logger.error(f"Error en vigilante: {e}")
                await asyncio.sleep(10)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo de watchlist"""
    logger.info("Iniciando Monitor de Watchlist...")
    while True:
        try:
            await asyncio.sleep(300)
            # Monitoreo básico
            pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en monitor: {e}")
            await asyncio.sleep(10)

# -------------------- COMANDOS TELEGRAM --------------------
# [ ... mismo código de comandos ... ]

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

    logger.info("--- Bot de Tokens Recientes listo ---")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Error fatal: {e}")

if __name__ == '__main__':
    main()
