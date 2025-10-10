"""
telegram_bot_final.py

FastAPI + Telegram webhook bot preparado para Railway.
- Modo webhook (manual registration)
- Modo privado: solo responde al TELEGRAM_CHAT_ID configurado
- Comandos: /start, /iniciar, /detener, /status, /ajustar_flat
- Módulos: Pump.fun monitor (WS Helius) y Flat scanner (Jupiter + DexScreener)
- PostgreSQL async (asyncpg) para evitar alertas duplicadas
- Comentarios y secciones para que puedas entender y modificar
"""

import os
import asyncio
import json
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
)

# ---------------------------
# CONFIGURACIÓN (variables de entorno)
# ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # usa la variable de Railway
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))  # tu chat id (modo privado)
DATABASE_URL = os.getenv("DATABASE_URL")

# APIs / Endpoints
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")  # WebSocket Helius (Pump.fun program logs)
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
JUPITER_API_BASE = os.getenv("JUPITER_API_BASE", "https://lite-api.jup.ag")

# Bot behavior settings (puedes cambiarlas por env vars si quieres)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))

PUMP_PRE_GRADUATION_MIN = float(os.getenv("PUMP_PRE_GRADUATION_MIN", "60000"))
PUMP_PRE_GRADUATION_MAX = float(os.getenv("PUMP_PRE_GRADUATION_MAX", "69000"))

FLAT_LIQUIDITY_MIN = float(os.getenv("FLAT_LIQUIDITY_MIN", "15000"))
FLAT_VOLUME_24H_MIN = float(os.getenv("FLAT_VOLUME_24H_MIN", "25000"))
FLAT_VOLATILITY_PCT = float(os.getenv("FLAT_VOLATILITY_PCT", "15.0"))
FLAT_VOLUME_AVG_PER_CANDLE_USD = float(os.getenv("FLAT_VOLUME_AVG_PER_CANDLE_USD", "300"))
FLAT_TOKEN_REPEAT_HOURS = int(os.getenv("FLAT_TOKEN_REPEAT_HOURS", "24"))

CANDLE_INTERVAL = "15m"
CANDLES_HOURS = int(os.getenv("CANDLES_HOURS", "48"))

MAX_RETRIES = 6
BASE_BACKOFF = 1.0

# ---------------------------
# SQL: esquema minimal (tabla de notificaciones)
# ---------------------------
DB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notified_tokens (
    mint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL
);
"""

# ---------------------------
# Utilidades
# ---------------------------
def now_ts() -> datetime:
    return datetime.utcnow()

def backoff_delay(attempt: int) -> float:
    return BASE_BACKOFF * (2 ** attempt) * (0.9 + 0.2 * (os.urandom(1)[0] / 255))

# ---------------------------
# Database helper
# ---------------------------
class DB:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if self.pool:
            return
        if not self.database_url:
            raise RuntimeError("DATABASE_URL no configurada")
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=6)
        async with self.pool.acquire() as conn:
            await conn.execute(DB_SCHEMA_SQL)

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def was_notified_recently(self, mint: str, kind: str, hours: int) -> bool:
        q = "SELECT notified_at FROM notified_tokens WHERE mint=$1 AND kind=$2"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, mint, kind)
            if not row:
                return False
            notified_at: datetime = row["notified_at"]
            return (now_ts() - notified_at) < timedelta(hours=hours)

    async def mark_notified(self, mint: str, kind: str):
        q = """
        INSERT INTO notified_tokens(mint, kind, notified_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (mint) DO UPDATE
          SET notified_at = EXCLUDED.notified_at, kind = EXCLUDED.kind;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, mint, kind, now_ts())

    async def count_alerts_last_24h(self) -> int:
        q = "SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1"
        async with self.pool.acquire() as conn:
            r = await conn.fetchval(q, now_ts() - timedelta(hours=24))
            return int(r or 0)

# ---------------------------
# HTTP client wrapper (aiohttp)
# ---------------------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None) -> Any:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                async with self.session.get(url, params=params, headers=headers, timeout=20) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in (429, 503):
                        await asyncio.sleep(backoff_delay(attempt))
                        attempt += 1
                        continue
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(backoff_delay(attempt))
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise
        raise RuntimeError("Max retries exceeded")

# ---------------------------
# Jupiter client: obtención de candidatos
# ---------------------------
class JupiterClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_candidates(self) -> List[Dict[str, Any]]:
        endpoints = [
            f"{JUPITER_API_BASE}/tokens/v2/toptraded/1h",
            f"{JUPITER_API_BASE}/tokens/v2/toporganicscore/1h",
            f"{JUPITER_API_BASE}/tokens/v2/recent"
        ]
        results = {}
        for ep in endpoints:
            try:
                resp = await self.http.get_json(ep)
                tokens = resp.get("tokens") or resp.get("data") or resp
                for t in tokens:
                    mint = t.get("mint") or t.get("address") or t.get("id")
                    if mint:
                        results[mint] = t
            except Exception:
                # tolerante: ignora endpoint si falla
                continue
        return list(results.values())

# ---------------------------
# DexScreener client: velas / token info
# ---------------------------
class DexScreenerClient:
    def __init__(self, http: HttpClient, base: str):
        self.http = http
        self.base = base.rstrip("/")

    async def get_token(self, mint: str) -> Dict[str, Any]:
        url = f"{self.base}/tokens/{mint}"
        try:
            return await self.http.get_json(url)
        except Exception:
            return {}

# ---------------------------
# Notifier (simple via telegram Application)
# ---------------------------
class TelegramNotifier:
    def __init__(self, app: Application):
        self.app = app

    async def send_markdown(self, text: str):
        # Modo privado: solo enviar si TELEGRAM_CHAT_ID configurado
        if not TELEGRAM_CHAT_ID:
            print("TELEGRAM_CHAT_ID no configurado; no se envía mensaje")
            return
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=False)
        except Exception as e:
            print("Error enviando Telegram:", e)

# ---------------------------
# Helius Pump.fun monitor (WebSocket)
# ---------------------------
class HeliusPumpMonitor:
    def __init__(self, wss_url: str, http: HttpClient, notifier: TelegramNotifier, db: DB):
        self.wss_url = wss_url
        self.http = http
        self.notifier = notifier
        self.db = db
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.cache: Dict[str, float] = {}

    async def start(self):
        if self.running:
            return
        if not self.wss_url:
            raise RuntimeError("HELIUS_WSS_URL no configurado")
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        attempt = 0
        while self.running:
            try:
                async with websockets.connect(self.wss_url, ping_interval=30, ping_timeout=10) as ws:
                    attempt = 0
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._process(msg)
            except Exception:
                # log y backoff
                attempt += 1
                await asyncio.sleep(backoff_delay(attempt))

    async def _process(self, msg: Dict[str, Any]):
        # Filtra el programa Pump.fun (program id conocido)
        program = msg.get("program") or msg.get("programId")
        if program != "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
            return
        # extraer mint posible
        mint = msg.get("mint") or msg.get("token")
        if not mint:
            # intentar buscar en logs si la estructura es diferente
            params = msg.get("params") or {}
            result = params.get("result", {}) if isinstance(params, dict) else {}
            logs = result.get("value", {}).get("logs", []) if isinstance(result, dict) else []
            if logs:
                import re
                combined = " ".join(logs)
                matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', combined)
                for m in matches:
                    if len(m) == 44:
                        mint = m
                        break
        if not mint:
            return
        # obtener marketcap (DexScreener)
        marketcap = await self._get_marketcap(mint)
        if marketcap is None:
            return
        if PUMP_PRE_GRADUATION_MIN <= marketcap <= PUMP_PRE_GRADUATION_MAX:
            already = await self.db.was_notified_recently(mint, "pump_pregrad", 24 * 7)
            if already:
                return
            # enviar alerta formato requerido
            symbol = msg.get("symbol", "UNK")
            name = msg.get("name", "")
            text = (
                "🚀 *ALERTA PUMP.FUN PRE-GRADUACIÓN* 🚀\n\n"
                f"Token: *{symbol}* - {name}\n"
                f"Market Cap Actual: ${marketcap:,.0f} / ${PUMP_PRE_GRADUATION_MAX:,.0f}\n\n"
                "📝 Mint Address:\n"
                f"`{mint}`\n\n"
                "🔗 Enlaces de Análisis:\n"
                f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
                f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})"
            )
            await self.notifier.send_markdown(text)
            await self.db.mark_notified(mint, "pump_pregrad")

    async def _get_marketcap(self, mint: str) -> Optional[float]:
        # Cache simple para reducir peticiones
        if mint in self.cache:
            return self.cache[mint]
        try:
            url = f"{DEXSCREENER_API.rstrip('/')}/tokens/{mint}"
            data = await self.http.get_json(url)
            token = data.get("token") or {}
            mc = token.get("marketCapUsd")
            if mc:
                mc_val = float(mc)
                self.cache[mint] = mc_val
                return mc_val
        except Exception:
            return None
        return None

# ---------------------------
# Flat scanner module
# ---------------------------
class FlatScanner:
    def __init__(self, jup: JupiterClient, dexs: DexScreenerClient, notifier: TelegramNotifier, db: DB):
        self.jup = jup
        self.dexs = dexs
        self.notifier = notifier
        self.db = db
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self.running:
            try:
                await self.scan_once()
            except Exception:
                pass
            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

    async def scan_once(self):
        async with self.lock:
            candidates = await self.jup.get_candidates()
            filtered = []
            for t in candidates:
                try:
                    liq = float(t.get("liquidity", 0))
                    vol24 = float(t.get("volume24h", 0) or t.get("stats24h", {}).get("buyVolume", 0) + t.get("stats24h", {}).get("sellVolume", 0))
                except Exception:
                    liq, vol24 = 0, 0
                if liq >= FLAT_LIQUIDITY_MIN and vol24 >= FLAT_VOLUME_24H_MIN:
                    filtered.append(t)
            sem = asyncio.Semaphore(6)
            tasks = [self._analyze(t, sem) for t in filtered]
            await asyncio.gather(*tasks)

    async def _analyze(self, token: Dict[str, Any], sem: asyncio.Semaphore):
        async with sem:
            mint = token.get("mint") or token.get("address") or token.get("id")
            if not mint:
                return
            # evitar alertas repetidas
            if await self.db.was_notified_recently(mint, "flat", FLAT_TOKEN_REPEAT_HOURS):
                return
            # obtener velas/info desde DexScreener
            resp = await self.dexs.get_token(mint)
            candles = self._parse_candles(resp)
            if not candles:
                return
            analysis = self._is_flat(candles)
            if analysis["is_flat"]:
                sym = token.get("symbol", "UNK")
                name = token.get("name", "")
                liquidity = token.get("liquidity", 0)
                mcap = token.get("marketCap", token.get("marketCapUsd", 0) or 0)
                avgv = analysis["avg_vol"]
                vpct = analysis["volatility_pct"]
                text = (
                    "🎯 *ALERTA DE PATRÓN FLAT DETECTADO* 🎯\n\n"
                    f"Token: *{sym}* - {name}\n"
                    f"Detectado en estado \"flat\" durante las últimas {CANDLES_HOURS} horas.\n\n"
                    "📊 Datos Clave:\n"
                    f"• Liquidez: ${float(liquidity):,.0f}\n"
                    f"• Market Cap: ${float(mcap):,.0f}\n"
                    f"• Volatilidad (48h): {vpct:.2f}%\n"
                    f"• Volumen Promedio/Vela: ${avgv:.2f}\n\n"
                    "📝 Mint Address:\n"
                    f"`{mint}`\n\n"
                    "🔗 Enlaces de Análisis:\n"
                    f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
                    f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
                    f"• [Birdeye](https://birdeye.so/token/{mint})"
                )
                await self.notifier.send_markdown(text)
                await self.db.mark_notified(mint, "flat")

    def _parse_candles(self, resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        candles = []
        if not resp:
            return candles
        if "candles" in resp:
            for c in resp["candles"]:
                if isinstance(c, list) and len(c) >= 6:
                    candles.append({
                        "time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume_usd": float(c[5])
                    })
        elif "pairs" in resp:
            p0 = resp["pairs"][0]
            chart = p0.get("chart", {})
            for c in chart.get("candles", []):
                candles.append({
                    "time": int(c.get("time", 0)),
                    "open": float(c.get("open", 0)),
                    "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)),
                    "close": float(c.get("close", 0)),
                    "volume_usd": float(c.get("volumeUsd", 0))
                })
        return candles

    def _is_flat(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        res = {"is_flat": False, "avg_vol": 0.0, "volatility_pct": 100.0}
        if not candles:
            return res
        cutoff = now_ts().timestamp() - (CANDLES_HOURS * 3600)
        filt = [c for c in candles if c["time"] >= cutoff]
        if not filt:
            filt = candles
        highs = [c["high"] for c in filt if "high" in c]
        lows = [c["low"] for c in filt if "low" in c]
        vols = [c["volume_usd"] for c in filt if "volume_usd" in c]
        if not highs or not lows or not vols:
            return res
        h = max(highs)
        l = min(lows)
        if l <= 0:
            return res
        vol_pct = ((h - l) / l) * 100
        avg_vol = sum(vols) / max(1, len(vols))
        tiny = sum(1 for v in vols if v < 20)
        spikes = sum(1 for v in vols if v > 200)
        green_runs = 0
        run = 0
        for c in filt:
            if c["close"] > c["open"] and c["volume_usd"] > 50:
                run += 1
            else:
                if run >= 3:
                    green_runs += 1
                run = 0
        if run >= 3:
            green_runs += 1
        cond = (
            vol_pct < FLAT_VOLATILITY_PCT
            and avg_vol < FLAT_VOLUME_AVG_PER_CANDLE_USD
            and tiny >= max(10, int(0.6 * len(vols)))
            and spikes <= 3
            and green_runs <= 1
        )
        res.update({"avg_vol": avg_vol, "volatility_pct": vol_pct})
        if cond:
            res["is_flat"] = True
        return res

# ---------------------------
# FastAPI + Telegram application wiring (webhook mode)
# ---------------------------
app = FastAPI()
telegram_app: Optional[Application] = None
db: Optional[DB] = None
pump_monitor: Optional[HeliusPumpMonitor] = None
flat_scanner: Optional[FlatScanner] = None
http_session: Optional[aiohttp.ClientSession] = None

async def init_bot():
    """
    Inicializa Application (telegram) y módulos. No registra webhook (manual).
    Este init se llama en startup.
    """
    global telegram_app, db, pump_monitor, flat_scanner, http_session

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurado")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID no configurado (modo privado)")

    # Telegram Application (no correr polling)
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # agregar handlers
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("ajustar_flat", cmd_ajustar_flat))

    # DB
    db = DB(DATABASE_URL)
    await db.connect()

    # HTTP session + clients
    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)
    jup = JupiterClient(http_client)
    dexs = DexScreenerClient(http_client, DEXSCREENER_API)
    notifier = TelegramNotifier(telegram_app)

    pump_monitor = HeliusPumpMonitor(HELIUS_WSS_URL, http_client, notifier, db)
    flat_scanner = FlatScanner(jup, dexs, notifier, db)

@app.on_event("startup")
async def on_startup():
    # Debug info
    port = os.getenv("PORT", "NO SET")
    print(f"🚀 Starting bot on port: {port}")
    print(f"🤖 Bot token configured: {'YES' if TELEGRAM_BOT_TOKEN else 'NO'}")
    print(f"💬 Chat ID: {TELEGRAM_CHAT_ID}")
    
    try:
        # inicializa bot y módulos
        await init_bot()
        
        # ✅ CRÍTICO: Inicializa la aplicación de Telegram
        await telegram_app.initialize()
        await telegram_app.start()
        
        print("✅ Telegram bot initialized successfully")
        
    except Exception as e:
        print(f"❌ Error during startup: {e}")
        raise

@app.on_event("shutdown")
async def on_shutdown():
    print("🛑 Shutting down bot...")
    # Detener monitores
    await pump_monitor.stop()
    await flat_scanner.stop()
    # Cerrar la aplicación de Telegram
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    # Cerrar sesión HTTP
    if http_session:
        await http_session.close()
    # Cerrar pool de DB
    if db:
        await db.close()
    print("✅ Bot shut down successfully")

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    """
    Endpoint webhook donde Telegram enviará updates.
    - No autoregistra el webhook. Tú lo harás con curl.
    - Validamos que el token en URL coincida con la env var.
    - En modo privado, validamos que el chat que envía comandos sea el TELEGRAM_CHAT_ID.
    """
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = await req.json()
    # Convertimos body a Update y lo encolamos en la aplicación telegram interna
    update = Update.de_json(body, telegram_app.bot)
    # Si el update tiene mensaje y proviene de otro usuario que no es TELEGRAM_CHAT_ID -> ignorar (modo privado)
    try:
        from_user = None
        if update.effective_user:
            from_user = update.effective_user.id
        # Si hay from_user y no coincide -> ignorar y devolver OK
        if from_user and int(from_user) != int(TELEGRAM_CHAT_ID):
            # No respondemos a usuarios no autorizados
            return JSONResponse({"ok": True, "note": "ignored - private bot"})
    except Exception:
        # si no hay user info, aceptamos (algún tipo de update)
        pass

    # poner update en queue para que los handlers lo procesen
    await telegram_app.update_queue.put(update)
    return JSONResponse({"ok": True})

# ---------------------------
# Telegram command handlers
# ---------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Verificar que quien lo llama sea el TELEGRAM_CHAT_ID (extra por seguridad)
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = (
        "🤖 *Solana Monitor Bot*\n\n"
        "Funciones:\n"
        "• Monitorea Pump.fun (pre-graduación)\n"
        "• Escanea tokens en patrón *flat*\n\n"
        "Comandos:\n"
        "/iniciar - Inicia monitores\n"
        "/detener - Detiene monitores\n"
        "/status - Estado actual\n"
        "/ajustar_flat <param> <valor> - Ajustar parámetros (ej: /ajustar_flat volatilidad 10)\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    # start modules
    await pump_monitor.start()
    await flat_scanner.start()
    await update.message.reply_text("Monitores iniciados ✅")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await pump_monitor.stop()
    await flat_scanner.stop()
    await update.message.reply_text("Monitores detenidos ⛔")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    running = (pump_monitor.running if pump_monitor else False) or (flat_scanner.running if flat_scanner else False)
    cnt = await db.count_alerts_last_24h()
    text = (
        f"Estado: {'🟢 Activos' if running else '🔴 Detenidos'}\n"
        f"Alertas (24h): {cnt}\n"
        f"PUMP umbral: ${PUMP_PRE_GRADUATION_MIN:,.0f}–${PUMP_PRE_GRADUATION_MAX:,.0f}\n"
        f"FLAT volatilidad<{FLAT_VOLATILITY_PCT}% , vol_avg<{FLAT_VOLUME_AVG_PER_CANDLE_USD}$\n"
    )
    await update.message.reply_text(text)

async def cmd_ajustar_flat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text("Uso: /ajustar_flat <parametro> <valor>")
        return
    param = args[0].lower()
    val = args[1]
    global FLAT_VOLATILITY_PCT, FLAT_VOLUME_AVG_PER_CANDLE_USD, FLAT_LIQUIDITY_MIN, FLAT_VOLUME_24H_MIN
    try:
        if param in ("volatilidad", "volatility"):
            FLAT_VOLATILITY_PCT = float(val)
        elif param in ("volumen_promedio", "avg_vol"):
            FLAT_VOLUME_AVG_PER_CANDLE_USD = float(val)
        elif param in ("liquidez", "liquidity"):
            FLAT_LIQUIDITY_MIN = float(val)
        elif param in ("vol24h", "volume24h"):
            FLAT_VOLUME_24H_MIN = float(val)
        else:
            await update.message.reply_text("Parámetro no reconocido.")
            return
        await update.message.reply_text(f"{param} actualizado a {val}")
    except Exception:
        await update.message.reply_text("Valor inválido.")

# ---------------------------
# Entrypoint - uvicorn if directly executed (Railway runs via gunicorn)
# ---------------------------
def run_uvicorn():
    import uvicorn
    port = int(os.getenv("PORT", "8000"))  # ✅ Usa el puerto de Railway o 8000 por defecto
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=port)

# This allows both: "python telegram_bot_final.py" (dev) or gunicorn
if __name__ == "__main__":
    # para desarrollo local puedes usar: python telegram_bot_final.py
    # en prod Railway usará el Procfile con gunicorn
    asyncio.run(init_bot())
    run_uvicorn()
