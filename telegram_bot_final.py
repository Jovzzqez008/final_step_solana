"""
telegram_bot_final.py - VERSIÓN MEJORADA Y CORREGIDA

Bot mejorado con:
- Detección más precisa de tokens Pump.fun pre-graduación
- Scanner de tokens flat mejorado con Jupiter API V2
- Filtros más inteligentes usando Organic Score y datos en tiempo real
- Múltiples fuentes de datos para mejor precisión
"""

import os
import asyncio
import json
import random
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler

# ---------------------------
# CONFIGURACIÓN MEJORADA
# ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# APIs Mejoradas
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
JUPITER_API_BASE = os.getenv("JUPITER_API_BASE", "https://lite-api.jup.ag")
RAYDIUM_API = "https://api-v3.raydium.io"

# Configuraciones mejoradas
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))  # Más frecuente

# Pump.fun - Rangos más precisos
PUMP_PRE_GRADUATION_MIN = float(os.getenv("PUMP_PRE_GRADUATION_MIN", "55000"))
PUMP_PRE_GRADUATION_MAX = float(os.getenv("PUMP_PRE_GRADUATION_MAX", "68000"))

# Flat Scanner - Criterios MEJORADOS (menos estrictos)
FLAT_LIQUIDITY_MIN = float(os.getenv("FLAT_LIQUIDITY_MIN", "10000"))
FLAT_VOLUME_24H_MIN = float(os.getenv("FLAT_VOLUME_24H_MIN", "15000"))
FLAT_VOLATILITY_PCT = float(os.getenv("FLAT_VOLATILITY_PCT", "15.0"))
FLAT_VOLUME_AVG_PER_CANDLE_USD = float(os.getenv("FLAT_VOLUME_AVG_PER_CANDLE_USD", "200"))
FLAT_TOKEN_REPEAT_HOURS = int(os.getenv("FLAT_TOKEN_REPEAT_HOURS", "6"))
FLAT_MIN_ORGANIC_SCORE = float(os.getenv("FLAT_MIN_ORGANIC_SCORE", "10"))  # Menos estricto

# Nuevos parámetros para mejor detección
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", "50"))  # Menos estricto
MIN_ORGANIC_VOLUME_RATIO = float(os.getenv("MIN_ORGANIC_VOLUME_RATIO", "0.3"))

CANDLE_INTERVAL = "5m"
CANDLES_HOURS = int(os.getenv("CANDLES_HOURS", "3"))

MAX_RETRIES = 8
BASE_BACKOFF = 1.0

# ---------------------------
# ESQUEMA DE BASE DE DATOS MEJORADO
# ---------------------------
DB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notified_tokens (
    mint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    symbol TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notified_tokens_kind ON notified_tokens(kind);
CREATE INDEX IF NOT EXISTS idx_notified_tokens_notified_at ON notified_tokens(notified_at);
"""

# ---------------------------
# UTILIDADES MEJORADAS
# ---------------------------
def now_ts() -> datetime:
    return datetime.utcnow()

def backoff_delay(attempt: int) -> float:
    return BASE_BACKOFF * (2 ** attempt) * (0.9 + 0.2 * (os.urandom(1)[0] / 255))

def format_number(num: float) -> str:
    """Formatea números grandes de manera legible"""
    if num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    elif num >= 1_000:
        return f"{num/1_000:.2f}K"
    else:
        return f"{num:.2f}"

# ---------------------------
# DATABASE HELPER MEJORADO
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
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        async with self.pool.acquire() as conn:
            # Verificar y crear esquema
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'notified_tokens')"
            )
            
            if table_exists:
                column_exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'notified_tokens' AND column_name = 'notified_at')"
                )
                if not column_exists:
                    await conn.execute("DROP TABLE notified_tokens")
                    print("🗑️ Tabla antigua eliminada, creando nueva...")
            
            await conn.execute(DB_SCHEMA_SQL)
            print("✅ Esquema de base de datos verificado/creado")

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

    async def mark_notified(self, mint: str, kind: str, symbol: str = None):
        q = """
        INSERT INTO notified_tokens(mint, kind, notified_at, symbol)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (mint) DO UPDATE
          SET notified_at = EXCLUDED.notified_at, kind = EXCLUDED.kind, symbol = EXCLUDED.symbol;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, mint, kind, now_ts(), symbol)

    async def count_alerts_last_24h(self) -> int:
        q = "SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1"
        async with self.pool.acquire() as conn:
            try:
                r = await conn.fetchval(q, now_ts() - timedelta(hours=24))
                return int(r or 0)
            except Exception as e:
                print(f"❌ Error contando alertas: {e}")
                return 0

# ---------------------------
# HTTP CLIENT MEJORADO
# ---------------------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None) -> Any:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                async with self.session.get(url, params=params, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in (429, 503, 502):
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
# JUPITER CLIENT MEJORADO CON API V2
# ---------------------------
class JupiterClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_token_info(self, mint: str) -> Optional[Dict[str, Any]]:
        """Obtiene información detallada del token usando Jupiter API V2"""
        url = f"{JUPITER_API_BASE}/tokens/v2/search?query={mint}"
        try:
            data = await self.http.get_json(url)
            if isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception as e:
            print(f"Error obteniendo info de token {mint}: {e}")
        return None

    async def get_verified_tokens(self) -> List[Dict[str, Any]]:
        """Obtiene tokens verificados con buena reputación"""
        url = f"{JUPITER_API_BASE}/tokens/v2/tag?query=verified"
        try:
            return await self.http.get_json(url)
        except Exception:
            return []

    async def get_high_organic_tokens(self) -> List[Dict[str, Any]]:
        """Obtiene tokens con alto organic score (mejor calidad)"""
        endpoints = [
            f"{JUPITER_API_BASE}/tokens/v2/toporganicscore/1h?limit=100",
            f"{JUPITER_API_BASE}/tokens/v2/toptraded/1h?limit=50",
            f"{JUPITER_API_BASE}/tokens/v2/recent?limit=30"
        ]
        
        all_tokens = []
        for endpoint in endpoints:
            try:
                tokens = await self.http.get_json(endpoint)
                if isinstance(tokens, list):
                    all_tokens.extend(tokens)
            except Exception as e:
                print(f"Error en endpoint {endpoint}: {e}")
                continue
        
        # Filtrar y ordenar por organic score
        filtered = []
        for token in all_tokens:
            try:
                organic_score = token.get('organicScore', 0)
                liquidity = float(token.get('liquidity', 0))
                volume_24h = float(token.get('stats24h', {}).get('buyVolume', 0) + 
                                 token.get('stats24h', {}).get('sellVolume', 0))
                
                if (organic_score >= FLAT_MIN_ORGANIC_SCORE and 
                    liquidity >= FLAT_LIQUIDITY_MIN and 
                    volume_24h >= FLAT_VOLUME_24H_MIN):
                    filtered.append(token)
            except (ValueError, TypeError):
                continue
        
        return filtered

    async def get_candidates(self) -> List[Dict[str, Any]]:
        """Obtiene candidatos de múltiples fuentes con mejor filtrado"""
        candidates = await self.get_high_organic_tokens()
        
        # Eliminar duplicados
        unique_tokens = {}
        for token in candidates:
            mint = token.get('id') or token.get('mint') or token.get('address')
            if mint:
                unique_tokens[mint] = token
        
        return list(unique_tokens.values())

# ---------------------------
# DEXSCREENER CLIENT MEJORADO
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

    async def get_token_pairs(self, mint: str) -> List[Dict[str, Any]]:
        """Obtiene todos los pairs asociados a un token"""
        url = f"{self.base}/tokens/{mint}"
        try:
            data = await self.http.get_json(url)
            return data.get('pairs', [])
        except Exception:
            return []

# ---------------------------
# RAYDIUM CLIENT (NUEVO)
# ---------------------------
class RaydiumClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_new_pools(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Obtiene pools recién creados en Raydium"""
        url = f"{RAYDIUM_API}/pools"
        try:
            params = {
                'sortField': 'createdAt',
                'sortType': 'desc',
                'limit': limit
            }
            data = await self.http.get_json(url, params=params)
            return data.get('data', [])
        except Exception as e:
            print(f"Error obteniendo pools de Raydium: {e}")
            return []

# ---------------------------
# NOTIFIER MEJORADO
# ---------------------------
class TelegramNotifier:
    def __init__(self, app: Application):
        self.app = app

    async def send_message(self, text: str, parse_mode: str = "HTML"):
        if not TELEGRAM_CHAT_ID:
            print("TELEGRAM_CHAT_ID no configurado; no se envía mensaje")
            return
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, 
                text=text, 
                parse_mode=parse_mode,
                disable_web_page_preview=False
            )
        except Exception as e:
            print("Error enviando Telegram:", e)

# ---------------------------
# HELIUS PUMP.FUN MONITOR MEJORADO
# ---------------------------
class HeliusPumpMonitor:
    def __init__(self, wss_url: str, http: HttpClient, notifier: TelegramNotifier, db: DB, jup: JupiterClient):
        self.wss_url = wss_url
        self.http = http
        self.notifier = notifier
        self.db = db
        self.jup = jup
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.cache: Dict[str, float] = {}
        self.recent_mints = set()

    async def start(self):
        if self.running:
            return
        if not self.wss_url:
            print("⚠️ HELIUS_WSS_URL no configurado - Pump monitor desactivado")
            return
        self.running = True
        self.task = asyncio.create_task(self._loop())
        print("🔍 Pump.fun Monitor iniciado")

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
                    print("✅ Conectado a WebSocket de Helius")
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._process(msg)
                        except Exception as e:
                            print(f"Error procesando mensaje WebSocket: {e}")
                            continue
            except Exception as e:
                attempt += 1
                print(f"❌ Error conexión WebSocket (intento {attempt}): {e}")
                await asyncio.sleep(backoff_delay(attempt))

    async def _process(self, msg: Dict[str, Any]):
        # Filtrar programa Pump.fun
        program = msg.get("program") or msg.get("programId")
        if program != "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
            return
        
        # Extraer mint address
        mint = self._extract_mint_from_message(msg)
        if not mint:
            return
        
        # Evitar procesar el mismo mint múltiples veces
        if mint in self.recent_mints:
            return
        self.recent_mints.add(mint)
        # Limpiar cache cada 100 mints
        if len(self.recent_mints) > 100:
            self.recent_mints.clear()

        # Obtener market cap y verificar condiciones
        marketcap = await self._get_marketcap(mint)
        if marketcap is None:
            return

        if PUMP_PRE_GRADUATION_MIN <= marketcap <= PUMP_PRE_GRADUATION_MAX:
            # Verificar si ya fue notificado
            if await self.db.was_notified_recently(mint, "pump_pregrad", 12):  # 12 horas en lugar de 7 días
                return

            # Obtener información adicional del token
            token_info = await self.jup.get_token_info(mint)
            symbol = token_info.get('symbol', 'UNK') if token_info else msg.get('symbol', 'UNK')
            name = token_info.get('name', '') if token_info else msg.get('name', '')

            # Enviar alerta mejorada
            await self._send_pump_alert(mint, symbol, name, marketcap, token_info)

    def _extract_mint_from_message(self, msg: Dict[str, Any]) -> Optional[str]:
        """Extrae mint address del mensaje de diferentes maneras"""
        # Intentar extraer directamente
        mint = msg.get("mint") or msg.get("token")
        if mint and len(mint) == 44:
            return mint

        # Buscar en logs
        params = msg.get("params") or {}
        result = params.get("result", {}) if isinstance(params, dict) else {}
        logs = result.get("value", {}).get("logs", []) if isinstance(result, dict) else []
        
        if logs:
            import re
            combined = " ".join(logs)
            matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', combined)
            for m in matches:
                if len(m) == 44:
                    return m
        return None

    async def _get_marketcap(self, mint: str) -> Optional[float]:
        if mint in self.cache:
            return self.cache[mint]
        
        try:
            # Intentar con DexScreener primero
            url = f"{DEXSCREENER_API.rstrip('/')}/tokens/{mint}"
            data = await self.http.get_json(url)
            token = data.get("token") or {}
            mc = token.get("marketCapUsd")
            
            if mc:
                mc_val = float(mc)
                self.cache[mint] = mc_val
                # Limpiar cache periódicamente
                if len(self.cache) > 500:
                    self.cache.clear()
                return mc_val
        except Exception:
            pass
        
        return None

    async def _send_pump_alert(self, mint: str, symbol: str, name: str, marketcap: float, token_info: Optional[Dict]):
        """Envía alerta de Pump.fun mejorada con más información"""
        
        # Información adicional si está disponible
        extra_info = ""
        if token_info:
            organic_score = token_info.get('organicScore', 'N/A')
            holder_count = token_info.get('holderCount', 'N/A')
            liquidity = token_info.get('liquidity', 0)
            volume_24h = token_info.get('stats24h', {}).get('buyVolume', 0) + token_info.get('stats24h', {}).get('sellVolume', 0)
            
            extra_info = (
                f"• Organic Score: {organic_score}\n"
                f"• Holders: {holder_count}\n"
                f"• Liquidez: ${format_number(liquidity)}\n"
                f"• Volumen 24h: ${format_number(volume_24h)}\n"
            )

        text = (
            "🚀 <b>ALERTA PUMP.FUN - PRE-GRADUACIÓN INMINENTE</b> 🚀\n\n"
            f"<b>Token:</b> {symbol} - {name}\n"
            f"<b>Market Cap:</b> ${marketcap:,.0f} / ${PUMP_PRE_GRADUATION_MAX:,.0f}\n\n"
            f"<b>📊 Métricas Clave:</b>\n"
            f"{extra_info}\n"
            f"<b>📝 Mint Address:</b>\n"
            f"<code>{mint}</code>\n\n"
            "<b>🔗 Enlaces Rápidos:</b>\n"
            f"• <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>\n"
            f"• <a href='https://rugcheck.xyz/tokens/{mint}'>RugCheck</a>\n"
            f"• <a href='https://birdeye.so/token/{mint}'>Birdeye</a>\n"
            f"• <a href='https://jup.ag/swap/{mint}-SOL'>Jupiter</a>\n\n"
            "<b>💡 Análisis Rápido:</b>\n"
            "• Verifica gráfico en DexScreener\n"
            "• Revisa RugCheck para seguridad\n"
            "• Analiza volumen y liquidez\n"
            "• Considera tu estrategia de entrada/salida"
        )
        
        await self.notifier.send_message(text)
        await self.db.mark_notified(mint, "pump_pregrad", symbol)

# ---------------------------
# FLAT SCANNER MEJORADO
# ---------------------------
class FlatScanner:
    def __init__(self, jup: JupiterClient, dexs: DexScreenerClient, notifier: TelegramNotifier, db: DB, raydium: RaydiumClient):
        self.jup = jup
        self.dexs = dexs
        self.notifier = notifier
        self.db = db
        self.raydium = raydium
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._loop())
        print("🔍 Flat Scanner mejorado iniciado")

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
            except Exception as e:
                print(f"Error en flat scanner: {e}")
            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

    async def scan_once(self):
        async with self.lock:
            print("🔄 Ejecutando scan de tokens flat...")
            
            # Obtener candidatos de múltiples fuentes
            candidates = await self.jup.get_candidates()
            print(f"📊 Candidatos iniciales: {len(candidates)}")
            
            # Filtrar candidatos prometedores
            filtered = []
            for token in candidates:
                if await self._is_promising_token(token):
                    filtered.append(token)
            
            print(f"🎯 Tokens prometedores después de filtro: {len(filtered)}")
            
            # Analizar tokens en paralelo
            sem = asyncio.Semaphore(8)  # Más paralelismo
            tasks = [self._analyze(token, sem) for token in filtered]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _is_promising_token(self, token: Dict[str, Any]) -> bool:
        """Filtra tokens prometedores usando múltiples criterios"""
        try:
            mint = token.get('id') or token.get('mint') or token.get('address')
            if not mint:
                return False

            # Verificar si ya fue notificado recientemente
            if await self.db.was_notified_recently(mint, "flat", FLAT_TOKEN_REPEAT_HOURS):
                return False

            # Criterios de filtrado mejorados
            liquidity = float(token.get('liquidity', 0))
            volume_24h = float(token.get('stats24h', {}).get('buyVolume', 0) + 
                             token.get('stats24h', {}).get('sellVolume', 0))
            organic_score = token.get('organicScore', 0)
            holder_count = token.get('holderCount', 0)

            return (liquidity >= FLAT_LIQUIDITY_MIN and 
                    volume_24h >= FLAT_VOLUME_24H_MIN and
                    organic_score >= FLAT_MIN_ORGANIC_SCORE and
                    holder_count >= MIN_HOLDER_COUNT)
                    
        except (ValueError, TypeError):
            return False

    async def _analyze(self, token: Dict[str, Any], sem: asyncio.Semaphore):
        async with sem:
            try:
                mint = token.get('id') or token.get('mint') or token.get('address')
                if not mint:
                    return

                # Obtener datos de velas
                resp = await self.dexs.get_token(mint)
                candles = self._parse_candles(resp)
                if not candles:
                    return

                # Análisis de patrón flat mejorado
                analysis = self._is_flat_improved(candles, token)
                if analysis["is_flat"]:
                    await self._send_flat_alert(token, analysis, mint)

            except Exception as e:
                print(f"Error analizando token {mint}: {e}")

    def _parse_candles(self, resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parsea velas de diferentes formatos de respuesta"""
        candles = []
        if not resp:
            return candles

        # Formato DexScreener estándar
        if "pairs" in resp and resp["pairs"]:
            pair = resp["pairs"][0]
            if "chart" in pair and "candles" in pair["chart"]:
                for candle in pair["chart"]["candles"]:
                    candles.append({
                        "time": int(candle.get("time", 0)) // 1000,  # Convertir a segundos
                        "open": float(candle.get("open", 0)),
                        "high": float(candle.get("high", 0)),
                        "low": float(candle.get("low", 0)),
                        "close": float(candle.get("close", 0)),
                        "volume_usd": float(candle.get("volumeUsd", 0))
                    })

        return candles

    def _is_flat_improved(self, candles: List[Dict[str, Any]], token_info: Dict[str, Any]) -> Dict[str, Any]:
        """Análisis de patrón flat mejorado con múltiples criterios"""
        res = {"is_flat": False, "avg_vol": 0.0, "volatility_pct": 100.0, "flat_period_hours": 0}
        
        if not candles:
            return res

        # Filtrar velas de las últimas 3 horas
        cutoff = now_ts().timestamp() - (CANDLES_HOURS * 3600)
        recent_candles = [c for c in candles if c["time"] >= cutoff]
        
        if len(recent_candles) < 6:  # Mínimo 6 velas de 5m para 30 minutos
            return res

        # Calcular métricas
        highs = [c["high"] for c in recent_candles]
        lows = [c["low"] for c in recent_candles]
        vols = [c["volume_usd"] for c in recent_candles]

        if not highs or not lows:
            return res

        max_high = max(highs)
        min_low = min(lows)
        
        if min_low <= 0:
            return res

        # Volatilidad porcentual
        volatility_pct = ((max_high - min_low) / min_low) * 100
        avg_volume = sum(vols) / len(vols)

        # Criterios mejorados para flat
        low_volume_candles = sum(1 for v in vols if v < FLAT_VOLUME_AVG_PER_CANDLE_USD)
        high_volume_spikes = sum(1 for v in vols if v > FLAT_VOLUME_AVG_PER_CANDLE_USD * 3)
        
        # Porcentaje de velas con volumen bajo
        low_volume_ratio = low_volume_candles / len(vols)
        
        # El token está en flat si:
        # 1. Baja volatilidad
        # 2. Alto porcentaje de velas con volumen bajo
        # 3. Pocos picos de volumen alto
        is_flat = (volatility_pct < FLAT_VOLATILITY_PCT and
                  low_volume_ratio >= 0.7 and  # 70% de velas con volumen bajo
                  high_volume_spikes <= 2)     # Máximo 2 picos de volumen

        res.update({
            "avg_vol": avg_volume,
            "volatility_pct": volatility_pct,
            "flat_period_hours": CANDLES_HOURS,
            "is_flat": is_flat
        })
        
        return res

    async def _send_flat_alert(self, token: Dict[str, Any], analysis: Dict[str, Any], mint: str):
        """Envía alerta de patrón flat mejorada"""
        symbol = token.get('symbol', 'UNK')
        name = token.get('name', '')
        liquidity = token.get('liquidity', 0)
        market_cap = token.get('marketCap', token.get('fdv', 0))
        organic_score = token.get('organicScore', 'N/A')
        holder_count = token.get('holderCount', 'N/A')

        text = (
            "🎯 <b>ALERTA - PATRÓN FLAT DETECTADO</b> 🎯\n\n"
            f"<b>Token:</b> {symbol} - {name}\n"
            f"<b>Duración del flat:</b> ~{analysis['flat_period_hours']} horas\n\n"
            "<b>📊 Métricas del Flat:</b>\n"
            f"• Volatilidad: {analysis['volatility_pct']:.2f}%\n"
            f"• Volumen Promedio: ${analysis['avg_vol']:.2f}\n"
            f"• Liquidez: ${format_number(liquidity)}\n"
            f"• Market Cap: ${format_number(market_cap)}\n\n"
            "<b>📈 Calidad del Token:</b>\n"
            f"• Organic Score: {organic_score}\n"
            f"• Holders: {holder_count}\n\n"
            f"<b>📝 Mint Address:</b>\n"
            f"<code>{mint}</code>\n\n"
            "<b>🔗 Enlaces de Análisis:</b>\n"
            f"• <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>\n"
            f"• <a href='https://rugcheck.xyz/tokens/{mint}'>RugCheck</a>\n"
            f"• <a href='https://birdeye.so/token/{mint}'>Birdeye</a>\n"
            f"• <a href='https://jup.ag/swap/{mint}-SOL'>Jupiter</a>\n\n"
            "<b>💡 Estrategia Sugerida:</b>\n"
            "• Token en consolidación, posible breakout\n"
            "• Verificar volumen de entrada/salida\n"
            "• Establecer stops adecuados\n"
            "• Considerar posición pequeña inicial"
        )

        await self.notifier.send_message(text)
        await self.db.mark_notified(mint, "flat", symbol)

# ---------------------------
# FASTAPI + TELEGRAM APPLICATION
# ---------------------------
app = FastAPI()
telegram_app: Optional[Application] = None
db: Optional[DB] = None
pump_monitor: Optional[HeliusPumpMonitor] = None
flat_scanner: Optional[FlatScanner] = None
http_session: Optional[aiohttp.ClientSession] = None

async def init_bot():
    """
    Inicialización mejorada del bot
    """
    global telegram_app, db, pump_monitor, flat_scanner, http_session

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurado")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID no configurado (modo privado)")

    # Telegram Application
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers mejorados
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("ajustar_flat", cmd_ajustar_flat))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("config", cmd_config))

    # Database
    db = DB(DATABASE_URL)
    await db.connect()

    # HTTP session + clients
    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)
    jup = JupiterClient(http_client)
    dexs = DexScreenerClient(http_client, DEXSCREENER_API)
    raydium = RaydiumClient(http_client)
    notifier = TelegramNotifier(telegram_app)

    # Monitores mejorados
    pump_monitor = HeliusPumpMonitor(HELIUS_WSS_URL, http_client, notifier, db, jup)
    flat_scanner = FlatScanner(jup, dexs, notifier, db, raydium)

@app.on_event("startup")
async def on_startup():
    port = os.getenv("PORT", "NO SET")
    print(f"🚀 Iniciando bot mejorado en puerto: {port}")
    print(f"🤖 Bot token configurado: {'YES' if TELEGRAM_BOT_TOKEN else 'NO'}")
    print(f"💬 Chat ID: {TELEGRAM_CHAT_ID}")
    
    try:
        await init_bot()
        await telegram_app.initialize()
        await telegram_app.start()
        
        print("✅ Telegram bot inicializado correctamente")
        
        # Iniciar monitores automáticamente
        await pump_monitor.start()
        await flat_scanner.start()
        print("✅ Monitores mejorados iniciados automáticamente")
        
    except Exception as e:
        print(f"❌ Error durante el startup: {e}")
        raise

@app.on_event("shutdown")
async def on_shutdown():
    print("🛑 Apagando bot...")
    await pump_monitor.stop()
    await flat_scanner.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    if db:
        await db.close()
    print("✅ Bot apagado correctamente")

# Endpoints de prueba mejorados
@app.get("/test")
async def test_endpoint():
    return {
        "status": "ok", 
        "bot_running": telegram_app is not None,
        "chat_id": TELEGRAM_CHAT_ID,
        "version": "2.0-mejorado"
    }

@app.get("/")
async def root():
    return {"status": "Bot mejorado funcionando", "version": "2.0", "webhook": "active"}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    """
    Endpoint webhook mejorado
    """
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid token")

        body = await req.json()
        print(f"📨 Webhook recibido: {body}")
        
        update = Update.de_json(body, telegram_app.bot)
        
        # Verificación de usuario (modo privado)
        if update.effective_user:
            if update.effective_user.id != TELEGRAM_CHAT_ID:
                print(f"🚫 Usuario no autorizado: {update.effective_user.id}")
                return JSONResponse({"ok": True, "note": "ignored - private bot"})
        
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
        
    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ---------------------------
# COMANDOS DE TELEGRAM MEJORADOS (HTML)
# ---------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    
    text = (
        "🤖 <b>Solana Monitor Bot - VERSIÓN MEJORADA</b> 🤖\n\n"
        "<b>Funciones Mejoradas:</b>\n"
        "• 🚀 Detección INTELIGENTE de tokens Pump.fun pre-graduación\n"
        "• 🎯 Scanner de tokens FLAT con filtros de calidad\n"
        "• 📊 Análisis con Jupiter API V2 y Organic Score\n"
        "• ⚡ Monitoreo en tiempo real mejorado\n\n"
        "<b>Comandos Disponibles:</b>\n"
        "/iniciar - Inicia todos los monitores\n"
        "/detener - Detiene todos los monitores\n"
        "/status - Estado y estadísticas\n"
        "/config - Ver configuración actual\n"
        "/ajustar_flat &lt;param&gt; &lt;valor&gt; - Ajustar parámetros\n\n"
        "<b>✨ Características Nuevas:</b>\n"
        "• Filtrado por Organic Score\n"
        "• Análisis de calidad de token\n"
        "• Alertas más informativas\n"
        "• Métricas mejoradas"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await pump_monitor.start()
    await flat_scanner.start()
    text = (
        "✅ <b>Monitores mejorados iniciados</b>\n\n"
        "Ahora con:\n"
        "• Filtros de calidad mejorados\n"
        "• Análisis en tiempo real\n"
        "• Métricas avanzadas"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await pump_monitor.stop()
    await flat_scanner.stop()
    await update.message.reply_text("⛔ <b>Monitores detenidos</b>", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    running = (pump_monitor.running if pump_monitor else False) or (flat_scanner.running if flat_scanner else False)
    cnt = await db.count_alerts_last_24h()
    
    text = (
        f"<b>📊 ESTADO DEL BOT MEJORADO</b> 📊\n\n"
        f"<b>Estado:</b> {'🟢 ACTIVOS' if running else '🔴 DETENIDOS'}\n"
        f"<b>Alertas (24h):</b> {cnt}\n\n"
        f"<b>🚀 PUMP.FUN MONITOR:</b>\n"
        f"• Umbral: ${PUMP_PRE_GRADUATION_MIN:,.0f}–${PUMP_PRE_GRADUATION_MAX:,.0f}\n"
        f"• Estado: {'🟢 Activo' if pump_monitor and pump_monitor.running else '🔴 Inactivo'}\n\n"
        f"<b>🎯 FLAT SCANNER:</b>\n"
        f"• Liquidez mínima: ${FLAT_LIQUIDITY_MIN:,.0f}\n"
        f"• Volumen 24h: ${FLAT_VOLUME_24H_MIN:,.0f}\n"
        f"• Volatilidad máxima: {FLAT_VOLATILITY_PCT}%\n"
        f"• Organic Score mínimo: {FLAT_MIN_ORGANIC_SCORE}\n"
        f"• Estado: {'🟢 Activo' if flat_scanner and flat_scanner.running else '🔴 Inactivo'}\n\n"
        f"<b>⚡ CONFIGURACIÓN:</b>\n"
        f"• Intervalo de escaneo: {CHECK_INTERVAL_MINUTES} min\n"
        f"• Horas sin repetir: {FLAT_TOKEN_REPEAT_HOURS}h"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    
    text = (
        "<b>⚙️ CONFIGURACIÓN ACTUAL</b> ⚙️\n\n"
        "<b>Pump.fun Monitor:</b>\n"
        f"• Mínimo: ${PUMP_PRE_GRADUATION_MIN:,.0f}\n"
        f"• Máximo: ${PUMP_PRE_GRADUATION_MAX:,.0f}\n\n"
        "<b>Flat Scanner:</b>\n"
        f"• Liquidez: ${FLAT_LIQUIDITY_MIN:,.0f}\n"
        f"• Volumen 24h: ${FLAT_VOLUME_24H_MIN:,.0f}\n"
        f"• Volatilidad: {FLAT_VOLATILITY_PCT}%\n"
        f"• Vol/vela: ${FLAT_VOLUME_AVG_PER_CANDLE_USD}\n"
        f"• Organic Score: {FLAT_MIN_ORGANIC_SCORE}\n"
        f"• Holders mín: {MIN_HOLDER_COUNT}\n\n"
        "<b>General:</b>\n"
        f"• Intervalo: {CHECK_INTERVAL_MINUTES} min\n"
        f"• Sin repetir: {FLAT_TOKEN_REPEAT_HOURS}h\n"
        f"• Velas: {CANDLES_HOURS}h ({CANDLE_INTERVAL})"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_ajustar_flat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    args = ctx.args or []
    if len(args) < 2:
        text = (
            "<b>📝 Uso:</b> /ajustar_flat &lt;parametro&gt; &lt;valor&gt;\n\n"
            "<b>Parámetros disponibles:</b>\n"
            "• <code>liquidez</code> - Liquidez mínima\n"
            "• <code>vol24h</code> - Volumen 24h mínimo\n" 
            "• <code>volatilidad</code> - % máxima de volatilidad\n"
            "• <code>volumen_promedio</code> - Volumen promedio por vela\n"
            "• <code>organic_score</code> - Puntuación orgánica mínima\n"
            "• <code>holders</code> - Mínimo de holders\n"
            "• <code>intervalo</code> - Minutos entre escaneos\n"
            "• <code>horas_sin_repetir</code> - Horas sin repetir alertas"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return
    
    param = args[0].lower()
    val = args[1]
    
    # CORRECCIÓN: Declaración global sin paréntesis
    global FLAT_VOLATILITY_PCT, FLAT_VOLUME_AVG_PER_CANDLE_USD, FLAT_LIQUIDITY_MIN, FLAT_VOLUME_24H_MIN, FLAT_MIN_ORGANIC_SCORE, MIN_HOLDER_COUNT, CHECK_INTERVAL_MINUTES, FLAT_TOKEN_REPEAT_HOURS
    
    try:
        if param in ("volatilidad", "volatility"):
            FLAT_VOLATILITY_PCT = float(val)
        elif param in ("volumen_promedio", "avg_vol"):
            FLAT_VOLUME_AVG_PER_CANDLE_USD = float(val)
        elif param in ("liquidez", "liquidity"):
            FLAT_LIQUIDITY_MIN = float(val)
        elif param in ("vol24h", "volume24h"):
            FLAT_VOLUME_24H_MIN = float(val)
        elif param in ("organic_score", "organic"):
            FLAT_MIN_ORGANIC_SCORE = float(val)
        elif param in ("holders", "holder_count"):
            MIN_HOLDER_COUNT = int(val)
        elif param in ("intervalo", "interval"):
            CHECK_INTERVAL_MINUTES = int(val)
        elif param in ("horas_sin_repetir", "repeat_hours"):
            FLAT_TOKEN_REPEAT_HOURS = int(val)
        else:
            await update.message.reply_text("❌ Parámetro no reconocido.")
            return
        
        await update.message.reply_text(f"✅ <code>{param}</code> actualizado a <code>{val}</code>", parse_mode="HTML")
        
    except Exception:
        await update.message.reply_text("❌ Valor inválido.")

# ---------------------------
# ENTRYPOINT MEJORADO
# ---------------------------
def run_uvicorn():
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=port)

if __name__ == "__main__":
    asyncio.run(init_bot())
    run_uvicorn()
