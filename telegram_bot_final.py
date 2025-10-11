import os
import json
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import logging

# -----------------
# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO EXACTAS
# -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# WebSockets - usando las variables exactas que tienes
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")

# APIs
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
DOMAIN = os.getenv("DOMAIN", "")

# Umbrales configurables desde variables de entorno
PUMP_PRE_MIN = int(os.getenv("PUMP_PRE_MIN", "60000"))
PUMP_PRE_MAX = int(os.getenv("PUMP_PRE_MAX", "65000"))
PUMP_MIN_HOLDERS = int(os.getenv("PUMP_MIN_HOLDERS", "40"))

FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "4"))
FLAT_PRICE_CHANGE_THRESHOLD = float(os.getenv("FLAT_PRICE_CHANGE_THRESHOLD", "10.0"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Base de datos
DATABASE_URL = os.getenv("DATABASE_URL")
CACHE_DB = os.getenv("CACHE_DB", "/data/cache_v5.db")

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------
# UTILIDADES
# -----------------
def now_ts(): 
    return datetime.utcnow()

def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def json_log(event: str, **kwargs):
    log_data = {"ts": now_ts().isoformat(), "event": event, **kwargs}
    print(json.dumps(log_data, ensure_ascii=False))

# -----------------
# CACHE (SQLite)
# -----------------
class Cache:
    def __init__(self, path=None):
        db_path = path or CACHE_DB
        # Asegurar que el directorio existe
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()
        
    def _create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens_seen (
                mint TEXT PRIMARY KEY,
                symbol TEXT,
                first_seen TEXT,
                last_seen TEXT,
                market_cap REAL,
                holders INTEGER,
                is_pump BOOLEAN DEFAULT FALSE
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications_sent (
                mint TEXT,
                notification_type TEXT,
                sent_at TEXT,
                PRIMARY KEY (mint, notification_type)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                mint TEXT,
                price REAL,
                volume REAL,
                timestamp TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pump_tokens (
                mint TEXT PRIMARY KEY,
                name TEXT,
                symbol TEXT,
                created_at TEXT,
                market_cap REAL,
                price REAL,
                liquidity REAL,
                holders INTEGER
            )
        """)
        self.conn.commit()

    def add_token_seen(self, mint: str, symbol: str = None, market_cap: float = None, 
                      holders: int = None, is_pump: bool = False):
        now = now_ts().isoformat()
        self.conn.execute("""
            INSERT OR REPLACE INTO tokens_seen 
            (mint, symbol, first_seen, last_seen, market_cap, holders, is_pump)
            VALUES (?, ?, COALESCE((SELECT first_seen FROM tokens_seen WHERE mint = ?), ?), ?, ?, ?, ?)
        """, (mint, symbol, mint, now, now, market_cap, holders, is_pump))
        self.conn.commit()

    def get_token_age(self, mint: str) -> Optional[float]:
        cursor = self.conn.execute(
            "SELECT first_seen FROM tokens_seen WHERE mint = ?", (mint,)
        )
        result = cursor.fetchone()
        if result:
            first_seen = datetime.fromisoformat(result[0])
            age_hours = (now_ts() - first_seen).total_seconds() / 3600
            return age_hours
        return None

    def mark_notification_sent(self, mint: str, notification_type: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications_sent (mint, notification_type, sent_at) VALUES (?, ?, ?)",
            (mint, notification_type, now_ts().isoformat())
        )
        self.conn.commit()

    def was_notification_sent(self, mint: str, notification_type: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM notifications_sent WHERE mint = ? AND notification_type = ?",
            (mint, notification_type)
        )
        return cursor.fetchone() is not None

    def add_price_point(self, mint: str, price: float, volume: float):
        self.conn.execute(
            "INSERT INTO price_history (mint, price, volume, timestamp) VALUES (?, ?, ?, ?)",
            (mint, price, volume, now_ts().isoformat())
        )
        self.conn.commit()

    def get_recent_price_data(self, mint: str, hours: int = 4) -> List[Tuple]:
        cutoff = (now_ts() - timedelta(hours=hours)).isoformat()
        cursor = self.conn.execute(
            "SELECT price, volume, timestamp FROM price_history WHERE mint = ? AND timestamp > ? ORDER BY timestamp",
            (mint, cutoff)
        )
        return cursor.fetchall()

    def update_pump_token(self, mint: str, name: str, symbol: str, market_cap: float, 
                         price: float, liquidity: float, holders: int):
        self.conn.execute("""
            INSERT OR REPLACE INTO pump_tokens 
            (mint, name, symbol, created_at, market_cap, price, liquidity, holders)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (mint, name, symbol, now_ts().isoformat(), market_cap, price, liquidity, holders))
        self.conn.commit()

# -----------------
# CLIENTES HTTP
# -----------------
class HttpClient:
    def __init__(self):
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            
    async def get_json(self, url: str, params: Dict = None, headers: Dict = None, timeout: int = 15):
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"HTTP {response.status} from {url}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {str(e)}")
            return None

# -----------------
# CLIENTES API
# -----------------
class DexScreenerClient:
    def __init__(self, http: HttpClient):
        self.http = http
        self.base_url = DEXSCREENER_API
    
    async def get_token_info(self, mint: str) -> Optional[Dict]:
        url = f"{self.base_url}/token/{mint}"
        return await self.http.get_json(url)
    
    async def search_tokens(self, query: str) -> Optional[Dict]:
        url = f"{self.base_url}/search"
        params = {"q": query}
        return await self.http.get_json(url, params=params)

class PumpFunClient:
    def __init__(self, http: HttpClient):
        self.http = http
        self.base_url = "https://frontend-api.pump.fun"
    
    async def get_trending_tokens(self) -> Optional[Dict]:
        """Obtiene tokens trending de Pump.fun"""
        url = f"{self.base_url}/trending"
        return await self.http.get_json(url)
    
    async def get_token_stats(self, mint: str) -> Optional[Dict]:
        url = f"{self.base_url}/tokens/{mint}"
        return await self.http.get_json(url)

# -----------------
# NOTIFICADOR TELEGRAM
# -----------------
class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        
    async def send_message(self, text: str, 
                         buttons: List[Tuple[str, str]] = None,
                         parse_mode: str = "HTML"):
        try:
            if not TELEGRAM_CHAT_ID:
                logger.error("TELEGRAM_CHAT_ID no configurado")
                return False
                
            markup = None
            if buttons:
                keyboard = []
                for label, url in buttons:
                    keyboard.append([InlineKeyboardButton(label, url=url)])
                markup = InlineKeyboardMarkup(keyboard)
                
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=parse_mode,
                reply_markup=markup,
                disable_web_page_preview=False
            )
            return True
        except Exception as e:
            logger.error(f"Error sending Telegram message: {str(e)}")
            return False

# -----------------
# MONITOR PRINCIPAL
# -----------------
class SolanaMonitor:
    def __init__(self):
        self.cache = Cache()
        self.bot_app = None
        self.notifier = None
        self.monitoring_active = False
        self.pump_monitor_task = None
        self.flat_monitor_task = None
        
    async def setup(self, bot_app: Application):
        self.bot_app = bot_app
        self.notifier = TelegramNotifier(bot_app)
        
    async def start_monitoring(self):
        """Inicia todos los monitores"""
        if self.monitoring_active:
            await self.notifier.send_message("⚠️ <b>El monitor ya está activo</b>")
            return
            
        self.monitoring_active = True
        
        if self.pump_monitor_task is None:
            self.pump_monitor_task = asyncio.create_task(self._pump_monitor_loop())
        if self.flat_monitor_task is None:
            self.flat_monitor_task = asyncio.create_task(self._flat_monitor_loop())
            
        logger.info("Todos los monitores iniciados")
        await self.notifier.send_message(
            "✅ <b>Monitor Solana iniciado</b>\n\n"
            f"• Pump.fun: ${PUMP_PRE_MIN:,} - ${PUMP_PRE_MAX:,} MC\n"
            f"• Flat tokens: >{FLAT_MIN_AGE_H}h edad\n"
            f"• Intervalo: {CHECK_INTERVAL}s"
        )
        
    async def stop_monitoring(self):
        """Detiene todos los monitores"""
        if not self.monitoring_active:
            return
            
        self.monitoring_active = False
        if self.pump_monitor_task:
            self.pump_monitor_task.cancel()
            self.pump_monitor_task = None
        if self.flat_monitor_task:
            self.flat_monitor_task.cancel()
            self.flat_monitor_task = None
            
        logger.info("Monitores detenidos")
        await self.notifier.send_message("🛑 <b>Monitor Solana detenido</b>")
        
    async def _pump_monitor_loop(self):
        """Loop principal para monitorizar tokens de Pump.fun"""
        backoff = 1
        while self.monitoring_active:
            try:
                async with HttpClient() as http:
                    await self._monitor_pump_tokens(http)
                await asyncio.sleep(CHECK_INTERVAL)
                backoff = 1  # Reset backoff on success
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en pump monitor: {str(e)}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                
    async def _flat_monitor_loop(self):
        """Loop principal para monitorizar tokens flat"""
        while self.monitoring_active:
            try:
                async with HttpClient() as http:
                    await self._check_flat_tokens(http)
                await asyncio.sleep(CHECK_INTERVAL * 2)  # Menos frecuente
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en flat monitor: {str(e)}")
                await asyncio.sleep(60)
                
    async def _monitor_pump_tokens(self, http: HttpClient):
        """Monitoriza tokens de Pump.fun cerca de graduación"""
        dexscreener = DexScreenerClient(http)
        
        # Buscar tokens de Pump.fun en DexScreener
        search_terms = ["pump.fun", "PUMP"]
        
        for term in search_terms:
            if not self.monitoring_active:
                break
                
            data = await dexscreener.search_tokens(term)
            if not data or 'pairs' not in data:
                continue
                
            for pair in data['pairs'][:25]:  # Limitar para no saturar
                if not self.monitoring_active:
                    break
                    
                try:
                    await self._process_pump_token(pair, dexscreener)
                    await asyncio.sleep(0.3)  # Rate limit
                except Exception as e:
                    logger.error(f"Error procesando token: {str(e)}")
    
    async def _process_pump_token(self, pair: Dict, dexscreener: DexScreenerClient):
        """Procesa un token individual de Pump.fun"""
        mint = pair.get('baseToken', {}).get('address')
        if not mint:
            return
            
        # Verificar si es token de Pump.fun
        if not self._is_pump_fun_token(pair):
            return
            
        symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
        market_cap = pair.get('marketCap')
        holders = pair.get('holders')
        
        if not market_cap:
            return
            
        # Actualizar cache
        self.cache.add_token_seen(mint, symbol, market_cap, holders, True)
        
        # Verificar si está cerca de graduación
        if PUMP_PRE_MIN <= market_cap <= PUMP_PRE_MAX:
            if not self.cache.was_notification_sent(mint, "pump_pregrad"):
                await self._send_pump_alert(pair, mint, symbol, market_cap, holders)
                self.cache.mark_notification_sent(mint, "pump_pregrad")
    
    def _is_pump_fun_token(self, pair: Dict) -> bool:
        """Determina si es un token de Pump.fun"""
        dex_id = pair.get('dexId', '').lower()
        pair_url = pair.get('url', '').lower()
        
        return ('pump' in dex_id or 'pump.fun' in pair_url)
    
    async def _send_pump_alert(self, pair: Dict, mint: str, symbol: str, 
                             market_cap: float, holders: int):
        """Envía alerta de token cerca de graduación"""
        price = pair.get('priceUsd', 0)
        price_change = pair.get('priceChange', {}).get('h24', 0)
        liquidity = pair.get('liquidity', {}).get('usd', 0)
        volume_24h = pair.get('volume', {}).get('h24', 0)
        
        message = (
            f"🚀 <b>PUMP.FUN - PRE-GRADUACIÓN</b> 🚀\n\n"
            f"<b>{safe_html(symbol)}</b>\n"
            f"• Market Cap: <b>${market_cap:,.0f}</b>\n"
            f"• Precio: ${price:.8f}\n"
            f"• 24h Change: {price_change:+.1f}%\n"
            f"• Holders: {holders or 'N/A'}\n"
            f"• Liquidez: ${liquidity:,.0f}\n"
            f"• Volumen 24h: ${volume_24h:,.0f}\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>"
        )
        
        buttons = [
            ("📊 DexScreener", f"https://dexscreener.com/solana/{mint}"),
            ("🔁 Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
            ("💧 Pump.fun", f"https://pump.fun/{mint}")
        ]
        
        await self.notifier.send_message(message, buttons)
        logger.info(f"Alert sent: {symbol} - MC: ${market_cap:,.0f}")
    
    async def _check_flat_tokens(self, http: HttpClient):
        """Busca tokens flat (antiguos y estables)"""
        dexscreener = DexScreenerClient(http)
        
        # Obtener tokens vistos recientemente que NO son de Pump.fun
        cursor = self.cache.conn.execute(
            "SELECT mint, symbol, first_seen FROM tokens_seen WHERE is_pump = FALSE"
        )
        tokens = cursor.fetchall()
        
        for mint, symbol, first_seen in tokens:
            if not self.monitoring_active:
                break
                
            try:
                token_data = await dexscreener.get_token_info(mint)
                if token_data and 'pairs' in token_data and token_data['pairs']:
                    pair = token_data['pairs'][0]
                    await self._process_flat_token(mint, symbol, pair, first_seen)
                    
                await asyncio.sleep(0.5)  # Rate limit
            except Exception as e:
                logger.error(f"Error procesando token flat {mint}: {str(e)}")
    
    async def _process_flat_token(self, mint: str, symbol: str, pair: Dict, first_seen: str):
        """Procesa token para detectar condición flat"""
        # Calcular edad del token
        first_seen_dt = datetime.fromisoformat(first_seen)
        age_hours = (now_ts() - first_seen_dt).total_seconds() / 3600
        
        # Solo tokens viejos
        if age_hours < FLAT_MIN_AGE_H:
            return
            
        price = pair.get('priceUsd')
        price_change_24h = pair.get('priceChange', {}).get('h24', 0)
        volume_24h = pair.get('volume', {}).get('h24', 0)
        market_cap = pair.get('marketCap')
        
        if not all([price, market_cap]):
            return
            
        # Guardar datos de precio para análisis
        self.cache.add_price_point(mint, price, volume_24h)
        
        # Verificar condición flat (precio estable)
        if abs(price_change_24h) <= FLAT_PRICE_CHANGE_THRESHOLD:
            if not self.cache.was_notification_sent(mint, "flat_token"):
                await self._send_flat_alert(mint, symbol, pair, age_hours, price_change_24h)
                self.cache.mark_notification_sent(mint, "flat_token")
    
    async def _send_flat_alert(self, mint: str, symbol: str, pair: Dict, 
                             age_hours: float, price_change: float):
        """Envía alerta de token flat"""
        price = pair.get('priceUsd', 0)
        market_cap = pair.get('marketCap', 0)
        volume_24h = pair.get('volume', {}).get('h24', 0)
        liquidity = pair.get('liquidity', {}).get('usd', 0)
        
        message = (
            f"📊 <b>TOKEN FLAT DETECTADO</b> 📊\n\n"
            f"<b>{safe_html(symbol)}</b>\n"
            f"• Edad: <b>{age_hours:.1f}h</b>\n"
            f"• Precio: ${price:.6f}\n"
            f"• 24h Change: {price_change:+.1f}%\n"
            f"• Market Cap: ${market_cap:,.0f}\n"
            f"• Volumen 24h: ${volume_24h:,.0f}\n"
            f"• Liquidez: ${liquidity:,.0f}\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>"
        )
        
        buttons = [
            ("📊 DexScreener", f"https://dexscreener.com/solana/{mint}"),
            ("🔁 Jupiter", f"https://jup.ag/swap/{mint}-SOL")
        ]
        
        await self.notifier.send_message(message, buttons)
        logger.info(f"Flat alert sent: {symbol} - Age: {age_hours:.1f}h")

# -----------------
# COMANDOS TELEGRAM
# -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    monitor = context.bot_data.get('monitor')
    user = update.effective_user
    
    welcome_msg = (
        f"👋 Hola {user.first_name}!\n\n"
        f"<b>Solana Monitor Bot</b>\n\n"
        f"<b>Funcionalidades:</b>\n"
        f"• 🚀 Alertas Pump.fun pre-graduación (${PUMP_PRE_MIN:,}-${PUMP_PRE_MAX:,} MC)\n"
        f"• 📊 Detección de tokens FLAT (>{FLAT_MIN_AGE_H}h edad)\n"
        f"• ⚡ Monitoreo en tiempo real\n\n"
        f"<b>Comandos:</b>\n"
        f"/start - Este mensaje\n"
        f"/status - Estado del monitor\n"
        f"/stop - Detener monitoreo\n"
        f"/start_monitor - Iniciar monitoreo\n"
        f"/set_pump_limits min max - Cambiar límites MC\n"
        f"/set_flat_age horas - Cambiar edad mínima flat"
    )
    
    await update.message.reply_html(welcome_msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /status"""
    monitor = context.bot_data.get('monitor')
    
    status_msg = (
        f"<b>Estado del Sistema</b>\n\n"
        f"• Monitor Activo: {monitor.monitoring_active if monitor else 'No inicializado'}\n"
        f"• Límites Pump: ${PUMP_PRE_MIN:,} - ${PUMP_PRE_MAX:,}\n"
        f"• Edad mínima Flat: {FLAT_MIN_AGE_H}h\n"
        f"• Intervalo: {CHECK_INTERVAL}s\n"
        f"• Chat ID: {TELEGRAM_CHAT_ID or 'No configurado'}\n"
        f"• WSS QuickNode: {'✅' if QUICKNODE_WSS_URL else '❌'}\n"
        f"• WSS Helius: {'✅' if HELIUS_WSS_URL else '❌'}"
    )
    
    await update.message.reply_html(status_msg)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /stop"""
    monitor = context.bot_data.get('monitor')
    if monitor:
        await monitor.stop_monitoring()
        await update.message.reply_html("🛑 <b>Monitor detenido</b>")
    else:
        await update.message.reply_html("❌ <b>Monitor no inicializado</b>")

async def start_monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start_monitor"""
    monitor = context.bot_data.get('monitor')
    if monitor:
        await monitor.start_monitoring()
        await update.message.reply_html("✅ <b>Monitor iniciado</b>")
    else:
        await update.message.reply_html("❌ <b>Monitor no inicializado</b>")

async def set_pump_limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /set_pump_limits min max"""
    if len(context.args) != 2:
        await update.message.reply_html("❌ <b>Uso:</b> /set_pump_limits min max")
        return
        
    try:
        global PUMP_PRE_MIN, PUMP_PRE_MAX
        PUMP_PRE_MIN = int(context.args[0])
        PUMP_PRE_MAX = int(context.args[1])
        
        await update.message.reply_html(
            f"✅ <b>Límites actualizados</b>\n"
            f"Nuevos límites: ${PUMP_PRE_MIN:,} - ${PUMP_PRE_MAX:,}"
        )
    except ValueError:
        await update.message.reply_html("❌ <b>Error:</b> Los valores deben ser números enteros")

async def set_flat_age_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /set_flat_age horas"""
    if len(context.args) != 1:
        await update.message.reply_html("❌ <b>Uso:</b> /set_flat_age horas")
        return
        
    try:
        global FLAT_MIN_AGE_H
        FLAT_MIN_AGE_H = int(context.args[0])
        
        await update.message.reply_html(
            f"✅ <b>Edad flat actualizada</b>\n"
            f"Nueva edad mínima: {FLAT_MIN_AGE_H}h"
        )
    except ValueError:
        await update.message.reply_html("❌ <b>Error:</b> El valor debe ser un número entero")

# -----------------
# FASTAPI APP
# -----------------
app = FastAPI()
monitor = SolanaMonitor()
bot_app = None

@app.on_event("startup")
async def startup_event():
    """Inicializa la aplicación al iniciar"""
    global bot_app
    
    # Verificar variables críticas
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    if not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_CHAT_ID no configurado")
        return

    # Inicializar bot de Telegram
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Configurar comandos
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("status", status_command))
    bot_app.add_handler(CommandHandler("stop", stop_command))
    bot_app.add_handler(CommandHandler("start_monitor", start_monitor_command))
    bot_app.add_handler(CommandHandler("set_pump_limits", set_pump_limits_command))
    bot_app.add_handler(CommandHandler("set_flat_age", set_flat_age_command))
    
    # Configurar monitor
    await monitor.setup(bot_app)
    bot_app.bot_data['monitor'] = monitor
    
    # Iniciar bot
    await bot_app.initialize()
    await bot_app.start()
    
    logger.info("✅ Aplicación iniciada correctamente")
    logger.info(f"📊 Configuración: Pump ${PUMP_PRE_MIN:,}-${PUMP_PRE_MAX:,}, Flat >{FLAT_MIN_AGE_H}h")

@app.on_event("shutdown")
async def shutdown_event():
    """Limpieza al cerrar la aplicación"""
    if bot_app:
        await monitor.stop_monitoring()
        await bot_app.stop()
        await bot_app.shutdown()
    logger.info("🛑 Aplicación detenida")

@app.get("/")
async def root():
    return {
        "status": "active", 
        "service": "Solana Monitor Bot",
        "monitoring": monitor.monitoring_active,
        "timestamp": now_ts().isoformat()
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "timestamp": now_ts().isoformat(),
        "monitoring": monitor.monitoring_active if monitor else False
    }

@app.post("/webhook")
async def webhook_handler(request: Request):
    """Manejador para webhooks externos"""
    try:
        data = await request.json()
        logger.info(f"Webhook recibido")
        return {"status": "received"}
    except Exception as e:
        logger.error(f"Error en webhook: {str(e)}")
        return {"status": "error"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
