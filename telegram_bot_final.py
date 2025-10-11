# telegram_bot_pump_fun_multisource_fixed.py
# SOLANA PUMP.FUN BOT - FUENTES MÚLTIPLES (ESQUEMA CORREGIDO)

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
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

# MÚLTIPLES FUENTES PARA DETECCIÓN
JUPITER_API_BASE = "https://lite-api.jup.ag/tokens/v2"
DEXSCREENER_API = "https://api.dexscreener.com/latest"
BIRDEYE_API = "https://public-api.birdeye.so"

# Parámetros de trading optimizados
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))
PUMP_MC_MAX = float(os.getenv("PUMP_MC_MAX", "50000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))
PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL CORREGIDA ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Conectar a PostgreSQL y crear/actualizar tablas"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        await self._update_tables()  # Actualizar esquema si es necesario
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        """Crear tablas esenciales con esquema actualizado"""
        async with self.pool.acquire() as conn:
            # Tabla de notificaciones de Pump.fun (ESQUEMA ACTUALIZADO)
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
            
            # Tabla de tokens vistos (ESQUEMA ACTUALIZADO)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT DEFAULT 'unknown'
                )
            """)
            
            # Eliminar tablas antiguas si existen
            try:
                await conn.execute("DROP TABLE IF EXISTS notifications")
                await conn.execute("DROP TABLE IF EXISTS metrics")
                logging.info("✅ Tablas antiguas eliminadas")
            except Exception as e:
                logging.warning(f"⚠️ No se pudieron eliminar tablas antiguas: {e}")

    async def _update_tables(self):
        """Actualizar tablas existentes con nuevas columnas"""
        async with self.pool.acquire() as conn:
            try:
                # Agregar columnas faltantes a pump_notifications
                await conn.execute("""
                    ALTER TABLE pump_notifications 
                    ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'
                """)
                await conn.execute("""
                    ALTER TABLE pump_notifications 
                    ADD COLUMN IF NOT EXISTS market_cap DECIMAL
                """)
                await conn.execute("""
                    ALTER TABLE pump_notifications 
                    ADD COLUMN IF NOT EXISTS graduated BOOLEAN DEFAULT FALSE
                """)
                
                # Agregar columnas faltantes a seen_tokens
                await conn.execute("""
                    ALTER TABLE seen_tokens 
                    ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'
                """)
                
                logging.info("✅ Esquema de base de datos actualizado")
            except Exception as e:
                logging.error(f"❌ Error actualizando esquema: {e}")

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

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, market_cap: float = None, source: str = "unknown"):
        """Marcar token como notificado"""
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
        """Marcar token como graduado"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pump_notifications 
                SET graduated = TRUE 
                WHERE mint = $1
            """, mint)

    async def seen_recently(self, mint: str, minutes: int = 3) -> bool:
        """Verificar si un token fue visto recientemente"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "unknown"):
        """Marcar token como visto"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        """Obtener tokens detectados que aún no se han graduado"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, source
                FROM pump_notifications 
                WHERE graduated = FALSE AND market_cap BETWEEN $1 AND $2
                ORDER BY detected_at DESC
            """, PUMP_MC_MIN, GRADUATION_MC_TARGET)
            return [dict(row) for row in rows]

# ========== CLIENTES API MÚLTIPLES ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, headers: dict = None, timeout: int = 8) -> Any:
        """Fetch JSON con timeout y headers"""
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=timeout) as response:
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

    async def get_recent_tokens(self, limit: int = 50) -> List[Dict]:
        """Obtener tokens recién lanzados - FUENTE PRINCIPAL"""
        url = f"{self.base_url}/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def get_trending_tokens(self, interval: str = "5m", limit: int = 30) -> List[Dict]:
        """Obtener tokens en tendencia"""
        url = f"{self.base_url}/toptrending/{interval}"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("tokens", [])
        return []

    async def search_token(self, mint: str) -> List[Dict]:
        """Buscar token por dirección mint"""
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

    async def get_recent_pairs(self, limit: int = 50) -> List[Dict]:
        """Obtener pairs recientes - FUENTE SECUNDARIA"""
        url = f"{self.base_url}/dex/pairs/recent"
        data = await self.client.fetch_json(url, params={"limit": limit})
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("pairs", [])
        return []

    async def get_token_info(self, mint: str) -> Dict:
        """Obtener información de token desde DexScreener"""
        url = f"{self.base_url}/tokens/{mint}"
        return await self.client.fetch_json(url)

# ========== NOTIFICADOR TELEGRAM ==========
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_pump_alert(self, symbol: str, mint: str, data: Dict, alert_type: str, source: str = "unknown"):
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
            message += f"• Fuente: {source}\n"
            message += "• ⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>\n\n"
            
        elif alert_type == "pre_graduation":
            message = f"🎯 <b>PRE-GRADUACIÓN INMINENTE</b> 🎯\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Objetivo Graduación: ${GRADUATION_MC_TARGET:,.0f}\n"
            message += f"• Diferencia: +{data.get('graduation_percent', 0):.1f}%\n"
            message += f"• Fuente: {source}\n"
            message += "• 📈 <b>GRADUACIÓN PRÓXIMA</b>\n\n"

        elif alert_type == "post_graduation_pump":
            message = f"🔥 <b>EXPLOSIÓN POST-GRADUACIÓN</b> 🔥\n\n"
            message += f"<b>{symbol}</b>\n"
            message += f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
            message += f"• Cambio Precio: {data.get('price_change_5m', 0):.2f}%\n"
            message += f"• Fuente: {source}\n"
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
            logging.info(f"✅ Alerta {alert_type} enviada: {symbol} desde {source}")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {str(e)}")

# ========== MONITOR PRINCIPAL SIMPLIFICADO ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False
        self.scan_round = 0

    async def scan_all_sources(self):
        """Escanear todas las fuentes disponibles"""
        self.scan_round += 1
        logging.info(f"🔄 Escaneo #{self.scan_round} - Buscando nuevos mints...")
        
        tokens_found = 0
        async with APIClient() as client:
            # FUENTE 1: Jupiter Recent Tokens (PRINCIPAL)
            try:
                jupiter = JupiterAPI(client)
                recent_tokens = await jupiter.get_recent_tokens(30)
                tokens_found += await self.process_tokens(recent_tokens, "jupiter_recent")
            except Exception as e:
                logging.error(f"❌ Error en Jupiter Recent: {e}")

            # FUENTE 2: Jupiter Trending Tokens
            try:
                trending_tokens = await jupiter.get_trending_tokens("5m", 20)
                tokens_found += await self.process_tokens(trending_tokens, "jupiter_trending")
            except Exception as e:
                logging.error(f"❌ Error en Jupiter Trending: {e}")

        logging.info(f"✅ Escaneo #{self.scan_round} completado. Tokens procesados: {tokens_found}")

    async def process_tokens(self, tokens: List[Dict], source: str) -> int:
        """Procesar tokens de cualquier fuente"""
        processed = 0
        for token in tokens:
            if not isinstance(token, dict):
                continue

            mint = token.get('id')
            if not mint:
                continue

            # Verificar si ya fue procesado recientemente
            if await self.db.seen_recently(mint, minutes=2):
                continue

            await self.db.mark_seen(mint, source)
            
            market_cap = token.get('mcap', 0)
            symbol = token.get('symbol', 'UNKNOWN')
            price = token.get('usdPrice', 0)

            # Solo procesar tokens con market cap razonable
            if market_cap < 1000 or market_cap > 100000:
                continue

            processed += await self.evaluate_token(mint, symbol, market_cap, price, source)
            
        return processed

    async def evaluate_token(self, mint: str, symbol: str, market_cap: float, price: float, source: str) -> int:
        """Evaluar token y enviar alertas si cumple criterios"""
        processed = 0
        
        # ESTRATEGIA 1: Detección temprana (MC bajo)
        if (PUMP_MC_MIN <= market_cap <= PUMP_MC_MIN * 3 and 
            not await self.db.was_notified(mint, "pump_early")):
            
            await self.notifier.send_pump_alert(
                symbol=symbol,
                mint=mint,
                data={
                    'market_cap': market_cap,
                    'price': price
                },
                alert_type="pump_early",
                source=source
            )
            await self.db.mark_notified(mint, "pump_early", symbol, market_cap, source)
            processed += 1
            logging.info(f"🚨 Alerta EARLY enviada: {symbol} - ${market_cap:,.0f}")

        # ESTRATEGIA 2: Pre-graduación (acercándose al MC objetivo)
        elif (market_cap >= GRADUATION_MC_TARGET * 0.7 and 
              market_cap <= GRADUATION_MC_TARGET and 
              not await self.db.was_notified(mint, "pre_graduation")):
            
            graduation_percent = (market_cap / GRADUATION_MC_TARGET) * 100
            
            await self.notifier.send_pump_alert(
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
            processed += 1
            logging.info(f"🎯 Alerta PRE-GRAD enviada: {symbol} - ${market_cap:,.0f}")

        return processed

    async def run(self):
        """Ejecutar scanner continuamente"""
        self.running = True
        while self.running:
            try:
                await self.scan_all_sources()
                await asyncio.sleep(CHECK_INTERVAL * 60)
            except Exception as e:
                logging.error(f"❌ Error en PumpFunMonitor: {str(e)}")
                await asyncio.sleep(30)

    async def start(self):
        """Iniciar monitor"""
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PumpFunMonitor iniciado - 2 FUENTES ACTIVAS")

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

# ========== SCANNER POST-GRADUACIÓN SIMPLIFICADO ==========
class PostGraduationScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan_graduated_tokens(self):
        """Escanear tokens que han sido graduados para detectar explosiones"""
        try:
            tokens = await self.db.get_pre_graduation_tokens()
            
            for token in tokens:
                mint = token['mint']
                
                current_data = await self.get_current_token_data(mint)
                if not current_data:
                    continue

                current_mc = current_data.get('market_cap', 0)

                # Si el token supera el MC de graduación, marcarlo como graduado
                if current_mc > GRADUATION_MC_TARGET and not await self.db.was_notified(mint, "graduated"):
                    await self.db.mark_graduated(mint)
                    await self.db.mark_notified(mint, "graduated")
                    logging.info(f"🎓 Token graduado: {token['symbol']} - MC: ${current_mc:,.0f}")

                # Detectar explosión post-graduación
                if (current_mc > GRADUATION_MC_TARGET and 
                    current_data.get('price_change_5m', 0) > 15 and
                    not await self.db.was_notified(mint, "post_graduation_pump")):
                    
                    await self.notifier.send_pump_alert(
                        symbol=token['symbol'],
                        mint=mint,
                        data=current_data,
                        alert_type="post_graduation_pump",
                        source="post_graduation"
                    )
                    await self.db.mark_notified(mint, "post_graduation_pump")
                    logging.info(f"🔥 Explosión post-graduación: {token['symbol']}")

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
                await asyncio.sleep(2 * 60)
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
app = FastAPI(title="Solana Pump.fun Bot - Esquema Corregido")

# Variables globales
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
post_graduation_scanner: Optional[PostGraduationScanner] = None

def is_authorized(update: Update) -> bool:
    """Verificar si el usuario está autorizado"""
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== COMANDOS TELEGRAM ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    
    welcome_text = """
🎯 <b>SOLANA PUMP.FUN BOT - ESQUEMA CORREGIDO</b> 🚀

<b>Fuentes activas:</b>
• 📡 Jupiter API (Recent Tokens)
• 📡 Jupiter API (Trending Tokens)

<b>Comandos:</b>
/iniciar - Activar monitores
/detener - Pausar monitores  
/status - Estado del sistema
/silent on|off - Modo silencioso

<b>Alertas:</b>
🚨 EARLY - MC bajo ($3K-$9K)
🎯 PRE-GRAD - Cerca de graduación ($45K-$65K)  
🔥 POST-GRAD - Explosión después de graduación

<code>Base de datos actualizada y funcionando</code>
    """
    await update.message.reply_text(welcome_text, parse_mode="HTML")

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
            "🎯 Pump.fun Monitor → ESCANEANDO 2 FUENTES\n"
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
        f"• Intervalo: {CHECK_INTERVAL} minuto(s)\n"
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
        "message": "Solana Pump.fun Bot - Esquema Corregido",
        "status": "operational"
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

    # Inicializar notificador y monitores
    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    post_graduation_scanner = PostGraduationScanner(db, notifier)

    logging.info("✅ Bot de Pump.fun inicializado - ESQUEMA CORREGIDO")

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
