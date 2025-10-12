# telegram_bot_final.py
# PUMP.FUN GRADUATION SNIPER - QUICKNODE WSS + FASTAPI

import os
import re
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN_URL = os.getenv("DOMAIN_URL")

# QuickNode WebSocket
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL")

# Parámetros de trading
CHECK_INTERVAL_SECS = int(os.getenv("CHECK_INTERVAL_SECS", "10"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))

PORT = int(os.getenv("PORT", "8080"))

# Regex para direcciones de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL"""
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL no configurada")
        
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas necesarias"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS graduation_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    price DECIMAL DEFAULT 0,
                    liquidity DECIMAL DEFAULT 0,
                    alert_type TEXT NOT NULL,
                    source TEXT DEFAULT 'quicknode',
                    detected_at TIMESTAMP DEFAULT NOW(),
                    tx_signature TEXT,
                    dex_name TEXT
                )
            """)
            
            # Tabla de tokens vistos (cache)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_mints (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT
                )
            """)

    async def was_alerted(self, mint: str) -> bool:
        """Verificar si ya se alertó sobre este token"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM graduation_alerts WHERE mint = $1", 
                mint
            )
            return bool(row)

    async def mark_alerted(self, mint: str, symbol: str, market_cap: float, 
                          price: float, liquidity: float, tx_signature: str, dex_name: str):
        """Marcar token como alertado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO graduation_alerts 
                (mint, symbol, market_cap, price, liquidity, alert_type, source, tx_signature, dex_name)
                VALUES ($1, $2, $3, $4, $5, 'graduated', 'quicknode_wss', $6, $7)
                ON CONFLICT (mint) DO NOTHING
            """, mint, symbol, market_cap, price, liquidity, tx_signature, dex_name)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_mints 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "quicknode"):
        """Marcar token como visto"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_mints (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        """Obtener alertas recientes"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, price, liquidity, source, detected_at, dex_name
                FROM graduation_alerts 
                ORDER BY detected_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_graduation_alert(self, symbol: str, mint: str, data: Dict):
        """Enviar alerta de graduación inmediata"""
        if self.silent_mode:
            logging.info(f"🔇 Silent mode - Skipping alert for {symbol}")
            return

        market_cap = data.get('market_cap', 0)
        price = data.get('price', 0)
        liquidity = data.get('liquidity', 0)
        dex_name = data.get('dex_name', 'Unknown DEX')
        tx_signature = data.get('tx_signature', '')[:16] + '...' if data.get('tx_signature') else 'N/A'

        message_lines = [
            "🎓 <b>¡GRADUACIÓN DETECTADA EN TIEMPO REAL!</b> 🎓",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Precio: <b>${price:.8f}</b>",
            f"• Liquidez: <b>${liquidity:,.0f}</b>",
            f"• DEX: <b>{dex_name.upper()}</b>",
            f"• TX: <code>{tx_signature}</code>",
            "",
            "⚡ <b>ACCIÓN INMEDIATA REQUERIDA</b>",
            "• Token recién graduado de Pump.fun",
            "• Pool creado en DEX",
            "• Oportunidad de entrada temprana",
            "",
            "<b>Mint Address:</b>",
            f"<code>{mint}</code>",
            "",
            "🚀 <i>¡Bot activo con QuickNode WSS!</i>"
        ]

        message = "\n".join(message_lines)

        # Botones de acción rápida
        keyboard = [
            [InlineKeyboardButton("⚡ Comprar Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 Ver en DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🔍 Raydium Swap", url=f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}")],
            [InlineKeyboardButton("🛡️ Rug Check", url=f"https://rugcheck.xyz/tokens/{mint}")]
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
            logging.info(f"✅ Alerta de graduación enviada: {symbol}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta Telegram: {str(e)}")

# ========== CLIENTE QUICKNODE WSS ==========
class QuickNodeMonitor:
    def __init__(self, wss_url: str, db: Database, notifier: TelegramNotifier):
        self.wss_url = wss_url
        self.db = db
        self.notifier = notifier
        self.websocket = None
        self.running = False
        self.reconnect_delay = 1
        self.task = None

        # Patrones para detectar creación de pools
        self.pool_creation_keywords = [
            'initialize', 'create', 'new_pool', 'initialize2', 
            'create_pool', 'initialize_pool', 'init_pool',
            'add_liquidity', 'initialize_account', 'raydium',
            'open_book', 'create_amm', 'initialize_amm'
        ]

    async def connect(self):
        """Conectar a QuickNode WebSocket"""
        try:
            self.websocket = await websockets.connect(
                self.wss_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10
            )
            
            # Suscribirse a logs
            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": []},  # Escuchar todos los logs
                    {"commitment": "confirmed"}
                ]
            }
            
            await self.websocket.send(json.dumps(subscribe_message))
            response = await self.websocket.recv()
            logging.info("✅ QuickNode WebSocket conectado y suscrito")
            return True
            
        except Exception as e:
            logging.error(f"❌ Error conectando QuickNode: {str(e)}")
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
                        self.reconnect_delay = min(self.reconnect_delay * 2, 60)
                        continue
                    self.reconnect_delay = 1

                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                
                await self.process_message(data)
                
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                logging.warning(f"🔌 Conexión WebSocket cerrada, reconectando...")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)
            except Exception as e:
                logging.error(f"❌ Error en WebSocket: {str(e)}")
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    async def process_message(self, data: Dict):
        """Procesar mensaje del WebSocket"""
        try:
            if data.get('method') == 'logsNotification':
                params = data.get('params', {})
                result = params.get('result', {})
                logs = result.get('logs', [])
                signature = result.get('signature', '')
                
                await self.analyze_logs(logs, signature)
                
        except Exception as e:
            logging.error(f"❌ Error procesando mensaje: {str(e)}")

    async def analyze_logs(self, logs: List[str], signature: str):
        """Analizar logs en busca de creación de pools"""
        try:
            logs_text = ' '.join(logs).lower()
            
            # Buscar patrones de creación de pool
            pool_created = any(keyword in logs_text for keyword in self.pool_creation_keywords)
            
            if pool_created:
                # Extraer mints de los logs
                mints_found = MINT_PATTERN.findall(' '.join(logs))
                unique_mints = list(set(mints_found))
                
                for mint in unique_mints:
                    if not await self.db.seen_recently(mint):
                        await self.db.mark_seen(mint)
                        await self.process_new_pool(mint, signature, logs_text)
                        
        except Exception as e:
            logging.error(f"❌ Error analizando logs: {str(e)}")

    async def process_new_pool(self, mint: str, signature: str, logs_text: str):
        """Procesar nuevo pool detectado"""
        try:
            # Verificar si ya alertamos sobre este mint
            if await self.db.was_alerted(mint):
                return

            # Determinar DEX basado en logs
            dex_name = "unknown"
            if 'raydium' in logs_text.lower():
                dex_name = "raydium"
            elif 'orca' in logs_text.lower():
                dex_name = "orca"
            elif 'jupiter' in logs_text.lower():
                dex_name = "jupiter"
            elif 'pump' in logs_text.lower():
                dex_name = "pump.fun"

            # Obtener datos del token desde Jupiter API
            token_data = await self.get_token_data(mint)
            symbol = token_data.get('symbol', 'UNKNOWN')
            market_cap = token_data.get('market_cap', 0)
            price = token_data.get('price', 0)
            liquidity = token_data.get('liquidity', 0)

            # Solo alertar si el market cap es razonable
            if market_cap >= PUMP_MC_MIN:
                # Enviar alerta
                alert_data = {
                    'market_cap': market_cap,
                    'price': price,
                    'liquidity': liquidity,
                    'dex_name': dex_name,
                    'tx_signature': signature
                }
                
                await self.notifier.send_graduation_alert(symbol, mint, alert_data)
                
                # Guardar en base de datos
                await self.db.mark_alerted(
                    mint, symbol, market_cap, price, liquidity, signature, dex_name
                )
                
                logging.info(f"🎓 Pool detectado: {symbol} en {dex_name} - MC: ${market_cap:,.0f}")

        except Exception as e:
            logging.error(f"❌ Error procesando pool {mint}: {str(e)}")

    async def get_token_data(self, mint: str) -> Dict:
        """Obtener datos del token desde Jupiter API"""
        try:
            async with aiohttp.ClientSession() as session:
                # Intentar con Jupiter API
                url = f"https://api.jup.ag/tokens/v2/search?query={mint}"
                async with session.get(url, timeout=8) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and len(data) > 0:
                            token = data[0]
                            return {
                                'symbol': token.get('symbol', 'UNKNOWN'),
                                'market_cap': token.get('mcap', 0),
                                'price': token.get('usdPrice', 0),
                                'liquidity': token.get('liquidity', 0)
                            }
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos de token {mint}: {str(e)}")
        
        return {'symbol': 'UNKNOWN', 'market_cap': 0, 'price': 0, 'liquidity': 0}

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
            
        self.task = asyncio.create_task(self.listen())
        logging.info("✅ QuickNode Monitor iniciado")

    async def stop(self):
        """Detener monitor"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ QuickNode Monitor detenido")

# ========== FASTAPI APP CON LIFESPAN ==========
# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
quicknode_monitor: Optional[QuickNodeMonitor] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager para FastAPI (reemplaza on_event)"""
    # Startup
    await startup()
    yield
    # Shutdown
    await shutdown()

app = FastAPI(title="Pump.fun Graduation Sniper - QuickNode", lifespan=lifespan)

def is_authorized(update: Update) -> bool:
    """Verificar si usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎓 <b>PUMP.FUN GRADUATION SNIPER BOT</b> ⚡

<b>Tecnología:</b>
• 🔗 QuickNode WebSocket (Tiempo real)
• 🗄️ PostgreSQL (Railway)
• 🤖 Telegram Bot API
• ⚡ Detección inmediata

<b>Comandos:</b>
/iniciar - Activar monitor QuickNode
/detener - Pausar monitor
/status - Estado del sistema
/alertas - Últimas alertas
/silent on|off - Modo silencioso

<code>Detección en tiempo real de graduaciones</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar - Activar monitor"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if quicknode_monitor:
            await quicknode_monitor.start()

        await update.message.reply_text(
            "✅ <b>MONITOR QUICKNODE ACTIVADO</b>\n\n"
            "🔗 WebSocket: CONECTADO\n"
            "🎯 Objetivo: Detección pools\n"
            "⚡ Velocidad: TIEMPO REAL\n"
            "📊 Fuente: QuickNode WSS\n\n"
            "<i>Escaneando transacciones en tiempo real...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitor activado por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error activando monitor: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener - Pausar monitor"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if quicknode_monitor:
            await quicknode_monitor.stop()

        await update.message.reply_text(
            "⛔ <b>MONITOR QUICKNODE DETENIDO</b>\n\n"
            "La detección en tiempo real ha sido pausada.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error deteniendo monitor: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status - Estado del sistema"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    monitor_status = "🟢 ACTIVO" if quicknode_monitor and quicknode_monitor.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    
    # Obtener estadísticas
    recent_alerts = await db.get_recent_alerts(5)
    alerts_count = len(recent_alerts)

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA</b>\n\n"
        f"🎯 QuickNode Monitor: {monitor_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n"
        f"📈 Alertas Recientes: <b>{alerts_count}</b>\n\n"
        
        "<b>🔗 Conexiones:</b>\n"
        f"• WebSocket: {'✅ CONECTADO' if quicknode_monitor and quicknode_monitor.websocket else '❌ DESCONECTADO'}\n"
        f"• Base Datos: {'✅ CONECTADO' if db.pool else '❌ DESCONECTADO'}\n"
        f"• Telegram Bot: {'✅ ACTIVO' if telegram_app else '❌ INACTIVO'}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• MC Mínimo: ${PUMP_MC_MIN:,.0f}\n"
        f"• QuickNode: {'✅ CONFIGURADO' if QUICKNODE_WSS_URL else '❌ NO CONFIGURADO'}\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def alertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /alertas - Últimas alertas"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    alerts = await db.get_recent_alerts(8)
    if not alerts:
        await update.message.reply_text("No hay alertas recientes.")
        return

    lines = ["<b>🎓 Últimas Graduaciones Detectadas:</b>\n"]
    for alert in alerts:
        symbol = alert.get('symbol', 'UNKNOWN')
        market_cap = alert.get('market_cap', 0)
        dex = alert.get('dex_name', 'unknown').upper()
        detected_at = alert.get('detected_at')
        
        if isinstance(detected_at, datetime):
            time_str = detected_at.strftime("%H:%M:%S")
        else:
            time_str = "reciente"
        
        lines.append(f"• <b>{symbol}</b>")
        lines.append(f"  MC: ${float(market_cap):,.0f} | DEX: {dex}")
        lines.append(f"  Hora: {time_str}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /silent - Modo silencioso"""
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

# ========== ENDPOINTS WEB ==========
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Webhook para Telegram"""
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
            
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.error(f"❌ Error en webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/last")
async def get_last_alerts(limit: int = 20):
    """Endpoint para últimas alertas"""
    alerts = await db.get_recent_alerts(limit)
    for alert in alerts:
        if isinstance(alert.get('detected_at'), datetime):
            alert['detected_at'] = alert['detected_at'].isoformat()
    return JSONResponse({"alerts": alerts})

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "database": bool(db.pool),
            "telegram_bot": bool(telegram_app),
            "quicknode_monitor": quicknode_monitor.running if quicknode_monitor else False,
            "websocket_connected": quicknode_monitor.websocket is not None if quicknode_monitor else False
        }
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Pump.fun Graduation Sniper Bot",
        "status": "operational",
        "technology": "QuickNode WSS + FastAPI",
        "version": "2.0"
    }

# ========== INICIALIZACIÓN ==========
async def startup():
    """Inicialización de la aplicación"""
    global telegram_app, notifier, quicknode_monitor

    # Verificar variables requeridas
    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "QUICKNODE_WSS_URL": QUICKNODE_WSS_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise RuntimeError(f"❌ Faltan variables: {', '.join(missing)}")

    # Conectar a base de datos
    await db.connect()

    # Inicializar aplicación de Telegram
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Registrar comandos
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("alertas", alertas_command))
    telegram_app.add_handler(CommandHandler("silent", silent_command))

    # Inicializar notificador y monitor
    notifier = TelegramNotifier(telegram_app)
    quicknode_monitor = QuickNodeMonitor(QUICKNODE_WSS_URL, db, notifier)

    # Inicializar Telegram
    await telegram_app.initialize()
    
    # Configurar webhook si hay DOMAIN_URL
    if DOMAIN_URL:
        webhook_url = f"{DOMAIN_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        logging.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        await telegram_app.start()
        logging.info("✅ Bot iniciado con polling")

    logging.info("🚀 Bot de Graduación listo - Usa /iniciar para comenzar")

async def shutdown():
    """Apagado de la aplicación"""
    logging.info("🛑 Apagando bot...")
    
    if quicknode_monitor:
        await quicknode_monitor.stop()
    
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado correctamente")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
