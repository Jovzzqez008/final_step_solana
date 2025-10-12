# telegram_bot_pump_fun_definitivo.py
# BOT DEFINITIVO PARA DETECCIÓN PRE-GRADUACIÓN EN PUMP.FUN

import os
import re
import json
import asyncio
import logging
import websockets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import aiohttp
import asyncpg
from fastapi import FastAPI, Request
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

# APIs PRINCIPALES
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
PUMP_FUN_API_BASE = "https://frontend-api.pump.fun"

# Parámetros de trading
UMBRAL_MCAP = float(os.getenv("UMBRAL_MCAP", "55000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "1000"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3"))
PORT = int(os.getenv("PORT", "8080"))

# Program ID de Pump.fun (CONFIRMADO)
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ========== BASE DE DATOS POSTGRESQL ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL not set")
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            # Tabla principal de tokens en monitoreo
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_tokens (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    price DECIMAL DEFAULT 0,
                    liquidity DECIMAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_checked TIMESTAMP DEFAULT NOW(),
                    pre_graduation_alert BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    source TEXT DEFAULT 'helius'
                )
            """)
            
            # Tabla de alertas históricas
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    alert_type TEXT,
                    progress_percent DECIMAL,
                    sent_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Tabla de tokens vistos (cache rápido)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT DEFAULT 'helius'
                )
            """)

    async def add_token(self, mint: str, symbol: str = None, name: str = None):
        """Agregar token a monitoreo"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_tokens (mint, symbol, name) 
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET 
                    last_checked = NOW(),
                    is_active = TRUE
            """, mint, symbol, name)

    async def update_token_data(self, mint: str, market_cap: float, price: float = None, liquidity: float = None):
        """Actualizar datos del token"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_tokens 
                SET market_cap = $1, price = $2, liquidity = $3, last_checked = NOW()
                WHERE mint = $4
            """, market_cap, price, liquidity, mint)

    async def mark_pre_graduation_alert(self, mint: str):
        """Marcar alerta pre-graduación enviada"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_tokens 
                SET pre_graduation_alert = TRUE, alert_sent_at = NOW()
                WHERE mint = $1
            """, mint)

    async def was_pre_graduation_alert_sent(self, mint: str) -> bool:
        """Verificar si ya se envió alerta pre-graduación"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pump_tokens WHERE mint = $1 AND pre_graduation_alert = TRUE", 
                mint
            )
            return bool(row)

    async def get_active_tokens(self) -> List[Dict]:
        """Obtener tokens activos para monitoreo"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, name, market_cap 
                FROM pump_tokens 
                WHERE is_active = TRUE 
                AND pre_graduation_alert = FALSE
                AND market_cap < $1
                AND last_checked > NOW() - INTERVAL '4 hours'
                ORDER BY market_cap DESC
                LIMIT 100
            """, GRADUATION_MC_TARGET)
            return [dict(row) for row in rows]

    async def log_alert(self, mint: str, symbol: str, market_cap: float, alert_type: str, progress: float = None):
        """Registrar alerta en historial"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_alerts (mint, symbol, market_cap, alert_type, progress_percent)
                VALUES ($1, $2, $3, $4, $5)
            """, mint, symbol, market_cap, alert_type, progress)

    async def get_recent_alerts(self, limit: int = 10) -> List[Dict]:
        """Obtener alertas recientes"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, alert_type, progress_percent, sent_at
                FROM pump_alerts 
                ORDER BY sent_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si token fue visto recientemente"""
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

    async def cleanup_old_tokens(self):
        """Limpiar tokens antiguos"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_tokens 
                SET is_active = FALSE 
                WHERE last_checked < NOW() - INTERVAL '6 hours'
                AND pre_graduation_alert = FALSE
            """)

# ========== CLIENTE API PUMP.FUN ==========
class PumpFunAPI:
    def __init__(self):
        self.base_url = PUMP_FUN_API_BASE
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site"
        }

    async def get_recent_coins(self, limit: int = 100) -> List[Dict]:
        """Obtener coins recientes de Pump.fun"""
        url = f"{self.base_url}/coins"
        params = {
            "limit": limit,
            "offset": 0,
            "sort": "createdAt",
            "order": "DESC",
            "includeNsfw": "true"
        }
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            coins = [self._parse_coin_data(coin) for coin in data if coin]
                            logging.info(f"✅ Pump.fun API: {len(coins)} coins recientes")
                            return coins
        except asyncio.TimeoutError:
            logging.warning("⏱️  Timeout obteniendo coins recientes")
        except Exception as e:
            logging.error(f"❌ Error obteniendo coins recientes: {str(e)}")
        
        return []

    async def get_coin_by_mint(self, mint: str) -> Optional[Dict]:
        """Obtener coin específico por mint address"""
        endpoints = [
            f"{self.base_url}/coins/{mint}",
            f"{self.base_url}/coin/{mint}",
        ]
        
        for url in endpoints:
            try:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    async with session.get(url, timeout=8) as response:
                        if response.status == 200:
                            data = await response.json()
                            return self._parse_coin_data(data, mint)
                        elif response.status == 404:
                            continue
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue
        
        return None

    async def get_trending_coins(self, limit: int = 50) -> List[Dict]:
        """Obtener coins en tendencia"""
        url = f"{self.base_url}/coins"
        params = {
            "limit": limit,
            "offset": 0,
            "sort": "volume",
            "order": "DESC",
            "includeNsfw": "true"
        }
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            return [self._parse_coin_data(coin) for coin in data if coin]
        except Exception as e:
            logging.error(f"❌ Error obteniendo trending coins: {str(e)}")
        
        return []

    def _parse_coin_data(self, data: Dict, mint: str = None) -> Optional[Dict]:
        """Parsear datos de un coin"""
        if not data:
            return None
            
        # Extraer mint address
        coin_mint = data.get('mint') or data.get('id') or mint
        if not coin_mint:
            return None
            
        # Extraer market cap
        market_cap = data.get('marketCap') or data.get('mcap') or 0
        try:
            market_cap = float(market_cap)
        except (TypeError, ValueError):
            market_cap = 0
            
        # Extraer precio
        price = data.get('price') or data.get('usdPrice') or 0
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = 0
            
        # Extraer liquidez
        liquidity = data.get('liquidity', {}).get('usd', 0) if isinstance(data.get('liquidity'), dict) else data.get('liquidity', 0)
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0

        return {
            'mint': coin_mint,
            'symbol': data.get('symbol', 'UNKNOWN'),
            'name': data.get('name', ''),
            'market_cap': market_cap,
            'price': price,
            'liquidity': liquidity,
            'volume_24h': data.get('volume24h', 0),
            'created_at': data.get('createdAt')
        }

# ========== CLIENTE HELIUS WEBSOCKET ==========
class HeliusWebSocket:
    def __init__(self, on_new_token_callback):
        self.wss_url = HELIUS_WSS_URL
        self.on_new_token_callback = on_new_token_callback
        self.websocket = None
        self.running = False
        self.reconnect_delay = 1
        self.connection_task = None

    async def connect(self):
        """Conectar a WebSocket de Helius"""
        try:
            self.websocket = await websockets.connect(
                self.wss_url, 
                ping_interval=20, 
                ping_timeout=10,
                close_timeout=10
            )
            
            # Suscribirse a logs del programa Pump.fun
            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {
                        "mentions": [PUMP_FUN_PROGRAM_ID]
                    },
                    {"commitment": "confirmed"}
                ]
            }
            
            await self.websocket.send(json.dumps(subscribe_message))
            response = await self.websocket.recv()
            logging.info("✅ Helius WebSocket conectado y suscrito a Pump.fun")
            return True
            
        except Exception as e:
            logging.error(f"❌ Error conectando Helius WebSocket: {str(e)}")
            self.websocket = None
            return False

    async def listen(self):
        """Escuchar mensajes del WebSocket"""
        self.running = True
        
        while self.running:
            try:
                if not self.websocket:
                    success = await self.connect()
                    if not success:
                        await asyncio.sleep(self.reconnect_delay)
                        self.reconnect_delay = min(self.reconnect_delay * 2, 30)
                        continue
                    self.reconnect_delay = 1
                
                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                
                # Procesar logs de transacciones
                if data.get('method') == 'logsNotification':
                    await self._process_log_notification(data)
                
                self.reconnect_delay = 1
                
            except asyncio.TimeoutError:
                # Timeout normal, verificar si todavía estamos conectados
                continue
            except websockets.exceptions.ConnectionClosed:
                logging.warning(f"🔌 Helius WebSocket desconectado, reconectando en {self.reconnect_delay}s...")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 30)
            except Exception as e:
                logging.error(f"❌ Error en Helius WebSocket: {str(e)}")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 30)

    async def _process_log_notification(self, data: Dict):
        """Procesar notificación de logs"""
        try:
            logs = data.get('params', {}).get('result', {}).get('value', {}).get('logs', [])
            signature = data.get('params', {}).get('result', {}).get('value', {}).get('signature', '')
            
            # Buscar patrones de creación de token
            creation_indicators = [
                'initialize_mint', 'create', 'new_token', 'initialize2',
                'create_token', 'initialize_account', 'create_mint'
            ]
            
            for log in logs:
                log_lower = log.lower()
                if any(indicator in log_lower for indicator in creation_indicators):
                    # Extraer mint address del log
                    mint_match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', log)
                    if mint_match:
                        mint = mint_match.group(0)
                        logging.info(f"🚀 Nuevo token detectado: {mint[:8]}...")
                        
                        # Llamar callback con el nuevo mint
                        await self.on_new_token_callback(mint)
                        break
                        
        except Exception as e:
            logging.error(f"❌ Error procesando log: {str(e)}")

    async def start(self):
        """Iniciar listener"""
        if self.connection_task and not self.connection_task.done():
            return
            
        self.connection_task = asyncio.create_task(self.listen())
        logging.info("✅ Helius WebSocket iniciado")

    async def stop(self):
        """Detener WebSocket"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
        if self.connection_task:
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ Helius WebSocket detenido")

# ========== NOTIFICADOR TELEGRAM ==========
def build_buttons_for_mint(mint: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⚡ Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("📈 Pump.Fun", url=f"https://pump.fun/coin/{mint}")]
    ]
    return InlineKeyboardMarkup(kb)

class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_pre_graduation_alert(self, symbol: str, mint: str, market_cap: float):
        """Enviar alerta EXCLUSIVA de pre-graduación"""
        if self.silent_mode:
            return

        progress = (market_cap / GRADUATION_MC_TARGET) * 100
        remaining = GRADUATION_MC_TARGET - market_cap
        
        message_lines = [
            "🎯 <b>ALERTA PRE-GRADUACIÓN PUMP.FUN</b> 🎯",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Objetivo: ${GRADUATION_MC_TARGET:,.0f}",
            f"• Progreso: <b>{progress:.1f}%</b>",
            f"• Restante: ${remaining:,.0f}",
            "",
            "⚡ <b>OPORTUNIDAD ANTES DE GRADUACIÓN</b>",
            "• Token cerca de alcanzar $69k MC",
            "• Momento crítico para entrada",
            "",
            "<b>Mint:</b>",
            f"<code>{mint}</code>",
            "",
            "⚠️ <i>Toma decisión rápida - Graduación inminente</i>"
        ]
        
        message = "\n".join(message_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta PRE-GRADUACIÓN enviada: {symbol} - ${market_cap:,.0f}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

    async def send_new_token_alert(self, symbol: str, mint: str, market_cap: float):
        """Enviar alerta de nuevo token detectado"""
        if self.silent_mode:
            return

        message_lines = [
            "🆕 <b>NUEVO TOKEN DETECTADO</b>",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: ${market_cap:,.0f}" if market_cap > 0 else "• Market Cap: Consultando...",
            "",
            "🔍 <b>EN MONITOREO PRE-GRADUACIÓN</b>",
            f"• Se alertará al alcanzar ${UMBRAL_MCAP:,.0f}",
            "",
            "<b>Mint:</b>",
            f"<code>{mint}</code>"
        ]
        
        message = "\n".join(message_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
        except Exception as e:
            logging.error(f"❌ Error enviando alerta nuevo token: {str(e)}")

# ========== MONITOR PRINCIPAL ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.pump_api = PumpFunAPI()
        self.helius_ws = HeliusWebSocket(self.handle_new_token_detection)
        
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.running = False
        self.scan_round = 0
        self.stats = {
            'tokens_detected': 0,
            'pre_graduation_alerts': 0,
            'last_scan': None
        }

    async def handle_new_token_detection(self, mint: str):
        """Manejar nuevo token detectado por Helius"""
        try:
            # Evitar duplicados recientes
            if await self.db.seen_recently(mint, minutes=3):
                return
                
            await self.db.mark_seen(mint)
            self.stats['tokens_detected'] += 1
            
            # Agregar a base de datos inmediatamente
            await self.db.add_token(mint)
            
            # Obtener datos iniciales del token
            token_data = await self.pump_api.get_coin_by_mint(mint)
            
            symbol = "CONSULTANDO..."
            market_cap = 0
            
            if token_data:
                symbol = token_data.get('symbol', 'UNKNOWN')
                market_cap = token_data.get('market_cap', 0)
                
                # Actualizar en BD
                await self.db.add_token(mint, symbol, token_data.get('name'))
                await self.db.update_token_data(
                    mint, 
                    market_cap, 
                    token_data.get('price'), 
                    token_data.get('liquidity')
                )

            # Solo monitorear tokens con market cap razonable
            if market_cap >= PUMP_MC_MIN:
                # Enviar alerta de nuevo token
                await self.notifier.send_new_token_alert(symbol, mint, market_cap)
                
                # Iniciar monitoreo continuo para pre-graduación
                await self.start_token_monitoring(mint, symbol)
                
                logging.info(f"✅ Token en monitoreo: {symbol} (${market_cap:,.0f})")

        except Exception as e:
            logging.error(f"❌ Error manejando nuevo token {mint}: {str(e)}")

    async def start_token_monitoring(self, mint: str, symbol: str):
        """Iniciar monitoreo continuo para pre-graduación"""
        if mint in self.monitoring_tasks:
            return

        async def monitor_single_token():
            consecutive_errors = 0
            max_errors = 3
            
            while self.running and mint in self.monitoring_tasks:
                try:
                    # Obtener datos actualizados desde Pump.fun
                    token_data = await self.pump_api.get_coin_by_mint(mint)
                    if not token_data:
                        consecutive_errors += 1
                        if consecutive_errors >= max_errors:
                            logging.warning(f"⚠️  Demasiados errores para {symbol}, deteniendo monitoreo")
                            await self.stop_token_monitoring(mint)
                            break
                        await asyncio.sleep(10)
                        continue
                    
                    consecutive_errors = 0
                    market_cap = token_data.get('market_cap', 0)
                    
                    # Actualizar en base de datos
                    await self.db.update_token_data(
                        mint, 
                        market_cap, 
                        token_data.get('price'), 
                        token_data.get('liquidity')
                    )

                    # VERIFICACIÓN EXCLUSIVA PARA PRE-GRADUACIÓN
                    if (market_cap >= UMBRAL_MCAP and 
                        market_cap <= GRADUATION_MC_TARGET and 
                        not await self.db.was_pre_graduation_alert_sent(mint)):
                        
                        # ENVIAR ALERTA EXCLUSIVA DE PRE-GRADUACIÓN
                        await self.notifier.send_pre_graduation_alert(symbol, mint, market_cap)
                        
                        # Marcar como alertado
                        await self.db.mark_pre_graduation_alert(mint)
                        progress = (market_cap / GRADUATION_MC_TARGET) * 100
                        await self.db.log_alert(mint, symbol, market_cap, "pre_graduation", progress)
                        
                        self.stats['pre_graduation_alerts'] += 1
                        logging.info(f"🎯 ALERTA PRE-GRADUACIÓN: {symbol} (${market_cap:,.0f})")

                    # Si ya se graduó, detener monitoreo
                    if market_cap > GRADUATION_MC_TARGET:
                        logging.info(f"🎓 Token graduado: {symbol}")
                        await self.stop_token_monitoring(mint)
                        break

                except Exception as e:
                    logging.error(f"❌ Error monitoreando {symbol}: {str(e)}")
                    consecutive_errors += 1
                    if consecutive_errors >= max_errors:
                        await self.stop_token_monitoring(mint)
                        break
                
                await asyncio.sleep(CHECK_INTERVAL)

        self.monitoring_tasks[mint] = asyncio.create_task(monitor_single_token())

    async def stop_token_monitoring(self, mint: str):
        """Detener monitoreo de un token"""
        if mint in self.monitoring_tasks:
            self.monitoring_tasks[mint].cancel()
            try:
                await self.monitoring_tasks[mint]
            except asyncio.CancelledError:
                pass
            del self.monitoring_tasks[mint]

    async def scan_existing_tokens(self):
        """Escanear tokens existentes en la base de datos"""
        self.scan_round += 1
        self.stats['last_scan'] = datetime.utcnow()
        
        logging.info(f"🔄 Escaneo pre-graduación #{self.scan_round}")
        
        try:
            tokens = await self.db.get_active_tokens()
            for token in tokens:
                mint = token['mint']
                symbol = token.get('symbol', 'UNKNOWN')
                
                # Si no está siendo monitoreado, iniciar monitoreo pre-graduación
                if mint not in self.monitoring_tasks:
                    await self.start_token_monitoring(mint, symbol)
                    
            logging.info(f"📊 Tokens en monitoreo: {len(self.monitoring_tasks)}")
                    
        except Exception as e:
            logging.error(f"❌ Error en escaneo pre-graduación: {str(e)}")

    async def bulk_scan_recent_tokens(self):
        """Escaneo masivo de tokens recientes"""
        try:
            recent_coins = await self.pump_api.get_recent_coins(50)
            for coin in recent_coins:
                mint = coin['mint']
                market_cap = coin['market_cap']
                
                if market_cap >= PUMP_MC_MIN and not await self.db.seen_recently(mint, minutes=10):
                    await self.db.add_token(mint, coin['symbol'], coin['name'])
                    await self.db.update_token_data(mint, market_cap, coin['price'], coin['liquidity'])
                    
                    if mint not in self.monitoring_tasks:
                        await self.start_token_monitoring(mint, coin['symbol'])
            
            logging.info(f"✅ Escaneo masivo: {len(recent_coins)} coins procesados")
            
        except Exception as e:
            logging.error(f"❌ Error en escaneo masivo: {str(e)}")

    async def start(self):
        """Iniciar monitor de pre-graduación"""
        self.running = True
        
        # Iniciar WebSocket de Helius
        await self.helius_ws.start()
        
        # Escaneo inicial masivo
        asyncio.create_task(self.bulk_scan_recent_tokens())
        
        # Iniciar escáner de tokens existentes
        asyncio.create_task(self._continuous_scan())
        
        # Iniciar limpieza periódica
        asyncio.create_task(self._periodic_cleanup())
        
        logging.info("✅ Monitor PRE-GRADUACIÓN iniciado")

    async def _continuous_scan(self):
        """Escaneo continuo para pre-graduación"""
        while self.running:
            try:
                await self.scan_existing_tokens()
            except Exception as e:
                logging.error(f"❌ Error en escaneo continuo: {str(e)}")
            await asyncio.sleep(60)  # Escanear cada minuto

    async def _periodic_cleanup(self):
        """Limpieza periódica de la base de datos"""
        while self.running:
            try:
                await self.db.cleanup_old_tokens()
                # Escaneo masivo cada 30 minutos
                await self.bulk_scan_recent_tokens()
            except Exception as e:
                logging.error(f"❌ Error en limpieza periódica: {str(e)}")
            await asyncio.sleep(1800)  # 30 minutos

    async def stop(self):
        """Detener monitor"""
        self.running = False
        
        # Detener WebSocket
        await self.helius_ws.stop()
        
        # Detener todas las tareas de monitoreo
        for mint in list(self.monitoring_tasks.keys()):
            await self.stop_token_monitoring(mint)
        
        logging.info("⛔ Monitor PRE-GRADUACIÓN detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - Definitivo")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = f"""
🎯 <b>BOT DEFINITIVO PUMP.FUN - PRE-GRADUACIÓN</b> 🚀

<b>Tecnología Integrada:</b>
• 🌐 Helius WebSocket (Detección instantánea)
• 🔥 API Directa Pump.fun (Datos reales)
• 🗄️ PostgreSQL (Seguimiento avanzado)
• 🔄 Escaneo Masivo (Backup)

<b>Objetivo Exclusivo:</b>
• Alertar en <b>${UMBRAL_MCAP:,.0f}</b> MC
• Antes de graduación (<b>${GRADUATION_MC_TARGET:,.0f}</b>)

<b>Comandos:</b>
/iniciar - Activar monitor completo
/detener - Pausar monitor
/status - Estado y estadísticas
/alertas - Historial de alertas
/silent on|off - Modo silencioso

<code>Sistema definitivo para pre-graduación Pump.fun</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.start()

        await update.message.reply_text(
            "✅ <b>MONITOR DEFINITIVO ACTIVADO</b>\n\n"
            "🎯 Objetivo: Tokens entre $" + f"{UMBRAL_MCAP:,.0f}" + " y $" + f"{GRADUATION_MC_TARGET:,.0f}\n"
            "🌐 Helius WebSocket → DETECTANDO\n"
            "🔥 API Pump.fun → MONITOREANDO\n"
            "🔄 Escaneo Masivo → ACTIVO\n\n"
            "<i>Sistema completo en funcionamiento...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitor definitivo iniciado por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.stop()

        await update.message.reply_text(
            "⛔ <b>MONITOR DEFINITIVO DETENIDO</b>\n\n"
            "Todas las funciones han sido pausadas.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    
    stats = pump_monitor.stats if pump_monitor else {}
    tokens_count = len(pump_monitor.monitoring_tasks) if pump_monitor else 0

    status_text = (
        "📊 <b>ESTADO DEFINITIVO</b>\n\n"
        f"🎯 Monitor Pump.fun: {pump_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n"
        f"📈 Tokens Monitoreando: <b>{tokens_count}</b>\n\n"
        
        "<b>📈 Estadísticas:</b>\n"
        f"• Tokens Detectados: {stats.get('tokens_detected', 0)}\n"
        f"• Alertas Pre-Graduación: {stats.get('pre_graduation_alerts', 0)}\n"
        f"• Escaneos Realizados: {stats.get('scan_round', 0)}\n\n"
        
        "<b>🎯 Configuración:</b>\n"
        f"• Alerta MC: <b>${UMBRAL_MCAP:,.0f}</b>\n"
        f"• Graduación MC: <b>${GRADUATION_MC_TARGET:,.0f}</b>\n"
        f"• Fuente: <code>API Directa Pump.fun</code>\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    args = context.args
    if not args or args[0] not in ['on', 'off']:
        await update.message.reply_text("Uso: /silent on|off")
        return

    if notifier:
        notifier.silent_mode = (args[0] == 'on')
        status = "🔇 ACTIVADO" if notifier.silent_mode else "🔔 DESACTIVADO"
        await update.message.reply_text(f"Modo silencioso: {status}")

async def alertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    rows = await db.get_recent_alerts(limit=8)
    if not rows:
        await update.message.reply_text("No hay alertas recientes.")
        return

    lines = ["<b>🎯 Alertas Pre-Graduación Recientes:</b>\n"]
    for r in rows:
        sym = r.get('symbol', 'UNKNOWN')
        mc = r.get('market_cap', 0)
        progress = r.get('progress_percent', 0)
        alert_type = "🎯 PRE-GRAD" if r.get('alert_type') == "pre_graduation" else "🆕 NUEVO"
        
        lines.append(f"• <b>{sym}</b> {alert_type}")
        lines.append(f"  MC: ${float(mc):,.0f} | Progreso: {float(progress):.1f}%")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
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
    stats = pump_monitor.stats if pump_monitor else {}
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitor": {
            "running": pump_monitor.running if pump_monitor else False,
            "tokens_monitoring": len(pump_monitor.monitoring_tasks) if pump_monitor else 0,
            "helius_connected": pump_monitor.helius_ws.websocket is not None if pump_monitor else False,
            "stats": stats
        },
        "focus": "pre_graduation_only",
        "api_source": "pump.fun_direct"
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Pump.fun Bot - Definitivo",
        "status": "operational",
        "version": "definitivo",
        "focus": "pre_graduation_detection"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "HELIUS_WSS_URL": HELIUS_WSS_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables: {', '.join(missing)}")

    await db.connect()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("alertas", alertas_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)

    logging.info("✅ Bot DEFINITIVO Pump.fun inicializado")

@app.on_event("startup")
async def startup_event():
    try:
        await initialize_app()
        await telegram_app.initialize()
        
        if os.getenv("RAILWAY_STATIC_URL"):
            webhook_url = f"{os.getenv('RAILWAY_STATIC_URL')}/webhook"
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            await telegram_app.start()
            logging.info("✅ Bot iniciado con polling")
            
        logging.info("🚀 Bot DEFINITIVO listo - Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot definitivo...")
    
    if pump_monitor:
        await pump_monitor.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot definitivo apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
