# bot_jupiter_v2_pro_v3.py
import os
import asyncio
import json
import time
import logging
import re
from datetime import datetime
from statistics import pstdev, mean
from collections import deque, defaultdict

import aiohttp
import asyncpg
import websockets
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== CONFIGURACIÓN =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "")
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")

# PUMP.FUN CONFIGURACIÓN FIJA
PUMPFUN_PROGRAM_ID = "pumpfun1Mt11111111111111111111111111111111"
PUMP_PRE_GRADUATION_THRESHOLD = float(os.getenv("PUMP_PRE_THRESHOLD", "60000"))

# PARÁMETROS AJUSTADOS - TIEMPO FLAT 100 MINUTOS
DEFAULTS = {
    "MIN_VOLUME_24H": float(os.getenv("MIN_VOLUME_24H", "50000")),
    "MIN_LIQUIDITY": float(os.getenv("MIN_LIQUIDITY", "50000")),
    "MIN_FLAT_MINUTES": int(os.getenv("MIN_FLAT_MINUTES", "100")),  # AJUSTADO A 100 MINUTOS
    "FLAT_STD_THRESHOLD": float(os.getenv("FLAT_STD_THRESHOLD", "0.12")),
    "VOLUME_SPIKE_THRESHOLD": float(os.getenv("VOLUME_SPIKE_THRESHOLD", "120")),
    "MIN_CONSECUTIVE_LOW": int(os.getenv("MIN_CONSECUTIVE_LOW", "5")),
    "CANDLES_TO_CHECK": int(os.getenv("CANDLES_TO_CHECK", "10")),
    "BREAKOUT_STEP": float(os.getenv("BREAKOUT_STEP", "10.0"))
}

DB_TABLE_NOTIFIED = "notified_mints"

# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v3")

# ===================== ESTADO GLOBAL =====================
http_session = None
pg_pool = None
telegram_bot = None
app_bot = None

price_histories = defaultdict(lambda: deque(maxlen=300))
flat_tokens = {}
monitored_tokens = set()
params = DEFAULTS.copy()

# ===================== FUNCIONES DE APOYO =====================
async def get_http_session():
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    return http_session

async def send_telegram(text: str, parse_mode=ParseMode.MARKDOWN):
    global telegram_bot
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token/chat no configurado")
        return
    if telegram_bot is None:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text=text, 
            parse_mode=parse_mode, 
            disable_web_page_preview=False
        )
    except Exception as e:
        logger.error(f"Error enviando Telegram: {e}")

# ===================== BASE DE DATOS =====================
async def init_db():
    global pg_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no configurado; ejecutando sin persistencia")
        return
    pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pg_pool.acquire() as conn:
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_NOTIFIED} (
                mint TEXT PRIMARY KEY,
                first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                data JSONB
            );
        """)
    logger.info("Base de datos inicializada")

async def mark_notified(mint: str, data: dict):
    if pg_pool is None:
        flat_tokens.setdefault(mint, {})['notified'] = True
        return
    async with pg_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {DB_TABLE_NOTIFIED}(mint, data) VALUES($1, $2) ON CONFLICT (mint) DO NOTHING", 
            mint, 
            json.dumps(data)
        )

async def is_notified(mint: str) -> bool:
    if pg_pool is None:
        return flat_tokens.get(mint, {}).get('notified', False)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT mint FROM {DB_TABLE_NOTIFIED} WHERE mint=$1", mint)
        return bool(row)

# ===================== CLIENTE JUPITER =====================
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
                    logger.debug(f"Jupiter request {url} status {resp.status}")
                    return None
        except Exception as e:
            logger.debug(f"Error request Jupiter: {e}")
            return None

    async def get_multiple_token_sources(self):
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=80",
            "/tokens/v2/toptraded/1h?limit=80", 
            "/tokens/v2/tag?query=verified",
            "/tokens/v2/recent?limit=50"
        ]
        results = []
        for i, ep in enumerate(endpoints):
            data = await self.request(ep, cache_key=f"jup_ep_{i}", ttl=600)
            if data:
                results.extend(data)
        return results

    async def get_token_by_id(self, mint: str):
        return await self.request(f"/tokens/v2/search?query={mint}", cache_key=f"jup_token_{mint}", ttl=60)

jupiter = JupiterClient()

# ===================== CANDLES DEXSCREENER =====================
async def fetch_candles_dexscreener(mint: str, interval_minutes=15, limit=None):
    limit = limit or params['CANDLES_TO_CHECK']
    session = await get_http_session()
    try:
        search_url = f"https://api.dexscreener.com/latest/dex/search/?q={mint}"
        async with session.get(search_url, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    pair = pairs[0]
                    pair_id = pair.get("pairAddress") or pair.get("pair")
                    pair_url = f"https://api.dexscreener.com/latest/dex/pair/{pair_id}"
                    
                    async with session.get(pair_url, timeout=8) as r2:
                        if r2.status == 200:
                            pair_data = await r2.json()
                            candles = pair_data.get("candles") or pair_data.get("chart") or []
                            
                            normalized = []
                            for c in candles[-limit:]:
                                if isinstance(c, list) and len(c) >= 6:
                                    normalized.append({
                                        "time": c[0],
                                        "open": c[1], 
                                        "high": c[2],
                                        "low": c[3],
                                        "close": c[4],
                                        "volume": c[5]
                                    })
                            return normalized
    except Exception as e:
        logger.debug(f"Error Dexscreener para {mint}: {e}")
    return []

# ===================== DETECCIÓN AVANZADA PATRÓN FLAT =====================
def calculate_volatility_from_candles(candles):
    prices = [c.get("close") for c in candles if c.get("close") is not None]
    if len(prices) < 4:
        return None
        
    returns = []
    for i in range(1, len(prices)):
        prev = prices[i-1]
        if prev == 0:
            continue
        ret = (prices[i] - prev) / prev * 100
        returns.append(ret)
    
    if not returns:
        return None
        
    return {
        "std_dev": pstdev(returns) if len(returns) > 1 else 0,
        "max_move": max(abs(x) for x in returns) if returns else 0,
        "avg_move": mean([abs(x) for x in returns]) if returns else 0,
        "min_price": min(prices) if prices else 0,
        "max_price": max(prices) if prices else 0,
    }

def analyze_volume_sequence(volumes, colors):
    """Analiza secuencia de volumen para patrón FLAT tipo PESHI"""
    if len(volumes) < 6:
        return False, {}
    
    low_cut = 20
    spike_thresh = params['VOLUME_SPIKE_THRESHOLD']
    min_consec_low = params['MIN_CONSECUTIVE_LOW']
    
    # Contar volúmenes por categoría
    low_count = sum(1 for v in volumes if v < low_cut)
    med_count = sum(1 for v in volumes if low_cut <= v <= 200)
    high_count = sum(1 for v in volumes if v > 200)
    
    # Buscar picos aislados (alto volumen rodeado de bajo volumen)
    isolated_spikes = 0
    for i in range(1, len(volumes)-1):
        prev_low = volumes[i-1] < 15
        current_high = volumes[i] > spike_thresh
        next_low = volumes[i+1] < 20
        
        if prev_low and current_high and next_low:
            isolated_spikes += 1
    
    # Condición mejorada para volumen fantasma
    volume_condition = (
        low_count >= min_consec_low and
        med_count <= 3 and
        high_count <= 2 and 
        isolated_spikes >= 1
    )
    
    return volume_condition, {
        "low_count": low_count,
        "med_count": med_count,
        "high_count": high_count,
        "isolated_spikes": isolated_spikes
    }

def detect_volume_manipulation(volumes, window=4):
    """Detecta patrones sospechosos de manipulación de volumen"""
    if len(volumes) < window:
        return False
    
    avg_volume = sum(volumes) / len(volumes)
    if avg_volume == 0:
        return False
    
    # Buscar picos anómalos (>10x el volumen promedio) aislados
    anomalous_spikes = 0
    for i, vol in enumerate(volumes):
        if vol > avg_volume * 10:
            # Verificar si es un pico aislado
            window_start = max(0, i-2)
            window_end = min(len(volumes), i+3)
            window_volumes = volumes[window_start:window_end]
            
            high_volumes_in_window = sum(1 for v in window_volumes if v > avg_volume * 5)
            if high_volumes_in_window <= 1:
                anomalous_spikes += 1
    
    return anomalous_spikes > 1

def detect_ghost_volume_pattern(candles):
    """Detección principal del patrón FLAT mejorado"""
    if len(candles) < 8:
        return False, {}
    
    volumes = [c.get('volume', 0) for c in candles]
    colors = ['green' if c.get('close', 0) >= c.get('open', 0) else 'red' for c in candles]
    
    # Análisis de secuencia de volumen
    vol_condition, vol_details = analyze_volume_sequence(volumes, colors)
    
    # Análisis de volatilidad
    volatility_metrics = calculate_volatility_from_candles(candles)
    if not volatility_metrics:
        return False, {}
    
    # Detección de manipulación
    manipulation_detected = detect_volume_manipulation(volumes)
    
    # Condiciones combinadas para patrón FLAT real
    flat_condition = (
        vol_condition and
        not manipulation_detected and
        volatility_metrics['std_dev'] < params['FLAT_STD_THRESHOLD'] and
        volatility_metrics['max_move'] < 0.8 and
        ((volatility_metrics['max_price'] - volatility_metrics['min_price']) / 
         (volatility_metrics['min_price'] or 1) * 100) < 2.0
    )
    
    return flat_condition, {
        **vol_details, 
        'metrics': volatility_metrics,
        'manipulation_detected': manipulation_detected,
        'avg_volume': sum(volumes) / len(volumes)
    }

async def evaluate_token_flat(mint: str):
    """Evaluación mejorada del token para patrón FLAT"""
    candles = await fetch_candles_dexscreener(mint)
    if not candles:
        return False, {"reason": "no_candles"}
    
    last_candles = candles[-params['CANDLES_TO_CHECK']:]
    is_flat, details = detect_ghost_volume_pattern(last_candles)
    
    return is_flat, {"candles_checked": len(last_candles), **details}

# ===================== AUDITORÍA JUPITER =====================
async def basic_jupiter_audit(mint: str):
    try:
        res = await jupiter.get_token_by_id(mint)
        if not res:
            return {}
        if isinstance(res, list) and res:
            token = res[0]
        elif isinstance(res, dict):
            token = res
        else:
            token = res
        audit = token.get('audit', {}) if isinstance(token, dict) else {}
        return audit
    except Exception as e:
        logger.debug(f"Error auditoría Jupiter para {mint}: {e}")
        return {}

# ===================== FORMATEO DE ALERTAS =====================
def format_links(mint: str) -> str:
    return (
        f"• DexScreener: https://dexscreener.com/solana/{mint}\n"
        f"• RugCheck: https://rugcheck.xyz/tokens/{mint}\n"
        f"• Birdeye: https://birdeye.so/token/{mint}?chain=solana\n"
    )

async def alert_flat_found(mint: str, token_meta: dict, flat_info: dict):
    if await is_notified(mint):
        logger.info(f"Token {mint} ya notificado, omitiendo")
        return
    
    symbol = token_meta.get('symbol', 'N/A')
    name = token_meta.get('name', 'N/A')
    liquidity = token_meta.get('liquidity', 0)
    volume24 = token_meta.get('volume24h', 0)

    audit = await basic_jupiter_audit(mint)
    mint_auth_disabled = audit.get('mintAuthorityDisabled', False)
    freeze_disabled = audit.get('freezeAuthorityDisabled', False)

    msg = (
        f"🚨 *TOKEN FLAT DETECTADO* 🚨\n\n"
        f"*Token:* {symbol} — {name}\n"
        f"*Mint Address:* {mint}\n"
        f"*Liquidez:* ${liquidity:,.0f}\n"
        f"*Volumen 24h:* ${volume24:,.0f}\n"
        f"*Mint Authority:* {'✅ DESACTIVADA' if mint_auth_disabled else '❌ ACTIVA'}\n"
        f"*Freeze Authority:* {'✅ DESACTIVADA' if freeze_disabled else '❌ ACTIVA'}\n\n"
        f"*Análisis Técnico:*\n"
        f"• Velas analizadas: {flat_info.get('candles_checked', 0)}\n"
        f"• Velas volumen bajo: {flat_info.get('low_count', 0)}\n"
        f"• Picos aislados: {flat_info.get('isolated_spikes', 0)}\n"
        f"• Volumen promedio: ${flat_info.get('avg_volume', 0):.2f}\n"
        f"• STD Precio: {flat_info.get('metrics', {}).get('std_dev', 0):.4f}\n\n"
        f"🔗 *ENLACES:*\n{format_links(mint)}\n"
        f"_Token en estado FLAT >{params['MIN_FLAT_MINUTES']} minutos - Listo para breakout_"
    )

    await send_telegram(msg)
    await mark_notified(mint, {
        "meta": token_meta, 
        "flat_info": flat_info, 
        "type": "flat_pattern",
        "detected_at": datetime.utcnow().isoformat()
    })

async def alert_pumpfun_pre_graduation(mint: str, market_cap: float, token_meta: dict = None):
    if await is_notified(mint):
        return

    symbol = token_meta.get('symbol', 'N/A') if token_meta else 'N/A'
    name = token_meta.get('name', 'N/A') if token_meta else 'N/A'

    msg = (
        f"🔥 *PUMP.FUN PRE-GRADUACIÓN* 🔥\n\n"
        f"*Token:* {symbol} — {name}\n"
        f"*Mint Address:* {mint}\n"
        f"*Market Cap Estimado:* ${market_cap:,.0f}\n"
        f"*Umbral Alerta:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n\n"
        f"🔗 *ENLACES:*\n{format_links(mint)}\n\n"
        f"_Token cerca de graduación (69K MC) - Verificar inmediatamente_"
    )

    await send_telegram(msg)
    await mark_notified(mint, {
        "pump_detected_mc": market_cap, 
        "meta": token_meta,
        "type": "pumpfun_pre_graduation",
        "detected_at": datetime.utcnow().isoformat()
    })

# ===================== WORKERS =====================
async def flat_scanner_worker(stop_event: asyncio.Event):
    """Worker mejorado para detección de patrón FLAT"""
    logger.info("🚀 Flat scanner worker mejorado iniciado")
    while not stop_event.is_set():
        try:
            candidates = await jupiter.get_multiple_token_sources()
            uniq = {}
            for t in candidates:
                tid = t.get('id') if isinstance(t, dict) else None
                if tid:
                    uniq[tid] = t
            token_list = list(uniq.keys())
            logger.info(f"🔍 Flat scanner analizando {len(token_list)} candidatos")
            
            sem = asyncio.Semaphore(6)

            async def eval_one(mint):
                async with sem:
                    if await is_notified(mint):
                        return
                    token_meta = uniq.get(mint, {})
                    is_flat, details = await evaluate_token_flat(mint)
                    if is_flat:
                        details['detected_at'] = datetime.utcnow().isoformat()
                        await alert_flat_found(mint, {
                            'symbol': token_meta.get('symbol'),
                            'name': token_meta.get('name'),
                            'liquidity': token_meta.get('liquidity'),
                            'volume24h': (token_meta.get('stats24h', {}).get('buyVolume', 0) + 
                                         token_meta.get('stats24h', {}).get('sellVolume', 0)),
                            'organic_score': token_meta.get('organicScore')
                        }, details)

            tasks = [asyncio.create_task(eval_one(m)) for m in token_list[:120]]
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"❌ Error flat scanner: {e}")
        await asyncio.sleep(30)

async def pumpfun_wss_worker(stop_event: asyncio.Event):
    """Worker para monitorizar Pump.fun vía WebSocket"""
    if not HELIUS_WSS_URL:
        logger.warning("HELIUS_WSS_URL no configurado, pumpfun_wss_worker no se ejecutará")
        return
        
    logger.info("🚀 Pump.fun WSS worker iniciado")
    while not stop_event.is_set():
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=20, ping_timeout=10) as ws:
                logger.info("✅ Conectado a HELIUS WSS para Pump.fun")
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [{
                        "mentions": [PUMPFUN_PROGRAM_ID]
                    }, {"commitment": "processed"}]
                }
                await ws.send(json.dumps(subscribe_msg))
                
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        msg = json.loads(raw)
                        params_msg = msg.get('params')
                        if not params_msg:
                            continue
                            
                        result = params_msg.get('result') if isinstance(params_msg, dict) else params_msg[0].get('result')
                        if not result:
                            continue
                            
                        value = result.get('value') if isinstance(result, dict) else None
                        logs = value.get('logs') if value else []
                        text = "\n".join([str(l) for l in logs])
                        
                        # Buscar market cap en los logs
                        for m in re.finditer(r"market_cap\W*[:=]\W*(\d+[.,]?\d*)", text, re.IGNORECASE):
                            mc = float(m.group(1).replace(',', ''))
                            if mc >= PUMP_PRE_GRADUATION_THRESHOLD:
                                # Buscar mint address
                                mm = re.search(r"mint\W*[:=]\W*([A-Za-z0-9]{32,44})", text)
                                mint = mm.group(1) if mm else None
                                if mint and not await is_notified(mint):
                                    meta = await jupiter.get_token_by_id(mint)
                                    token_meta = meta[0] if isinstance(meta, list) and meta else (meta if isinstance(meta, dict) else {})
                                    await alert_pumpfun_pre_graduation(mint, mc, token_meta)
                                    
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                        
        except Exception as e:
            logger.error(f"❌ Error conexión WSS Pump.fun: {e}")
            await asyncio.sleep(5)

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Jupiter V3 Bot - Cazador de Tokens FLAT + Pump.fun*\n\n"
        "🎯 *Configuración Actual:*\n"
        f"• Tiempo FLAT mínimo: {params['MIN_FLAT_MINUTES']} minutos\n"
        f"• Pre-graduación Pump.fun: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f} MC\n"
        f"• STD Threshold: {params['FLAT_STD_THRESHOLD']}\n\n"
        "📊 *Comandos disponibles:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener monitoreo\n"
        "• /status - Estado del sistema\n"
        "• /planos - Lista de tokens planos detectados\n"
        "• /tokens - Lista de tokens monitoreados",
        parse_mode="Markdown"
    )

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo"""
    global monitor_task, pump_task, stop_evt
    
    if not stop_evt.is_set():
        await update.message.reply_text("⚠️ Monitoreo ya activo")
        return
        
    stop_evt.clear()
    monitor_task = asyncio.create_task(flat_scanner_worker(stop_evt))
    pump_task = asyncio.create_task(pumpfun_wss_worker(stop_evt))
    
    await update.message.reply_text(
        "🎯 *SISTEMA DE CAZA ACTIVADO*\n\n"
        f"• FLAT Scanner: {params['MIN_FLAT_MINUTES']} minutos mínimos\n"
        f"• Pump.fun Sniper: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}+ MC\n"
        f"• Análisis: {params['CANDLES_TO_CHECK']} velas\n\n"
        "_Buscando oportunidades..._",
        parse_mode="Markdown"
    )

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    stop_evt.set()
    await update.message.reply_text("🛑 Monitoreo detenido")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del sistema"""
    flat_count = len(flat_tokens)
    notified_count = "DB" if pg_pool else "Memory"
    
    status_msg = (
        f"📊 *Estado del Sistema V3*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if not stop_evt.is_set() else '🔴 DETENIDO'}\n"
        f"📈 Tokens monitoreados: {len(monitored_tokens)}\n"
        f"📉 Tokens FLAT detectados: {flat_count}\n"
        f"💾 Persistencia: {notified_count}\n\n"
        f"⚙️ *Configuración FLAT:*\n"
        f"• Mínimo: {params['MIN_FLAT_MINUTES']} minutos\n"
        f"• STD: {params['FLAT_STD_THRESHOLD']}\n"
        f"• Volumen Spike: ${params['VOLUME_SPIKE_THRESHOLD']}\n"
        f"• Velas analizadas: {params['CANDLES_TO_CHECK']}"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista de tokens planos detectados"""
    if not flat_tokens:
        await update.message.reply_text("📭 No hay tokens planos detectados")
        return
    
    current_time = time.time()
    flat_list = "📊 *TOKENS PLANOS DETECTADOS:*\n\n"
    
    for i, (mint, flat_info) in enumerate(list(flat_tokens.items())[:15], 1):
        symbol = flat_info.get('symbol', 'N/A')
        flat_since = flat_info.get('flat_since', current_time)
        flat_minutes = int((current_time - flat_since) / 60)
        
        flat_list += (
            f"{i}. *{symbol}*\n"
            f"   📍 `{mint}`\n"
            f"   ⏰ {flat_minutes} minutos en flat\n"
            f"   🔗 https://dexscreener.com/solana/{mint}\n\n"
        )
    
    if len(flat_tokens) > 15:
        flat_list += f"📋 ... y {len(flat_tokens) - 15} tokens más\n"
    
    await update.message.reply_text(flat_list, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista de tokens siendo monitoreados"""
    if not monitored_tokens:
        await update.message.reply_text("📭 No hay tokens siendo monitoreados")
        return
    
    tokens_list = "🎯 *TOKENS MONITOREADOS:*\n\n"
    
    for i, mint in enumerate(list(monitored_tokens)[:15], 1):
        metadata = token_metadata.get(mint, {})
        symbol = metadata.get('symbol', 'N/A')
        
        tokens_list += (
            f"{i}. *{symbol}*\n"
            f"   📍 `{mint}`\n"
            f"   💰 Liq: ${metadata.get('liquidity', 0):,.0f}\n"
            f"   🔗 https://dexscreener.com/solana/{mint}\n\n"
        )
    
    if len(monitored_tokens) > 15:
        tokens_list += f"📋 ... y {len(monitored_tokens) - 15} tokens más\n"
    
    await update.message.reply_text(tokens_list, parse_mode="Markdown")

# ===================== MAIN =====================
async def main():
    global stop_evt, monitor_task, pump_task, app_bot
    
    logger.info("🚀 Iniciando Jupiter v2 Pro - Bot V3 Mejorado")
    await init_db()
    
    stop_evt = asyncio.Event()
    stop_evt.set()  # Inicialmente detenido

    # Iniciar workers
    monitor_task = asyncio.create_task(flat_scanner_worker(stop_evt))
    pump_task = asyncio.create_task(pumpfun_wss_worker(stop_evt))

    # Configurar bot de Telegram para comandos
    if TELEGRAM_BOT_TOKEN:
        app_bot = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Registrar comandos
        app_bot.add_handler(CommandHandler("start", cmd_start))
        app_bot.add_handler(CommandHandler("cazar", cmd_cazar))
        app_bot.add_handler(CommandHandler("parar", cmd_parar))
        app_bot.add_handler(CommandHandler("status", cmd_status))
        app_bot.add_handler(CommandHandler("planos", cmd_planos))
        app_bot.add_handler(CommandHandler("tokens", cmd_tokens))
        
        # Ejecutar bot de Telegram en segundo plano
        asyncio.create_task(app_bot.run_polling())

    try:
        await asyncio.gather(monitor_task, pump_task)
    except asyncio.CancelledError:
        logger.info("🔴 Bot cancelado")
    finally:
        stop_evt.set()
        if http_session:
            await http_session.close()
        if pg_pool:
            await pg_pool.close()
        if app_bot:
            await app_bot.shutdown()
        logger.info("✅ Apagado completo")

if __name__ == '__main__':
    stop_evt = asyncio.Event()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🔴 Interrumpido por el usuario")
