"""
bot_jupiter_v2_pro_full.py

Monolítico y modularizado en un solo archivo para desplegar en Railway.
Incluye:
 - Scanner de tokens 'FLAT' (estilo PESHI) usando Jupiter V2 + DexScreener
 - Monitor Pump.fun pre-graduación vía WebSocket (HELIUS_WSS_URL)
 - Verificación rápida con Jupiter V2 (audit: mintAuthority / freezeAuthority)
 - Alerta por Telegram (un único canal)
 - Almacenamiento simple de mints notificados en Postgres (asyncpg)

Dependencias (requirements.txt):
aiohttp
asyncpg
python-telegram-bot==20.3
websockets
sqlalchemy[aio]

Notas importantes:
 - Ajusta los endpoints de DexScreener si tu cuenta/plan requiere API key.
 - Debes definir las variables de entorno que ya tienes en Railway:
   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL,
   HELIUS_RPC_URL, HELIUS_WSS_URL, DEXSCREENER_API (opcional),
   JUPITER_LITE_URL (opcional, por defecto https://lite-api.jup.ag)
 - Define PUMPFUN_PROGRAM_ID si quieres filtrar por programa de Pump.fun en el WebSocket

Uso:
 1) Despliega a Railway con las variables de entorno
 2) Ejecuta: python bot_jupiter_v2_pro_full.py

"""

import os
import asyncio
import json
import time
import logging
from datetime import datetime, timedelta
from statistics import pstdev, mean
from collections import deque, defaultdict

import aiohttp
import asyncpg
import websockets
from telegram import Bot
from telegram.constants import ParseMode

# ------------------ CONFIG & ENV ------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "")  # optional
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")
JUPITER_TOKENS_V2 = f"{JUPITER_BASE}/tokens/v2"

# Pump.fun specifics
PUMPFUN_PROGRAM_ID = os.getenv("PUMPFUN_PROGRAM_ID", "pumpfun1Mt11111111111111111111111111111111")
PUMP_PRE_GRADUATION_THRESHOLD = float(os.getenv("PUMP_PRE_THRESHOLD", "60000"))  # 60k

# Monitoring params (editable)
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "70000"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "70000"))
MIN_FLAT_MINUTES = int(os.getenv("MIN_FLAT_MINUTES", "22"))
FLAT_STD_THRESHOLD = float(os.getenv("FLAT_STD_THRESHOLD", "0.12"))
BREAKOUT_STEP = float(os.getenv("BREAKOUT_STEP", "10.0"))

# Candle settings
CANDLE_INTERVAL_MINUTES = 15
CANDLES_TO_CHECK = 12  # lookback of 12 candles * 15m = 3 hours (adjustable)

# DB tables
DB_TABLE_NOTIFIED = "notified_mints"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_bot_full")

# ------------------ GLOBAL STATE ------------------
http_session: aiohttp.ClientSession | None = None
pg_pool: asyncpg.Pool | None = None
telegram_bot: Bot | None = None

# price history per token (keep recent samples)
price_histories = defaultdict(lambda: deque(maxlen=200))
flat_tokens = {}  # token -> info
monitored_tokens = set()

# ------------------ UTIL HELPERS ------------------
async def get_http_session():
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    return http_session

async def send_telegram(text: str, parse_mode=ParseMode.MARKDOWN):
    global telegram_bot
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram token/chat not configured. Skipping alert.")
        return
    if telegram_bot is None:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=False)
    except Exception as e:
        logger.error(f"Error sending telegram: {e}")

# ------------------ DB ------------------
async def init_db():
    global pg_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not provided; running without persistence.")
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
    logger.info("DB initialized")

async def mark_notified(mint: str, data: dict):
    if pg_pool is None:
        # no persistence
        flat_tokens[mint] = flat_tokens.get(mint, {})
        flat_tokens[mint]["notified"] = True
        return
    async with pg_pool.acquire() as conn:
        await conn.execute(f"INSERT INTO {DB_TABLE_NOTIFIED}(mint, data) VALUES($1, $2) ON CONFLICT (mint) DO NOTHING", mint, json.dumps(data))

async def is_notified(mint: str) -> bool:
    if pg_pool is None:
        return flat_tokens.get(mint, {}).get("notified", False)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT mint FROM {DB_TABLE_NOTIFIED} WHERE mint=$1", mint)
        return bool(row)

# ------------------ JUPITER V2 CLIENT (simplified) ------------------
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
            logger.debug(f"Jupiter request error: {e}")
            return None

    async def get_multiple_token_sources(self):
        # Gather candidates from endpoints similar to your previous script
        endpoints = [
            f"/tokens/v2/toporganicscore/1h?limit=80",
            f"/tokens/v2/toptraded/1h?limit=80",
            f"/tokens/v2/tag?query=verified",
            f"/tokens/v2/recent?limit=50"
        ]
        results = []
        for i, ep in enumerate(endpoints):
            data = await self.request(ep, cache_key=f"jup_ep_{i}", ttl=600)
            if data:
                results.extend(data)
        return results

    async def get_token_by_id(self, mint: str):
        data = await self.request(f"/tokens/v2/search?query={mint}", cache_key=f"jup_token_{mint}", ttl=60)
        return data

jupiter = JupiterClient(JUPITER_BASE)

# ------------------ DEXSCREENER CANDLES (best-effort) ------------------
async def fetch_candles_dexscreener(mint: str, interval_minutes=15, limit=CANDLES_TO_CHECK):
    """
    Best-effort: try common dexscreener endpoints. If your account has a specific API key or
    endpoint, set DEXSCREENER_API env variable and adapt here.
    Returns: list of candles with dicts: {"time", "open", "close", "high", "low", "volume"}
    """
    session = await get_http_session()
    # Try generic /latest/dex/search?q= minted address
    tried = []
    # Endpoint 1: search
    try:
        search_url = f"https://api.dexscreener.com/latest/dex/search/?q={mint}"
        async with session.get(search_url, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                # data may contain pairs array; find a pair on solana with token in path
                pairs = data.get("pairs") or data.get("pairs", [])
                if pairs:
                    # Choose the first pair and fetch candles via pairType
                    pair_id = pairs[0].get("pairAddress") or pairs[0].get("pair")
                    # try pair candles
                    pair_url = f"https://api.dexscreener.com/latest/dex/pair/{pair_id}"
                    async with session.get(pair_url, timeout=8) as r2:
                        if r2.status == 200:
                            pair_data = await r2.json()
                            # dexscreener responses often contain 'candles' or 'chart' keys
                            candles = pair_data.get("candles") or pair_data.get("chart") or []
                            if candles:
                                # normalize
                                normalized = []
                                # candles may be [timestamp, open, high, low, close, volume]
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
        tried.append(f"search_err:{e}")

    # Endpoint 2: page-based fallback (html) - try to scrape minimal json from page
    try:
        page_url = f"https://dexscreener.com/solana/{mint}"
        async with session.get(page_url, timeout=8) as resp:
            if resp.status == 200:
                text = await resp.text()
                # attempt to find a JSON blob in page (best-effort). Not robust; user can replace
                import re
                m = re.search(r"window\.__INITIAL_STATE__ = (\{.+?\});", text)
                if m:
                    blob = json.loads(m.group(1))
                    # dig for candles - site structure may change
                    # return empty if not found
    except Exception as e:
        tried.append(f"page_err:{e}")

    logger.debug(f"Dexscreener attempts failed or returned no candles: {tried}")
    return []

# ------------------ FLAT DETECTION LOGIC ------------------

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


def analyze_flat_pattern(candles):
    # Convert candle volumes (assume USD volumes from dexscreener if available)
    volumes = [c.get("volume", 0) for c in candles]
    if len(volumes) < 6:
        return False, {}
    # Conditions derived from your PESHI pattern
    low_count = sum(1 for v in volumes if v < 10)
    avg_vol = sum(volumes) / len(volumes)
    # check for 3 consecutive green large volumes > 500
    consec_big_buys = 0
    found_big_seq = False
    for i in range(2, len(candles)):
        # crude: green if close >= open
        o1, c1 = candles[i-2].get('open', 0), candles[i-2].get('close', 0)
        o2, c2 = candles[i-1].get('open', 0), candles[i-1].get('close', 0)
        o3, c3 = candles[i].get('open', 0), candles[i].get('close', 0)
        v1, v2, v3 = candles[i-2].get('volume', 0), candles[i-1].get('volume', 0), candles[i].get('volume', 0)
        if c1 >= o1 and c2 >= o2 and c3 >= o3 and v1 > 500 and v2 > 500 and v3 > 500:
            found_big_seq = True
            break
    vol_condition = (low_count >= 3 and avg_vol < 150 and not found_big_seq)
    vol_details = {"low_count": low_count, "avg_vol": avg_vol, "found_big_seq": found_big_seq}
    metrics = calculate_volatility_from_candles(candles)
    if metrics:
        flat_condition = (vol_condition and metrics['std_dev'] < FLAT_STD_THRESHOLD and metrics['max_move'] < 0.6 and ((metrics['max_price'] - metrics['min_price']) / (metrics['min_price'] or 1) * 100) < 1.5)
    else:
        flat_condition = vol_condition
    return flat_condition, {**vol_details, **({'metrics': metrics} if metrics else {})}

# ------------------ PROCESS TOKEN FLOW ------------------
async def evaluate_token_flat(mint: str):
    # get candles
    candles = await fetch_candles_dexscreener(mint)
    if not candles:
        return False, {"reason": "no_candles"}
    # reduce to last CANDLES_TO_CHECK candles
    last_candles = candles[-CANDLES_TO_CHECK:]
    is_flat, details = analyze_flat_pattern(last_candles)
    return is_flat, {"candles_checked": len(last_candles), **details}

async def basic_jupiter_audit(mint: str):
    # use Jupiter v2 token info to get audit fields
    try:
        res = await jupiter.get_token_by_id(mint)
        if not res:
            return {}
        # res could be list or object; normalize
        if isinstance(res, list) and res:
            token = res[0]
        elif isinstance(res, dict):
            token = res
        else:
            token = res
        audit = token.get('audit', {}) if isinstance(token, dict) else {}
        return audit
    except Exception as e:
        logger.debug(f"Jupiter audit error for {mint}: {e}")
        return {}

# ------------------ ALERT FORMATTING ------------------

def format_links(mint: str) -> str:
    return (
        f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
        f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
        f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
        f"• [Solscan](https://solscan.io/token/{mint})\n"
        f"• [Jupiter token](https://jup.ag/tokens/{mint})\n"
    )

async def alert_flat_found(mint: str, token_meta: dict, flat_info: dict):
    # check notified
    if await is_notified(mint):
        logger.info(f"Mint {mint} already notified, skipping")
        return
    # build message
    symbol = token_meta.get('symbol', 'N/A')
    name = token_meta.get('name', 'N/A')
    liquidity = token_meta.get('liquidity', 0)
    volume24 = token_meta.get('volume24h', 0)
    organic = token_meta.get('organic_score', 'N/A')

    audit = await basic_jupiter_audit(mint)
    mint_auth_disabled = audit.get('mintAuthorityDisabled', False)
    freeze_disabled = audit.get('freezeAuthorityDisabled', False)

    msg = (
        f"🚨 *TOKEN EN PUNTO FRÍO DETECTADO* 🚨\n\n"
        f"*Token:* {symbol} — _{name}_\n"
        f"*Mint:* `{mint}`\n"
        f"*Liquidez:* ${liquidity:,.0f}\n"
        f"*Volumen 24h:* ${volume24:,.0f}\n"
        f"*Score orgánico:* {organic}\n"
        f"*Audit - MintDisabled:* {'✅' if mint_auth_disabled else '❌'}  | *FreezeDisabled:* {'✅' if freeze_disabled else '❌'}\n\n"
        f"*Detalles Plano:* {json.dumps(flat_info, default=str)}\n\n"
        f"🔗 Enlaces:\n{format_links(mint)}\n"
        f"_Pattern: token en plano >= {MIN_FLAT_MINUTES} min → atento a breakout {BREAKOUT_STEP}% +_"
    )

    await send_telegram(msg)
    # mark notified
    await mark_notified(mint, {"meta": token_meta, "flat_info": flat_info})

# ------------------ WORKERS ------------------
async def flat_scanner_worker(stop_event: asyncio.Event):
    """
    Periodically get candidate tokens from Jupiter V2 and evaluate flat pattern.
    """
    logger.info("Flat scanner worker started")
    while not stop_event.is_set():
        try:
            candidates = await jupiter.get_multiple_token_sources()
            # normalize unique
            uniq = {}
            for t in candidates:
                tid = t.get('id') if isinstance(t, dict) else None
                if tid:
                    uniq[tid] = t
            token_list = list(uniq.keys())
            logger.info(f"Flat scanner got {len(token_list)} candidates")
            # Evaluate each token (bounded concurrency)
            sem = asyncio.Semaphore(6)

            async def eval_one(mint):
                async with sem:
                    if await is_notified(mint):
                        return
                    token_meta = uniq.get(mint, {})
                    is_flat, details = await evaluate_token_flat(mint)
                    if is_flat:
                        # compute duration estimate based on candles
                        details['detected_at'] = datetime.utcnow().isoformat()
                        await alert_flat_found(mint, {
                            'symbol': token_meta.get('symbol'),
                            'name': token_meta.get('name'),
                            'liquidity': token_meta.get('liquidity'),
                            'volume24h': (token_meta.get('stats24h', {}).get('buyVolume', 0) + token_meta.get('stats24h', {}).get('sellVolume', 0)),
                            'organic_score': token_meta.get('organicScore')
                        }, details)

            tasks = [asyncio.create_task(eval_one(m)) for m in token_list[:120]]
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"Flat scanner error: {e}")
        # Sleep between full scans
        await asyncio.sleep(30)
    logger.info("Flat scanner worker stopped")

async def pumpfun_wss_worker(stop_event: asyncio.Event):
    """
    Connects to HELIUS_WSS_URL and listens for logs/events. This is a best-effort generic
    websocket listener. For Pump.fun specific payloads, adapt parsing to the exact message schema
    that your HELIUS WSS provides for program logs or transactions.
    """
    if not HELIUS_WSS_URL:
        logger.warning("HELIUS_WSS_URL not configured, pumpfun_wss_worker will not run")
        return
    logger.info("Pump.fun WSS worker started")
    while not stop_event.is_set():
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=20, ping_timeout=10) as ws:
                logger.info("Connected to HELIUS WSS")
                # Example subscription message - adapt if your provider uses different RPC
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
                        # Parse event - best-effort
                        # Many Helius logSubscribe messages include 'params' -> 'result' -> 'value' -> 'logs'
                        params = msg.get('params')
                        if not params:
                            continue
                        result = params.get('result') if isinstance(params, dict) else params[0].get('result') if isinstance(params, list) else None
                        if not result:
                            continue
                        value = result.get('value') if isinstance(result, dict) else None
                        # Implement detection heuristics: search logs for 'market_cap' or 'graduat'
                        logs = []
                        if value:
                            logs = value.get('logs') or []
                        # Scan logs for pumpfun patterns
                        text = "\n".join([str(l) for l in logs])
                        # crude extraction: find decimal numbers that look like market cap
                        import re
                        for m in re.finditer(r"market_cap\W*[:=]\W*(\d+[.,]?\d*)", text, re.IGNORECASE):
                            mc = float(m.group(1).replace(',', ''))
                            if mc >= PUMP_PRE_GRADUATION_THRESHOLD:
                                # try to extract mint address nearby
                                mm = re.search(r"mint\W*[:=]\W*([A-Za-z0-9]{32,44})", text)
                                mint = mm.group(1) if mm else None
                                if mint and not await is_notified(mint):
                                    # quick metadata via jupiter
                                    meta = await jupiter.get_token_by_id(mint)
                                    token_meta = meta[0] if isinstance(meta, list) and meta else (meta if isinstance(meta, dict) else {})
                                    msg_text = (
                                        f"🔥 *Pump.fun Pre-Graduation Alert* 🔥\n\n"
                                        f"*Mint:* `{mint}`\n"
                                        f"*Estim. MC:* ${mc:,.0f}\n"
                                        f"*Nombre:* {token_meta.get('name', 'N/A')} ({token_meta.get('symbol', 'N/A')})\n\n"
                                        f"🔗 Enlaces:\n{format_links(mint)}\n\n"
                                        f"_Este token alcanzó MC >= {PUMP_PRE_GRADUATION_THRESHOLD}$. Esto es antes de la graduación (69k). Actúa con cautela._"
                                    )
                                    await send_telegram(msg_text)
                                    await mark_notified(mint, {"pump_detected_mc": mc})
                        # small sleep
                    except asyncio.TimeoutError:
                        # keepalive
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
        except Exception as e:
            logger.error(f"Pumpfun WSS connection error: {e}")
            await asyncio.sleep(5)
    logger.info("Pump.fun WSS worker stopped")

# ------------------ MAIN ------------------
async def main():
    logger.info("Starting Jupiter v2 Pro - Full Bot")
    await init_db()
    stop_event = asyncio.Event()

    # workers
    flat_task = asyncio.create_task(flat_scanner_worker(stop_event))
    pump_task = asyncio.create_task(pumpfun_wss_worker(stop_event))

    try:
        # run until cancelled
        await asyncio.gather(flat_task, pump_task)
    except asyncio.CancelledError:
        logger.info("Cancelled")
    finally:
        stop_event.set()
        if http_session:
            await http_session.close()
        if pg_pool:
            await pg_pool.close()
        logger.info("Shutdown complete")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
