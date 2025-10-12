# telegram_bot_pump_fun_helius_moralis_fixed.py
# SOLANA PUMP.FUN BOT - HELIUS WEBSOCKET + MORALIS API (ENDPOINTS CORREGIDOS)

import os
import re
import json
import asyncio
import logging
import websockets
from datetime import datetime
from typing import Any, Dict, List, Optional

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
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")

# Parámetros de trading
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "1000"))
UMBRAL_MCAP = float(os.getenv("UMBRAL_MCAP", "55000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
PORT = int(os.getenv("PORT", "8080"))

# Program ID de Pump.fun (confirmado)
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_monitor_tokens (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_checked TIMESTAMP DEFAULT NOW(),
                    alerted BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMP,
                    source TEXT DEFAULT 'helius'
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_alerts_history (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    alert_type TEXT,
                    sent_at TIMESTAMP DEFAULT NOW()
                )
            """)

    async def add_monitor_token(self, mint: str, symbol: str = None, name: str = None):
        """Agregar token a monitoreo"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_monitor_tokens (mint, symbol, name) 
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET last_checked = NOW()
            """, mint, symbol, name)

    async def update_token_market_cap(self, mint: str, market_cap: float):
        """Actualizar market cap del token"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_monitor_tokens 
                SET market_cap = $1, last_checked = NOW()
                WHERE mint = $2
            """, market_cap, mint)

    async def mark_token_alerted(self, mint: str):
        """Marcar token como alertado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_monitor_tokens 
                SET alerted = TRUE, alert_sent_at = NOW()
                WHERE mint = $1
            """, mint)

    async def was_alerted(self, mint: str) -> bool:
        """Verificar si ya se alertó sobre este token"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pump_monitor_tokens WHERE mint = $1 AND alerted = TRUE", 
                mint
            )
            return bool(row)

    async def get_tokens_to_monitor(self) -> List[Dict]:
        """Obtener tokens activos para monitorear"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, name, market_cap 
                FROM pump_monitor_tokens 
                WHERE alerted = FALSE 
                AND last_checked > NOW() - INTERVAL '2 hours'
                ORDER BY created_at DESC
                LIMIT 50
            """)
            return [dict(row) for row in rows]

    async def log_alert(self, mint: str, symbol: str, market_cap: float, alert_type: str):
        """Registrar alerta en historial"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_alerts_history (mint, symbol, market_cap, alert_type)
                VALUES ($1, $2, $3, $4)
            """, mint, symbol, market_cap, alert_type)

    async def get_recent_alerts(self, limit: int = 10) -> List[Dict]:
        """Obtener alertas recientes"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, alert_type, sent_at
                FROM pump_alerts_history 
                ORDER BY sent_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

# ========== CLIENTE MORALIS API CORREGIDO ==========
class MoralisAPI:
    def __init__(self):
        self.base_url = "https://solana-gateway.moralis.io"
        self.headers = {
            "X-API-Key": MORALIS_API_KEY,
            "Accept": "application/json"
        }

    async def get_token_bonding_status(self, mint: str) -> Optional[Dict]:
        """Obtener estado de bonding de un token específico - ENDPOINT CORREGIDO"""
        # ENDPOINT CORRECTO según documentación: Get bonding status by token address
        url = f"{self.base_url}/token/mainnet/{mint}/bonding-status"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_token_data(data)
                    elif response.status == 404:
                        # Intentar con el endpoint alternativo
                        return await self._try_alternative_endpoints(mint)
                    else:
                        logging.warning(f"Moralis API HTTP {response.status} for mint {mint}")
        except asyncio.TimeoutError:
            logging.warning(f"Timeout fetching Moralis data for {mint}")
        except Exception as e:
            logging.error(f"Error fetching Moralis data for {mint}: {str(e)}")
        
        return None

    async def _try_alternative_endpoints(self, mint: str) -> Optional[Dict]:
        """Intentar endpoints alternativos de Moralis"""
        endpoints = [
            # Endpoint de metadata básica
            f"{self.base_url}/token/mainnet/{mint}/metadata",
            # Endpoint de price
            f"{self.base_url}/token/mainnet/{mint}/price",
        ]
        
        for url in endpoints:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=self.headers, timeout=5) as response:
                        if response.status == 200:
                            data = await response.json()
                            return self._parse_alternative_data(data, mint)
            except Exception:
                continue
        
        return None

    async def get_bonding_tokens(self, limit: int = 100) -> List[Dict]:
        """Obtener todos los tokens en bonding curve - ENDPOINT CORREGIDO"""
        # ENDPOINT CORRECTO según documentación: Get tokens in bonding phase by exchange
        url = f"{self.base_url}/token/mainnet/exchange/pumpfun/bonding"
        params = {"limit": limit}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_bonding_tokens(data)
                    else:
                        logging.warning(f"Moralis bonding tokens HTTP {response.status}")
        except Exception as e:
            logging.error(f"Error fetching bonding tokens: {str(e)}")
        
        return []

    async def get_new_tokens(self, limit: int = 50) -> List[Dict]:
        """Obtener nuevos tokens de Pump.fun - ENDPOINT CORREGIDO"""
        # ENDPOINT CORRECTO: Get new tokens by exchange
        url = f"{self.base_url}/token/mainnet/exchange/pumpfun/new"
        params = {"limit": limit}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_bonding_tokens(data)
        except Exception as e:
            logging.error(f"Error fetching new tokens: {str(e)}")
        
        return []

    def _parse_token_data(self, data: Dict) -> Dict:
        """Parsear datos del token de bonding-status"""
        if not data:
            return {}
            
        # Estructura esperada del endpoint bonding-status
        mint = data.get('tokenAddress') or data.get('mint')
        if not mint:
            return {}
            
        return {
            'mint': mint,
            'symbol': data.get('symbol', 'UNKNOWN'),
            'name': data.get('name', ''),
            'market_cap': float(data.get('marketCap', 0)),
            'price': data.get('price'),
            'liquidity': data.get('liquidity', {}).get('usd', 0) if isinstance(data.get('liquidity'), dict) else data.get('liquidity', 0),
            'volume_24h': data.get('volume24h', 0)
        }

    def _parse_alternative_data(self, data: Dict, mint: str) -> Dict:
        """Parsear datos de endpoints alternativos"""
        return {
            'mint': mint,
            'symbol': data.get('symbol', 'UNKNOWN'),
            'name': data.get('name', ''),
            'market_cap': 0,  # No disponible en endpoints alternativos
            'price': data.get('usdPrice') or data.get('price'),
            'liquidity': 0,
            'volume_24h': 0
        }

    def _parse_bonding_tokens(self, data: List[Dict]) -> List[Dict]:
        """Parsear lista de tokens en bonding"""
        tokens = []
        
        if isinstance(data, list):
            for item in data:
                if token_data := self._parse_token_data(item):
                    tokens.append(token_data)
        elif isinstance(data, dict) and 'result' in data:
            for item in data['result']:
                if token_data := self._parse_token_data(item):
                    tokens.append(token_data)
                    
        return tokens

# ========== CLIENTE HELIUS WEBSOCKET ==========
class HeliusWebSocket:
    def __init__(self, on_new_token_callback):
        self.wss_url = HELIUS_WSS_URL
        self.on_new_token_callback = on_new_token_callback
        self.websocket = None
        self.running = False
        self.reconnect_delay = 1

    async def connect(self):
        """Conectar a WebSocket de Helius"""
        try:
            self.websocket = await websockets.connect(self.wss_url, ping_interval=30, ping_timeout=10)
            
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
            
        except Exception as e:
            logging.error(f"❌ Error conectando Helius WebSocket: {str(e)}")
            raise

    async def listen(self):
        """Escuchar mensajes del WebSocket"""
        self.running = True
        
        while self.running:
            try:
                if not self.websocket:
                    await self.connect()
                    self.reconnect_delay = 1
                
                message = await self.websocket.recv()
                data = json.loads(message)
                
                # Procesar logs de transacciones
                if data.get('method') == 'logsNotification':
                    await self._process_log_notification(data)
                
                self.reconnect_delay = 1  # Reset delay on success
                
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
            
            # Buscar logs que indiquen creación de token
            for log in logs:
                if 'initialize_mint' in log.lower() or 'create' in log.lower():
                    # Extraer mint address del log (simplificado)
                    mint_match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', log)
                    if mint_match:
                        mint = mint_match.group(0)
                        logging.info(f"🚀 Nuevo token detectado: {mint}")
                        
                        # Llamar callback con el nuevo mint
                        await self.on_new_token_callback(mint)
                        break
                        
        except Exception as e:
            logging.error(f"❌ Error procesando log: {str(e)}")

    async def start(self):
        """Iniciar listener"""
        if self.running:
            return
        asyncio.create_task(self.listen())
        logging.info("✅ Helius WebSocket iniciado")

    async def stop(self):
        """Detener WebSocket"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
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

    async def send_pump_alert(self, symbol: str, mint: str, data: Dict, alert_type: str = "pre_graduation"):
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        if alert_type == "pre_graduation":
            title = "🎯 PRE-GRADUACIÓN INMINENTE"
            message = self._build_pre_graduation_message(title, symbol, mint, data)
        else:
            title = "🚨 NUEVO TOKEN DETECTADO"
            message = self._build_new_token_message(title, symbol, mint, data)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta enviada: {symbol} - ${data.get('market_cap', 0):,.0f}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

    def _build_pre_graduation_message(self, title: str, symbol: str, mint: str, data: Dict) -> str:
        market_cap = data.get('market_cap', 0)
        progress = (market_cap / GRADUATION_MC_TARGET) * 100
        
        message_lines = [
            f"<b>{title}</b>",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Objetivo: ${GRADUATION_MC_TARGET:,.0f}",
            f"• Progreso: <b>{progress:.1f}%</b>",
            "",
            "⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>",
            "",
            "<b>Mint:</b>",
            f"<code>{mint}</code>"
        ]
        
        return "\n".join(message_lines)

    def _build_new_token_message(self, title: str, symbol: str, mint: str, data: Dict) -> str:
        market_cap = data.get('market_cap', 0)
        
        message_lines = [
            f"<b>{title}</b>",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>" if market_cap > 0 else "• Market Cap: <i>Consultando...</i>",
            "",
            "🔍 <b>NUEVO TOKEN EN MONITOREO</b>",
            "• Se alertará al alcanzar $" + f"{UMBRAL_MCAP:,.0f}",
            "",
            "<b>Mint:</b>",
            f"<code>{mint}</code>"
        ]
        
        return "\n".join(message_lines)

# ========== MONITOR PRINCIPAL ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.moralis = MoralisAPI()
        self.helius_ws = HeliusWebSocket(self.handle_new_token)
        
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.running = False
        self.scan_round = 0

    async def handle_new_token(self, mint: str):
        """Manejar nuevo token detectado por Helius"""
        try:
            # Agregar a base de datos inmediatamente
            await self.db.add_monitor_token(mint)
            
            # Obtener datos del token
            token_data = await self.moralis.get_token_bonding_status(mint)
            
            symbol = "CONSULTANDO..."
            market_cap = 0
            
            if token_data:
                symbol = token_data.get('symbol', 'UNKNOWN')
                market_cap = token_data.get('market_cap', 0)
                
                # Actualizar símbolo en BD
                await self.db.add_monitor_token(mint, symbol, token_data.get('name'))
                await self.db.update_token_market_cap(mint, market_cap)

            # Solo monitorear tokens con market cap razonable o si no tenemos datos
            if market_cap >= PUMP_MC_MIN or market_cap == 0:
                # Enviar alerta de nuevo token detectado
                if not await self.db.was_alerted(mint):
                    await self.notifier.send_pump_alert(
                        symbol=symbol,
                        mint=mint,
                        data=token_data or {'market_cap': 0},
                        alert_type="new_token"
                    )

                # Iniciar monitoreo continuo
                await self.start_token_monitoring(mint, symbol)
                
                logging.info(f"✅ Token agregado a monitoreo: {symbol} (${market_cap:,.0f})")
            else:
                logging.info(f"⏭️  Saltando token {mint} - MC muy bajo: ${market_cap:,.0f}")

        except Exception as e:
            logging.error(f"❌ Error manejando nuevo token {mint}: {str(e)}")

    async def start_token_monitoring(self, mint: str, symbol: str):
        """Iniciar monitoreo continuo de un token"""
        if mint in self.monitoring_tasks:
            return

        async def monitor_single_token():
            while self.running and mint in self.monitoring_tasks:
                try:
                    # Obtener datos actualizados
                    token_data = await self.moralis.get_token_bonding_status(mint)
                    if not token_data:
                        await asyncio.sleep(10)
                        continue

                    market_cap = token_data.get('market_cap', 0)
                    
                    # Actualizar en base de datos
                    await self.db.update_token_market_cap(mint, market_cap)

                    # Verificar si supera umbral para alerta
                    if (market_cap >= UMBRAL_MCAP and 
                        market_cap <= GRADUATION_MC_TARGET and 
                        not await self.db.was_alerted(mint)):
                        
                        await self.notifier.send_pump_alert(
                            symbol=symbol,
                            mint=mint,
                            data=token_data,
                            alert_type="pre_graduation"
                        )
                        
                        await self.db.mark_token_alerted(mint)
                        await self.db.log_alert(mint, symbol, market_cap, "pre_graduation")
                        logging.info(f"🎯 Alerta PRE-GRAD enviada: {symbol} (${market_cap:,.0f})")

                    # Si ya se graduó, detener monitoreo
                    if market_cap > GRADUATION_MC_TARGET:
                        logging.info(f"🎓 Token graduado: {symbol} - deteniendo monitoreo")
                        await self.stop_token_monitoring(mint)
                        break

                except Exception as e:
                    logging.error(f"❌ Error monitoreando {symbol}: {str(e)}")
                
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
        logging.info(f"🔄 Escaneo de tokens existentes #{self.scan_round}")
        
        try:
            tokens = await self.db.get_tokens_to_monitor()
            for token in tokens:
                mint = token['mint']
                symbol = token.get('symbol', 'UNKNOWN')
                
                # Si no está siendo monitoreado, iniciar monitoreo
                if mint not in self.monitoring_tasks:
                    await self.start_token_monitoring(mint, symbol)
                    
        except Exception as e:
            logging.error(f"❌ Error en escaneo de tokens: {str(e)}")

    async def start(self):
        """Iniciar monitor"""
        self.running = True
        
        # Iniciar WebSocket de Helius
        await self.helius_ws.start()
        
        # Iniciar escáner de tokens existentes
        asyncio.create_task(self._continuous_scan())
        
        logging.info("✅ PumpFunMonitor iniciado - Helius + Moralis ACTIVOS")

    async def _continuous_scan(self):
        """Escaneo continuo de tokens existentes"""
        while self.running:
            try:
                await self.scan_existing_tokens()
            except Exception as e:
                logging.error(f"❌ Error en escaneo continuo: {str(e)}")
            await asyncio.sleep(60)  # Escanear cada minuto

    async def stop(self):
        """Detener monitor"""
        self.running = False
        
        # Detener WebSocket
        await self.helius_ws.stop()
        
        # Detener todas las tareas de monitoreo
        for mint in list(self.monitoring_tasks.keys()):
            await self.stop_token_monitoring(mint)
        
        logging.info("⛔ PumpFunMonitor detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - Helius + Moralis (Fixed)")

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
🎯 <b>SOLANA PUMP.FUN BOT - HELIUS + MORALIS</b> 🚀

<b>Endpoints Corregidos:</b>
• ✅ /token/mainnet/:mint/bonding-status
• ✅ /token/mainnet/exchange/pumpfun/bonding
• ✅ /token/mainnet/exchange/pumpfun/new

<b>Comandos:</b>
/iniciar - Activar monitores
/detener - Pausar monitores  
/status - Estado del sistema
/silent on|off - Modo silencioso
/alertas - Historial de alertas

<b>Configuración:</b>
• Alerta en: <b>${UMBRAL_MCAP:,.0f}</b>
• Graduación: <b>${GRADUATION_MC_TARGET:,.0f}</b>

<code>Endpoints Moralis corregidos y funcionando</code>
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
            "✅ <b>MONITOR ACTIVADO</b>\n\n"
            "🌐 Helius WebSocket → ESCUCHANDO\n"
            "📊 Moralis API → ENDPOINTS CORREGIDOS\n"
            "🗄️ PostgreSQL → ALMACENANDO\n\n"
            "<i>Buscando oportunidades pre-graduación...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitor iniciado por: {update.effective_user.id}")
        
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
            "⛔ <b>MONITOR DETENIDO</b>\n\n"
            "El bot ha dejado de escanear.\n"
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
    
    # Contar tokens en monitoreo
    tokens_count = len(pump_monitor.monitoring_tasks) if pump_monitor else 0

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA</b>\n\n"
        f"🎯 Pump.fun Monitor: {pump_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n"
        f"📈 Tokens Monitoreando: <b>{tokens_count}</b>\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• Alerta MC: ${UMBRAL_MCAP:,.0f}\n"
        f"• Graduación MC: ${GRADUATION_MC_TARGET:,.0f}\n"
        f"• Endpoints: <code>CORREGIDOS</code>\n"
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

    lines = ["<b>🔔 Últimas Alertas:</b>\n"]
    for r in rows:
        sym = r.get('symbol', 'UNKNOWN')
        mint_short = r.get('mint', '')[:8] + "..."
        mc = r.get('market_cap', 0)
        alert_type = "🎯 PRE-GRAD" if r.get('alert_type') == "pre_graduation" else "🆕 NUEVO"
        
        lines.append(f"• <b>{sym}</b> {alert_type}")
        lines.append(f"  MC: ${float(mc):,.0f} | {mint_short}")
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
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "tokens_monitoring": len(pump_monitor.monitoring_tasks) if pump_monitor else 0,
            "helius_connected": pump_monitor.helius_ws.websocket is not None if pump_monitor else False
        },
        "endpoints": "corrected"
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Pump.fun Bot - Helius + Moralis (Endpoints Corregidos)",
        "status": "operational",
        "endpoints_corrected": True
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "HELIUS_WSS_URL": HELIUS_WSS_URL,
        "MORALIS_API_KEY": MORALIS_API_KEY
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

    logging.info("✅ Bot de Pump.fun con endpoints Moralis corregidos")

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
            
        logging.info("🚀 Bot listo - Endpoints Moralis corregidos")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot...")
    
    if pump_monitor:
        await pump_monitor.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
