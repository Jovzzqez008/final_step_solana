"""
bot_jupiter_v2_pro_optimized_fixed.py

Bot optimizado con corrección para PostgreSQL existente
"""

import os
import asyncio
import json
import time
import logging
from datetime import datetime, timedelta
from statistics import pstdev, mean
from collections import defaultdict
import re

import aiohttp
import asyncpg
import websockets
from telegram import Bot
from telegram.constants import ParseMode

# ------------------ CONFIGURACIÓN ------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")

# Pump.fun
PUMPFUN_PROGRAM_ID = os.getenv("PUMPFUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_PRE_GRADUATION_THRESHOLD = float(os.getenv("PUMP_PRE_THRESHOLD", "60000"))

# FILTROS RELAJADOS
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "40000"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "5000"))
MAX_VOLUME_24H = float(os.getenv("MAX_VOLUME_24H", "300000"))

# Detección FLAT
MIN_FLAT_MINUTES = int(os.getenv("MIN_FLAT_MINUTES", "180"))
FLAT_STD_THRESHOLD = float(os.getenv("FLAT_STD_THRESHOLD", "0.15"))
VOLUME_SPIKE_THRESHOLD = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "100"))
MIN_CONSECUTIVE_LOW = int(os.getenv("MIN_CONSECUTIVE_LOW", "6"))

# Configuración de análisis
CANDLE_INTERVAL_MINUTES = 15
CANDLES_TO_CHECK = 16

# DB tables
DB_TABLE_NOTIFIED = "notified_mints"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_optimized.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("jupiter_bot_optimized")

# ------------------ ESTADO GLOBAL ------------------
http_session: aiohttp.ClientSession | None = None
pg_pool: asyncpg.Pool | None = None
telegram_bot: Bot | None = None

# ------------------ MANEJO DE CONEXIONES ------------------
async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        http_session = aiohttp.ClientSession(timeout=timeout)
    return http_session

async def safe_send_telegram(text: str, parse_mode=ParseMode.MARKDOWN):
    global telegram_bot
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram no configurado")
        return False
    
    if telegram_bot is None:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    
    try:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text=text, 
            parse_mode=parse_mode, 
            disable_web_page_preview=False
        )
        logger.info("✅ Alerta enviada a Telegram")
        return True
    except Exception as e:
        logger.error(f"❌ Error enviando telegram: {e}")
        return False

# ------------------ POSTGRESQL CORREGIDO ------------------
async def init_db():
    global pg_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no proporcionado; ejecutando sin persistencia.")
        return
    
    try:
        pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with pg_pool.acquire() as conn:
            # Primero verificar si la tabla existe
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = $1
                );
            """, DB_TABLE_NOTIFIED)
            
            if table_exists:
                logger.info("✅ Tabla existente detectada, verificando columnas...")
                # Verificar y agregar columnas faltantes si es necesario
                columns = await conn.fetch("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = $1;
                """, DB_TABLE_NOTIFIED)
                
                existing_columns = {row['column_name'] for row in columns}
                required_columns = {'mint', 'first_notified_at', 'data'}
                
                # Solo crear columnas faltantes
                if not required_columns.issubset(existing_columns):
                    logger.info("🔧 Actualizando estructura de tabla...")
                    await conn.execute(f"""
                        CREATE TABLE IF NOT EXISTS {DB_TABLE_NOTIFIED} (
                            mint TEXT PRIMARY KEY,
                            first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                            data JSONB
                        );
                    """)
            else:
                # Crear tabla nueva
                await conn.execute(f"""
                    CREATE TABLE {DB_TABLE_NOTIFIED} (
                        mint TEXT PRIMARY KEY,
                        first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                        data JSONB
                    );
                """)
                logger.info("✅ Tabla creada exitosamente")
            
        logger.info("✅ PostgreSQL inicializado correctamente")
        return True
    except Exception as e:
        logger.error(f"❌ Error inicializando PostgreSQL: {e}")
        return False

async def mark_notified(mint: str, data: dict):
    if pg_pool is None:
        return
    
    try:
        async with pg_pool.acquire() as conn:
            # Verificar estructura de la tabla
            columns = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = $1;
            """, DB_TABLE_NOTIFIED)
            
            existing_columns = {row['column_name'] for row in columns}
            
            if 'data' in existing_columns:
                await conn.execute(f"""
                    INSERT INTO {DB_TABLE_NOTIFIED}(mint, data) 
                    VALUES($1, $2)
                    ON CONFLICT (mint) DO UPDATE SET 
                        data = EXCLUDED.data
                """, mint, json.dumps(data))
            else:
                # Fallback para estructura simple
                await conn.execute(f"""
                    INSERT INTO {DB_TABLE_NOTIFIED}(mint) 
                    VALUES($1)
                    ON CONFLICT (mint) DO NOTHING
                """, mint)
                
        logger.debug(f"✅ Token {mint[:8]}... marcado como notificado")
    except Exception as e:
        logger.error(f"❌ Error marcando como notificado: {e}")

async def is_notified(mint: str) -> bool:
    if pg_pool is None:
        return False
    
    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT mint FROM {DB_TABLE_NOTIFIED} WHERE mint=$1", mint)
            return bool(row)
    except Exception as e:
        logger.error(f"❌ Error verificando notificación: {e}")
        return False

# ------------------ CLIENTE JUPITER OPTIMIZADO ------------------
class JupiterClient:
    def __init__(self, base_url=JUPITER_BASE):
        self.base = base_url
        self.cache = {}
        self.cache_ttl = 300

    async def request(self, path: str, cache_key: str = None, ttl: int = None):
        session = await get_http_session()
        ttl = ttl if ttl is not None else self.cache_ttl
        
        if cache_key and cache_key in self.cache:
            cached, ts = self.cache[cache_key]
            if time.time() - ts < ttl:
                return cached
        
        url = f"{self.base}{path}" if path.startswith("/") else f"{self.base}/{path}"
        
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if cache_key:
                        self.cache[cache_key] = (data, time.time())
                    return data
                else:
                    logger.debug(f"HTTP {resp.status} para {url}")
                    return None
        except Exception as e:
            logger.debug(f"Request error {url}: {e}")
            return None

    async def get_potential_flat_tokens(self):
        """Obtiene tokens con potencial de patrón FLAT - FILTROS RELAJADOS"""
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=40",
            "/tokens/v2/toptraded/1h?limit=30",
            "/tokens/v2/recent?limit=30"
        ]
        
        all_tokens = []
        for i, ep in enumerate(endpoints):
            data = await self.request(ep, cache_key=f"jup_ep_{i}", ttl=300)
            if data:
                all_tokens.extend(data)
            await asyncio.sleep(0.3)
        
        # FILTROS RELAJADOS - ENFOCADOS EN ENCONTRAR PLANOS
        filtered_tokens = []
        for token in all_tokens:
            if not isinstance(token, dict):
                continue
                
            mint = token.get('id')
            liquidity = token.get('liquidity', 0)
            volume24h = token.get('volume24h', 0) or 0
            
            # FILTROS MÍNIMOS RELAJADOS
            has_liquidity = liquidity >= MIN_LIQUIDITY
            has_recent_activity = volume24h >= MIN_VOLUME_24H and volume24h <= MAX_VOLUME_24H
            
            if has_liquidity and has_recent_activity and mint:
                filtered_tokens.append(token)
        
        logger.info(f"🎯 De {len(all_tokens)} tokens -> {len(filtered_tokens)} con potencial de plano")
        return filtered_tokens

    async def get_token_by_id(self, mint: str):
        return await self.request(f"/tokens/v2/search?query={mint}", cache_key=f"jup_token_{mint}", ttl=120)

jupiter = JupiterClient(JUPITER_BASE)

# ------------------ DEXSCREENER OPTIMIZADO ------------------
async def fetch_candles_dexscreener(mint: str, limit=CANDLES_TO_CHECK):
    """Obtiene velas de DexScreener"""
    session = await get_http_session()
    
    try:
        search_url = f"https://api.dexscreener.com/latest/dex/search/?q={mint}"
        async with session.get(search_url, timeout=8) as resp:
            if resp.status != 200:
                return []
                
            data = await resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return []
            
            # Encontrar par de Solana
            solana_pair = next((p for p in pairs if p.get('chainId') == 'solana'), pairs[0])
            pair_address = solana_pair.get('pairAddress')
            
            if not pair_address:
                return []
                
            pair_url = f"https://api.dexscreener.com/latest/dex/pair/{pair_address}"
            async with session.get(pair_url, timeout=8) as r2:
                if r2.status != 200:
                    return []
                    
                pair_data = await r2.json()
                candles = pair_data.get("candles") or pair_data.get("chart") or []
                
                if not candles:
                    return []
                
                # Normalizar velas
                normalized = []
                for c in candles[-limit:]:
                    if isinstance(c, list) and len(c) >= 6:
                        try:
                            normalized.append({
                                "time": c[0],
                                "open": float(c[1]) if c[1] else 0,
                                "high": float(c[2]) if c[2] else 0,
                                "low": float(c[3]) if c[3] else 0,
                                "close": float(c[4]) if c[4] else 0,
                                "volume": float(c[5]) if c[5] else 0
                            })
                        except (ValueError, TypeError):
                            continue
                
                logger.debug(f"📊 Obtenidas {len(normalized)} velas para {mint[:8]}...")
                return normalized
                
    except Exception as e:
        logger.debug(f"DexScreener error para {mint[:8]}...: {e}")
        return []

# ------------------ DETECCIÓN FLAT MEJORADA ------------------
def analyze_volume_pattern(volumes):
    """Analiza el patrón de volumen para identificar planos"""
    if len(volumes) < 10:
        return False, {}
    
    # Contar velas por categoría de volumen
    very_low_vol = sum(1 for v in volumes if v < 10)
    low_vol = sum(1 for v in volumes if 10 <= v < 50)
    medium_vol = sum(1 for v in volumes if 50 <= v <= 200)
    high_vol = sum(1 for v in volumes if v > 200)
    
    # Buscar picos aislados (patrón clave)
    isolated_spikes = 0
    for i in range(2, len(volumes)-2):
        if (volumes[i-2] < 15 and 
            volumes[i-1] < 15 and 
            volumes[i] > VOLUME_SPIKE_THRESHOLD and 
            volumes[i+1] < 15 and 
            volumes[i+2] < 15):
            isolated_spikes += 1
    
    # Calcular métricas
    avg_volume = sum(volumes) / len(volumes)
    volume_std = pstdev(volumes) if len(volumes) > 1 else 0
    
    # CONDICIÓN PRINCIPAL RELAJADA
    volume_condition = (
        (very_low_vol + low_vol) >= MIN_CONSECUTIVE_LOW and
        isolated_spikes >= 1 and
        avg_volume < 150 and
        volume_std < 200
    )
    
    return volume_condition, {
        "very_low_volume_candles": very_low_vol,
        "low_volume_candles": low_vol,
        "medium_volume_candles": medium_vol,
        "high_volume_candles": high_vol,
        "isolated_spikes": isolated_spikes,
        "avg_volume": round(avg_volume, 2),
        "volume_volatility": round(volume_std, 2)
    }

def calculate_price_stability(candles):
    """Calcula estabilidad del precio"""
    if len(candles) < 8:
        return None
        
    prices = [c.get("close", 0) for c in candles if c.get("close") is not None]
    if len(prices) < 8:
        return None
    
    # Calcular retornos porcentuales
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
            returns.append(ret)
    
    if not returns:
        return None
    
    # Calcular rango de precio
    price_range = max(prices) - min(prices)
    avg_price = sum(prices) / len(prices)
    range_percent = (price_range / avg_price * 100) if avg_price > 0 else 100
    
    # Calcular tendencia general
    first_price = prices[0]
    last_price = prices[-1]
    total_change = ((last_price - first_price) / first_price * 100) if first_price > 0 else 0
    
    return {
        "std_dev": round(pstdev(returns), 4) if len(returns) > 1 else 0,
        "max_move": round(max(abs(r) for r in returns), 2) if returns else 0,
        "price_range_percent": round(range_percent, 2),
        "total_change_percent": round(total_change, 2),
        "min_price": min(prices),
        "max_price": max(prices),
        "direction": "bajista" if total_change < -1 else "alcista" if total_change > 1 else "neutral"
    }

def detect_flat_pattern(candles):
    """Detección principal de patrón FLAT"""
    if len(candles) < CANDLES_TO_CHECK:
        return False, {"reason": "not_enough_candles"}
    
    volumes = [c.get('volume', 0) for c in candles]
    
    # Análisis de volumen
    vol_condition, vol_analysis = analyze_volume_pattern(volumes)
    
    # Análisis de precio
    price_analysis = calculate_price_stability(candles)
    if not price_analysis:
        return False, {"reason": "invalid_price_data"}
    
    # CONDICIONES RELAJADAS
    flat_detected = (
        vol_condition and
        price_analysis['std_dev'] < FLAT_STD_THRESHOLD and
        price_analysis['max_move'] < 1.0 and
        abs(price_analysis['total_change_percent']) < 5.0
    )
    
    return flat_detected, {
        **vol_analysis,
        "price_metrics": price_analysis,
        "candles_analyzed": len(candles),
        "pattern_type": f"flat_{price_analysis['direction']}" if flat_detected else "no_pattern"
    }

# ------------------ FLUJO DE ANÁLISIS MEJORADO ------------------
async def evaluate_token_flat(mint: str, token_meta: dict):
    """Evaluación de token para patrón FLAT"""
    if await is_notified(mint):
        return False, {"reason": "already_notified"}
    
    # Obtener velas
    candles = await fetch_candles_dexscreener(mint, CANDLES_TO_CHECK)
    if not candles or len(candles) < 12:
        return False, {"reason": "insufficient_candle_data"}
    
    # Analizar patrón
    is_flat, analysis = detect_flat_pattern(candles)
    
    if is_flat:
        logger.info(f"🎯 PATRÓN FLAT DETECTADO: {token_meta.get('symbol', 'N/A')} | {mint[:12]}... | Tipo: {analysis.get('pattern_type', 'N/A')}")
        analysis['detected_at'] = datetime.utcnow().isoformat()
        analysis['mint_short'] = mint[:12] + "..."
    
    return is_flat, analysis

async def basic_jupiter_audit(mint: str):
    """Auditoría básica del token"""
    try:
        res = await jupiter.get_token_by_id(mint)
        if not res:
            return {}
            
        token = res[0] if isinstance(res, list) and res else res
        if isinstance(token, dict):
            return token.get('audit', {})
        return {}
    except Exception as e:
        logger.debug(f"Audit error: {e}")
        return {}

# ------------------ FORMATO DE ALERTAS MEJORADO ------------------
def format_links(mint: str) -> str:
    """Formato mejorado de enlaces con mint completo"""
    return (
        f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
        f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
        f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
        f"• [Jupiter](https://jup.ag/tokens/{mint})\n"
        f"• [Solscan](https://solscan.io/token/{mint})\n"
    )

def format_mint_for_display(mint: str) -> str:
    """Formatea el mint para mostrar en Telegram"""
    return f"`{mint[:12]}...{mint[-6:]}`"

async def alert_flat_found(mint: str, token_meta: dict, flat_analysis: dict):
    """Envía alerta de patrón FLAT detectado"""
    symbol = token_meta.get('symbol', 'N/A')
    name = token_meta.get('name', 'N/A')
    liquidity = token_meta.get('liquidity', 0)
    volume24h = token_meta.get('volume24h', 0)
    
    audit = await basic_jupiter_audit(mint)
    mint_disabled = audit.get('mintAuthorityDisabled', False)
    freeze_disabled = audit.get('freezeAuthorityDisabled', False)
    
    # Análisis de volumen
    vol_analysis = (
        f"*📊 Análisis de Volumen:*\n"
        f"  • Velas volumen muy bajo: {flat_analysis.get('very_low_volume_candles', 0)}\n"
        f"  • Velas volumen bajo: {flat_analysis.get('low_volume_candles', 0)}\n"
        f"  • Picos aislados: {flat_analysis.get('isolated_spikes', 0)}\n"
        f"  • Volumen promedio: ${flat_analysis.get('avg_volume', 0)}\n"
        f"  • Volatilidad volumen: {flat_analysis.get('volume_volatility', 0)}\n"
    )
    
    # Métricas de precio
    price_metrics = flat_analysis.get('price_metrics', {})
    price_analysis = (
        f"*📈 Análisis de Precio:*\n"
        f"  • Volatilidad: {price_metrics.get('std_dev', 0)}%\n"
        f"  • Movimiento máximo: {price_metrics.get('max_move', 0)}%\n"
        f"  • Rango total: {price_metrics.get('price_range_percent', 0)}%\n"
        f"  • Cambio total: {price_metrics.get('total_change_percent', 0)}%\n"
        f"  • Dirección: {price_metrics.get('direction', 'N/A')}\n"
    )
    
    # Información del token
    token_info = (
        f"*🔍 Información del Token:*\n"
        f"  • Símbolo: {symbol}\n"
        f"  • Nombre: {name}\n"
        f"  • Liquidez: ${liquidity:,.0f}\n"
        f"  • Volumen 24h: ${volume24h:,.0f}\n"
        f"  • Mint: {format_mint_for_display(mint)}\n"
    )
    
    msg = (
        f"🚨 *PATRÓN FLAT DETECTADO* 🚨\n\n"
        f"{token_info}\n"
        f"{vol_analysis}\n"
        f"{price_analysis}\n"
        f"*🛡️ Auditoría:* MintDisabled: {'✅' if mint_disabled else '❌'} | FreezeDisabled: {'✅' if freeze_disabled else '❌'}\n\n"
        f"🔗 *Enlaces Rápidos:*\n{format_links(mint)}\n"
        f"_⏰ Detectado: {datetime.now().strftime('%H:%M')} - Patrón de {MIN_FLAT_MINUTES}min_"
    )
    
    if await safe_send_telegram(msg):
        await mark_notified(mint, {
            "meta": token_meta, 
            "analysis": flat_analysis,
            "type": "flat_pattern"
        })

async def alert_pumpfun_pre_graduation(mint: str, market_cap: float):
    """Alerta de pre-graduación de Pump.fun"""
    if await is_notified(mint):
        return
        
    token_data = await jupiter.get_token_by_id(mint)
    token_meta = token_data[0] if isinstance(token_data, list) and token_data else {}
    
    symbol = token_meta.get('symbol', 'N/A')
    name = token_meta.get('name', 'N/A')
    
    msg = (
        f"🔥 *PUMP.FUN PRE-GRADUACIÓN* 🔥\n\n"
        f"*Token:* {symbol} - {name}\n"
        f"*Mint:* {format_mint_for_display(mint)}\n"
        f"*Market Cap:* ${market_cap:,.0f}\n"
        f"*Umbral:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n\n"
        f"🔗 *Enlaces:*\n{format_links(mint)}\n\n"
        f"_⚠️ Cerca de graduación (69k) - Actuar con cautela_"
    )
    
    if await safe_send_telegram(msg):
        await mark_notified(mint, {
            "market_cap": market_cap,
            "meta": token_meta,
            "type": "pumpfun_pre_graduation"
        })

# ------------------ WORKERS OPTIMIZADOS ------------------
async def flat_scanner_worker(stop_event: asyncio.Event):
    """Worker optimizado para scanner FLAT"""
    logger.info("🔄 Scanner FLAT iniciado - Buscando Planos Reales")
    
    while not stop_event.is_set():
        try:
            # Obtener tokens con potencial
            potential_tokens = await jupiter.get_potential_flat_tokens()
            
            if not potential_tokens:
                logger.warning("No se encontraron tokens con potencial")
                await asyncio.sleep(45)
                continue
            
            logger.info(f"🎯 Analizando {len(potential_tokens)} tokens con potencial de plano")
            
            # Procesar tokens
            sem = asyncio.Semaphore(4)
            
            async def process_token(token):
                async with sem:
                    mint = token.get('id')
                    if not mint:
                        return
                    
                    is_flat, analysis = await evaluate_token_flat(mint, token)
                    if is_flat:
                        await alert_flat_found(mint, token, analysis)
            
            tasks = [process_token(token) for token in potential_tokens]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            logger.info(f"✅ Ciclo de análisis completado. Tokens procesados: {len(potential_tokens)}")
            
        except Exception as e:
            logger.error(f"❌ Error en scanner FLAT: {e}")
        
        await asyncio.sleep(60)

async def pumpfun_wss_worker(stop_event: asyncio.Event):
    """Worker para Pump.fun WebSocket"""
    if not HELIUS_WSS_URL:
        logger.warning("HELIUS_WSS_URL no configurado")
        return
        
    logger.info("🔄 Pump.fun WebSocket iniciado")
    
    while not stop_event.is_set():
        try:
            async with websockets.connect(
                HELIUS_WSS_URL, 
                ping_interval=30, 
                ping_timeout=20
            ) as ws:
                logger.info("✅ Conectado a Helius WebSocket")
                
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [PUMPFUN_PROGRAM_ID]},
                        {"commitment": "confirmed"}
                    ]
                }
                await ws.send(json.dumps(subscribe_msg))
                
                while not stop_event.is_set():
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=40)
                        data = json.loads(message)
                        await process_pumpfun_message(data)
                        
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("🔌 Conexión WebSocket cerrada")
                        break
                        
        except Exception as e:
            logger.error(f"❌ Error WebSocket Pump.fun: {e}")
            await asyncio.sleep(5)

async def process_pumpfun_message(data: dict):
    """Procesa mensajes de Pump.fun"""
    try:
        result = data.get('params', {}).get('result', {})
        logs = result.get('value', {}).get('logs', [])
        
        if not logs:
            return
        
        logs_text = " ".join(str(log) for log in logs)
        
        # Buscar market cap
        market_cap_patterns = [
            r"market_cap[\s:=]+([\d,]+\.?\d*)",
            r"marketcap[\s:=]+([\d,]+\.?\d*)", 
            r"mc[\s:=]+([\d,]+\.?\d*)",
        ]
        
        market_cap = None
        for pattern in market_cap_patterns:
            match = re.search(pattern, logs_text, re.IGNORECASE)
            if match:
                try:
                    market_cap = float(match.group(1).replace(',', ''))
                    break
                except ValueError:
                    continue
        
        if not market_cap or market_cap < PUMP_PRE_GRADUATION_THRESHOLD:
            return
        
        # Buscar mint address
        mint_pattern = r"([1-9A-HJ-NP-Za-km-z]{32,44})"
        mint_match = re.search(mint_pattern, logs_text)
        
        if mint_match:
            mint = mint_match.group(1)
            logger.info(f"🎯 Pump.fun detectado: {mint[:12]}... MC: ${market_cap:,.0f}")
            await alert_pumpfun_pre_graduation(mint, market_cap)
            
    except Exception as e:
        logger.debug(f"Error procesando mensaje Pump.fun: {e}")

# ------------------ MAIN ------------------
async def main():
    logger.info("🚀 INICIANDO BOT JUPITER OPTIMIZADO v2.0")
    logger.info("==========================================")
    
    # Mostrar configuración
    logger.info(f"🎯 Configuración de Búsqueda:")
    logger.info(f"   • Liquidez mínima: ${MIN_LIQUIDITY:,.0f}")
    logger.info(f"   • Volumen 24h: ${MIN_VOLUME_24H:,.0f} - ${MAX_VOLUME_24H:,.0f}")
    logger.info(f"   • Flat mínimo: {MIN_FLAT_MINUTES}min")
    logger.info(f"   • Enfoque: Patrones reales (alcistas/bajistas)")
    
    # Inicializar PostgreSQL
    if not await init_db():
        logger.error("❌ No se pudo inicializar PostgreSQL")
        return
    
    stop_event = asyncio.Event()
    
    try:
        await asyncio.gather(
            flat_scanner_worker(stop_event),
            pumpfun_wss_worker(stop_event),
            return_exceptions=True
        )
    except KeyboardInterrupt:
        logger.info("🛑 Bot detenido por usuario")
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}")
    finally:
        stop_event.set()
        if http_session:
            await http_session.close()
        if pg_pool:
            await pg_pool.close()
        logger.info("✅ Bot finalizado")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Interrumpido por usuario")
