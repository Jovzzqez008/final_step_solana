# telegram_bot_final_v7_pro.py
# SOLANA MONITOR BOT v7 PRO - VERSIÓN DEFINITIVA
# PostgreSQL + QuickNode WSS + Jupiter V2 + Múltiples APIs + Control Total
# Deployment: Railway (Puerto 8080)

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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_BASE = "https://lite-api.jup.ag/tokens/v2"
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Parámetros de trading
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "100000"))
PRE_WINDOW_MIN = int(os.getenv("PRE_WINDOW_MIN", "10"))
FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "72"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))
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
        """Crear tablas esenciales sin índices problemáticos"""
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
        
        # Manejar diferentes formatos de respuesta
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_trending_tokens(self, interval: str = "5m", limit: int = 20) -> List[Dict]:
        """Obtener tokens en tendencia - MANEJO ROBUSTO"""
        url = f"{self.base_url}/toptrending/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_recent_tokens(self, limit: int = 30) -> List[Dict]:
        """Obtener tokens recién lanzados - MANEJO ROBUSTO"""
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_organic_tokens(self, interval: str = "5m", limit: int = 50) -> List[Dict]:
        """Obtener tokens con mejor organic score - MANEJO ROBUSTO"""
        url = f"{self.base_url}/toporganicscore/{interval}"
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

        # Construir mensaje base con formato profesional
        message = f"🚀 <b>{title}</b> 🚀\n\n"
        message += f"<b>{symbol}</b>\n"

        # Agregar datos específicos según el tipo de alerta
        if alert_type == "pump_fun":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0):.6f}\n"
            if data.get('organic_score'):
                message += f"• Organic Score: {data['organic_score']}\n"
            message += "• 🆕 Nuevo Pool Detectado\n"

        elif alert_type == "pre_explosion":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Volumen 5m: +{data.get('volume_change_5m', 0):.0f}%\n"
            message += f"• Traders 5m: +{data.get('traders_change_5m', 0):.0f}%\n"
            message += "• 📈 Momentum Pre-Explosión Detectado\n"

        elif alert_type == "trending":
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Cambio Precio: {data.get('price_change_24h', 0):.2f}%\n"
            message += "• 🔥 En Tendencia (Jupiter API)\n"

        message += f"\n<b>Mint:</b>\n<code>{mint}</code>"

        # Botones de acción mejorados
        keyboard = [
            [InlineKeyboardButton("🔁 Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("🔍 Go+ Security", url=f"https://gopluslabs.io/token-security/{mint}")]
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

# ========== MONITORES OPTIMIZADOS ==========
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
                await self.db.mark_notified(mint, "pump_fun", token_data.get('symbol'))
                await self.db.add_metric(mint, market_cap=market_cap)

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
                    'organic_score': token.get('organicScore'),
                    'holders': token.get('holderCount')
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
                    'holders': token.get('holderCount')
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
            trending_tokens = await jupiter.get_trending_tokens("5m", 30)
            organic_tokens = await jupiter.get_organic_tokens("5m", 30)
            recent_tokens = await jupiter.get_recent_tokens(20)

            all_tokens = trending_tokens + organic_tokens + recent_tokens
            
            # Filtrar tokens únicos
            unique_tokens = {}
            for token in all_tokens:
                mint = token.get('id')
                if mint:
                    unique_tokens[mint] = token

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

                # Detectar acumulación silenciosa (patrón pre-explosión)
                if (volume_change > 200 and traders_change > 30 and abs(price_change) < 15):
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
                    await self.db.mark_notified(mint, "pre_explosion", token.get('symbol'))
                    await self.db.add_metric(mint, market_cap=market_cap)

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(CHECK_INTERVAL * 60)
            except Exception as e:
                logging.error(f"❌ Error en PreExplosionScanner: {str(e)}")
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
            trending_tokens = await jupiter.get_trending_tokens("5m", 15)

            for token in trending_tokens:
                mint = token.get('id')
                if not mint or await self.db.was_notified(mint, "trending"):
                    continue

                market_cap = token.get('mcap', 0)
                # Alertar solo tokens con MC menor a 100k para early detection
                if market_cap < 100000:
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
                    await self.db.add_metric(mint, market_cap=market_cap)

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(3 * 60)  # Escanear cada 3 minutos
            except Exception as e:
                logging.error(f"❌ Error en TrendingScanner: {str(e)}")
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

# ========== APLICACIÓN FASTAPI + TELEGRAM ==========
app = FastAPI(title="Solana Monitor Bot v7 PRO")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
pre_explosion_scanner: Optional[PreExplosionScanner] = None
trending_scanner: Optional[TrendingScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM MEJORADOS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start mejorado"""
    if not is_authorized(update):
        return
    
    welcome_text = """
🤖 <b>SOLANA MONITOR BOT v7 PRO</b> 🚀

<b>Comandos disponibles:</b>
/iniciar - Iniciar todos los monitores
/detener - Detener todos los monitores  
/status - Ver estado de monitores
/silent on|off - Modo silencioso
/estadisticas - Métricas del bot

<b>Monitores activos:</b>
• 🎯 Pump.Fun Monitor (WSS QuickNode)
• 🔥 Pre-Explosión Scanner (Jupiter API)  
• 📈 Trending Scanner (Tendencia 5m)

<b>Características:</b>
• ✅ Market Cap filtering
• ✅ Análisis pre-explosión
• ✅ Detección temprana de tendencias
• ✅ Botones de acción rápida
• ✅ Base de datos PostgreSQL

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

        await update.message.reply_text(
            "✅ <b>TODOS LOS MONITORES INICIADOS</b>\n\n"
            "🎯 Pump.Fun Monitor → ACTIVO\n"
            "🔥 Pre-Explosión Scanner → ACTIVO\n"  
            "📈 Trending Scanner → ACTIVO\n\n"
            "<i>El bot está ahora escaneando el mercado en tiempo real...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitores iniciados por usuario: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando monitores: {str(e)}")
        logging.error(f"❌ Error en iniciar_command: {str(e)}")

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

        await update.message.reply_text(
            "⛔ <b>TODOS LOS MONITORES DETENIDOS</b>\n\n"
            "El bot ha dejado de escanear el mercado.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitores detenidos por usuario: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitores: {str(e)}")
        logging.error(f"❌ Error en detener_command: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status - Ver estado de monitores"""
    if not is_authorized(update):
        return

    # Verificar estado de cada monitor
    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    pre_status = "🟢 ACTIVO" if pre_explosion_scanner and pre_explosion_scanner.running else "🔴 INACTIVO"
    trend_status = "🟢 ACTIVO" if trending_scanner and trending_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    status_text = (
        "📊 <b>ESTADO DE MONITORES</b>\n\n"
        f"🎯 Pump.Fun Monitor: {pump_status}\n"
        f"🔥 Pre-Explosión Scanner: {pre_status}\n"
        f"📈 Trending Scanner: {trend_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        
        "<b>⚙️ Parámetros Actuales:</b>\n"
        f"• Pump MC Min: ${PUMP_MC_MIN:,.0f}\n"
        f"• Pre-Explosión MC Min: ${PRE_EXPLOSION_MC_MIN:,.0f}\n"
        f"• Ventana Pre-Explosión: {PRE_WINDOW_MIN}min\n"
        f"• Intervalo Escaneo: {CHECK_INTERVAL}min\n\n"
        
        "<b>🔧 Configuración:</b>\n"
        f"• QuickNode WSS: {'✅ CONFIGURADO' if QUICKNODE_WSS_URL else '❌ NO CONFIGURADO'}\n"
        f"• Base Datos: {'✅ CONECTADO' if db.pool else '❌ DESCONECTADO'}\n"
    )

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

async def estadisticas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /estadisticas - Métricas del bot"""
    if not is_authorized(update):
        return

    stats_text = (
        "📈 <b>ESTADÍSTICAS DEL BOT</b>\n\n"
        "<b>Monitores Activos:</b>\n"
        "• Pump.Fun Monitor (WSS QuickNode)\n"
        "• Pre-Explosión Scanner (Jupiter API)\n"
        "• Trending Scanner (Tendencia 5m)\n\n"
        
        "<b>Características:</b>\n"
        "✅ Detección en tiempo real\n"
        "✅ Análisis pre-explosión\n"
        "✅ Filtrado por Market Cap\n"
        "✅ Múltiples APIs de respaldo\n"
        "✅ Base de datos PostgreSQL\n\n"
        
        "<i>El bot está optimizado para detectar oportunidades\ntempranas en el ecosistema Solana.</i>"
    )

    await update.message.reply_text(stats_text, parse_mode="HTML")

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
        logging.error(f"❌ Webhook error: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "v7-pro-definitivo",
        "database": "connected" if db.pool else "disconnected",
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "pre_explosion_scanner": pre_explosion_scanner.running if pre_explosion_scanner else False,
            "trending_scanner": trending_scanner.running if trending_scanner else False
        }
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Solana Monitor Bot v7 PRO - Versión Definitiva",
        "status": "operational",
        "docs": "/docs",
        "health": "/health"
    }

# ========== INICIALIZACIÓN ROBUSTA ==========
async def initialize_app():
    """Inicializar toda la aplicación de forma robusta"""
    global telegram_app, notifier, pump_monitor, pre_explosion_scanner, trending_scanner

    # Verificar variables críticas
    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables de entorno: {', '.join(missing)}")

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
    telegram_app.add_handler(CommandHandler("estadisticas", estadisticas_command))

    # Inicializar notificador y monitores
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    pre_explosion_scanner = PreExplosionScanner(db, notifier)
    trending_scanner = TrendingScanner(db, notifier)

    logging.info("✅ Aplicación inicializada correctamente")

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de FastAPI"""
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
    """Evento de apagado de FastAPI"""
    logging.info("🛑 Apagando bot...")
    
    # Detener todos los monitores
    if pump_monitor:
        await pump_monitor.stop()
    if pre_explosion_scanner:
        await pre_explosion_scanner.stop()
    if trending_scanner:
        await trending_scanner.stop()

    # Cerrar aplicación de Telegram
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
