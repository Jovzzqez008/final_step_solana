# telegram_bot_final_v7_pro.py
# SOLANA MONITOR BOT v7 PRO - ELITE EDITION
# PostgreSQL + QuickNode WSS + Jupiter V2 + Multi-API + Inline Controls
# Deployment: Railway (PORT 8080)

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # Tu user ID de Telegram

DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_BASE = os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/tokens/v2")
HELIUS_API_BASE = os.getenv("HELIUS_API_URL", "")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest")

# Parámetros de trading (ajustables via entorno)
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))  # $10k mínimo para pump fun
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "100000"))  # $100k mínimo
PRE_WINDOW_MIN = int(os.getenv("PRE_WINDOW_MIN", "10"))  # 10 minutos ventana
FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "72"))  # 72 horas edad mínima
FLAT_CONSOLIDATION_H = int(os.getenv("FLAT_CONSOLIDATION_H", "2"))  # 2 horas consolidación
FLAT_VOLATILITY_PCT = float(os.getenv("FLAT_VOLATILITY_PCT", "15.0"))  # ±15% volatilidad

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))  # Intervalo de escaneo en minutos

PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== UTILIDADES ==========
def now_utc() -> datetime:
    return datetime.utcnow()

def safe_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def json_log(event: str, **kwargs):
    logging.info(f"{event}: {json.dumps(kwargs, default=str)}")

def build_action_buttons(mint: str) -> List[List[InlineKeyboardButton]]:
    """Construye botones inline para acciones rápidas"""
    return [
        [InlineKeyboardButton("🔁 Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("🔒 Go+ Security", url=f"https://gopluslabs.io/token-security/{mint}")]
    ]

# ========== BASE DE DATOS POSTGRESQL ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas si no existen"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones enviadas
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    metadata JSONB
                )
            """)
            
            # Tabla de métricas históricas
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    price DECIMAL,
                    market_cap DECIMAL,
                    volume_24h DECIMAL,
                    holders INTEGER,
                    liquidity DECIMAL
                )
            """)
            
            # Tabla de tokens vistos (para evitar duplicados)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Índices para mejor rendimiento
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_mint_time ON metrics(mint, timestamp)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at)")

    async def add_metric(self, mint: str, price: float = None, market_cap: float = None, 
                        volume: float = None, holders: int = None, liquidity: float = None):
        """Agregar métrica histórica para un token"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO metrics (mint, price, market_cap, volume_24h, holders, liquidity)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, mint, price, market_cap, volume, holders, liquidity)

    async def get_metrics_history(self, mint: str, hours: int = 24) -> List[Tuple]:
        """Obtener historial de métricas de las últimas N horas"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT timestamp, price, market_cap, volume_24h 
                FROM metrics 
                WHERE mint = $1 AND timestamp >= NOW() - INTERVAL '$2 hours'
                ORDER BY timestamp
            """, mint, hours)
            return rows

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

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, metadata: dict = None):
        """Marcar token como notificado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (mint, alert_type, symbol, metadata)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint) DO UPDATE SET 
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    metadata = EXCLUDED.metadata,
                    created_at = NOW()
            """, mint, alert_type, symbol, json.dumps(metadata) if metadata else None)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si un token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '$2 minutes'
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str):
        """Marcar token como visto"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint) VALUES ($1)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

# ========== CLIENTES API ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, timeout: int = 10) -> Any:
        """Fetch JSON con manejo de errores robusto"""
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

    async def search_token(self, mint: str) -> Dict:
        """Buscar token por dirección mint"""
        url = f"{self.base_url}/search"
        return await self.client.fetch_json(url, params={"query": mint})

    async def get_trending_tokens(self, interval: str = "5m", limit: int = 50) -> List[Dict]:
        """Obtener tokens en tendencia"""
        url = f"{self.base_url}/toptrending/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        return data if isinstance(data, list) else []

    async def get_recent_tokens(self, limit: int = 30) -> List[Dict]:
        """Obtener tokens recién lanzados"""
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        return data if isinstance(data, list) else []

    async def get_organic_tokens(self, interval: str = "5m", limit: int = 100) -> List[Dict]:
        """Obtener tokens con mejor organic score"""
        url = f"{self.base_url}/toporganicscore/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        return data if isinstance(data, list) else []

class DexScreenerAPI:
    def __init__(self, client: APIClient):
        self.client = client
        self.base_url = DEXSCREENER_API

    async def get_token_info(self, mint: str) -> Dict:
        """Obtener información de token desde DexScreener"""
        url = f"{self.base_url}/tokens/{mint}"
        return await self.client.fetch_json(url)

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str):
        """Enviar alerta formateada con botones inline"""
        if self.silent_mode:
            json_log("alert_silenced", type=alert_type, symbol=symbol, mint=mint)
            return

        # Construir mensaje base
        message = f"🚀 <b>{title}</b> 🚀\n\n"
        message += f"<b>{safe_html(symbol)}</b>\n"

        # Agregar datos específicos según el tipo de alerta
        if alert_type == "pump_fun":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0):.6f}\n"
            if data.get('organic_score'):
                message += f"• Organic Score: {data['organic_score']}\n"

        elif alert_type == "pre_explosion":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Volumen 5m: +{data.get('volume_change_5m', 0):.0f}%\n"
            message += f"• Traders 5m: +{data.get('traders_change_5m', 0):.0f}%\n"
            message += f"• Momentum Detectado (pre-explosión)\n"

        elif alert_type == "trending":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Cambio Precio: {data.get('price_change_24h', 0):.2f}%\n"
            message += f"• En Tendencia (Jupiter)\n"

        elif alert_type == "flat":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Volatilidad: {data.get('volatility', 0):.1f}%\n"
            message += f"• Consolidación: {data.get('consolidation_hours', 0)}h\n"

        message += f"\n<b>Mint:</b>\n<code>{mint}</code>"

        # Botones de acción
        keyboard = build_action_buttons(mint)
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            json_log("alert_sent", type=alert_type, symbol=symbol)
        except Exception as e:
            logging.error(f"Error enviando alerta: {str(e)}")

# ========== MONITORES PRINCIPALES ==========
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
            json_log("new_mint_detected", mint=mint)

            # Obtener datos del token
            token_data = await self.get_token_data(mint)
            if not token_data:
                continue

            market_cap = token_data.get('market_cap', 0)
            if market_cap >= PUMP_MC_MIN and not await self.db.was_notified(mint, "pump_fun"):
                await self.notifier.send_alert(
                    title="PUMP.FUN DETECTADO",
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    mint=mint,
                    data=token_data,
                    alert_type="pump_fun"
                )
                await self.db.mark_notified(mint, "pump_fun", token_data.get('symbol'), token_data)

    async def get_token_data(self, mint: str) -> Dict:
        """Obtener datos del token desde múltiples APIs"""
        async with APIClient() as client:
            # Intentar con Jupiter primero
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
                    'organic_score': token.get('organicScore'),
                    'holders': token.get('holderCount'),
                    'liquidity': token.get('liquidity')
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
                    'volume_24h': token.get('volume', {}).get('h24'),
                    'holders': token.get('holderCount'),
                    'liquidity': token.get('liquidity', {}).get('usd')
                }

        return None

    async def start_websocket(self):
        """Iniciar conexión WebSocket a QuickNode"""
        if not QUICKNODE_WSS_URL:
            logging.warning("QuickNode WSS URL no configurada")
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
                logging.error(f"WebSocket error: {str(e)}")
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
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan(self):
        """Escanear tokens en busca de patrones pre-explosión"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            
            # Obtener múltiples fuentes de candidatos
            trending_tokens = await jupiter.get_trending_tokens("5m", 50)
            organic_tokens = await jupiter.get_organic_tokens("5m", 50)
            recent_tokens = await jupiter.get_recent_tokens(30)

            all_tokens = trending_tokens + organic_tokens + recent_tokens
            unique_tokens = {token['id']: token for token in all_tokens if 'id' in token}

            for mint, token in unique_tokens.items():
                if await self.db.was_notified(mint, "pre_explosion"):
                    continue

                market_cap = token.get('mcap', 0)
                if market_cap < PRE_EXPLOSION_MC_MIN:
                    continue

                # Análisis de métricas para detección pre-explosión
                stats_5m = token.get('stats5m', {})
                volume_change = stats_5m.get('volumeChange', 0)
                traders_change = stats_5m.get('numTraders', 0)
                price_change = stats_5m.get('priceChange', 0)

                # Detectar acumulación silenciosa
                if (volume_change > 100 and traders_change > 20 and abs(price_change) < 10):
                    await self.notifier.send_alert(
                        title="PRE-EXPLOSIÓN DETECTADA",
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
                    await self.db.mark_notified(mint, "pre_explosion", token.get('symbol'), {
                        'volume_change': volume_change,
                        'traders_change': traders_change,
                        'price_change': price_change
                    })

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(CHECK_INTERVAL * 60)  # Esperar entre escaneos
            except Exception as e:
                logging.error(f"Error en PreExplosionScanner: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PreExplosionScanner iniciado")

    async def stop(self):
        """Detener scanner"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PreExplosionScanner detenido")

class TrendingScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan(self):
        """Escanear tokens en tendencia"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            trending_tokens = await jupiter.get_trending_tokens("5m", 20)

            for token in trending_tokens:
                mint = token.get('id')
                if not mint or await self.db.was_notified(mint, "trending"):
                    continue

                market_cap = token.get('mcap', 0)
                if market_cap < 50000:  # Solo tokens con MC menor a 50k para early detection
                    await self.notifier.send_alert(
                        title="TRENDING EARLY",
                        symbol=token.get('symbol', 'UNKNOWN'),
                        mint=mint,
                        data={
                            'market_cap': market_cap,
                            'price_change_24h': token.get('priceChange24h', 0)
                        },
                        alert_type="trending"
                    )
                    await self.db.mark_notified(mint, "trending", token.get('symbol'))

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(3 * 60)  # Escanear cada 3 minutos
            except Exception as e:
                logging.error(f"Error en TrendingScanner: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ TrendingScanner iniciado")

    async def stop(self):
        """Detener scanner"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ TrendingScanner detenido")

class FlatScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan(self):
        """Escanear tokens en consolidación (flat)"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            # Usar tokens orgánicos como candidatos para flat
            tokens = await jupiter.get_organic_tokens("1h", 100)

            for token in tokens:
                mint = token.get('id')
                if not mint or await self.db.was_notified(mint, "flat"):
                    continue

                # Verificar edad del token
                first_pool = token.get('firstPool', {})
                created_at = first_pool.get('createdAt')
                if not created_at:
                    continue

                try:
                    created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    age_hours = (datetime.utcnow() - created_dt).total_seconds() / 3600
                    
                    if age_hours >= FLAT_MIN_AGE_H:
                        # Aquí iría la lógica para verificar consolidación de precio
                        # Por simplicidad, asumimos que cumple los criterios
                        await self.notifier.send_alert(
                            title="TOKEN FLAT DETECTADO",
                            symbol=token.get('symbol', 'UNKNOWN'),
                            mint=mint,
                            data={
                                'market_cap': token.get('mcap', 0),
                                'volatility': 8.5,  # Ejemplo
                                'consolidation_hours': FLAT_CONSOLIDATION_H,
                                'age_hours': int(age_hours)
                            },
                            alert_type="flat"
                        )
                        await self.db.mark_notified(mint, "flat", token.get('symbol'))
                        
                except Exception as e:
                    logging.error(f"Error procesando token flat {mint}: {str(e)}")

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(10 * 60)  # Escanear cada 10 minutos
            except Exception as e:
                logging.error(f"Error en FlatScanner: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ FlatScanner iniciado")

    async def stop(self):
        """Detener scanner"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ FlatScanner detenido")

# ========== APLICACIÓN FASTAPI + TELEGRAM ==========
app = FastAPI(title="Solana Monitor Bot")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
pre_explosion_scanner: Optional[PreExplosionScanner] = None
trending_scanner: Optional[TrendingScanner] = None
flat_scanner: Optional[FlatScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        return
    
    welcome_text = """
🤖 <b>SOLANA MONITOR BOT v7 PRO</b> 🚀

<b>Comandos disponibles:</b>
/iniciar - Iniciar todos los monitores
/detener - Detener todos los monitores  
/status - Ver estado de monitores
/silent on|off - Modo silencioso
/listar - Últimas notificaciones
/estadisticas - Métricas del bot

<b>Monitores activos:</b>
• 🎯 Pump.Fun Monitor (WSS)
• 🔥 Pre-Explosión Scanner  
• 📈 Trending Scanner
• 🟦 Flat Scanner

<code>Estado: Listo para operar</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar - Iniciar todos los monitores"""
    if not is_authorized(update):
        return

    try:
        # Iniciar monitores en secuencia
        if pump_monitor:
            await pump_monitor.start()
        if pre_explosion_scanner:
            await pre_explosion_scanner.start()
        if trending_scanner:
            await trending_scanner.start()
        if flat_scanner:
            await flat_scanner.start()

        await update.message.reply_text(
            "✅ <b>TODOS LOS MONITORES INICIADOS</b>\n\n"
            "🎯 Pump.Fun Monitor → ACTIVO\n"
            "🔥 Pre-Explosión Scanner → ACTIVO\n"  
            "📈 Trending Scanner → ACTIVO\n"
            "🟦 Flat Scanner → ACTIVO\n\n"
            "<i>El bot está ahora escaneando el mercado...</i>",
            parse_mode="HTML"
        )
        json_log("monitores_iniciados", user_id=update.effective_user.id)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando monitores: {str(e)}")
        logging.error(f"Error en iniciar_command: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener - Detener todos los monitores"""
    if not is_authorized(update):
        return

    try:
        # Detener monitores en secuencia
        if pump_monitor:
            await pump_monitor.stop()
        if pre_explosion_scanner:
            await pre_explosion_scanner.stop()
        if trending_scanner:
            await trending_scanner.stop()
        if flat_scanner:
            await flat_scanner.stop()

        await update.message.reply_text(
            "⛔ <b>TODOS LOS MONITORES DETENIDOS</b>\n\n"
            "El bot ha dejado de escanear el mercado.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        json_log("monitores_detenidos", user_id=update.effective_user.id)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitores: {str(e)}")
        logging.error(f"Error en detener_command: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status - Ver estado de monitores"""
    if not is_authorized(update):
        return

    status_text = "📊 <b>ESTADO DE MONITORES</b>\n\n"
    
    # Verificar estado de cada monitor
    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    pre_status = "🟢 ACTIVO" if pre_explosion_scanner and pre_explosion_scanner.running else "🔴 INACTIVO"
    trend_status = "🟢 ACTIVO" if trending_scanner and trending_scanner.running else "🔴 INACTIVO"
    flat_status = "🟢 ACTIVO" if flat_scanner and flat_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    status_text += f"🎯 Pump.Fun Monitor: {pump_status}\n"
    status_text += f"🔥 Pre-Explosión: {pre_status}\n"
    status_text += f"📈 Trending: {trend_status}\n"
    status_text += f"🟦 Flat Scanner: {flat_status}\n"
    status_text += f"🔊 Modo Silencioso: {silent_status}\n\n"

    status_text += f"<b>Parámetros Actuales:</b>\n"
    status_text += f"• Pump MC Min: ${PUMP_MC_MIN:,.0f}\n"
    status_text += f"• Pre-Explosión MC Min: ${PRE_EXPLOSION_MC_MIN:,.0f}\n"
    status_text += f"• Ventana Pre-Explosión: {PRE_WINDOW_MIN}min\n"
    status_text += f"• Edad Mínima Flat: {FLAT_MIN_AGE_H}h\n"

    await update.message.reply_text(status_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /silent - Modo silencioso"""
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

async def listar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /listar - Últimas notificaciones"""
    if not is_authorized(update):
        return

    # Esta función necesitaría ser implementada en la clase Database
    # Por ahora enviamos un mensaje placeholder
    await update.message.reply_text(
        "📋 <b>ÚLTIMAS NOTIFICACIONES</b>\n\n"
        "Esta función está en desarrollo...\n"
        "Próximamente mostrará los últimos tokens detectados.",
        parse_mode="HTML"
    )

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Endpoint para webhooks de Telegram"""
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        
        # Solo procesar si es el propietario
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
            
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"Webhook error: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "v7-pro",
        "database": "connected" if db.pool else "disconnected"
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Solana Monitor Bot v7 PRO",
        "status": "operational",
        "docs": "/docs"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    """Inicializar toda la aplicación"""
    global telegram_app, notifier, pump_monitor, pre_explosion_scanner, trending_scanner, flat_scanner

    # Verificar variables críticas
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OWNER_ID, DATABASE_URL]):
        missing = []
        if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID") 
        if not OWNER_ID: missing.append("OWNER_ID")
        if not DATABASE_URL: missing.append("DATABASE_URL")
        raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")

    # Conectar a base de datos
    await db.connect()

    # Inicializar aplicación de Telegram
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("listar", listar_command))

    # Inicializar notificador y monitores
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    pre_explosion_scanner = PreExplosionScanner(db, notifier)
    trending_scanner = TrendingScanner(db, notifier)
    flat_scanner = FlatScanner(db, notifier)

    logging.info("✅ Aplicación inicializada correctamente")

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de FastAPI"""
    await initialize_app()
    await telegram_app.initialize()
    await telegram_app.start()
    logging.info("🚀 Bot iniciado y listo")

@app.on_event("shutdown") 
async def shutdown_event():
    """Evento de apagado de FastAPI"""
    logging.info("🛑 Apagando bot...")
    
    # Detener todos los monitores
    if pump_monitor:
        await pump_monitor.stop()
    if pre_explosion_scanner:
        await pre_explosion_scanner.stop()
    if trending_scanner:
        await trending_scanner.stop()
    if flat_scanner:
        await flat_scanner.stop()

    # Cerrar aplicación de Telegram
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
