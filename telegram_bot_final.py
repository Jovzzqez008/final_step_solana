# telegram_bot_final_v8_pumpfocus.py
# SOLANA MONITOR BOT v8 - ENFOCADO EN PUMP.FUN PRE-GRADUACIÓN Y POST-GRAD PRE-EXPLOSIÓN
# PostgreSQL + QuickNode WSS + Jupiter V2 + Múltiples APIs + Control Total
# Deployment: Railway (Puerto 8080)

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_BASE = "https://lite-api.jup.ag/tokens/v2"
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Parámetros de trading (ajustables vía env)
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))         # market cap mínimo para alertar pre-grad
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "100000"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))         # minutos
PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL ROBUSTA ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas si no existen"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas esenciales"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Tabla de tokens vistos
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Tabla de métricas históricas
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    market_cap DECIMAL,
                    price DECIMAL,
                    volume_24h DECIMAL
                )
            """)

            # Tabla pump_candidates para enfoque exclusivo en pump.fun
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_candidates (
                    mint TEXT PRIMARY KEY,
                    symbol TEXT,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'pregraduado', -- valores: pregraduado, graduado, preexplosion_alertado
                    graduated_at TIMESTAMP,
                    last_metric_at TIMESTAMP
                )
            """)

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        """Verificar si un token ya fue notificado"""
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notifications WHERE mint = $1 AND alert_type = $2", 
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None):
        """Marcar token como notificado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (mint, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET 
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    created_at = NOW()
            """, mint, alert_type, symbol)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si un token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str):
        """Marcar token como visto"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint) VALUES ($1)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

    async def add_metric(self, mint: str, market_cap: float = None, price: float = None, volume: float = None):
        """Agregar métrica histórica"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO metrics (mint, market_cap, price, volume_24h)
                VALUES ($1, $2, $3, $4)
            """, mint, market_cap, price, volume)

    # --- Pump candidates helpers
    async def upsert_pump_candidate(self, mint: str, symbol: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_candidates (mint, symbol)
                VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET symbol = EXCLUDED.symbol
            """, mint, symbol)

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_candidates
                SET status = 'graduado', graduated_at = NOW()
                WHERE mint = $1
            """, mint)

    async def list_pregraduated(self) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT mint FROM pump_candidates WHERE status = 'pregraduado'")
            return [r['mint'] for r in rows]

    async def mark_preexplosion_alerted(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_candidates
                SET status = 'preexplosion_alertado', last_metric_at = NOW()
                WHERE mint = $1
            """, mint)

    async def list_graduated(self) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT mint FROM pump_candidates WHERE status = 'graduado'")
            return [r['mint'] for r in rows]

# ========== CLIENTES API ROBUSTOS ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, timeout: int = 8) -> Any:
        """Fetch JSON con manejo robusto de errores"""
        try:
            async with self.session.get(url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logging.warning(f"HTTP {response.status} from {url}")
                    return None
        except asyncio.TimeoutError:
            logging.warning(f"Timeout fetching {url}")
            return None
        except Exception as e:
            logging.error(f"Error fetching {url}: {str(e)}")
            return None

class JupiterAPI:
    def __init__(self, client: APIClient):
        self.client = client
        self.base_url = JUPITER_API_BASE

    async def search_token(self, mint: str) -> List[Dict]:
        """Buscar token por dirección mint - MANEJO ROBUSTO"""
        url = f"{self.base_url}/search"
        data = await self.client.fetch_json(url, params={"query": mint})
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_trending_tokens(self, interval: str = "5m", limit: int = 20) -> List[Dict]:
        url = f"{self.base_url}/toptrending/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_recent_tokens(self, limit: int = 30) -> List[Dict]:
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

class DexScreenerAPI:
    def __init__(self, client: APIClient):
        self.client = client
        self.base_url = DEXSCREENER_API

    async def get_token_info(self, mint: str) -> Dict:
        """Obtener información de token desde DexScreener"""
        url = f"{self.base_url}/tokens/{mint}"
        return await self.client.fetch_json(url)

# ========== NOTIFICADOR TELEGRAM MEJORADO ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str):
        """Enviar alerta formateada con botones inline mejorados"""
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        message = f"🚀 <b>{title}</b> 🚀\n\n"
        message += f"<b>{symbol}</b>\n"

        if alert_type in ("pump_fun", "post_graduation", "PUMP.FUN PRE-GRADUACIÓN"):
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0):.8f}\n"
            message += "• ⏱️ Alerta temprana: antes de graduación\n"
        elif alert_type == "pre_explosion":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Volumen 5m: +{data.get('volume_change_5m', 0):.0f}%\n"
            message += f"• Traders 5m: +{data.get('traders_change_5m', 0):.0f}%\n"
            message += "• 📈 Señal de acumulación detectada\n"

        message += f"\n<b>Mint:</b>\n<code>{mint}</code>"

        # Botones: Jupiter, DexScreener, RugCheck (sin Go+)
        keyboard = [
            [InlineKeyboardButton("🔁 Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta enviada: {symbol} - {alert_type}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

# ========== MONITORES ENFOCADOS EN PUMP.FUN ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def process_ws_message(self, message: str):
        """Procesar mensaje WebSocket y detectar nuevos mints"""
        mints = MINT_PATTERN.findall(message)
        for mint in set(mints):
            if await self.db.seen_recently(mint):
                continue

            await self.db.mark_seen(mint)
            logging.info(f"🆕 Nuevo mint detectado: {mint}")

            token_data = await self.get_token_data(mint)
            if not token_data:
                continue

            market_cap = token_data.get('market_cap', 0) or 0
            price = token_data.get('price', 0) or 0

            # Guardar candidato pregraduado si supera umbral
            if market_cap >= PUMP_MC_MIN:
                await self.db.upsert_pump_candidate(mint, token_data.get('symbol'))
                # Notificación pre-graduación (única por mint+tipo)
                if not await self.db.was_notified(mint, "pump_fun"):
                    await self.notifier.send_alert(
                        title="PUMP.FUN PRE-GRADUACIÓN",
                        symbol=token_data.get('symbol', 'UNKNOWN'),
                        mint=mint,
                        data={'market_cap': market_cap, 'price': price},
                        alert_type="pump_fun"
                    )
                    await self.db.mark_notified(mint, "pump_fun", token_data.get('symbol'))
                    await self.db.add_metric(mint, market_cap=market_cap, price=price)

    async def get_token_data(self, mint: str) -> Dict:
        """Obtener datos del token con manejo robusto"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            tokens = await jupiter.search_token(mint)
            if tokens and len(tokens) > 0:
                token = tokens[0]
                return {
                    'symbol': token.get('symbol', 'UNKNOWN'),
                    'name': token.get('name', 'Unknown'),
                    'price': token.get('usdPrice'),
                    'market_cap': token.get('mcap'),
                    'volume_24h': token.get('volume24h'),
                    'organic_score': token.get('organicScore')
                }

            # Fallback a DexScreener
            dexscreener = DexScreenerAPI(client)
            data = await dexscreener.get_token_info(mint)
            if data and 'token' in data:
                token = data['token']
                return {
                    'symbol': token.get('symbol', 'UNKNOWN'),
                    'name': token.get('name', 'Unknown'),
                    'price': token.get('priceUsd'),
                    'market_cap': token.get('marketCap'),
                    'volume_24h': token.get('volume', {}).get('h24')
                }

        return None

    async def start_websocket(self):
        """Iniciar conexión WebSocket a QuickNode con reconexión automática"""
        if not QUICKNODE_WSS_URL:
            logging.warning("⚠️ QuickNode WSS URL no configurada")
            return

        self.running = True
        reconnect_delay = 1

        while self.running:
            try:
                async with websockets.connect(QUICKNODE_WSS_URL) as ws:
                    logging.info("✅ Conectado a QuickNode WebSocket")
                    reconnect_delay = 1
                    async for message in ws:
                        if not self.running:
                            break
                        await self.process_ws_message(message)
            except Exception as e:
                logging.error(f"❌ WebSocket error: {str(e)}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.start_websocket())
        logging.info("✅ PumpFunMonitor iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PumpFunMonitor detenido")

class PreExplosionScanner:
    """Reorientado: vigila pump_candidates para detectar graduación y luego señales de acumulación post-graduación"""
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def check_graduation(self):
        """Revisa mints pregraduados para ver si ya tienen markets (heurística)"""
        pre_list = await self.db.list_pregraduated()
        if not pre_list:
            return
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            dexscreener = DexScreenerAPI(client)
            for mint in pre_list:
                tokens = await jupiter.search_token(mint)
                graduated = False
                token = {}
                if tokens and len(tokens) > 0:
                    token = tokens[0]
                    # heurística: si token tiene markets/pairs o mcap subió notablemente
                    if token.get('markets') or (token.get('mcap') and token.get('mcap') > PUMP_MC_MIN * 1.5):
                        graduated = True
                if not graduated:
                    data = await dexscreener.get_token_info(mint)
                    if data and data.get('pairs'):
                        graduated = True
                        token = data.get('token', {})
                if graduated:
                    await self.db.mark_graduated(mint)
                    await self.notifier.send_alert(
                        title="TOKEN GRADUADO (POST-GRAD)",
                        symbol=token.get('symbol', 'UNKNOWN'),
                        mint=mint,
                        data={'market_cap': token.get('mcap', 0), 'price': token.get('usdPrice', 0)},
                        alert_type="post_graduation"
                    )
                    await self.db.add_metric(mint, market_cap=token.get('mcap'), price=token.get('usdPrice'))

    async def scan_for_preexplosion(self):
        """Para cada token graduado, analizar métricas de 5m para pre-explosión"""
        graduated_list = await self.db.list_graduated()
        if not graduated_list:
            return
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            for mint in graduated_list:
                tokens = await jupiter.search_token(mint)
                if not tokens:
                    continue
                token = tokens[0]
                stats_5m = token.get('stats5m', {}) or {}
                volume_change = stats_5m.get('volumeChange', 0) or 0
                traders_change = stats_5m.get('numTraders', 0) or 0
                price_change = stats_5m.get('priceChange', 0) or 0
                market_cap = token.get('mcap', 0) or 0
                # Criterio ajustable: ejemplo conservador
                if (market_cap >= PRE_EXPLOSION_MC_MIN and volume_change > 150 and traders_change > 20 and price_change > 2):
                    if not await self.db.was_notified(mint, "pre_explosion"):
                        await self.notifier.send_alert(
                            title="PRE-EXPLOSIÓN (POST-GRAD)",
                            symbol=token.get('symbol', 'UNKNOWN'),
                            mint=mint,
                            data={
                                'market_cap': market_cap,
                                'volume_change_5m': volume_change,
                                'traders_change_5m': traders_change,
                                'price_change_5m': price_change
                            },
                            alert_type="pre_explosion"
                        )
                        await self.db.mark_notified(mint, "pre_explosion", token.get('symbol'))
                        await self.db.mark_preexplosion_alerted(mint)
                        await self.db.add_metric(mint, market_cap=market_cap, price=token.get('usdPrice'))

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.check_graduation()
                await self.scan_for_preexplosion()
                await asyncio.sleep(CHECK_INTERVAL * 60)
            except Exception as e:
                logging.error(f"❌ Error en PreExplosionScanner: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PreExplosionScanner (reoriented) iniciado")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PreExplosionScanner detenido")

# ========== APLICACIÓN FASTAPI + TELEGRAM ==========
app = FastAPI(title="Solana Monitor Bot v8 PUMP-FOCUS")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
pre_explosion_scanner: Optional[PreExplosionScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM (ENFOCADOS) ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    welcome_text = """
🤖 <b>SOLANA MONITOR BOT v8 - PUMP.FUN FOCUS</b> 🚀

Comandos:
 /iniciar - Iniciar monitores (pump + pre-explosion)
 /detener - Detener monitores
 /status - Estado
 /silent on|off - Modo silencioso
 /estadisticas - Métricas del bot
 /simulate <mint> <mxn_amount> <entry_price> <exit_price> - Simula ROI
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        if pump_monitor:
            await pump_monitor.start()
        if pre_explosion_scanner:
            await pre_explosion_scanner.start()
        await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")
        logging.info(f"📱 Monitores iniciados por usuario: {update.effective_user.id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando monitores: {str(e)}")
        logging.error(f"❌ Error en iniciar_command: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        if pump_monitor:
            await pump_monitor.stop()
        if pre_explosion_scanner:
            await pre_explosion_scanner.stop()
        await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")
        logging.info(f"📱 Monitores detenidos por usuario: {update.effective_user.id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitores: {str(e)}")
        logging.error(f"❌ Error en detener_command: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    pre_status = "🟢 ACTIVO" if pre_explosion_scanner and pre_explosion_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    status_text = (
        "📊 <b>ESTADO DE MONITORES</b>\n\n"
        f"🎯 Pump.Fun Monitor: {pump_status}\n"
        f"🔥 Pre-Explosión Scanner: {pre_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        "<b>Parámetros:</b>\n"
        f"• Pump MC Min: ${PUMP_MC_MIN:,.0f}\n"
        f"• Pre-Explosión MC Min: ${PRE_EXPLOSION_MC_MIN:,.0f}\n"
        f"• Intervalo Escaneo: {CHECK_INTERVAL}min\n\n"
        f"• QuickNode WSS: {'✅ CONFIGURADO' if QUICKNODE_WSS_URL else '❌ NO CONFIGURADO'}\n"
        f"• Base Datos: {'✅ CONECTADO' if db.pool else '❌ DESCONECTADO'}\n"
    )
    await update.message.reply_text(status_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args = context.args
    if not args or args[0] not in ['on', 'off']:
        await update.message.reply_text("Uso: /silent on|off")
        return
    if notifier:
        notifier.silent_mode = (args[0] == 'on')
        status = "🔇 ACTIVADO" if notifier.silent_mode else "🔔 DESACTIVADO"
        await update.message.reply_text(f"Modo silencioso: {status}")

async def estadisticas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    stats_text = (
        "📈 <b>ESTADÍSTICAS DEL BOT</b>\n\n"
        "Monitores activos: PumpFun Monitor, Pre-Explosion Scanner\n"
        "Enfoque: mints pre-graduación (pump.fun) y señales post-grad de acumulación\n"
    )
    await update.message.reply_text(stats_text, parse_mode="HTML")

async def simulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text("Uso: /simulate <mint> <mxn_amount> <entry_price> <exit_price>")
        return
    try:
        mint = args[0]
        mxn_amount = float(args[1])
        entry_price = float(args[2])
        exit_price = float(args[3])
        result = estimate_roi(mxn_amount, entry_price, exit_price)
        text = (
            f"Simulación para {mint}:\n"
            f"Tokens comprados: {result['tokens']:.4f}\n"
            f"Valor final (misma unidad): {result['final_value']:.2f}\n"
            f"ROI %: {result['roi_percent']:.2f}%\n"
        )
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Error en simulación: {str(e)}")

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"❌ Webhook error: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "v8-pump-focus",
        "database": "connected" if db.pool else "disconnected",
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "pre_explosion_scanner": pre_explosion_scanner.running if pre_explosion_scanner else False
        }
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Monitor Bot v8 PUMP-FOCUS",
        "status": "operational",
        "docs": "/docs",
        "health": "/health"
    }

# ========== UTILIDADES ==========
def estimate_roi(entry_mxn: float, entry_price: float, exit_price: float) -> Dict:
    """
    Estima cantidad de tokens comprados y valor final.
    entry_mxn: MXN que invertirías.
    entry_price: precio token en la misma unidad (si usas USD convierte primero).
    exit_price: precio objetivo (misma unidad).
    """
    tokens = entry_mxn / entry_price if entry_price and entry_price > 0 else 0
    final_value = tokens * exit_price
    roi = ((final_value - entry_mxn) / entry_mxn * 100) if entry_mxn > 0 else 0
    return {"tokens": tokens, "final_value": final_value, "roi_percent": roi}

# ========== INICIALIZACIÓN ROBUSTA ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor, pre_explosion_scanner

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables de entorno: {', '.join(missing)}")

    await db.connect()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("estadisticas", estadisticas_command))
    telegram_app.add_handler(CommandHandler("simulate", simulate_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    pre_explosion_scanner = PreExplosionScanner(db, notifier)

    logging.info("✅ Aplicación inicializada correctamente")

@app.on_event("startup")
async def startup_event():
    try:
        await initialize_app()
        await telegram_app.initialize()
        await telegram_app.start()
        logging.info("🚀 Bot iniciado y listo - USA /iniciar PARA COMENZAR")
    except Exception as e:
        logging.error(f"❌ Error durante el startup: {str(e)}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("🛑 Apagando bot...")
    if pump_monitor:
        await pump_monitor.stop()
    if pre_explosion_scanner:
        await pre_explosion_scanner.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
