# telegram_bot_final.py
# VERSIÓN CORREGIDA - COMANDOS TELEGRAM FUNCIONANDO

import os
import re
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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

# APIs
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
JUPITER_API_BASE = "https://api.jup.ag/tokens/v2"

# Parámetros
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "50000"))
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS graduation_alerts (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL DEFAULT 0,
                    price DECIMAL DEFAULT 0,
                    liquidity DECIMAL DEFAULT 0,
                    alert_type TEXT NOT NULL,
                    source TEXT DEFAULT 'helius',
                    detected_at TIMESTAMP DEFAULT NOW(),
                    tx_signature TEXT,
                    dex_name TEXT,
                    is_pump_token BOOLEAN DEFAULT FALSE,
                    organic_score DECIMAL DEFAULT 0
                )
            """)
            
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
                          price: float, liquidity: float, tx_signature: str, 
                          dex_name: str, is_pump_token: bool, organic_score: float):
        """Marcar token como alertado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO graduation_alerts 
                (mint, symbol, market_cap, price, liquidity, alert_type, source, 
                 tx_signature, dex_name, is_pump_token, organic_score)
                VALUES ($1, $2, $3, $4, $5, 'graduated', 'helius_wss', 
                        $6, $7, $8, $9)
                ON CONFLICT (mint) DO NOTHING
            """, mint, symbol, market_cap, price, liquidity, 
                tx_signature, dex_name, is_pump_token, organic_score)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        """Verificar si token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_mints 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "helius"):
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
                SELECT mint, symbol, market_cap, price, liquidity, source, 
                       detected_at, dex_name, is_pump_token, organic_score
                FROM graduation_alerts 
                ORDER BY detected_at DESC 
                LIMIT $1
            """, limit)
            return [dict(row) for row in rows]

# ========== CLIENTE JUPITER API ==========
class JupiterAPI:
    def __init__(self):
        self.base_url = JUPITER_API_BASE

    async def search_token(self, mint: str) -> Optional[Dict]:
        """Buscar token por mint en Jupiter API"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/search"
                params = {"query": mint}
                
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and len(data) > 0:
                            return data[0]
        except Exception as e:
            logging.error(f"❌ Error buscando token {mint} en Jupiter: {str(e)}")
        
        return None

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_graduation_alert(self, symbol: str, mint: str, data: Dict):
        """Enviar alerta de graduación inmediata"""
        if self.silent_mode:
            return

        market_cap = data.get('market_cap', 0)
        price = data.get('price', 0)
        liquidity = data.get('liquidity', 0)
        dex_name = data.get('dex_name', 'Unknown DEX')
        is_pump_token = data.get('is_pump_token', False)

        pump_emoji = "🎯" if is_pump_token else "🎓"

        message_lines = [
            f"{pump_emoji} <b>¡GRADUACIÓN DETECTADA EN TIEMPO REAL!</b> {pump_emoji}",
            "",
            f"<b>{symbol}</b>",
            f"• Market Cap: <b>${market_cap:,.0f}</b>",
            f"• Precio: <b>${price:.8f}</b>",
            f"• Liquidez: <b>${liquidity:,.0f}</b>",
            f"• DEX: <b>{dex_name.upper()}</b>",
            "",
            "⚡ <b>ACCIÓN INMEDIATA REQUERIDA</b>",
            "• Token recién graduado de Pump.fun" if is_pump_token else "• Token recién listado en DEX",
            "• Pool creado en Raydium",
            "• Oportunidad de entrada temprana",
            "",
            "<b>Mint Address:</b>",
            f"<code>{mint}</code>"
        ]

        message = "\n".join(message_lines)

        keyboard = [
            [InlineKeyboardButton("⚡ Comprar Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🔍 Raydium", url=f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}")]
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
            logging.info(f"✅ Alerta enviada: {symbol}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

# ========== CLIENTE HELIUS WSS ==========
class HeliusMonitor:
    def __init__(self, wss_url: str, db: Database, notifier: TelegramNotifier):
        self.wss_url = wss_url
        self.db = db
        self.notifier = notifier
        self.jupiter_api = JupiterAPI()
        self.websocket = None
        self.running = False
        self.task = None

    async def connect(self):
        """Conectar a Helius WebSocket"""
        try:
            self.websocket = await websockets.connect(
                self.wss_url,
                ping_interval=30,
                ping_timeout=10
            )
            
            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": ["RVKd61ztZW9GUwhRbbLoYVRE5Xf1B2tVscKqwZqXgEr"]},
                    {"commitment": "confirmed"}
                ]
            }
            
            await self.websocket.send(json.dumps(subscribe_message))
            await self.websocket.recv()
            logging.info("✅ Helius WebSocket conectado")
            return True
            
        except Exception as e:
            logging.error(f"❌ Error conectando Helius: {str(e)}")
            return False

    async def listen(self):
        """Escuchar mensajes del WebSocket"""
        self.running = True
        
        while self.running:
            try:
                if not self.websocket:
                    if not await self.connect():
                        await asyncio.sleep(5)
                        continue

                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                
                if data.get('method') == 'logsNotification':
                    params = data.get('params', {})
                    result = params.get('result', {})
                    logs = result.get('logs', [])
                    signature = result.get('signature', '')
                    
                    await self.analyze_logs(logs, signature)
                
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                logging.warning("🔌 Conexión WebSocket cerrada, reconectando...")
                self.websocket = None
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"❌ Error en WebSocket: {str(e)}")
                self.websocket = None
                await asyncio.sleep(5)

    async def analyze_logs(self, logs: List[str], signature: str):
        """Analizar logs en busca de creación de pools"""
        try:
            logs_text = ' '.join(logs).lower()
            
            # Buscar patrones de creación de pool
            pool_keywords = ['initialize', 'create', 'new_pool', 'create_pool', 'raydium']
            pool_created = any(keyword in logs_text for keyword in pool_keywords)
            
            if pool_created:
                mints_found = MINT_PATTERN.findall(' '.join(logs))
                unique_mints = list(set(mints_found))
                
                for mint in unique_mints:
                    if not await self.db.seen_recently(mint):
                        await self.db.mark_seen(mint)
                        await self.process_graduation(mint, signature, logs_text)
                        
        except Exception as e:
            logging.error(f"❌ Error analizando logs: {str(e)}")

    async def process_graduation(self, mint: str, signature: str, logs_text: str):
        """Procesar graduación detectada"""
        try:
            if await self.db.was_alerted(mint):
                return

            token_data = await self.jupiter_api.search_token(mint)
            if not token_data:
                return

            symbol = token_data.get('symbol', 'UNKNOWN')
            market_cap = token_data.get('mcap', 0)
            price = token_data.get('usdPrice', 0)
            liquidity = token_data.get('liquidity', 0)

            # Detectar si es token de Pump.fun
            is_pump_token = await self.is_pump_fun_token(symbol, mint, token_data)

            dex_name = "raydium" if 'raydium' in logs_text.lower() else "unknown"

            # Solo alertar si cumple criterios
            if market_cap >= GRADUATION_MC_TARGET and market_cap >= PUMP_MC_MIN and is_pump_token:
                alert_data = {
                    'market_cap': market_cap,
                    'price': price,
                    'liquidity': liquidity,
                    'dex_name': dex_name,
                    'tx_signature': signature,
                    'is_pump_token': is_pump_token
                }
                
                await self.notifier.send_graduation_alert(symbol, mint, alert_data)
                
                await self.db.mark_alerted(
                    mint, symbol, market_cap, price, liquidity, 
                    signature, dex_name, is_pump_token, 0
                )
                
                logging.info(f"🎓 Graduación detectada: {symbol} - MC: ${market_cap:,.0f}")

        except Exception as e:
            logging.error(f"❌ Error procesando graduación {mint}: {str(e)}")

    async def is_pump_fun_token(self, symbol: str, mint: str, token_data: Dict) -> bool:
        """Determinar si es un token de Pump.fun"""
        try:
            # Verificar símbolo contiene "pump"
            if 'pump' in symbol.lower():
                return True

            # Verificar si es token reciente
            first_pool = token_data.get('firstPool', {})
            if first_pool:
                created_at = first_pool.get('createdAt')
                if created_at:
                    pool_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if datetime.now(pool_time.tzinfo) - pool_time < timedelta(hours=24):
                        return True

            return False
        except Exception:
            return False

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
            
        self.task = asyncio.create_task(self.listen())
        logging.info("✅ Helius Monitor iniciado")

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
        logging.info("⛔ Helius Monitor detenido")

# ========== FASTAPI APP ==========
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
helius_monitor: Optional[HeliusMonitor] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager para FastAPI"""
    await startup()
    yield
    await shutdown()

app = FastAPI(title="Pump.fun Graduation Sniper", lifespan=lifespan)

def is_authorized(update: Update) -> bool:
    """Verificar si usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM - VERSIÓN SIMPLIFICADA ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎯 <b>PUMP.FUN GRADUATION SNIPER</b>

<b>Comandos disponibles:</b>
/start - Mostrar este mensaje
/iniciar - Activar monitor
/detener - Detener monitor  
/status - Estado del sistema
/alertas - Últimas alertas
/silent - Modo silencioso

<code>Bot especializado en tokens Pump.fun</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")
    logging.info(f"📱 Comando /start recibido de: {update.effective_user.id}")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if helius_monitor:
            await helius_monitor.start()

        await update.message.reply_text(
            "✅ <b>MONITOR ACTIVADO</b>\n\n"
            "🔗 Helius WebSocket: CONECTADO\n"
            "🎯 Objetivo: Tokens Pump.fun\n"
            "⚡ Estado: ESCANEANDO\n\n"
            "<i>Buscando graduaciones en tiempo real...</i>",
            parse_mode="HTML"
        )
        logging.info(f"🚀 Monitor activado por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        logging.error(f"❌ Error activando monitor: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if helius_monitor:
            await helius_monitor.stop()

        await update.message.reply_text(
            "⛔ <b>MONITOR DETENIDO</b>\n\n"
            "La detección ha sido pausada.\n"
            "Usa /iniciar para reactivar.",
            parse_mode="HTML"
        )
        logging.info(f"⛔ Monitor detenido por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    monitor_status = "🟢 ACTIVO" if helius_monitor and helius_monitor.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"
    
    alerts = await db.get_recent_alerts(5)
    alerts_count = len(alerts)

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA</b>\n\n"
        f"🎯 Monitor: {monitor_status}\n"
        f"🔊 Silencioso: {silent_status}\n"
        f"📈 Alertas: {alerts_count}\n\n"
        f"🔗 WebSocket: {'✅' if helius_monitor and helius_monitor.websocket else '❌'}\n"
        f"🗄️ Base datos: {'✅' if db.pool else '❌'}\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

async def alertas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /alertas"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    alerts = await db.get_recent_alerts(5)
    if not alerts:
        await update.message.reply_text("No hay alertas recientes.")
        return

    lines = ["<b>Últimas alertas:</b>\n"]
    for alert in alerts:
        symbol = alert.get('symbol', 'UNKNOWN')
        market_cap = alert.get('market_cap', 0)
        lines.append(f"• {symbol} - ${market_cap:,.0f}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /silent"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Uso: /silent on|off")
        return

    if notifier:
        notifier.silent_mode = (args[0].lower() == 'on')
        status = "🔇 ON" if notifier.silent_mode else "🔔 OFF"
        await update.message.reply_text(f"Modo silencioso: {status}")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejar comandos desconocidos"""
    await update.message.reply_text(
        "❌ Comando no reconocido. Usa /start para ver los comandos disponibles."
    )

# ========== WEBHOOK CORREGIDO ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Webhook para Telegram - VERSIÓN CORREGIDA"""
    try:
        # Leer el body directamente como bytes y luego parsear JSON
        body_bytes = await request.body()
        body_str = body_bytes.decode('utf-8')
        data = json.loads(body_str)
        
        logging.info(f"📨 Webhook recibido: {data.keys()}")
        
        # Procesar la actualización
        update = Update.de_json(data, telegram_app.bot)
        
        # Verificar autorización y procesar
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
            logging.info(f"✅ Update procesado para user: {update.effective_user.id}")
        else:
            logging.warning(f"⚠️ Usuario no autorizado: {update.effective_user.id if update.effective_user else 'None'}")
            
        return JSONResponse({"status": "ok"})
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Error decodificando JSON: {str(e)}")
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)
    except Exception as e:
        logging.error(f"❌ Error en webhook: {str(e)}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/health")
async def health_check():
    return {
        "status": "healthy", 
        "timestamp": datetime.utcnow().isoformat(),
        "telegram_bot": bool(telegram_app),
        "helius_monitor": helius_monitor.running if helius_monitor else False
    }

@app.get("/")
async def root():
    return {"message": "Pump.fun Graduation Sniper Bot", "status": "running"}

# ========== INICIALIZACIÓN CORREGIDA ==========
async def startup():
    """Inicialización de la aplicación - VERSIÓN CORREGIDA"""
    global telegram_app, notifier, helius_monitor

    logging.info("🚀 Iniciando aplicación...")

    # Verificar variables críticas
    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID, 
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "HELIUS_WSS_URL": HELIUS_WSS_URL
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        error_msg = f"❌ Faltan variables: {', '.join(missing)}"
        logging.error(error_msg)
        raise RuntimeError(error_msg)

    # Conectar a base de datos
    try:
        await db.connect()
        logging.info("✅ Base de datos conectada")
    except Exception as e:
        logging.error(f"❌ Error conectando a BD: {str(e)}")
        raise

    # Inicializar aplicación de Telegram
    try:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logging.info("✅ Aplicación Telegram creada")
    except Exception as e:
        logging.error(f"❌ Error creando app Telegram: {str(e)}")
        raise

    # REGISTRAR COMANDOS - ESTO ES CRÍTICO
    try:
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
        telegram_app.add_handler(CommandHandler("detener", detener_command))
        telegram_app.add_handler(CommandHandler("status", status_command))
        telegram_app.add_handler(CommandHandler("alertas", alertas_command))
        telegram_app.add_handler(CommandHandler("silent", silent_command))
        telegram_app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
        
        logging.info("✅ Todos los comandos registrados")
    except Exception as e:
        logging.error(f"❌ Error registrando comandos: {str(e)}")
        raise

    # Inicializar notificador y monitor
    notifier = TelegramNotifier(telegram_app)
    helius_monitor = HeliusMonitor(HELIUS_WSS_URL, db, notifier)

    # Inicializar Telegram (IMPORTANTE: sin webhook para Railway)
    try:
        await telegram_app.initialize()
        
        # EN RAILWAY USAMOS WEBHOOK MANUALMENTE
        if DOMAIN_URL:
            webhook_url = f"{DOMAIN_URL.rstrip('/')}/webhook"
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            # Fallback a polling si no hay DOMAIN_URL
            await telegram_app.start()
            logging.info("✅ Bot iniciado con polling")
            
        logging.info("🤖 Bot de Telegram inicializado correctamente")
        
    except Exception as e:
        logging.error(f"❌ Error inicializando Telegram: {str(e)}")
        raise

    logging.info("🎉 Aplicación iniciada correctamente - Los comandos deberían funcionar")

async def shutdown():
    """Apagado de la aplicación"""
    logging.info("🛑 Apagando aplicación...")
    
    if helius_monitor:
        await helius_monitor.stop()
    
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as e:
            logging.error(f"❌ Error apagando Telegram: {str(e)}")

    logging.info("✅ Aplicación apagada correctamente")

# ========== EJECUCIÓN ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
