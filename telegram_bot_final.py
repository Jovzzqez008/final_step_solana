# telegram_bot_final_v7_pro_complete.py
# SOLANA MONITOR BOT v7 PRO - COMPLETO CON TODOS LOS COMANDOS
# PostgreSQL + QuickNode WSS + Jupiter V2 + Sistema de Triggers Mejorado

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from math import isfinite

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN MEJORADA ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Variables de entorno
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_BASE = os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/tokens/v2")
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Parámetros de trading MEJORADOS
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "50000"))
PRE_GRADUATION_MC_THRESHOLD = float(os.getenv("PRE_GRADUATION_MC_THRESHOLD", "25000"))

# Triggers de Pre-Explosión
WATCHLIST_INTERVAL = 60
UP_TRIGGER_PCT = 10.0
REVERSAL_NEGATIVE_THRESHOLD = -20.0
REVERSAL_UP_PCT = 12.0

# Filtros de calidad
MIN_ORGANIC_SCORE = int(os.getenv("MIN_ORGANIC_SCORE", "30"))
MAX_WATCHLIST_SIZE = 100

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))
PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL MEJORADA ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas si no existen"""
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas esenciales incluyendo sistema de watchlist"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(mint, alert_type)
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
                    volume_24h DECIMAL,
                    organic_score INTEGER
                )
            """)

            # TABLA DE CANDIDATOS (WATCHLIST)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    base_price DECIMAL,
                    base_price_24h_change DECIMAL,
                    last_price DECIMAL,
                    market_cap DECIMAL,
                    organic_score INTEGER,
                    status TEXT DEFAULT 'watching',
                    source TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Índices para mejor performance
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candidates_status 
                ON candidates(status, updated_at)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candidates_organic 
                ON candidates(organic_score DESC)
            """)

    # Métodos existentes para notificaciones
    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
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
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (mint, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint, alert_type) DO UPDATE SET 
                    symbol = EXCLUDED.symbol,
                    created_at = NOW()
            """, mint, alert_type, symbol)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint) VALUES ($1)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

    async def add_metric(self, mint: str, market_cap: float = None, price: float = None, 
                        volume: float = None, organic_score: int = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO metrics (mint, market_cap, price, volume_24h, organic_score)
                VALUES ($1, $2, $3, $4, $5)
            """, mint, market_cap, price, volume, organic_score)

    # MÉTODOS PARA SISTEMA DE WATCHLIST
    async def add_candidate(self, mint: str, symbol: str = None, name: str = None,
                          base_price: float = None, base_price_24h_change: float = None,
                          market_cap: float = None, organic_score: int = None, 
                          source: str = "wss") -> bool:
        """Añadir token a watchlist. Retorna True si fue añadido, False si ya existe"""
        async with self.pool.acquire() as conn:
            try:
                # Verificar si ya existe
                existing = await conn.fetchrow(
                    "SELECT 1 FROM candidates WHERE mint = $1", mint
                )
                if existing:
                    return False

                # Limpiar candidatos viejos si hay demasiados
                count = await conn.fetchval("SELECT COUNT(*) FROM candidates WHERE status = 'watching'")
                if count >= MAX_WATCHLIST_SIZE:
                    await conn.execute("""
                        DELETE FROM candidates 
                        WHERE id IN (
                            SELECT id FROM candidates 
                            WHERE status = 'watching' 
                            ORDER BY updated_at ASC 
                            LIMIT 10
                        )
                    """)

                # Insertar nuevo candidato
                await conn.execute("""
                    INSERT INTO candidates 
                    (mint, symbol, name, base_price, base_price_24h_change, 
                     last_price, market_cap, organic_score, source)
                    VALUES ($1, $2, $3, $4, $5, $4, $6, $7, $8)
                """, mint, symbol, name, base_price, base_price_24h_change, 
                   market_cap, organic_score, source)
                return True
            except Exception as e:
                logging.error(f"Error añadiendo candidato {mint}: {str(e)}")
                return False

    async def get_watchlist_candidates(self, limit: int = 15) -> List[asyncpg.Record]:
        """Obtener candidatos para monitoreo"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM candidates 
                WHERE status = 'watching' 
                ORDER BY organic_score DESC NULLS LAST, created_at ASC
                LIMIT $1
            """, limit)

    async def update_candidate_price(self, mint: str, price: float, market_cap: float = None):
        """Actualizar precio actual de candidato"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE candidates 
                SET last_price = $2, 
                    market_cap = COALESCE($3, market_cap),
                    updated_at = NOW()
                WHERE mint = $1
            """, mint, price, market_cap)

    async def mark_candidate_triggered(self, mint: str):
        """Marcar candidato como triggered"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE candidates 
                SET status = 'triggered', updated_at = NOW()
                WHERE mint = $1
            """, mint)

    async def cleanup_old_candidates(self, hours: int = 24):
        """Limpiar candidatos antiguos"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM candidates 
                WHERE created_at < NOW() - INTERVAL '1 hour' * $1
            """, hours)

    async def get_stats(self):
        """Obtener estadísticas del sistema"""
        async with self.pool.acquire() as conn:
            total_candidates = await conn.fetchval("SELECT COUNT(*) FROM candidates")
            watching_candidates = await conn.fetchval("SELECT COUNT(*) FROM candidates WHERE status = 'watching'")
            triggered_candidates = await conn.fetchval("SELECT COUNT(*) FROM candidates WHERE status = 'triggered'")
            total_notifications = await conn.fetchval("SELECT COUNT(*) FROM notifications")
            
            return {
                'total_candidates': total_candidates,
                'watching_candidates': watching_candidates,
                'triggered_candidates': triggered_candidates,
                'total_notifications': total_notifications
            }

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
        """Buscar token por dirección mint"""
        url = f"{self.base_url}/search"
        data = await self.client.fetch_json(url, params={"query": mint})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_trending_tokens(self, interval: str = "5m", limit: int = 20) -> List[Dict]:
        """Obtener tokens en tendencia"""
        url = f"{self.base_url}/toptrending/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_recent_tokens(self, limit: int = 30) -> List[Dict]:
        """Obtener tokens recién lanzados"""
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_organic_tokens(self, interval: str = "5m", limit: int = 50) -> List[Dict]:
        """Obtener tokens con mejor organic score"""
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

        # Construir mensaje según tipo de alerta
        if alert_type == "pre_explosion_up":
            message = f"🚀 <b>{title}</b> 🚀\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Precio Base: ${data.get('base_price', 0):.6f}\n"
            message += f"• Precio Actual: ${data.get('current_price', 0):.6f}\n"
            message += f"• Cambio: +{data.get('price_change_pct', 0):.2f}%\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += "• 📈 BREAKOUT DETECTADO\n"

        elif alert_type == "pre_explosion_reversal":
            message = f"🔄 <b>{title}</b> 🔄\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Precio Base: ${data.get('base_price', 0):.6f}\n"
            message += f"• Precio Actual: ${data.get('current_price', 0):.6f}\n"
            message += f"• Cambio 24h Base: {data.get('base_24h_change', 0):.2f}%\n"
            message += f"• Recuperación: +{data.get('price_change_pct', 0):.2f}%\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += "• 📈 REVERSIÓN DESDE ZONA NEGATIVA\n"

        elif alert_type == "pump_fun_early":
            message = f"🎯 <b>{title}</b> 🎯\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0):.6f}\n"
            if data.get('organic_score'):
                message += f"• Organic Score: {data['organic_score']}\n"
            message += "• 🆕 POOL DETECTADO - PRE-GRADUACIÓN\n"

        else:
            # Alertas genéricas
            message = f"🔔 <b>{title}</b>\n\n"
            message += f"<b>{symbol}</b>\n"
            for key, value in data.items():
                if isinstance(value, (int, float)) and key != 'price':
                    message += f"• {key.replace('_', ' ').title()}: {value:,.0f}\n"
                else:
                    message += f"• {key.replace('_', ' ').title()}: {value}\n"

        message += f"\n<b>Mint:</b>\n<code>{mint}</code>"

        # Botones de acción
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

# ========== MONITORES MEJORADOS ==========

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
            organic_score = token_data.get('organic_score', 0)

            # AÑADIR A WATCHLIST SI CUMPLE CRITERIOS MÍNIMOS
            if (market_cap >= PUMP_MC_MIN and 
                organic_score >= MIN_ORGANIC_SCORE and
                token_data.get('price')):
                
                added = await self.db.add_candidate(
                    mint=mint,
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    name=token_data.get('name', 'Unknown'),
                    base_price=token_data.get('price'),
                    base_price_24h_change=token_data.get('price_change_24h'),
                    market_cap=market_cap,
                    organic_score=organic_score,
                    source="pump_fun"
                )
                
                if added:
                    logging.info(f"✅ Candidato añadido a watchlist: {token_data.get('symbol')}")

            # ALERTA INMEDIATA SI ES MUY PROMETEDOR (PRE-GRADUACIÓN)
            if (market_cap >= PUMP_MC_MIN and 
                market_cap <= PRE_GRADUATION_MC_THRESHOLD and
                organic_score >= 50 and
                not await self.db.was_notified(mint, "pump_fun_early")):
                
                await self.notifier.send_alert(
                    title="PUMP.FUN EARLY DETECTION",
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    mint=mint,
                    data=token_data,
                    alert_type="pump_fun_early"
                )
                await self.db.mark_notified(mint, "pump_fun_early", token_data.get('symbol'))

            await self.db.add_metric(
                mint, 
                market_cap=market_cap, 
                price=token_data.get('price'),
                organic_score=organic_score
            )

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
                    'price_change_24h': token.get('priceChange24h'),
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
        logging.info("✅ PumpFunMonitor mejorado iniciado")

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

class WatchlistMonitor:
    """MONITOR MEJORADO DE WATCHLIST CON CONVERSIONES DE TIPO ARREGLADAS"""
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def check_candidates(self):
        """Revisar todos los candidatos en watchlist para triggers"""
        candidates = await self.db.get_watchlist_candidates(limit=15)
        
        if not candidates:
            return

        logging.info(f"🔍 Revisando {len(candidates)} candidatos en watchlist...")

        for candidate in candidates:
            try:
                await self.check_single_candidate(candidate)
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Error revisando candidato {candidate['mint']}: {str(e)}")

    async def check_single_candidate(self, candidate):
        """Revisar un solo candidato para triggers de precio - CONVERSIONES ARREGLADAS"""
        mint = candidate['mint']
        symbol = candidate['symbol'] or 'UNKNOWN'
        
        # CONVERSIONES SEGURAS DE TIPOS DECIMAL → FLOAT
        base_price = self.safe_convert_decimal(candidate['base_price'])
        if not base_price or base_price <= 0:
            return

        base_24h_change = self.safe_convert_decimal(candidate['base_price_24h_change'])
        
        # Obtener precio actual
        current_data = await self.get_current_token_data(mint)
        if not current_data or not current_data.get('price'):
            return

        current_price = current_data['price']
        current_mcap = current_data.get('market_cap')

        # CONVERSIÓN SEGURA DEL PRECIO ACTUAL
        try:
            current_price = float(current_price) if current_price is not None else None
        except (TypeError, ValueError):
            return

        if current_price is None or current_price <= 0:
            return

        # Calcular cambio porcentual - AHORA AMBOS SON FLOATS
        price_change_pct = ((current_price - base_price) / base_price) * 100

        # Actualizar precio en base de datos
        await self.db.update_candidate_price(mint, current_price, current_mcap)

        # 🎯 TRIGGER 1: Subida normal >= 10%
        if price_change_pct >= UP_TRIGGER_PCT:
            if not await self.db.was_notified(mint, "pre_explosion_up"):
                await self.notifier.send_alert(
                    title="PRE-EXPLOSIÓN DETECTADA",
                    symbol=symbol,
                    mint=mint,
                    data={
                        'base_price': base_price,
                        'current_price': current_price,
                        'price_change_pct': price_change_pct,
                        'market_cap': current_mcap
                    },
                    alert_type="pre_explosion_up"
                )
                await self.db.mark_notified(mint, "pre_explosion_up", symbol)
                await self.db.mark_candidate_triggered(mint)
                logging.info(f"🎯 Trigger UP disparado: {symbol} +{price_change_pct:.2f}%")
                return

        # 🎯 TRIGGER 2: Reversión desde zona negativa
        if (base_24h_change is not None and 
            base_24h_change <= REVERSAL_NEGATIVE_THRESHOLD and
            price_change_pct >= REVERSAL_UP_PCT):
            
            if not await self.db.was_notified(mint, "pre_explosion_reversal"):
                await self.notifier.send_alert(
                    title="REVERSIÓN PRE-EXPLOSIÓN",
                    symbol=symbol,
                    mint=mint,
                    data={
                        'base_price': base_price,
                        'current_price': current_price,
                        'price_change_pct': price_change_pct,
                        'base_24h_change': base_24h_change,
                        'market_cap': current_mcap
                    },
                    alert_type="pre_explosion_reversal"
                )
                await self.db.mark_notified(mint, "pre_explosion_reversal", symbol)
                await self.db.mark_candidate_triggered(mint)
                logging.info(f"🔄 Trigger REVERSAL disparado: {symbol} +{price_change_pct:.2f}% desde {base_24h_change:.2f}%")

    def safe_convert_decimal(self, value):
        """CONVERSIÓN SEGURA DE decimal.Decimal A float"""
        if value is None:
            return None
        try:
            if isinstance(value, Decimal):
                return float(value)
            elif isinstance(value, (int, float)):
                return float(value)
            else:
                return float(str(value))
        except (TypeError, ValueError) as e:
            logging.warning(f"Error convirtiendo valor {value}: {str(e)}")
            return None

    async def get_current_token_data(self, mint: str) -> Dict:
        """Obtener datos actualizados del token - CON CONVERSIONES SEGURAS"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            tokens = await jupiter.search_token(mint)
            
            if tokens and len(tokens) > 0:
                token = tokens[0]
                price = token.get('usdPrice')
                market_cap = token.get('mcap')
                
                # CONVERSIONES SEGURAS
                try:
                    safe_price = float(price) if price is not None else None
                    safe_mcap = float(market_cap) if market_cap is not None else None
                except (TypeError, ValueError):
                    return None
                    
                return {
                    'price': safe_price,
                    'market_cap': safe_mcap,
                    'volume_24h': token.get('volume24h')
                }
            
            # Fallback a DexScreener
            dexscreener = DexScreenerAPI(client)
            data = await dexscreener.get_token_info(mint)
            if data and 'token' in data:
                token = data['token']
                price = token.get('priceUsd')
                market_cap = token.get('marketCap')
                
                # CONVERSIONES SEGURAS
                try:
                    safe_price = float(price) if price is not None else None
                    safe_mcap = float(market_cap) if market_cap is not None else None
                except (TypeError, ValueError):
                    return None
                    
                return {
                    'price': safe_price,
                    'market_cap': safe_mcap
                }

        return None

    async def run(self):
        """Ejecutar monitor continuamente"""
        self.running = True
        
        # Limpieza inicial
        await self.db.cleanup_old_candidates(24)
        
        while self.running:
            try:
                await self.check_candidates()
                await asyncio.sleep(WATCHLIST_INTERVAL)
            except Exception as e:
                logging.error(f"❌ Error en WatchlistMonitor: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ WatchlistMonitor mejorado iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ WatchlistMonitor detenido")

class TrendingScanner:
    """Scanner de tendencias que también alimenta la watchlist"""
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan(self):
        """Escanear tokens en tendencia y añadir a watchlist"""
        async with APIClient() as client:
            jupiter = JupiterAPI(client)
            
            # Obtener múltiples fuentes
            trending_tokens = await jupiter.get_trending_tokens("5m", 20)
            organic_tokens = await jupiter.get_organic_tokens("5m", 20)
            
            all_tokens = trending_tokens + organic_tokens
            
            for token in all_tokens:
                mint = token.get('id')
                if not mint:
                    continue

                # Verificar si ya está en watchlist
                existing = await self.db.was_notified(mint, "any")
                if existing:
                    continue

                market_cap = token.get('mcap', 0)
                organic_score = token.get('organicScore', 0)
                price = token.get('usdPrice')

                # Añadir a watchlist si cumple criterios
                if (market_cap >= PRE_EXPLOSION_MC_MIN and 
                    organic_score >= MIN_ORGANIC_SCORE and
                    price and price > 0):
                    
                    await self.db.add_candidate(
                        mint=mint,
                        symbol=token.get('symbol', 'UNKNOWN'),
                        name=token.get('name', 'Unknown'),
                        base_price=price,
                        base_price_24h_change=token.get('priceChange24h'),
                        market_cap=market_cap,
                        organic_score=organic_score,
                        source="trending"
                    )

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan()
                await asyncio.sleep(5 * 60)  # Escanear cada 5 minutos
            except Exception as e:
                logging.error(f"❌ Error en TrendingScanner: {str(e)}")
                await asyncio.sleep(60)

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ TrendingScanner mejorado iniciado")

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
app = FastAPI(title="Solana Monitor Bot v7 PRO - Enhanced")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
watchlist_monitor: Optional[WatchlistMonitor] = None
trending_scanner: Optional[TrendingScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM COMPLETOS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start mejorado"""
    if not is_authorized(update):
        return
    
    welcome_text = """
🤖 <b>SOLANA MONITOR BOT v7 PRO - ENHANCED</b> 🚀

<b>🆕 Sistema Mejorado de Watchlist:</b>
• Almacenamiento automático de tokens candidatos
• Monitoreo cada 60 segundos  
• Triggers: +10% normal O +12% desde zona negativa
• Alertas INSTANTÁNEAS al detectar movimiento

<b>🎯 Pre-Graduación PumpFun:</b>
• Detección antes del listing en DEX
• Filtrado por organic score ≥ 30
• Market Cap temprano para entrada oportuna

<b>Comandos disponibles:</b>
/iniciar - Iniciar todos los monitores
/detener - Detener todos los monitores  
/status - Ver estado y estadísticas
/watchlist - Ver tokens en observación
/silent on|off - Modo silencioso
/estadisticas - Estadísticas del sistema

<code>Sistema optimizado para detección temprana</code>
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
        if watchlist_monitor:
            await watchlist_monitor.start()
        if trending_scanner:
            await trending_scanner.start()

        await update.message.reply_text(
            "✅ <b>TODOS LOS MONITORES INICIADOS</b>\n\n"
            "🎯 Pump.Fun Monitor → ACTIVO\n"
            "📊 Watchlist Monitor → ACTIVO\n"  
            "📈 Trending Scanner → ACTIVO\n\n"
            "<i>El sistema está ahora monitoreando tokens en tiempo real...</i>",
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
        if watchlist_monitor:
            await watchlist_monitor.stop()
        if trending_scanner:
            await trending_scanner.stop()

        await update.message.reply_text(
            "⛔ <b>TODOS LOS MONITORES DETENIDOS</b>\n\n"
            "El sistema ha dejado de monitorear el mercado.\n"
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
    watchlist_status = "🟢 ACTIVO" if watchlist_monitor and watchlist_monitor.running else "🔴 INACTIVO"
    trend_status = "🟢 ACTIVO" if trending_scanner and trending_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    # Obtener estadísticas
    stats = await db.get_stats()

    status_text = (
        "📊 <b>ESTADO DE MONITORES</b>\n\n"
        f"🎯 Pump.Fun Monitor: {pump_status}\n"
        f"📊 Watchlist Monitor: {watchlist_status}\n"
        f"📈 Trending Scanner: {trend_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        
        "<b>📈 ESTADÍSTICAS:</b>\n"
        f"• Tokens en observación: {stats['watching_candidates']}\n"
        f"• Total candidatos: {stats['total_candidates']}\n"
        f"• Alertas disparadas: {stats['triggered_candidates']}\n"
        f"• Notificaciones totales: {stats['total_notifications']}\n\n"
        
        "<b>⚙️ PARÁMETROS ACTIVOS:</b>\n"
        f"• Watchlist Interval: {WATCHLIST_INTERVAL}s\n"
        f"• Up Trigger: {UP_TRIGGER_PCT}%\n"
        f"• Reversal Trigger: {REVERSAL_UP_PCT}% (desde ≤{REVERSAL_NEGATIVE_THRESHOLD}%)\n"
        f"• Min Organic Score: {MIN_ORGANIC_SCORE}\n\n"
        
        "<b>🔧 CONFIGURACIÓN:</b>\n"
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

    stats = await db.get_stats()
    
    stats_text = (
        "📈 <b>ESTADÍSTICAS DEL SISTEMA</b>\n\n"
        f"<b>Base de Datos:</b>\n"
        f"• Total Candidatos: {stats['total_candidates']}\n"
        f"• En Observación: {stats['watching_candidates']}\n"
        f"• Alertas Disparadas: {stats['triggered_candidates']}\n"
        f"• Notificaciones: {stats['total_notifications']}\n\n"
        
        "<b>Monitores Activos:</b>\n"
        "• 🎯 Pump.Fun Monitor (WSS QuickNode)\n"
        "• 📊 Watchlist Monitor (Triggers Duales)\n"
        "• 📈 Trending Scanner (Jupiter API)\n\n"
        
        "<b>Triggers Configurados:</b>\n"
        f"• Subida Normal: ≥ {UP_TRIGGER_PCT}%\n"
        f"• Reversión: ≥ {REVERSAL_UP_PCT}% desde zona ≤ {REVERSAL_NEGATIVE_THRESHOLD}%\n"
        f"• Organic Score Mínimo: {MIN_ORGANIC_SCORE}\n\n"
        
        "<i>El sistema está optimizado para detección\ntemprana de oportunidades en Solana.</i>"
    )

    await update.message.reply_text(stats_text, parse_mode="HTML")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /watchlist - Ver tokens en observación"""
    if not is_authorized(update):
        return

    try:
        candidates = await db.get_watchlist_candidates(limit=20)
        if not candidates:
            await update.message.reply_text("📭 No hay tokens en watchlist actualmente")
            return

        message = "👁️ <b>TOKENS EN OBSERVACIÓN</b>\n\n"
        
        for i, candidate in enumerate(candidates, 1):
            symbol = candidate['symbol'] or 'UNKNOWN'
            base_price = candidate['base_price'] or 0
            last_price = candidate['last_price'] or base_price
            mcap = candidate['market_cap'] or 0
            organic = candidate['organic_score'] or 0
            
            # Calcular cambio
            if base_price and base_price > 0:
                change_pct = ((last_price - base_price) / base_price) * 100
                change_str = f"{change_pct:+.1f}%"
            else:
                change_str = "N/A"
            
            message += (
                f"{i}. <b>{symbol}</b>\n"
                f"   💰 ${last_price:.6f} ({change_str})\n"
                f"   📊 MC: ${mcap:,.0f} | 🌱 {organic}\n"
                f"   <code>{candidate['mint'][:8]}...{candidate['mint'][-6:]}</code>\n\n"
            )

        await update.message.reply_text(message, parse_mode="HTML")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error obteniendo watchlist: {str(e)}")
        logging.error(f"Error en watchlist_command: {str(e)}")

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Endpoint para webhooks de Telegram"""
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
    """Endpoint de health check mejorado"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "v7-pro-enhanced",
        "watchlist_system": "active",
        "triggers": {
            "up_trigger_pct": UP_TRIGGER_PCT,
            "reversal_trigger_pct": REVERSAL_UP_PCT,
            "reversal_negative_threshold": REVERSAL_NEGATIVE_THRESHOLD
        },
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "watchlist_monitor": watchlist_monitor.running if watchlist_monitor else False,
            "trending_scanner": trending_scanner.running if trending_scanner else False
        }
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Solana Monitor Bot v7 PRO - Enhanced Watchlist System",
        "status": "operational",
        "docs": "/docs",
        "health": "/health"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    """Inicializar toda la aplicación"""
    global telegram_app, notifier, pump_monitor, watchlist_monitor, trending_scanner

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

    # Registrar TODOS los comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("estadisticas", estadisticas_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))

    # Inicializar notificador y monitores MEJORADOS
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    watchlist_monitor = WatchlistMonitor(db, notifier)
    trending_scanner = TrendingScanner(db, notifier)

    logging.info("✅ Aplicación mejorada inicializada correctamente")

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de FastAPI"""
    try:
        await initialize_app()
        await telegram_app.initialize()
        await telegram_app.start()
        logging.info("🚀 Bot mejorado iniciado - USA /iniciar PARA COMENZAR")
    except Exception as e:
        logging.error(f"❌ Error durante el startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    """Evento de apagado de FastAPI"""
    logging.info("🛑 Apagando bot mejorado...")
    
    # Detener todos los monitores
    if pump_monitor:
        await pump_monitor.stop()
    if watchlist_monitor:
        await watchlist_monitor.stop()
    if trending_scanner:
        await trending_scanner.stop()

    # Cerrar aplicación de Telegram
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot mejorado apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
