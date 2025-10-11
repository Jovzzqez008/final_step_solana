# telegram_bot_pump_fun_ultra_final.py
# SOLANA PUMP.FUN BOT - GRAPHQL + WEBSOCKET DUAL SOURCE (ULTRA RÁPIDO)

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

import aiohttp
import asyncpg
import websockets
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

# FUENTES DUALES: GraphQL + WebSocket
PUMP_FUN_GRAPHQL_URL = "https://pump.fun/api/graphql"
PUMP_FUN_WEBSOCKET_URL = os.getenv("PUMP_FUN_WEBSOCKET_URL", "wss://pump.fun/api/graphql")

# Fallback externo
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Parámetros de trading
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))
PUMP_MC_MAX = float(os.getenv("PUMP_MC_MAX", "50000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))
PORT = int(os.getenv("PORT", "8080"))

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
                CREATE TABLE IF NOT EXISTS pump_notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    graduated BOOLEAN DEFAULT FALSE,
                    source TEXT DEFAULT 'unknown'
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT DEFAULT 'unknown'
                )
            """)

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pump_notifications WHERE mint = $1 AND alert_type = $2", 
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, market_cap: float = None, source: str = "unknown"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, alert_type, symbol, market_cap, source)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint) DO UPDATE SET
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    market_cap = EXCLUDED.market_cap,
                    source = EXCLUDED.source,
                    detected_at = NOW()
            """, mint, alert_type, symbol, market_cap, source)

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE pump_notifications SET graduated = TRUE WHERE mint = $1", mint)

    async def seen_recently(self, mint: str, minutes: int = 3) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "unknown"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, source
                FROM pump_notifications 
                WHERE graduated = FALSE AND market_cap BETWEEN $1 AND $2
                ORDER BY detected_at DESC
                LIMIT 50
            """, PUMP_MC_MIN, GRADUATION_MC_TARGET)
            return [dict(r) for r in rows]

    async def list_recent_notifications(self, limit: int = 10) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT mint, symbol, alert_type, market_cap, detected_at FROM pump_notifications ORDER BY detected_at DESC LIMIT $1", 
                limit
            )
            return [dict(r) for r in rows]

# ========== CLIENTE HTTP CON HEADERS ANTI-CLOUDFLARE ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/",
            "Content-Type": "application/json",
            "x-fun-platform": "pumpfun-web"
        })
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, method: str = "GET", data: dict = None, timeout: int = 10) -> Any:
        try:
            if method.upper() == "POST":
                async with self.session.post(url, json=data, timeout=timeout) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logging.warning(f"HTTP {response.status} from {url}")
            else:
                async with self.session.get(url, timeout=timeout) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logging.warning(f"HTTP {response.status} from {url}")
        except asyncio.TimeoutError:
            logging.warning(f"Timeout fetching {url}")
        except Exception as e:
            logging.error(f"Error fetching {url}: {str(e)}")
        return None

# ========== PUMP.FUN GRAPHQL CLIENT ==========
class PumpFunGraphQL:
    def __init__(self, client: APIClient):
        self.client = client
        self.url = PUMP_FUN_GRAPHQL_URL

    async def get_recent_tokens(self, limit: int = 50) -> List[Dict]:
        """Consulta GraphQL interna de Pump.fun - TOKENS NUEVOS EN TIEMPO REAL"""
        query = """
        {
            feed(input: {limit: %d, filter: NEW}) {
                coins {
                    id
                    mint
                    name
                    symbol
                    marketCap
                    createdAt
                    creator {
                        address
                    }
                }
            }
        }
        """ % limit

        data = await self.client.fetch_json(self.url, method="POST", data={"query": query})
        
        if data and "data" in data and "feed" in data["data"]:
            coins = data["data"]["feed"].get("coins", [])
            logging.info(f"✅ GraphQL: {len(coins)} tokens nuevos detectados")
            return coins
        return []

    async def get_token_by_mint(self, mint: str) -> Optional[Dict]:
        """Obtener token específico por mint address"""
        query = """
        query GetToken($mint: String!) {
            coin(mint: $mint) {
                id
                mint
                name
                symbol
                marketCap
                createdAt
                creator {
                    address
                }
            }
        }
        """
        
        data = await self.client.fetch_json(self.url, method="POST", data={
            "query": query,
            "variables": {"mint": mint}
        })
        
        if data and "data" in data and "coin" in data["data"]:
            return data["data"]["coin"]
        return None

# ========== PUMP.FUN WEBSOCKET CLIENT ==========
class PumpFunWebSocket:
    def __init__(self, on_new_token: Callable):
        self.url = PUMP_FUN_WEBSOCKET_URL
        self.on_new_token = on_new_token
        self.websocket = None
        self.running = False
        self.task = None

    async def connect(self):
        """Conectar y autenticar WebSocket"""
        try:
            self.websocket = await websockets.connect(self.url, ping_interval=20, ping_timeout=10)
            
            # Subscription query para nuevos tokens
            subscribe_message = {
                "type": "connection_init",
                "payload": {}
            }
            await self.websocket.send(json.dumps(subscribe_message))
            
            # Esperar conexión establecida
            response = await self.websocket.recv()
            logging.info(f"✅ WebSocket conectado: {response}")
            
            # Suscribirse a nuevos tokens
            subscription_query = {
                "id": "1",
                "type": "start",
                "payload": {
                    "query": """
                    subscription {
                        coinSub {
                            id
                            mint
                            name
                            symbol
                            marketCap
                            createdAt
                            creator {
                                address
                            }
                        }
                    }
                    """
                }
            }
            
            await self.websocket.send(json.dumps(subscription_query))
            logging.info("✅ Suscrito a nuevos tokens via WebSocket")
            
        except Exception as e:
            logging.error(f"❌ Error conectando WebSocket: {str(e)}")
            raise

    async def listen(self):
        """Escuchar mensajes del WebSocket"""
        self.running = True
        reconnect_delay = 1
        
        while self.running:
            try:
                if not self.websocket:
                    await self.connect()
                    reconnect_delay = 1
                
                message = await self.websocket.recv()
                data = json.loads(message)
                
                if data.get("type") == "data" and "payload" in data:
                    coin_data = data["payload"].get("data", {}).get("coinSub")
                    if coin_data:
                        logging.info(f"🚀 WebSocket NEW TOKEN: {coin_data.get('symbol')} - {coin_data.get('mint')}")
                        await self.on_new_token(coin_data, "websocket")
                
                reconnect_delay = 1  # Reset delay on successful message
                
            except websockets.exceptions.ConnectionClosed:
                logging.warning(f"🔌 WebSocket desconectado, reconectando en {reconnect_delay}s...")
                self.websocket = None
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)  # Exponential backoff
                
            except Exception as e:
                logging.error(f"❌ Error en WebSocket: {str(e)}")
                self.websocket = None
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def start(self):
        """Iniciar listener de WebSocket"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.listen())
        logging.info("✅ PumpFunWebSocket iniciado")

    async def stop(self):
        """Detener WebSocket"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PumpFunWebSocket detenido")

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

    async def send_pump_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str, source: str = "pump.fun"):
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        msg_lines = []
        msg_lines.append(f"🚨 <b>{title}</b> 🚨")
        msg_lines.append("")
        msg_lines.append(f"<b>{symbol}</b>")
        
        if data.get('market_cap'):
            try:
                mc_str = f"• Market Cap: ${float(data['market_cap']):,.0f}"
                msg_lines.append(mc_str)
            except Exception:
                pass
        
        if data.get('price'):
            msg_lines.append(f"• Precio: {data.get('price')}")
        
        if alert_type == "pump_early":
            msg_lines.append("• ⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>")
        elif alert_type == "pre_graduation":
            msg_lines.append("• 📈 <b>PRE-GRADUACIÓN INMINENTE</b>")
            if data.get('graduation_percent'):
                msg_lines.append(f"• Progreso: {data['graduation_percent']:.1f}%")
        elif alert_type == "post_graduation_pump":
            msg_lines.append("• 🔥 <b>EXPLOSIÓN POST-GRADUACIÓN</b>")
            if data.get('price_change_5m'):
                msg_lines.append(f"• Cambio 5m: {data['price_change_5m']:.2f}%")
        
        msg_lines.append(f"• Fuente: {source}")
        msg_lines.append("• 🚀 <b>ULTRA EARLY DETECTION</b>" if source == "websocket" else "• ⚡ <b>EARLY DETECTION</b>")
        msg_lines.append("")
        msg_lines.append("<b>Mint:</b>")
        msg_lines.append(f"<code>{mint}</code>")

        message = "\n".join(msg_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta {alert_type} enviada: {symbol} via {source}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

# ========== MONITOR PRINCIPAL DUAL SOURCE ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.graphql_client = None
        self.websocket = None
        self.running = False
        self.tasks = []
        self.scan_round = 0

    async def handle_new_token(self, token_data: Dict, source: str):
        """Procesar nuevo token desde cualquier fuente"""
        mint = token_data.get('mint')
        symbol = token_data.get('symbol', 'UNKNOWN')
        market_cap = token_data.get('marketCap', 0)
        
        if not mint:
            return

        # Evitar duplicados recientes
        if await self.db.seen_recently(mint, minutes=1):
            return

        await self.db.mark_seen(mint, source)
        
        try:
            market_cap = float(market_cap) if market_cap else 0
        except (TypeError, ValueError):
            market_cap = 0

        # Filtro por market cap
        if market_cap < PUMP_MC_MIN or market_cap > PUMP_MC_MAX:
            return

        # ALERTA EARLY - Detección inmediata
        if not await self.db.was_notified(mint, "pump_early"):
            speed_indicator = "🚀 ULTRA EARLY" if source == "websocket" else "⚡ EARLY"
            
            await self.notifier.send_pump_alert(
                title=f"PUMP.FUN {speed_indicator} DETECTION",
                symbol=symbol,
                mint=mint,
                data={"market_cap": market_cap},
                alert_type="pump_early",
                source=source
            )
            await self.db.mark_notified(mint, "pump_early", symbol, market_cap, source)
            logging.info(f"🚨 Alerta EARLY enviada: {symbol} (${market_cap:,.0f}) via {source}")

        # PRE-GRADUACIÓN - Si está cerca del objetivo
        if (market_cap >= GRADUATION_MC_TARGET * 0.7 and 
            market_cap <= GRADUATION_MC_TARGET and 
            not await self.db.was_notified(mint, "pre_graduation")):
            
            graduation_percent = (market_cap / GRADUATION_MC_TARGET) * 100
            
            await self.notifier.send_pump_alert(
                title="PRE-GRADUACIÓN INMINENTE",
                symbol=symbol,
                mint=mint,
                data={
                    'market_cap': market_cap,
                    'graduation_percent': graduation_percent
                },
                alert_type="pre_graduation",
                source=source
            )
            await self.db.mark_notified(mint, "pre_graduation", symbol, market_cap, source)
            logging.info(f"🎯 Alerta PRE-GRAD enviada: {symbol} (${market_cap:,.0f})")

    async def graphql_scanner(self):
        """Scanner periódico via GraphQL"""
        while self.running:
            try:
                self.scan_round += 1
                logging.info(f"🔄 GraphQL Scan round #{self.scan_round}")
                
                async with APIClient() as client:
                    graphql = PumpFunGraphQL(client)
                    tokens = await graphql.get_recent_tokens(30)
                    
                    for token in tokens:
                        await self.handle_new_token(token, "graphql")
                        
            except Exception as e:
                logging.error(f"❌ Error en GraphQL scanner: {str(e)}")
            
            await asyncio.sleep(max(30, CHECK_INTERVAL * 60))

    async def start(self):
        """Iniciar monitor dual (GraphQL + WebSocket)"""
        self.running = True
        
        # Iniciar WebSocket si está configurado
        if PUMP_FUN_WEBSOCKET_URL and PUMP_FUN_WEBSOCKET_URL.startswith("wss://"):
            self.websocket = PumpFunWebSocket(self.handle_new_token)
            await self.websocket.start()
            logging.info("✅ WebSocket monitor iniciado")
        else:
            logging.warning("⚠️ WebSocket URL no configurada, solo usando GraphQL")
        
        # Iniciar scanner GraphQL
        graphql_task = asyncio.create_task(self.graphql_scanner())
        self.tasks.append(graphql_task)
        
        logging.info("✅ PumpFunMonitor DUAL SOURCE iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        
        if self.websocket:
            await self.websocket.stop()
        
        for task in self.tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logging.info("⛔ PumpFunMonitor detenido")

# ========== POST-GRADUATION SCANNER ==========
class PostGraduationScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan_graduated_tokens(self):
        """Escanear tokens para detección post-graduación"""
        try:
            tokens = await self.db.get_pre_graduation_tokens()
            
            async with APIClient() as client:
                graphql = PumpFunGraphQL(client)
                
                for token in tokens:
                    mint = token['mint']
                    symbol = token.get('symbol', 'UNKNOWN')
                    
                    # Obtener datos actualizados
                    current_data = await graphql.get_token_by_mint(mint)
                    if not current_data:
                        continue
                    
                    current_mc = current_data.get('marketCap', 0)
                    try:
                        current_mc = float(current_mc)
                    except (TypeError, ValueError):
                        current_mc = 0

                    # Marcar como graduado si supera el target
                    if current_mc > GRADUATION_MC_TARGET and not await self.db.was_notified(mint, "graduated"):
                        await self.db.mark_graduated(mint)
                        await self.db.mark_notified(mint, "graduated")
                        logging.info(f"🎓 Token graduado: {symbol} - MC: ${current_mc:,.0f}")

                    # Detectar explosión post-graduación (simulado - GraphQL no tiene price_change_5m)
                    if (current_mc > GRADUATION_MC_TARGET * 1.1 and 
                        not await self.db.was_notified(mint, "post_graduation_pump")):
                        
                        await self.notifier.send_pump_alert(
                            title="EXPLOSIÓN POST-GRADUACIÓN",
                            symbol=symbol,
                            mint=mint,
                            data={"market_cap": current_mc},
                            alert_type="post_graduation_pump",
                            source="post_graduation"
                        )
                        await self.db.mark_notified(mint, "post_graduation_pump")
                        logging.info(f"🔥 Explosión post-graduación: {symbol}")

        except Exception as e:
            logging.error(f"❌ Error en scan_graduated_tokens: {str(e)}")

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan_graduated_tokens()
            except Exception as e:
                logging.error(f"❌ Error en PostGraduationScanner: {str(e)}")
            await asyncio.sleep(2 * 60)  # Cada 2 minutos

    async def start(self):
        """Iniciar scanner"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PostGraduationScanner iniciado")

    async def stop(self):
        """Detener scanner"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PostGraduationScanner detenido")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - DUAL SOURCE ULTRA")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
post_graduation_scanner: Optional[PostGraduationScanner] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎯 <b>SOLANA PUMP.FUN BOT - DUAL SOURCE ULTRA</b> 🚀

<b>Fuentes activas:</b>
• 📡 GraphQL Interno Pump.fun (NEW tokens)
• 🌐 WebSocket Real-time (ULTRA EARLY)

<b>Comandos:</b>
/iniciar - Activar monitores
/detener - Pausar monitores  
/status - Estado del sistema
/silent on|off - Modo silencioso
/lista - Últimos tokens detectados

<b>Alertas:</b>
🚨 EARLY - MC bajo ($3K-$50K)
🎯 PRE-GRAD - Cerca de graduación  
🔥 POST-GRAD - Explosión post-graduación

<code>Dual Source: GraphQL + WebSocket</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.start()
        if post_graduation_scanner:
            await post_graduation_scanner.start()

        await update.message.reply_text(
            "✅ <b>MONITORES DUAL ACTIVADOS</b>\n\n"
            "🎯 Pump.fun Monitor → GRAPHQL + WEBSOCKET\n"
            "🔥 Post-Graduation Scanner → ACTIVO\n\n"
            "<i>Buscando oportunidades ULTRA EARLY...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitores iniciados por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.stop()
        if post_graduation_scanner:
            await post_graduation_scanner.stop()

        await update.message.reply_text(
            "⛔ <b>MONITORES DETENIDOS</b>\n\n"
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
    post_status = "🟢 ACTIVO" if post_graduation_scanner and post_graduation_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    websocket_status = "🟢 CONECTADO" if pump_monitor and pump_monitor.websocket else "🔴 NO CONFIGURADO"

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA DUAL</b>\n\n"
        f"🎯 Pump.fun Monitor: {pump_status}\n"
        f"🌐 WebSocket: {websocket_status}\n"
        f"🔥 Post-Graduation: {post_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• MC Mínimo: ${PUMP_MC_MIN:,.0f}\n"
        f"• MC Máximo: ${PUMP_MC_MAX:,.0f}\n"
        f"• Objetivo Graduación: ${GRADUATION_MC_TARGET:,.0f}\n"
        f"• Intervalo: {CHECK_INTERVAL} minuto(s)\n"
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

async def lista_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    rows = await db.list_recent_notifications(limit=10)
    if not rows:
        await update.message.reply_text("No hay notificaciones recientes.")
        return

    lines = ["<b>Últimas notificaciones:</b>\n"]
    for r in rows:
        sym = r.get('symbol', 'UNKNOWN')
        mint = r.get('mint', '')
        mc = r.get('market_cap', 0)
        alert_type = r.get('alert_type', '')
        detected = r.get('detected_at')
        
        lines.append(f"• <b>{sym}</b> [{alert_type}]")
        lines.append(f"  MC: ${float(mc):,.0f}")
        lines.append(f"  <code>{mint}</code>\n")

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
            "post_graduation_scanner": post_graduation_scanner.running if post_graduation_scanner else False,
            "websocket_connected": pump_monitor.websocket is not None if pump_monitor else False
        }
    }

@app.get("/")
async def root():
    return {
        "message": "Solana Pump.fun Bot - DUAL SOURCE ULTRA",
        "status": "operational",
        "sources": ["graphql", "websocket"]
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor, post_graduation_scanner

    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables: {', '.join(missing)}")

    await db.connect()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("lista", lista_command))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    post_graduation_scanner = PostGraduationScanner(db, notifier)

    logging.info("✅ Bot de Pump.fun DUAL SOURCE inicializado")

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
            
        logging.info("🚀 Bot DUAL SOURCE listo - Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    logging.info("🛑 Apagando bot DUAL SOURCE...")
    
    if pump_monitor:
        await pump_monitor.stop()
    if post_graduation_scanner:
        await post_graduation_scanner.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot DUAL SOURCE apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
