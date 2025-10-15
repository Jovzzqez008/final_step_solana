#!/usr/bin/env python3
"""
🤖 BOT PUMP.FUN - ALERTAS PROFESIONALES
Arquitectura completa con QuickNode/Helius RPC + PostgreSQL + Parámetros dinámicos
"""

import os
import json
import asyncio
import logging
import signal
import sys
import aiohttp
import asyncpg
import websockets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from decimal import Decimal

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext

# ===========================
# CONFIGURACIÓN Y CONSTANTES
# ===========================

class Config:
    """Cargador de configuración desde config.json y variables de entorno"""
    
    def __init__(self):
        self.config_path = os.path.join(os.path.dirname(__file__), 'config.json')
        self.load_config()
        
    def load_config(self):
        """Carga configuración desde archivo y variables de entorno"""
        # Cargar config.json si existe
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                self.data = json.load(f)
        else:
            self.data = {}
            
        # Telegram
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
        
        # Database
        self.DATABASE_URL = os.getenv('DATABASE_URL', '')
        
        # RPC Endpoints
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', '')
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', '')
        
        # Pump.fun
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', 
                                      self.data.get('websocket', {}).get('uri', 
                                      'wss://pumpportal.fun/api/data'))
        
        # Parámetros del bot (env vars tienen prioridad sobre config.json)
        self.ALERT_RULES = self.data.get('alert_rules', [
            {
                "name": "moon_shot",
                "alert_percent": 500,
                "time_window_min": 8,
                "description": "500% en 8 minutos"
            }
        ])
        
        monitoring_config = self.data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', 
                                                  monitoring_config.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', 
                                                    monitoring_config.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', 
                                                     monitoring_config.get('price_poll_interval_sec', 5)))
        
        # Modo
        self.MODE = os.getenv('MODE', 'PROD')
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
        self.ENABLE_DB = os.getenv('ENABLE_DB', 'true').lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'

# ===========================
# MODELOS DE DATOS
# ===========================

@dataclass
class TokenData:
    """Estructura para almacenar datos del token"""
    mint: str
    symbol: str
    name: str
    initial_price: float
    initial_market_cap: float
    max_price: float
    start_time: datetime
    status: str = "monitoring"
    metadata: Dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

@dataclass
class AlertData:
    """Estructura para datos de alerta"""
    token_address: str
    rule_name: str
    gain_percent: float
    time_elapsed_min: float
    price_at_alert: float
    market_cap_at_alert: float
    extra_data: Dict = None
    
    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

# ===========================
# CLIENTE RPC (QUICKNODE/HELIUS)
# ===========================

class RPCClient:
    """Cliente para conexión RPC con QuickNode/Helius"""
    
    def __init__(self, config: Config):
        self.config = config
        self.current_provider = 'quicknode'
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            
    def get_rpc_url(self):
        """Obtiene la URL RPC activa"""
        if self.current_provider == 'quicknode' and self.config.QUICKNODE_RPC_URL:
            return self.config.QUICKNODE_RPC_URL
        elif self.current_provider == 'helius' and self.config.HELIUS_RPC_URL:
            return self.config.HELIUS_RPC_URL
        else:
            # Fallback a RPC público (no recomendado para producción)
            return "https://api.mainnet-beta.solana.com"
    
    async def make_rpc_call(self, method: str, params: list) -> Optional[Dict]:
        """Realiza llamada RPC con fallback automático entre proveedores"""
        url = self.get_rpc_url()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        
        try:
            async with self.session.post(url, json=payload, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('result')
                else:
                    raise Exception(f"RPC error: {response.status}")
        except Exception as e:
            logging.warning(f"RPC call failed with {self.current_provider}: {e}")
            # Switch provider
            if self.current_provider == 'quicknode':
                self.current_provider = 'helius'
            else:
                self.current_provider = 'quicknode'
                
            if self.get_rpc_url() != url:  # Si hay un proveedor alternativo
                logging.info(f"Switching to {self.current_provider} RPC")
                return await self.make_rpc_call(method, params)
            else:
                raise e
    
    async def get_token_price(self, mint: str) -> Optional[Dict[str, float]]:
        """
        Obtiene precio y market cap del token usando DexScreener API
        como fallback rápido y confiable
        """
        try:
            # Primero intentamos con DexScreener (más rápido para precio)
            async with self.session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}", 
                timeout=5
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        pair = data['pairs'][0]
                        price = float(pair.get('priceUsd', 0))
                        market_cap = float(pair.get('marketCap', 0))
                        volume = float(pair.get('volume', {}).get('h24', 0))
                        
                        return {
                            'price': price,
                            'market_cap': market_cap,
                            'volume_24h': volume,
                            'source': 'dexscreener'
                        }
        except Exception as e:
            logging.debug(f"DexScreener price fetch failed for {mint}: {e}")
            
        # Fallback: intentar con RPC para datos on-chain
        try:
            # Aquí podrías implementar lógica para obtener precio desde bonding curve
            # usando getAccountInfo del programa de Pump.fun
            pass
        except Exception as e:
            logging.debug(f"RPC price fetch failed for {mint}: {e}")
            
        return None

# ===========================
# BASE DE DATOS POSTGRESQL
# ===========================

class Database:
    """Manejador de base de datos PostgreSQL"""
    
    def __init__(self, config: Config):
        self.config = config
        self.pool = None
        
    async def connect(self):
        """Conecta a la base de datos y crea tablas si no existen"""
        if not self.config.ENABLE_DB or not self.config.DATABASE_URL:
            logging.info("Database disabled or DATABASE_URL not set")
            return
            
        try:
            self.pool = await asyncpg.create_pool(
                self.config.DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            
            # Crear tablas si no existen
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_monitoring (
                        token_address TEXT PRIMARY KEY,
                        symbol TEXT,
                        name TEXT,
                        initial_price DECIMAL(30, 15),
                        initial_market_cap DECIMAL(30, 2),
                        max_price DECIMAL(30, 15),
                        start_time TIMESTAMPTZ DEFAULT NOW(),
                        last_checked TIMESTAMPTZ,
                        status TEXT,
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
                
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_alerts (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        alert_rule_name TEXT,
                        gain_percent DECIMAL(10, 2),
                        time_elapsed_min DECIMAL(10, 2),
                        price_at_alert DECIMAL(30, 15),
                        market_cap_at_alert DECIMAL(30, 2),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        extra_data JSONB
                    )
                ''')
                
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_price_history (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        price DECIMAL(30, 15),
                        market_cap DECIMAL(30, 2),
                        volume_24h DECIMAL(30, 2)
                    )
                ''')
                
                # Crear índices
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_token_monitoring_status 
                    ON token_monitoring(status)
                ''')
                
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_token_alerts_created_at 
                    ON token_alerts(created_at)
                ''')
                
            logging.info("✅ Database connected and schema verified")
            
        except Exception as e:
            logging.error(f"❌ Database connection failed: {e}")
            self.pool = None
    
    async def disconnect(self):
        """Cierra la conexión a la base de datos"""
        if self.pool:
            await self.pool.close()
            logging.info("Database connection closed")
    
    async def add_monitored_token(self, token: TokenData):
        """Añade token a la tabla de monitoreo"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring 
                    (token_address, symbol, name, initial_price, initial_market_cap, 
                     max_price, start_time, status, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (token_address) 
                    DO UPDATE SET 
                        last_checked = NOW(),
                        status = EXCLUDED.status,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price)
                ''', token.mint, token.symbol, token.name, 
                   Decimal(str(token.initial_price)), 
                   Decimal(str(token.initial_market_cap)),
                   Decimal(str(token.max_price)), 
                   token.start_time, token.status,
                   json.dumps(token.metadata))
        except Exception as e:
            logging.error(f"Error adding monitored token: {e}")
    
    async def record_alert(self, alert: AlertData):
        """Registra una alerta en la base de datos"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts 
                    (token_address, alert_rule_name, gain_percent, time_elapsed_min,
                     price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''', alert.token_address, alert.rule_name,
                   Decimal(str(alert.gain_percent)),
                   Decimal(str(alert.time_elapsed_min)),
                   Decimal(str(alert.price_at_alert)),
                   Decimal(str(alert.market_cap_at_alert)),
                   json.dumps(alert.extra_data))
        except Exception as e:
            logging.error(f"Error recording alert: {e}")
    
    async def record_price_history(self, token_address: str, price: float, 
                                 market_cap: float, volume: float):
        """Registra histórico de precios"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_price_history 
                    (token_address, price, market_cap, volume_24h)
                    VALUES ($1, $2, $3, $4)
                ''', token_address, Decimal(str(price)), 
                   Decimal(str(market_cap)), Decimal(str(volume)))
        except Exception as e:
            logging.debug(f"Error recording price history: {e}")

# ===========================
# MOTOR DE ALERTAS
# ===========================

class AlertEngine:
    """Motor para evaluar reglas de alerta"""
    
    def __init__(self, config: Config):
        self.config = config
        self.alert_rules = config.ALERT_RULES
        
    async def evaluate_token(self, token: TokenData, current_price: float, 
                           current_market_cap: float) -> List[AlertData]:
        """Evalúa un token contra todas las reglas de alerta"""
        if current_price <= 0:
            return []
            
        # Calcular métricas
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100
        elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60
        
        triggered_alerts = []
        
        for rule in self.alert_rules:
            if self._check_rule(rule, gain_percent, elapsed_min):
                alert = AlertData(
                    token_address=token.mint,
                    rule_name=rule['name'],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=current_market_cap,
                    extra_data={
                        'initial_price': token.initial_price,
                        'max_price': token.max_price,
                        'symbol': token.symbol,
                        'name': token.name
                    }
                )
                triggered_alerts.append(alert)
                
        return triggered_alerts
    
    def _check_rule(self, rule: Dict, gain_percent: float, elapsed_min: float) -> bool:
        """Verifica si se cumple una regla específica"""
        required_gain = rule['alert_percent']
        time_window = rule['time_window_min']
        
        return gain_percent >= required_gain and elapsed_min <= time_window

# ===========================
# GESTOR DE TOKENS
# ===========================

class TokenManager:
    """Gestiona tokens en monitoreo y sus tareas"""
    
    def __init__(self, config: Config, database: Database, alert_engine: AlertEngine):
        self.config = config
        self.db = database
        self.alert_engine = alert_engine
        self.monitored_tokens: Dict[str, TokenData] = {}
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        self.rpc_client = None
        
    async def add_token(self, mint: str, symbol: str, name: str, 
                       initial_price: float = 0, initial_market_cap: float = 0):
        """Añade un nuevo token para monitoreo"""
        if mint in self.monitored_tokens:
            logging.debug(f"Token {mint} already being monitored")
            return
            
        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name,
            initial_price=initial_price,
            initial_market_cap=initial_market_cap,
            max_price=initial_price,
            start_time=datetime.now(timezone.utc)
        )
        
        self.monitored_tokens[mint] = token
        await self.db.add_monitored_token(token)
        
        # Iniciar tarea de monitoreo
        task = asyncio.create_task(self._monitor_token(mint))
        self.monitor_tasks[mint] = task
        
        logging.info(f"🔍 Started monitoring {symbol} ({mint[:8]}...)")
    
    async def _monitor_token(self, mint: str):
        """Tarea asincrónica para monitorear un token específico"""
        token = self.monitored_tokens.get(mint)
        if not token:
            return
            
        try:
            async with RPCClient(self.config) as rpc_client:
                self.rpc_client = rpc_client
                
                while mint in self.monitored_tokens:
                    # Verificar timeout
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"⏰ Token {token.symbol} timeout after {elapsed_min:.1f}min")
                        await self._remove_token(mint, "timeout")
                        break
                    
                    # Obtener precio actual
                    price_data = await self.rpc_client.get_token_price(mint)
                    if not price_data:
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        continue
                    
                    current_price = price_data['price']
                    current_market_cap = price_data['market_cap']
                    
                    # Actualizar precio máximo
                    if current_price > token.max_price:
                        token.max_price = current_price
                        await self.db.add_monitored_token(token)
                    
                    # Registrar histórico
                    await self.db.record_price_history(
                        mint, current_price, current_market_cap, 
                        price_data.get('volume_24h', 0)
                    )
                    
                    # Verificar dump
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"📉 Token {token.symbol} dumped {loss_from_max:.1f}%")
                            await self._remove_token(mint, "dumped")
                            break
                    
                    # Evaluar alertas
                    alerts = await self.alert_engine.evaluate_token(
                        token, current_price, current_market_cap
                    )
                    
                    for alert in alerts:
                        logging.info(f"🚨 ALERT: {token.symbol} +{alert.gain_percent:.1f}% in {alert.time_elapsed_min:.1f}min")
                        await self.db.record_alert(alert)
                        await self._send_telegram_alert(token, alert)
                        await self._remove_token(mint, "alert_sent")
                        break  # Solo una alerta por token
                    
                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                    
        except asyncio.CancelledError:
            logging.info(f"Monitoring task for {mint} cancelled")
        except Exception as e:
            logging.error(f"Error monitoring {mint}: {e}")
            await self._remove_token(mint, "error")
    
    async def _remove_token(self, mint: str, reason: str):
        """Remueve token del monitoreo"""
        if mint in self.monitored_tokens:
            token = self.monitored_tokens[mint]
            token.status = reason
            await self.db.add_monitored_token(token)
            del self.monitored_tokens[mint]
            
        if mint in self.monitor_tasks:
            task = self.monitor_tasks[mint]
            task.cancel()
            del self.monitor_tasks[mint]
            
        logging.info(f"Removed token {mint}: {reason}")
    
    async def _send_telegram_alert(self, token: TokenData, alert: AlertData):
        """Envía alerta por Telegram"""
        if not self.config.ENABLE_TELEGRAM or not self.config.TELEGRAM_BOT_TOKEN:
            return
            
        try:
            message = self._format_telegram_message(token, alert)
            keyboard = self._format_telegram_keyboard(token.mint)
            
            from telegram import Bot
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            
            await bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode='Markdown',
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            
            logging.info(f"📨 Telegram alert sent for {token.symbol}")
            
        except Exception as e:
            logging.error(f"Error sending Telegram alert: {e}")
    
    def _format_telegram_message(self, token: TokenData, alert: AlertData) -> str:
        """Formatea mensaje de Telegram"""
        return f"""
🚀 **ALERTA DE PUMP DETECTADA** 🚀

**Token:** {token.name} ({token.symbol})
**Ganancia:** +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f}min
**Regla Activada:** {alert.rule_name}

📊 **Métricas:**
• Precio Actual: {alert.price_at_alert:.8f} SOL
• Market Cap: ${alert.market_cap_at_alert:,.0f}
• Precio Inicial: {token.initial_price:.8f} SOL
• Máximo Alcanzado: {token.max_price:.8f} SOL

🔗 **Enlaces Rápidos:**
• [Pump.fun](https://pump.fun/{token.mint})
• [DexScreener](https://dexscreener.com/solana/{token.mint}) 
• [RugCheck](https://rugcheck.xyz/tokens/{token.mint})
• [Birdeye](https://birdeye.so/token/{token.mint}?chain=solana)

📈 **ROI Potencial:** {alert.gain_percent:.1f}%

🕒 **Tiempo desde creación:** {alert.time_elapsed_min:.1f} minutos
"""
    
    def _format_telegram_keyboard(self, mint: str) -> InlineKeyboardMarkup:
        """Crea teclado inline para Telegram"""
        keyboard = [
            [
                InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}"),
                InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")
            ],
            [
                InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                InlineKeyboardButton("👁️ Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    async def stop_all_monitoring(self):
        """Detiene todo el monitoreo"""
        for mint in list(self.monitored_tokens.keys()):
            await self._remove_token(mint, "shutdown")
    
    def get_status(self) -> Dict:
        """Obtiene estado actual del gestor"""
        return {
            'monitored_tokens': len(self.monitored_tokens),
            'active_tasks': len(self.monitor_tasks),
            'tokens': list(self.monitored_tokens.keys())
        }

# ===========================
# CLIENTE WEBSOCKET
# ===========================

class WebSocketClient:
    """Cliente WebSocket para PumpPortal"""
    
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.websocket = None
        self.is_connected = False
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        
    async def connect(self):
        """Conecta al WebSocket de PumpPortal"""
        uri = self.config.PUMPPORTAL_WSS
            
        while True:
            try:
                logging.info(f"Connecting to WebSocket: {uri}")
                self.websocket = await websockets.connect(uri, ping_interval=20, ping_timeout=10)
                self.is_connected = True
                self.reconnect_delay = 5
                
                # Suscribirse a nuevos tokens
                subscribe_msg = {"method": "subscribeNewToken"}
                await self.websocket.send(json.dumps(subscribe_msg))
                logging.info("Subscribed to new tokens")
                
                # Escuchar mensajes
                await self._listen()
                
            except Exception as e:
                logging.error(f"WebSocket connection failed: {e}")
                self.is_connected = False
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
    
    async def _listen(self):
        """Escucha mensajes del WebSocket"""
        async for message in self.websocket:
            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON message: {message}")
            except Exception as e:
                logging.error(f"Error handling message: {e}")
    
    async def _handle_message(self, data: Dict):
        """Maneja mensajes recibidos del WebSocket"""
        try:
            # Extraer datos del token
            mint = data.get('mint') or (data.get('data') or {}).get('mint')
            symbol = data.get('symbol') or (data.get('data') or {}).get('symbol', 'UNKNOWN')
            name = data.get('name') or (data.get('data') or {}).get('name', symbol)
            
            if not mint:
                logging.debug("Message without mint, skipping")
                return
                
            # Intentar obtener precio inicial si está disponible
            initial_price = 0
            initial_market_cap = 0
            
            if isinstance(data.get('pairs'), list) and len(data['pairs']) > 0:
                pair = data['pairs'][0]
                initial_price = float(pair.get('priceUsd', pair.get('price', 0)))
                initial_market_cap = float(pair.get('marketCap', 0))
            
            # Añadir token para monitoreo
            await self.token_manager.add_token(
                mint=mint,
                symbol=symbol,
                name=name,
                initial_price=initial_price,
                initial_market_cap=initial_market_cap
            )
            
        except Exception as e:
            logging.error(f"Error processing token data: {e}")
    
    async def disconnect(self):
        """Desconecta del WebSocket"""
        self.is_connected = False
        if self.websocket:
            await self.websocket.close()

# ===========================
# BOT PRINCIPAL
# ===========================

class PumpFunBot:
    """Bot principal de Pump.fun"""
    
    def __init__(self):
        self.config = Config()
        self.setup_logging()
        
        self.db = Database(self.config)
        self.alert_engine = AlertEngine(self.config)
        self.token_manager = TokenManager(self.config, self.db, self.alert_engine)
        self.ws_client = WebSocketClient(self.config, self.token_manager)
        
        self.telegram_app = None
        self.is_running = False
        self.ws_task = None

    def setup_logging(self):
        """Configura el sistema de logging (solo consola, compatible con Railway)"""
        logging.basicConfig(
            level=getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        
    async def start(self):
        """Inicia el bot"""
        self.is_running = True
        
        # Configurar manejo de señales
        self.setup_signal_handlers()
        
        # Conectar a base de datos
        await self.db.connect()
        
        # Iniciar bot de Telegram si está habilitado
        if self.config.ENABLE_TELEGRAM and self.config.TELEGRAM_BOT_TOKEN:
            await self.start_telegram_bot()
        
        # Iniciar WebSocket en una tarea separada
        self.ws_task = asyncio.create_task(self.ws_client.connect())
        
        logging.info("🤖 Pump.fun Bot started successfully")
        logging.info(f"🔧 Mode: {self.config.MODE}")
        logging.info(f"📊 Alert rules: {len(self.config.ALERT_RULES)}")
        logging.info(f"💾 Database: {'Enabled' if self.config.ENABLE_DB else 'Disabled'}")
        logging.info(f"📱 Telegram: {'Enabled' if self.config.ENABLE_TELEGRAM else 'Disabled'}")
        
        # Mantener el bot corriendo
        try:
            while self.is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await self.stop()
    
    async def stop(self):
        """Detiene el bot gracefulmente"""
        self.is_running = False
        logging.info("🛑 Stopping Pump.fun Bot...")
        
        if self.ws_client:
            await self.ws_client.disconnect()
            
        if self.token_manager:
            await self.token_manager.stop_all_monitoring()
            
        if self.telegram_app:
            await self.telegram_app.stop()
            
        await self.db.disconnect()
        
        if self.ws_task:
            self.ws_task.cancel()
            
        logging.info("✅ Pump.fun Bot stopped successfully")
    
    def setup_signal_handlers(self):
        """Configura manejo de señales para shutdown graceful"""
        def signal_handler(signum, frame):
            logging.info(f"Received signal {signum}, shutting down...")
            asyncio.create_task(self.stop())
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def start_telegram_bot(self):
        """Inicia el bot de Telegram"""
        try:
            self.telegram_app = Application.builder()\
                .token(self.config.TELEGRAM_BOT_TOKEN)\
                .build()
            
            # Añadir comandos
            self.telegram_app.add_handler(CommandHandler("start", self.telegram_start))
            self.telegram_app.add_handler(CommandHandler("status", self.telegram_status))
            self.telegram_app.add_handler(CommandHandler("stats", self.telegram_stats))
            self.telegram_app.add_handler(CommandHandler("rules", self.telegram_rules))
            self.telegram_app.add_handler(CommandHandler("tokens", self.telegram_tokens))
            self.telegram_app.add_handler(CommandHandler("pause", self.telegram_pause))
            self.telegram_app.add_handler(CommandHandler("resume", self.telegram_resume))
            
            # Iniciar polling en segundo plano
            asyncio.create_task(self.telegram_app.run_polling())
            
            logging.info("✅ Telegram bot started")
            
        except Exception as e:
            logging.error(f"❌ Failed to start Telegram bot: {e}")
    
    # ===========================
    # COMANDOS TELEGRAM
    # ===========================
    
    async def telegram_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        welcome_text = """
🤖 *BOT PUMP.FUN - ALERTAS PROFESIONALES*

*Comandos disponibles:*
/start - Muestra este mensaje
/status - Estado del sistema
/stats - Estadísticas de performance
/rules - Reglas de alerta activas
/tokens - Tokens en monitoreo
/pause - Pausar monitoreo
/resume - Reanudar monitoreo

*Configuración:*
• RPC: QuickNode/Helius
• Database: PostgreSQL
• Alertas: Parámetros dinámicos
"""
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
    
    async def telegram_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /status"""
        status = self.token_manager.get_status()
        
        status_text = f"""
📊 *ESTADO DEL SISTEMA*

*Monitoreo:*
• Tokens activos: {status['monitored_tokens']}
• Tareas: {status['active_tasks']}
• WebSocket: {'✅ Conectado' if self.ws_client.is_connected else '❌ Desconectado'}

*Configuración:*
• Reglas: {len(self.config.ALERT_RULES)}
• Poll interval: {self.config.PRICE_POLL_INTERVAL_SEC}s
• Max monitor: {self.config.MAX_MONITOR_TIME_MIN}min
• Dump threshold: {self.config.DUMP_THRESHOLD_PERCENT}%
"""
        await update.message.reply_text(status_text, parse_mode='Markdown')
    
    async def telegram_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /stats - Estadísticas de performance"""
        if not self.db.pool:
            await update.message.reply_text("❌ Database no disponible")
            return
            
        try:
            async with self.db.pool.acquire() as conn:
                # Estadísticas básicas
                result = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as total_alerts,
                        AVG(gain_percent) as avg_gain,
                        AVG(time_elapsed_min) as avg_time
                    FROM token_alerts 
                    WHERE created_at >= NOW() - INTERVAL '24 hours'
                ''')
                
                stats_text = f"""
📈 *ESTADÍSTICAS (24h)*

• Alertas totales: {result['total_alerts']}
• Ganancia promedio: {float(result['avg_gain'] or 0):.1f}%
• Tiempo promedio: {float(result['avg_time'] or 0):.1f}min

*Reglas más efectivas:*
"""
                
                # Estadísticas por regla
                rules_result = await conn.fetch('''
                    SELECT 
                        alert_rule_name,
                        COUNT(*) as count,
                        AVG(gain_percent) as avg_gain
                    FROM token_alerts 
                    WHERE created_at >= NOW() - INTERVAL '24 hours'
                    GROUP BY alert_rule_name
                    ORDER BY count DESC
                ''')
                
                for rule in rules_result:
                    stats_text += f"• {rule['alert_rule_name']}: {rule['count']} alerts, avg {float(rule['avg_gain'] or 0):.1f}%\n"
                
                await update.message.reply_text(stats_text, parse_mode='Markdown')
                
        except Exception as e:
            logging.error(f"Error getting stats: {e}")
            await update.message.reply_text("❌ Error obteniendo estadísticas")
    
    async def telegram_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /rules - Muestra reglas activas"""
        rules_text = "🎯 *REGLAS DE ALERTA ACTIVAS*\n\n"
        
        for rule in self.config.ALERT_RULES:
            rules_text += f"• *{rule['name']}*: +{rule['alert_percent']}% en {rule['time_window_min']}min\n"
            rules_text += f"  _{rule['description']}_\n\n"
            
        await update.message.reply_text(rules_text, parse_mode='Markdown')
    
    async def telegram_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /tokens - Tokens en monitoreo"""
        status = self.token_manager.get_status()
        
        if not status['monitored_tokens']:
            await update.message.reply_text("🔍 No hay tokens en monitoreo")
            return
            
        tokens_text = f"🔍 *TOKENS EN MONITOREO ({status['monitored_tokens']})*\n\n"
        
        for i, mint in enumerate(status['tokens'][:10], 1):  # Máximo 10 tokens
            token = self.token_manager.monitored_tokens[mint]
            elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60
            tokens_text += f"{i}. *{token.symbol}* - {elapsed:.1f}min\n"
            tokens_text += f"   `{mint[:16]}...`\n"
            
        if status['monitored_tokens'] > 10:
            tokens_text += f"\n... y {status['monitored_tokens'] - 10} más"
            
        await update.message.reply_text(tokens_text, parse_mode='Markdown')
    
    async def telegram_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /pause - Pausar monitoreo"""
        await self.ws_client.disconnect()
        await self.token_manager.stop_all_monitoring()
        await update.message.reply_text("⏸️ Monitoreo pausado")
    
    async def telegram_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /resume - Reanudar monitoreo"""
        asyncio.create_task(self.ws_client.connect())
        await update.message.reply_text("▶️ Monitoreo reanudado")

# ===========================
# EJECUCIÓN PRINCIPAL
# ===========================

async def main():
    """Función principal"""
    bot = PumpFunBot()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt")
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
    finally:
        await bot.stop()

if __name__ == "__main__":
    # Verificar variables de entorno críticas
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print("❌ TELEGRAM_BOT_TOKEN no configurado")
        sys.exit(1)
        
    if not os.getenv('DATABASE_URL'):
        print("❌ DATABASE_URL no configurado")
        sys.exit(1)
    
    # Ejecutar bot
    asyncio.run(main())
