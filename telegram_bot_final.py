#!/usr/bin/env python3
"""
Pump.fun Alert Bot v4.0 - Railway + QuickNode Optimized
Con detección en tiempo real + Bonding Curve decoding + Alertas Telegram
"""

import os
import json
import asyncio
import logging
import signal
import sys
import base64
import time
import aiohttp
import asyncpg
import websockets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from decimal import Decimal
from aiohttp import web

# Telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------
# CONFIGURACIÓN
# ---------------------------

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

class Config:
    def __init__(self):
        # QuickNode WebSocket (CRÍTICO)
        self.QUICKNODE_WSS = os.getenv("QUICKNODE_WSS", "")
        
        # Telegram
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
        
        # Database
        self.DATABASE_URL = os.getenv("DATABASE_URL", "")
        
        # Alert Rules
        self.ALERT_RULES = [
            {"name": "momentum_300_15", "alert_percent": 300.0, "time_window_min": 15, "description": "300% en 15 minutos"}
        ]
        
        # Monitoring
        self.MAX_MONITOR_TIME_MIN = 30.0
        self.DUMP_THRESHOLD_PERCENT = -50.0
        self.PRICE_POLL_INTERVAL_SEC = 3.0
        self.MAX_CONCURRENT_MONITORS = 50
        
        # System
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
        self.ENABLE_DB = os.getenv("ENABLE_DB", "true").lower() == "true"
        self.ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"

# ---------------------------
# DECODIFICADOR BONDING CURVE (Basado en IDL Oficial)
# ---------------------------

class BondingCurveDecoder:
    """
    Decodificador exacto del bonding curve account de Pump.fun
    Layout según IDL oficial:
    - virtualTokenReserves: u64 (offset 0)
    - virtualSolReserves: u64 (offset 8) 
    - realTokenReserves: u64 (offset 16)
    - realSolReserves: u64 (offset 24)
    - tokenTotalSupply: u64 (offset 32)
    - complete: bool (offset 40)
    """
    
    @staticmethod
    def decode_bonding_curve(data_b64: str) -> Optional[Dict[str, Any]]:
        try:
            raw_data = base64.b64decode(data_b64)
            
            if len(raw_data) < 41:
                return None
            
            # Decodificar campos según layout del IDL
            result = {
                'virtual_token_reserves': int.from_bytes(raw_data[0:8], 'little'),
                'virtual_sol_reserves': int.from_bytes(raw_data[8:16], 'little'),
                'real_token_reserves': int.from_bytes(raw_data[16:24], 'little'),
                'real_sol_reserves': int.from_bytes(raw_data[24:32], 'little'),
                'token_total_supply': int.from_bytes(raw_data[32:40], 'little'),
                'complete': bool(raw_data[40])
            }
            
            # Calcular métricas de precio
            if result['virtual_token_reserves'] > 0:
                # Precio en SOL (lamports por token)
                price_lamports = result['virtual_sol_reserves'] / result['virtual_token_reserves']
                result['price_sol'] = price_lamports / 1_000_000_000  # Convertir a SOL
                
                # Precio en USD (usar API real en producción)
                sol_price_usd = 150.0  # Placeholder - deberías usar CoinGecko API
                result['price_usd'] = result['price_sol'] * sol_price_usd
                
                # Market cap (tokens tienen 6 decimales)
                result['market_cap_usd'] = (result['token_total_supply'] / 1_000_000) * result['price_usd']
                
                # Progreso hacia $69K
                result['progress_to_69k'] = min(100.0, (result['market_cap_usd'] / 69000) * 100)
            
            return result
            
        except Exception as e:
            logging.error(f"Error decoding bonding curve: {e}")
            return None

# ---------------------------
# CLIENTE RPC
# ---------------------------

class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = aiohttp.ClientSession()
        self.metrics = {
            "rpc_calls": 0,
            "rpc_errors": 0,
            "bonding_curve_decodes": 0
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()

    async def get_account_info(self, account: str) -> Optional[Dict]:
        """Obtiene información de cuenta desde Solana RPC"""
        url = "https://api.mainnet-beta.solana.com"  # Puedes usar QuickNode RPC aquí
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [account, {"encoding": "base64"}]
        }
        
        try:
            async with self.session.post(url, json=payload, timeout=10) as response:
                self.metrics["rpc_calls"] += 1
                if response.status == 200:
                    result = await response.json()
                    return result.get("result")
                else:
                    self.metrics["rpc_errors"] += 1
                    return None
        except Exception as e:
            self.metrics["rpc_errors"] += 1
            logging.debug(f"RPC error for {account}: {e}")
            return None

    async def get_bonding_curve_data(self, bonding_curve_address: str) -> Optional[Dict[str, Any]]:
        """Obtiene y decodifica datos del bonding curve"""
        account_info = await self.get_account_info(bonding_curve_address)
        if not account_info or not account_info.get("value"):
            return None
            
        data = account_info["value"]["data"]
        if isinstance(data, list) and len(data) > 0:
            bonding_data = BondingCurveDecoder.decode_bonding_curve(data[0])
            if bonding_data:
                self.metrics["bonding_curve_decodes"] += 1
                return bonding_data
                
        return None

# ---------------------------
# MODELOS DE DATOS
# ---------------------------

@dataclass
class TokenData:
    mint: str
    bonding_curve: Optional[str] = None
    symbol: str = "UNKNOWN"
    name: str = "UNKNOWN"
    initial_price: float = 0.0
    max_price: float = 0.0
    start_time: datetime = None
    status: str = "monitoring"

    def __post_init__(self):
        if self.start_time is None:
            self.start_time = datetime.now(timezone.utc)

# ---------------------------
# LISTENER WEB SOCKET (QUICKNODE)
# ---------------------------

class QuickNodeListener:
    def __init__(self, config: Config, token_manager):
        self.config = config
        self.token_manager = token_manager
        self.running = False
        self.reconnect_delay = 3

    async def start(self):
        """Inicia el listener principal de QuickNode WebSocket"""
        self.running = True
        logging.info("🚀 Iniciando QuickNode WebSocket Listener...")
        
        if not self.config.QUICKNODE_WSS:
            logging.error("❌ QUICKNODE_WSS no configurado")
            return

        while self.running:
            try:
                logging.info(f"🔌 Conectando a QuickNode: {self.config.QUICKNODE_WSS[:50]}...")
                
                async with websockets.connect(
                    self.config.QUICKNODE_WSS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10
                ) as ws:
                    
                    logging.info("✅ Conectado a QuickNode WebSocket")
                    
                    # Suscribirse a logs del programa Pump.fun
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [PUMP_FUN_PROGRAM_ID]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    
                    await ws.send(json.dumps(subscribe_msg))
                    logging.info("✅ Suscrito a logs de Pump.fun")
                    
                    self.reconnect_delay = 3  # Reset delay
                    
                    async for message in ws:
                        if not self.running:
                            break
                        await self._handle_websocket_message(message)
                        
            except websockets.exceptions.ConnectionClosed:
                logging.warning("🔌 Conexión WebSocket cerrada, reconectando...")
            except Exception as e:
                logging.error(f"❌ Error WebSocket: {e}")
            
            # Reconexión con backoff exponencial
            if self.running:
                logging.info(f"🔄 Reconectando en {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 1.5, 60)

    async def _handle_websocket_message(self, message: str):
        """Procesa mensajes del WebSocket"""
        try:
            data = json.loads(message)
            logging.debug(f"📨 Mensaje WebSocket: {json.dumps(data)[:200]}...")
            
            # Extraer información del token desde los logs
            token_info = self._extract_token_from_logs(data)
            if token_info and token_info.get("mint"):
                await self.token_manager.add_token(**token_info)
                
        except json.JSONDecodeError:
            logging.error("❌ Mensaje JSON inválido")
        except Exception as e:
            logging.error(f"❌ Error procesando mensaje: {e}")

    def _extract_token_from_logs(self, data: Dict) -> Optional[Dict]:
        """Extrae información del token desde los logs de transacción"""
        try:
            # Los logs vienen en data['params']['result']['value']['logs']
            result = data.get('params', {}).get('result', {})
            logs = result.get('value', {}).get('logs', [])
            
            if not logs:
                return None

            # Buscar el mint address en los logs
            mint = None
            for log in logs:
                if isinstance(log, str) and "initializeMint" in log:
                    # Buscar dirección de 32-44 caracteres (mint address)
                    words = log.split()
                    for word in words:
                        if len(word) in [32, 44] and word.isalnum():
                            mint = word
                            break
                if mint:
                    break

            if mint:
                return {
                    "mint": mint,
                    "symbol": "UNKNOWN",  # Se puede extraer de metadata después
                    "name": "Nuevo Token"
                }
                
        except Exception as e:
            logging.debug(f"Error extrayendo token de logs: {e}")
            
        return None

    async def stop(self):
        self.running = False

# ---------------------------
# GESTOR DE TOKENS
# ---------------------------

class TokenManager:
    def __init__(self, config: Config):
        self.config = config
        self.monitored_tokens: Dict[str, TokenData] = {}
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        self.alerted_tokens: set = set()
        self.semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_MONITORS)
        self.metrics = {
            "tokens_detected": 0,
            "tokens_monitored": 0,
            "alerts_triggered": 0,
            "rpc_calls": 0,
            "rpc_errors": 0
        }

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", **kwargs):
        """Añade un nuevo token para monitoreo"""
        if mint in self.monitored_tokens:
            return

        logging.info(f"🎯 Nuevo token detectado: {symbol} ({mint[:16]}...)")
        
        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name
        )
        
        self.monitored_tokens[mint] = token
        self.metrics["tokens_detected"] += 1
        self.metrics["tokens_monitored"] = len(self.monitored_tokens)
        
        # Iniciar monitoreo
        task = asyncio.create_task(self._monitor_token(mint))
        self.monitor_tasks[mint] = task
        
        # Notificación de detección
        await self._send_detection_notification(token)

    async def _monitor_token(self, mint: str):
        """Monitorea un token durante 30 minutos"""
        async with self.semaphore:
            token = self.monitored_tokens.get(mint)
            if not token:
                return

            logging.info(f"👀 Monitoreando: {token.symbol} ({mint[:16]}...)")
            
            try:
                async with RPCClient(self.config) as rpc_client:
                    start_time = datetime.now(timezone.utc)
                    
                    while mint in self.monitored_tokens:
                        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                        elapsed_minutes = elapsed / 60.0
                        
                        # Timeout de 30 minutos
                        if elapsed_minutes >= self.config.MAX_MONITOR_TIME_MIN:
                            logging.info(f"⏰ Timeout: {token.symbol} ({mint[:16]}...)")
                            await self._remove_token(mint, "timeout")
                            break
                        
                        # Obtener precio actual
                        price_data = await self._get_current_price(rpc_client, token)
                        if price_data:
                            current_price = price_data.get("price_usd", 0.0)
                            market_cap = price_data.get("market_cap_usd", 0.0)
                            
                            # Establecer precio inicial
                            if token.initial_price == 0 and current_price > 0:
                                token.initial_price = current_price
                                logging.info(f"💰 Precio inicial {token.symbol}: ${current_price:.6f}")
                            
                            # Actualizar máximo
                            if current_price > token.max_price:
                                token.max_price = current_price
                            
                            # Verificar alertas
                            if self._check_alert_conditions(token, current_price, elapsed_minutes):
                                await self._trigger_alert(token, current_price, market_cap, elapsed_minutes)
                                break
                            
                            # Verificar dump
                            if self._check_dump_condition(token, current_price):
                                logging.info(f"📉 Dump detectado: {token.symbol}")
                                await self._remove_token(mint, "dumped")
                                break
                        
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        
            except asyncio.CancelledError:
                logging.info(f"🛑 Monitoreo cancelado: {mint[:16]}...")
            except Exception as e:
                logging.error(f"❌ Error monitoreando {mint[:16]}: {e}")
                await self._remove_token(mint, "error")

    async def _get_current_price(self, rpc_client: RPCClient, token: TokenData) -> Optional[Dict]:
        """Obtiene el precio actual del token"""
        try:
            # Si no tenemos bonding curve, intentar derivarla del mint
            if not token.bonding_curve:
                # En una implementación real, aquí buscarías la bonding curve asociada
                # Por ahora, usamos el mint como placeholder
                token.bonding_curve = token.mint
            
            if token.bonding_curve:
                return await rpc_client.get_bonding_curve_data(token.bonding_curve)
                
        except Exception as e:
            logging.debug(f"Error obteniendo precio para {token.mint}: {e}")
            
        return None

    def _check_alert_conditions(self, token: TokenData, current_price: float, elapsed_minutes: float) -> bool:
        """Verifica si se cumplen las condiciones de alerta"""
        if token.initial_price <= 0 or current_price <= 0:
            return False
            
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100.0
        
        # Verificar regla: +300% en 15 minutos
        if gain_percent >= 300.0 and elapsed_minutes <= 15.0:
            logging.info(f"🚨 ALERTA: {token.symbol} +{gain_percent:.1f}% en {elapsed_minutes:.1f}min")
            return True
            
        return False

    def _check_dump_condition(self, token: TokenData, current_price: float) -> bool:
        """Verifica condición de dump"""
        if token.max_price <= 0 or current_price <= 0:
            return False
            
        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100.0
        return loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT

    async def _trigger_alert(self, token: TokenData, current_price: float, market_cap: float, elapsed_minutes: float):
        """Dispara una alerta"""
        if token.mint in self.alerted_tokens:
            return
            
        self.alerted_tokens.add(token.mint)
        self.metrics["alerts_triggered"] += 1
        
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100.0
        
        logging.info(f"🚨 ENVIANDO ALERTA: {token.symbol} +{gain_percent:.1f}%")
        
        await self._send_telegram_alert(token, current_price, market_cap, gain_percent, elapsed_minutes)
        await self._remove_token(token.mint, "alert_sent")

    async def _send_detection_notification(self, token: TokenData):
        """Envía notificación de detección"""
        if not self.config.ENABLE_TELEGRAM:
            return
            
        try:
            from telegram import Bot
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            
            message = f"""🔍 **Nuevo Token Detectado**

**{token.name}** ({token.symbol})
`{token.mint}`

⏰ Monitoreando por 30 minutos
🎯 Buscando +300% en 15 minutos"""

            await bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            logging.error(f"❌ Error enviando notificación: {e}")

    async def _send_telegram_alert(self, token: TokenData, current_price: float, market_cap: float, 
                                 gain_percent: float, elapsed_minutes: float):
        """Envía alerta por Telegram"""
        if not self.config.ENABLE_TELEGRAM:
            return
            
        try:
            from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            
            message = f"""🚨 **ALERTA DE MOMENTUM** 🚨

**Token:** {token.name} ({token.symbol})
**Mint:** `{token.mint}`
**Ganancia:** +{gain_percent:.1f}% en {elapsed_minutes:.1f}min

📊 **Métricas:**
• Precio Actual: ${current_price:.6f}
• Market Cap: ${market_cap:,.0f}
• Precio Inicial: ${token.initial_price:.6f}
• Máximo Alcanzado: ${token.max_price:.6f}

🔗 **Enlaces Rápidos:**"""

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{token.mint}"),
                    InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{token.mint}")
                ],
                [
                    InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{token.mint}"),
                    InlineKeyboardButton("👁️ Birdeye", url=f"https://birdeye.so/token/{token.mint}?chain=solana")
                ]
            ])
            
            await bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {e}")

    async def _remove_token(self, mint: str, reason: str):
        """Elimina token del monitoreo"""
        if mint in self.monitored_tokens:
            del self.monitored_tokens[mint]
            self.metrics["tokens_monitored"] = len(self.monitored_tokens)
            
        if mint in self.monitor_tasks:
            task = self.monitor_tasks[mint]
            if not task.done():
                task.cancel()
            del self.monitor_tasks[mint]
            
        logging.info(f"🗑️ Token removido {mint[:16]}...: {reason}")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            **self.metrics,
            "currently_monitoring": len(self.monitored_tokens),
            "active_tasks": len(self.monitor_tasks),
            "alerts_sent": len(self.alerted_tokens)
        }

    async def stop_all_monitoring(self):
        """Detiene todo el monitoreo"""
        for mint in list(self.monitored_tokens.keys()):
            await self._remove_token(mint, "shutdown")

# ---------------------------
# SERVIDOR WEB (Para Railway)
# ---------------------------

class HealthServer:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.app = web.Application()
        self.runner = None
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/metrics", self.handle_metrics)

    async def handle_health(self, request):
        return web.json_response({
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "4.0"
        })

    async def handle_metrics(self, request):
        metrics = self.token_manager.get_metrics()
        prometheus_lines = []
        for key, value in metrics.items():
            prometheus_lines.append(f"pumpfun_bot_{key} {value}")
        prometheus_lines.append(f"pumpfun_bot_uptime_seconds {int(time.time())}")
        
        return web.Response(
            text="\n".join(prometheus_lines),
            content_type="text/plain"
        )

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.config.HEALTH_PORT)
        await site.start()
        logging.info(f"🌐 Health server en puerto {self.config.HEALTH_PORT}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

# ---------------------------
# BOT PRINCIPAL
# ---------------------------

class PumpFunBot:
    def __init__(self):
        self.config = Config()
        self._setup_logging()
        
        self.token_manager = TokenManager(self.config)
        self.listener = QuickNodeListener(self.config, self.token_manager)
        self.health_server = HealthServer(self.config, self.token_manager)
        self.running = False

    def _setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        logging.info("🤖 Iniciando PumpFunBot v4.0 - QuickNode Optimized")

    async def start(self):
        self.running = True
        logging.info("🚀 Iniciando bot...")
        
        # Verificaciones críticas
        if not self.config.QUICKNODE_WSS:
            logging.error("❌ QUICKNODE_WSS no configurado - Bot no puede iniciar")
            return
            
        if self.config.ENABLE_TELEGRAM and not self.config.TELEGRAM_BOT_TOKEN:
            logging.warning("⚠️ TELEGRAM_BOT_TOKEN no configurado - Notificaciones deshabilitadas")
        
        # Iniciar servidor de salud
        await self.health_server.start()
        
        # Iniciar listener
        self.listener_task = asyncio.create_task(self.listener.start())
        
        # Mantener bot corriendo
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self.running = False
        logging.info("🛑 Deteniendo bot...")
        
        if hasattr(self, 'listener_task'):
            self.listener_task.cancel()
        
        await self.listener.stop()
        await self.token_manager.stop_all_monitoring()
        await self.health_server.stop()
        
        logging.info("✅ Bot detenido correctamente")

# ---------------------------
# MANEJO DE SEÑALES
# ---------------------------

async def main():
    bot = PumpFunBot()
    
    # Configurar manejo de señales
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def signal_handler():
        logging.info("🛑 Señal de terminación recibida")
        stop_event.set()
    
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await bot.start()
        await stop_event.wait()
    except KeyboardInterrupt:
        logging.info("⌨️ Interrupción por teclado")
    finally:
        await bot.stop()

if __name__ == "__main__":
    # Verificaciones iniciales
    config = Config()
    
    logging.info("🔧 Verificando configuraciones...")
    if not config.QUICKNODE_WSS:
        logging.error("❌ QUICKNODE_WSS no configurado")
        sys.exit(1)
        
    if config.ENABLE_TELEGRAM and not config.TELEGRAM_BOT_TOKEN:
        logging.warning("⚠️ Telegram deshabilitado - Sin TELEGRAM_BOT_TOKEN")
    
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"💥 Error fatal: {e}")
        sys.exit(1)
