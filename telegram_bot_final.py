# telegram_bot_pump_fun_focused.py
# SOLANA PUMP.FUN BOT - ESTRATEGIA PRE-GRADUACIÓN
# PostgreSQL + QuickNode WSS + Jupiter V2
# Deployment: Railway (Puerto 8080)

import os
import re
import json
import asyncio
import logging
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

# Parámetros de trading optimizados para PRE-GRADUACIÓN
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "5000"))
PUMP_MC_MAX = float(os.getenv("PUMP_MC_MAX", "50000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))
PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL OPTIMIZADA ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas optimizadas"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas esenciales para estrategia Pump.fun"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones de Pump.fun
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    graduated BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Tabla de tokens vistos (para evitar duplicados)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                )
            """)

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        """Verificar si un token ya fue notificado"""
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM pump_notifications WHERE mint = $1 AND alert_type = $2", 
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, market_cap: float = None):
        """Marcar token como notificado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, alert_type, symbol, market_cap)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint) DO UPDATE SET 
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    market_cap = EXCLUDED.market_cap,
                    detected_at = NOW()
            """, mint, alert_type, symbol, market_cap)

    async def mark_graduated(self, mint: str):
        """Marcar token como graduado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_notifications 
                SET graduated = TRUE 
                WHERE mint = $1
            """, mint)

    async def seen_recently(self, mint: str, minutes: int = 2) -> bool:
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

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        """Obtener tokens detectados que aún no se han graduado"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap 
                FROM pump_notifications 
                WHERE graduated = FALSE AND market_cap BETWEEN $1 AND $2
                ORDER BY detected_at DESC
            """, PUMP_MC_MIN, GRADUATION_MC_TARGET)
            return [dict(row) for row in rows]

# ========== CLIENTES API OPTIMIZADOS ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, timeout: int = 5) -> Any:
        """Fetch JSON con timeout corto para velocidad"""
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
        """Buscar token por dirección mint - OPTIMIZADO PARA VELOCIDAD"""
        url = f"{self.base_url}/search"
        data = await self.client.fetch_json(url, params={"query": mint})
        
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

# ========== NOTIFICADOR TELEGRAM ENFOCADO EN PUMP.FUN ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_pump_alert(self, symbol: str, mint: str, data: Dict, alert_type: str):
        """Enviar alerta de Pump.fun optimizada para decisión rápida"""
        if self.silent_mode:
            logging.info(f"Silent mode ON - skipping alert for {symbol}")
            return

        # Mensaje ultra-rápido para decisión inmediata
        if alert_type == "pump_early":
            message = f"🚨 <b>PUMP.FUN EARLY DETECTION</b> 🚨\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Precio: ${data.get('price', 0):.8f}\n"
            message += "• ⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>\n\n"
            
        elif alert_type == "pre_graduation":
            message = f"🎯 <b>PRE-GRADUACIÓN INMINENTE</b> 🎯\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Objetivo Graduación: ${GRADUATION_MC_TARGET:,.0f}\n"
            message += f"• Diferencia: +{data.get('graduation_percent', 0):.1f}%\n"
            message += "• 📈 <b>GRADUACIÓN PRÓXIMA</b>\n\n"

        elif alert_type == "post_graduation_pump":
            message = f"🔥 <b>EXPLOSIÓN POST-GRADUACIÓN</b> 🔥\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Cambio Precio: {data.get('price_change_5m', 0):.2f}%\n"
            message += "• 🚀 <b>MOMENTUM POST-GRADUACIÓN</b>\n\n"

        message += f"<b>Mint:</b>\n<code>{mint}</code>"

        # Botones de acción ULTRA-RÁPIDOS
        keyboard = [
            [InlineKeyboardButton("⚡ Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("📈 Pump.Fun", url=f"https://pump.fun/coin/{mint}")]
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
            logging.info(f"✅ Alerta {alert_type} enviada: {symbol}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

# ========== MONITOR PRINCIPAL PUMP.FUN ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def process_ws_message(self, message: str):
        """Procesar mensaje WebSocket y detectar nuevos mints ULTRA-RÁPIDO"""
        try:
            mints = MINT_PATTERN.findall(message)
            
            for mint in set(mints):
                # Verificación ultra-rápida de duplicados
                if await self.db.seen_recently(mint, minutes=1):
                    continue

                await self.db.mark_seen(mint)
                
                # Obtener datos del token inmediatamente
                token_data = await self.get_token_data(mint)
                if not token_data:
                    continue

                market_cap = token_data.get('market_cap', 0)
                symbol = token_data.get('symbol', 'UNKNOWN')

                # ESTRATEGIA 1: Detección temprana (MC bajo)
                if (PUMP_MC_MIN <= market_cap <= PUMP_MC_MIN * 3 and 
                    not await self.db.was_notified(mint, "pump_early")):
                    
                    await self.notifier.send_pump_alert(
                        symbol=symbol,
                        mint=mint,
                        data=token_data,
                        alert_type="pump_early"
                    )
                    await self.db.mark_notified(mint, "pump_early", symbol, market_cap)

                # ESTRATEGIA 2: Pre-graduación (acercándose al MC objetivo)
                elif (market_cap >= GRADUATION_MC_TARGET * 0.7 and 
                      market_cap <= GRADUATION_MC_TARGET and 
                      not await self.db.was_notified(mint, "pre_graduation")):
                    
                    graduation_percent = (market_cap / GRADUATION_MC_TARGET) * 100
                    token_data['graduation_percent'] = graduation_percent
                    
                    await self.notifier.send_pump_alert(
                        symbol=symbol,
                        mint=mint,
                        data=token_data,
                        alert_type="pre_graduation"
                    )
                    await self.db.mark_notified(mint, "pre_graduation", symbol, market_cap)
        except Exception as e:
            logging.error(f"❌ Error procesando mensaje WS: {str(e)}")

    async def get_token_data(self, mint: str) -> Dict:
        """Obtener datos del token OPTIMIZADO para velocidad"""
        try:
            async with APIClient() as client:
                # Primero intentar con Jupiter (más rápido para datos básicos)
                jupiter = JupiterAPI(client)
                tokens = await jupiter.search_token(mint)
                
                if tokens and len(tokens) > 0:
                    token = tokens[0]
                    return {
                        'symbol': token.get('symbol', 'UNKNOWN'),
                        'name': token.get('name', 'Unknown'),
                        'price': token.get('usdPrice', 0),
                        'market_cap': token.get('mcap', 0),
                        'volume_24h': token.get('volume24h', 0)
                    }

                # Fallback a DexScreener si Jupiter no encuentra
                dexscreener = DexScreenerAPI(client)
                data = await dexscreener.get_token_info(mint)
                if data and 'token' in data:
                    token = data['token']
                    return {
                        'symbol': token.get('symbol', 'UNKNOWN'),
                        'name': token.get('name', 'Unknown'),
                        'price': float(token.get('priceUsd', 0)),
                        'market_cap': float(token.get('marketCap', 0)),
                        'volume_24h': token.get('volume', {}).get('h24', 0)
                    }
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos del token {mint}: {str(e)}")
        
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
                    logging.info("✅ Conectado a QuickNode WebSocket - MONITORANDO PUMP.FUN")
                    reconnect_delay = 1

                    async for message in ws:
                        if not self.running:
                            break
                        await self.process_ws_message(message)

            except Exception as e:
                logging.error(f"❌ WebSocket error: {str(e)}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.start_websocket())
        logging.info("✅ PumpFunMonitor iniciado - ESTRATEGIA PRE-GRADUACIÓN")

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

# ========== SCANNER POST-GRADUACIÓN ==========
class PostGraduationScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan_graduated_tokens(self):
        """Escanear tokens que han sido graduados para detectar explosiones"""
        try:
            # Obtener tokens que detectamos pre-graduación
            tokens = await self.db.get_pre_graduation_tokens()
            
            for token in tokens:
                mint = token['mint']
                
                # Verificar si el token se ha graduado (MC > objetivo)
                current_data = await self.get_current_token_data(mint)
                if not current_data:
                    continue

                current_mc = current_data.get('market_cap', 0)
                previous_mc = token.get('market_cap', 0)

                # Si el token supera el MC de graduación, marcarlo como graduado
                if current_mc > GRADUATION_MC_TARGET and not await self.db.was_notified(mint, "graduated"):
                    await self.db.mark_graduated(mint)
                    await self.db.mark_notified(mint, "graduated")

                # Detectar explosión post-graduación (aumento significativo de precio)
                if (current_mc > GRADUATION_MC_TARGET and 
                    current_data.get('price_change_5m', 0) > 10 and
                    not await self.db.was_notified(mint, "post_graduation_pump")):
                    
                    await self.notifier.send_pump_alert(
                        symbol=token['symbol'],
                        mint=mint,
                        data=current_data,
                        alert_type="post_graduation_pump"
                    )
                    await self.db.mark_notified(mint, "post_graduation_pump")
        except Exception as e:
            logging.error(f"❌ Error en scan_graduated_tokens: {str(e)}")

    async def get_current_token_data(self, mint: str) -> Dict:
        """Obtener datos actualizados del token"""
        try:
            async with APIClient() as client:
                jupiter = JupiterAPI(client)
                tokens = await jupiter.search_token(mint)
                
                if tokens and len(tokens) > 0:
                    token = tokens[0]
                    stats_5m = token.get('stats5m', {})
                    return {
                        'market_cap': token.get('mcap', 0),
                        'price': token.get('usdPrice', 0),
                        'price_change_5m': stats_5m.get('priceChange', 0)
                    }
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos actualizados {mint}: {str(e)}")
        return None

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan_graduated_tokens()
                await asyncio.sleep(CHECK_INTERVAL * 60)
            except Exception as e:
                logging.error(f"❌ Error en PostGraduationScanner: {str(e)}")
                await asyncio.sleep(30)

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

# ========== APLICACIÓN FASTAPI + TELEGRAM ==========
app = FastAPI(title="Solana Pump.fun Bot - Estrategia Pre-Graduación")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
post_graduation_scanner: Optional[PostGraduationScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM OPTIMIZADOS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start enfocado en Pump.fun"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎯 <b>SOLANA PUMP.FUN BOT - ESTRATEGIA PRE-GRADUACIÓN</b> 🚀

<b>Estrategia Principal:</b>
• Detección ultra-temprana de mints en Pump.fun
• Alertas PRE-graduación (MC: $5K - $65K)
• Monitoreo POST-graduación para explosiones

<b>Comandos:</b>
/iniciar - Activar monitores
/detener - Pausar monitores  
/status - Estado del sistema
/silent on|off - Modo silencioso
/estrategia - Ver parámetros

<b>Alertas:</b>
🚨 EARLY - MC bajo (oportunidad máxima)
🎯 PRE-GRAD - Cerca de graduación  
🔥 POST-GRAD - Explosión después

<code>Enfocado en ganancias pre-graduación</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def estrategia_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /estrategia - Mostrar parámetros de trading"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    strategy_text = f"""
📊 <b>PARÁMETROS DE ESTRATEGIA</b>

🎯 <b>Detección Temprana:</b>
• MC Mínimo: ${PUMP_MC_MIN:,.0f}
• MC Máximo Early: ${PUMP_MC_MIN * 3:,.0f}

🎓 <b>Pre-Graduación:</b>
• MC Objetivo: ${GRADUATION_MC_TARGET:,.0f}
• Rango Alerta: ${GRADUATION_MC_TARGET * 0.7:,.0f} - ${GRADUATION_MC_TARGET:,.0f}

⚡ <b>Configuración:</b>
• Intervalo: {CHECK_INTERVAL} min
• QuickNode: {'✅ ACTIVO' if QUICKNODE_WSS_URL else '❌ INACTIVO'}

<b>Ejemplo Tariffcoin:</b>
• Creación: ~$1,000 MC
• Alerta Early: ~$5,000 MC  
• Pre-Graduación: ~$45,000 MC
• Graduación: ~$65,000 MC
• Explosión: +12,000% post-graduación

<i>Objetivo: Entrar entre $5K-$45K MC</i>
    """
    await update.message.reply_text(strategy_text, parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /iniciar - Iniciar monitores"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    try:
        if pump_monitor:
            await pump_monitor.start()
        if post_graduation_scanner:
            await post_graduation_scanner.start()

        await update.message.reply_text(
            "✅ <b>MONITORES ACTIVADOS</b>\n\n"
            "🎯 Pump.fun Monitor → ESCANEANDO\n"
            "🔥 Post-Graduation Scanner → ACTIVO\n\n"
            "<i>Buscando oportunidades pre-graduación...</i>",
            parse_mode="HTML"
        )
        logging.info(f"📱 Monitores iniciados por: {update.effective_user.id}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error iniciando: {str(e)}")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /detener - Detener monitores"""
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
    """Comando /status - Estado del sistema"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return

    pump_status = "🟢 ACTIVO" if pump_monitor and pump_monitor.running else "🔴 INACTIVO"
    post_status = "🟢 ACTIVO" if post_graduation_scanner and post_graduation_scanner.running else "🔴 INACTIVO"
    silent_status = "🔇 ON" if notifier and notifier.silent_mode else "🔔 OFF"

    status_text = (
        "📊 <b>ESTADO DEL SISTEMA</b>\n\n"
        f"🎯 Pump.fun Monitor: {pump_status}\n"
        f"🔥 Post-Graduation: {post_status}\n"
        f"🔊 Modo Silencioso: {silent_status}\n\n"
        
        "<b>⚙️ Configuración:</b>\n"
        f"• QuickNode: {'✅ CONECTADO' if QUICKNODE_WSS_URL else '❌ NO CONFIGURADO'}\n"
        f"• Base Datos: {'✅ CONECTADO' if db.pool else '❌ DESCONECTADO'}\n"
    )

    await update.message.reply_text(status_text, parse_mode="HTML")

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

# ========== WEBHOOK ENDPOINTS ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Endpoint para webhooks de Telegram"""
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
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "strategy": "pump_fun_pre_graduation",
        "timestamp": datetime.utcnow().isoformat(),
        "monitors": {
            "pump_monitor": pump_monitor.running if pump_monitor else False,
            "post_graduation_scanner": post_graduation_scanner.running if post_graduation_scanner else False
        }
    }

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "Solana Pump.fun Bot - Estrategia Pre-Graduación",
        "status": "operational",
        "docs": "/docs",
        "health": "/health"
    }

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    """Inicializar aplicación"""
    global telegram_app, notifier, pump_monitor, post_graduation_scanner

    # Verificar variables críticas
    required_vars = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL
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
    telegram_app.add_handler(CommandHandler("silent", silent_command))
    telegram_app.add_handler(CommandHandler("estrategia", estrategia_command))

    # Inicializar notificador y monitores
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    post_graduation_scanner = PostGraduationScanner(db, notifier)

    logging.info("✅ Bot de Pump.fun inicializado - ESTRATEGIA PRE-GRADUACIÓN")

@app.on_event("startup")
async def startup_event():
    """Evento de inicio de FastAPI"""
    try:
        await initialize_app()
        await telegram_app.initialize()
        
        # Configurar webhook si está en producción
        if os.getenv("RAILWAY_STATIC_URL"):
            webhook_url = f"{os.getenv('RAILWAY_STATIC_URL')}/webhook"
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            # Usar polling en desarrollo
            await telegram_app.start()
            logging.info("✅ Bot iniciado con polling")
            
        logging.info("🚀 Bot listo - Usa /iniciar para comenzar")
    except Exception as e:
        logging.error(f"❌ Error en startup: {str(e)}")
        raise

@app.on_event("shutdown") 
async def shutdown_event():
    """Evento de apagado de FastAPI"""
    logging.info("🛑 Apagando bot...")
    
    if pump_monitor:
        await pump_monitor.stop()
    if post_graduation_scanner:
        await post_graduation_scanner.stop()

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logging.info("✅ Bot apagado")

# ========== EJECUCIÓN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
