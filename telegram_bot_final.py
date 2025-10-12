# telegram_bot_pump_fun_direct_api.py
# SOLANA PUMP.FUN BOT - API DIRECTA DE PUMP.FUN PARA PRE-GRADUACIÓN

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

# APIs EXCLUSIVAS PARA PUMP.FUN
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
PUMP_FUN_API_BASE = "https://frontend-api.pump.fun"

# Parámetros EXCLUSIVOS para pre-graduación
UMBRAL_MCAP = float(os.getenv("UMBRAL_MCAP", "55000"))  # Alerta a $55k
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))  # Graduación a $69k
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "1000"))  # Mínimo para monitorear

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))  # Segundos entre checks
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_pre_graduation_tokens (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_checked TIMESTAMP DEFAULT NOW(),
                    pre_graduation_alert_sent BOOLEAN DEFAULT FALSE,
                    alert_sent_at TIMESTAMP,
                    source TEXT DEFAULT 'pump.fun'
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_pre_graduation_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    progress_percent DECIMAL,
                    sent_at TIMESTAMP DEFAULT NOW()
                )
            """)

    async def add_pre_graduation_token(self, mint: str, symbol: str = None, name: str = None):
        """Agregar token a monitoreo pre-graduación"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_pre_graduation_tokens (mint, symbol, name) 
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET last_checked = NOW()
            """, mint, symbol, name)

    async def update_token_market_cap(self, mint: str, market_cap: float):
        """Actualizar market cap del token"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_pre_graduation_tokens 
                SET market_cap = $1, last_checked = NOW()
                WHERE mint = $2
            """, market_cap, mint)

    async def mark_pre_graduation_alert_sent(self, mint: str):
        """Marcar alerta pre-graduación enviada"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_pre_graduation_tokens 
                SET pre_graduation_alert_sent = TRUE, alert_sent_at = NOW()
                WHERE mint = $1
            """, mint)

    async def was_pre_graduation_alert_sent(self, mint: str) -> bool:
        """Verificar si ya se envió alerta pre-graduación"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pump_pre_graduation_tokens WHERE mint = $1 AND pre_graduation_alert_sent = TRUE", 
                mint
            )
            return bool(row)

    async def get_tokens_to_monitor(self) -> List[Dict]:
        """Obtener tokens activos para monitoreo pre-graduación"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, name, market_cap 
                FROM pump_pre_graduation_tokens 
                WHERE pre_graduation_alert_sent = FALSE 
                AND market_cap < $1
                AND last_checked > NOW() - INTERVAL '3 hours'
                ORDER BY market_cap DESC
                LIMIT 50
            """, GRADUATION_MC_TARGET)
            return [dict(row) for row in rows]

    async def log_pre_graduation_alert(self, mint: str, symbol: str, market_cap: float, progress: float):
        """Registrar alerta pre-graduación"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_pre_graduation_alerts (mint, symbol, market_cap, progress_percent)
                VALUES ($1, $2, $3, $4)
            """, mint, symbol, market_cap, progress)

    async def get_recent_pre_graduation_alerts(self, limit: int = 10) -> List[Dict]:
        """Obtener alertas pre-graduación recientes"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, progress_percent, sent_at
                FROM pump_pre_graduation_alerts 
                ORDER BY sent_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

# ========== CLIENTE API DIRECTA DE PUMP.FUN ==========
class PumpFunAPI:
    def __init__(self):
        self.base_url = PUMP_FUN_API_BASE
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/"
        }

    async def get_recent_coins(self, limit: int = 50) -> List[Dict]:
        """Obtener coins recientes de Pump.fun - ENDPOINT DIRECTO"""
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
                        return self._parse_coins_data(data)
                    else:
                        logging.warning(f"Pump.fun API HTTP {response.status}")
        except Exception as e:
            logging.error(f"Error fetching Pump.fun coins: {str(e)}")
        
        return []

    async def get_coin_by_mint(self, mint: str) -> Optional[Dict]:
        """Obtener coin específico por mint address"""
        # Intentar diferentes endpoints posibles
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
            except Exception:
                continue
        
        return None

    def _parse_coins_data(self, data: List[Dict]) -> List[Dict]:
        """Parsear datos de lista de coins"""
        coins = []
        
        for coin in data:
            if parsed_coin := self._parse_coin_data(coin):
                coins.append(parsed_coin)
        
        logging.info(f"✅ Pump.fun API: {len(coins)} coins obtenidos")
        return coins

    def _parse_coin_data(self, data: Dict, mint: str = None) -> Optional[Dict]:
        """Parsear datos de un coin individual"""
        if not data:
            return None
            
        # Extraer mint address
        coin_mint = data.get('mint') or data.get('id') or mint
        if not coin_mint:
            return None
            
        # Extraer market cap - Pump.fun usa marketCap
        market_cap = data.get('marketCap') or data.get('mcap') or 0
        
        return {
            'mint': coin_mint,
            'symbol': data.get('symbol', 'UNKNOWN'),
            'name': data.get('name', ''),
            'market_cap': float(market_cap) if market_cap else 0,
            'price': data.get('price') or data.get('usdPrice'),
            'liquidity': data.get('liquidity', {}).get('usd', 0) if isinstance(data.get('liquidity'), dict) else data.get('liquidity', 0),
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
                    # Extraer mint address del log
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

    async def send_pre_graduation_alert(self, symbol: str, mint: str, market_cap: float):
        """Enviar alerta EXCLUSIVA de pre-graduación"""
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        progress = (market_cap / GRADUATION_MC_TARGET) * 100
        
        message_lines = [
            "🎯 <b>ALERTA PRE-GRADUACIÓN PUMP.FUN</b> 🎯",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Objetivo Graduación: ${GRADUATION_MC_TARGET:,.0f}",
            f"• Progreso: <b>{progress:.1f}%</b>",
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
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

# ========== MONITOR PRINCIPAL PRE-GRADUACIÓN ==========
class PumpFunPreGraduationMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.pump_api = PumpFunAPI()
        self.helius_ws = HeliusWebSocket(self.handle_new_token)
        
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.running = False
        self.scan_round = 0

    async def handle_new_token(self, mint: str):
        """Manejar nuevo token detectado por Helius"""
        try:
            # Agregar a base de datos inmediatamente
            await self.db.add_pre_graduation_token(mint)
            
            # Obtener datos del token desde Pump.fun
            token_data = await self.pump_api.get_coin_by_mint(mint)
            
            symbol = "CONSULTANDO..."
            market_cap = 0
            
            if token_data:
                symbol = token_data.get('symbol', 'UNKNOWN')
                market_cap = token_data.get('market_cap', 0)
                
                # Actualizar en BD
                await self.db.add_pre_graduation_token(mint, symbol, token_data.get('name'))
                await self.db.update_token_market_cap(mint, market_cap)

            # Solo monitorear tokens con market cap razonable
            if market_cap >= PUMP_MC_MIN:
                # Iniciar monitoreo continuo para pre-graduación
                await self.start_pre_graduation_monitoring(mint, symbol)
                logging.info(f"✅ Token en monitoreo pre-graduación: {symbol} (${market_cap:,.0f})")

        except Exception as e:
            logging.error(f"❌ Error manejando nuevo token {mint}: {str(e)}")

    async def start_pre_graduation_monitoring(self, mint: str, symbol: str):
        """Iniciar monitoreo continuo para pre-graduación"""
        if mint in self.monitoring_tasks:
            return

        async def monitor_single_token():
            while self.running and mint in self.monitoring_tasks:
                try:
                    # Obtener datos actualizados desde Pump.fun
                    token_data = await self.pump_api.get_coin_by_mint(mint)
                    if not token_data:
                        await asyncio.sleep(10)
                        continue

                    market_cap = token_data.get('market_cap', 0)
                    
                    # Actualizar en base de datos
                    await self.db.update_token_market_cap(mint, market_cap)

                    # VERIFICACIÓN EXCLUSIVA PARA PRE-GRADUACIÓN
                    if (market_cap >= UMBRAL_MCAP and 
                        market_cap <= GRADUATION_MC_TARGET and 
                        not await self.db.was_pre_graduation_alert_sent(mint)):
                        
                        # ENVIAR ALERTA EXCLUSIVA DE PRE-GRADUACIÓN
                        await self.notifier.send_pre_graduation_alert(
                            symbol=symbol,
                            mint=mint,
                            market_cap=market_cap
                        )
                        
                        # Marcar como alertado
                        await self.db.mark_pre_graduation_alert_sent(mint)
                        progress = (market_cap / GRADUATION_MC_TARGET) * 100
                        await self.db.log_pre_graduation_alert(mint, symbol, market_cap, progress)
                        
                        logging.info(f"🎯 ALERTA PRE-GRADUACIÓN: {symbol} (${market_cap:,.0f})")

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
        logging.info(f"🔄 Escaneo pre-graduación #{self.scan_round}")
        
        try:
            tokens = await self.db.get_tokens_to_monitor()
            for token in tokens:
                mint = token['mint']
                symbol = token.get('symbol', 'UNKNOWN')
                
                # Si no está siendo monitoreado, iniciar monitoreo pre-graduación
                if mint not in self.monitoring_tasks:
                    await self.start_pre_graduation_monitoring(mint, symbol)
                    
        except Exception as e:
            logging.error(f"❌ Error en escaneo pre-graduación: {str(e)}")

    async def start(self):
        """Iniciar monitor de pre-graduación"""
        self.running = True
        
        # Iniciar WebSocket de Helius
        await self.helius_ws.start()
        
        # Iniciar escáner de tokens existentes
        asyncio.create_task(self._continuous_pre_graduation_scan())
        
        logging.info("✅ Monitor PRE-GRADUACIÓN iniciado - Pump.fun API DIRECTA")

    async def _continuous_pre_graduation_scan(self):
        """Escaneo continuo para pre-graduación"""
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
        
        logging.info("⛔ Monitor PRE-GRADUACIÓN detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - Pre-Graduación Exclusiva")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunPreGraduationMonitor] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = f"""
🎯 <b>SOLANA PUMP.FUN BOT - PRE-GRADUACIÓN EXCLUSIVA</b> 🚀

<b>Objetivo Único:</b>
• Alertar cuando tokens alcancen <b>${UMBRAL_MCAP:,.0f}</b> MC
• Antes de graduación (<b>${GRADUATION_MC_TARGET:,.0f}</b>)

<b>Tecnología:</b>
• 🌐 Helius WebSocket (Detección instantánea)
• 🔥 API Directa Pump.fun (Datos reales)
• 🗄️ PostgreSQL (Seguimiento tokens)

<b>Comandos:</b>
/iniciar - Activar monitor pre-graduación
/detener - Pausar monitor
/status - Estado del sistema
/alertas - Historial alertas pre-graduación

<code>100% enfocado en detección pre-graduación Pump.fun</code>
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
            "✅ <b>MONITOR PRE-GRADUACIÓN ACTIVADO</b>\n\n"
            "🎯 Objetivo: Tokens entre $" + f"{UMBRAL_MCAP:,.0f}" + " y $" + f"{GRADUATION_MC_TARGET:,.0f}\n"
            "🌐 Helius WebSocket → DETECTANDO\n"
            "🔥 API Pump.fun → MONITOREANDO\n\n"
            "<i>Buscando oportunidades pre-graduación...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitor pre-graduación iniciado por: {update.effective_user.id}")
        
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
            "⛔ <b>MONITOR PRE-GRADUACIÓN DETENIDO</b>\n\n"
            "No se detectarán nuevos tokens.\n"
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
        "📊 <b>ESTADO PRE-GRADUACIÓN</b>\n\n"
        f"🎯 Monitor Pump.fun: {pump_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n"
        f"📈 Tokens Monitoreando: <b>{tokens_count}</b>\n\n"
        
        "<b>🎯 Configuración Pre-Graduación:</b>\n"
        f"• Alerta MC: <b>${UMBRAL_MCAP:,.0f}</b>\n"
        f"• Graduación MC: <b>${GRADUATION_MC_TARGET:,.0f}</b>\n"
        f"• Rango: ${UMBRAL_MCAP:,.0f} - ${GRADUATION_MC_TARGET:,.0f}\n"
        f"• Fuente: <code>API Directa Pump.fun</code>\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def alertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    rows = await db.get_recent_pre_graduation_alerts(limit=8)
    if not rows:
        await update.message.reply_text("No hay alertas pre-graduación recientes.")
        return

    lines = ["<b>🎯 Alertas Pre-Graduación Recientes:</b>\n"]
    for r in rows:
        sym = r.get('symbol', 'UNKNOWN')
        mc = r.get('market_cap', 0)
        progress = r.get('progress_percent', 0)
        
        lines.append(f"• <b>{sym}</b>")
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
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitor": {
            "pre_graduation_monitor": pump_monitor.running if pump_monitor else False,
            "tokens_monitoring": len(pump_monitor.monitoring_tasks) if pump_monitor else 0,
            "helius_connected": pump_monitor.helius_ws.websocket is not None if pump_monitor else False
        },
        "focus": "pre_graduation_only",
        "api_source": "pump.fun_direct"
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Pump.fun Bot - Pre-Graduación Exclusiva",
        "status": "operational",
        "focus": "pre_graduation_detection",
        "api_source": "pump.fun_direct"
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
    telegram_app.add_handler(CommandHandler("alertas", alertas_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunPreGraduationMonitor(db, notifier)

    logging.info("✅ Bot de PRE-GRADUACIÓN Pump.fun inicializado")

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
            
        logging.info("🚀 Bot PRE-GRADUACIÓN listo - Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot pre-graduación...")
    
    if pump_monitor:
        await pump_monitor.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot pre-graduación apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
