# main.py - CONFIGURACIÓN COMPLETA PARA RAILWAY
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN RAILWAY =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")

# Parámetros ajustables via variables de entorno
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "50000.0"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000.0"))
FLAT_STD_THRESHOLD = float(os.getenv("FLAT_STD_THRESHOLD", "0.15"))
BREAKOUT_STEP = float(os.getenv("BREAKOUT_STEP", "15.0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("breakout_bot_railway")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=50))
flat_tokens = {}
watchlist = []
token_metadata = {}
whale_watchlist = set()

# ===================== API CLIENT =====================
class PriceAPI:
    def __init__(self):
        self.session = None
        self.base_url = DEXSCREENER_API
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def get_token_price(self, token_address: str):
        """Obtiene precio real desde DexScreener"""
        try:
            session = await self.get_session()
            url = f"{self.base_url}/tokens/{token_address}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        pair = data['pairs'][0]
                        return {
                            'price': float(pair.get('priceUsd', 0)),
                            'volume24h': float(pair.get('volume', {}).get('h24', 0)),
                            'liquidity': float(pair.get('liquidity', {}).get('usd', 0)),
                            'price_change24h': float(pair.get('priceChange', {}).get('h24', 0)),
                            'dex': pair.get('dexId'),
                            'pair_address': pair.get('pairAddress')
                        }
            return None
        except Exception as e:
            logger.debug(f"Error obteniendo precio para {token_address}: {e}")
            return None

price_api = PriceAPI()

# ===================== FUNCIONES PRINCIPALES =====================
def calculate_metrics(hist):
    """Calcula métricas para detección de tokens planos"""
    if len(hist) < 8:
        return None
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 2:
        return None
        
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
            returns.append(ret)
    
    if not returns:
        return None
        
    return {
        'std_dev': pstdev(returns),
        'max_return': max(returns),
        'min_return': min(returns),
        'avg_return': mean(returns)
    }

def is_flat_improved(hist):
    """Detección mejorada de tokens planos"""
    metrics = calculate_metrics(hist)
    if not metrics:
        return False
        
    return (metrics['std_dev'] < FLAT_STD_THRESHOLD and 
            abs(metrics['max_return']) < 0.5 and 
            abs(metrics['min_return']) < 0.5 and
            abs(metrics['avg_return']) < 0.1)

# ===================== MONITOR PRINCIPAL =====================
async def update_token_prices():
    """Actualiza precios periódicamente"""
    while True:
        try:
            if watchlist:
                for token_addr in watchlist[:30]:  # Límite para no exceder rate limits
                    data = await price_api.get_token_price(token_addr)
                    if data and data.get('price', 0) > 0:
                        price_histories[token_addr].append({
                            "ts": time.time(),
                            "price": data['price']
                        })
                        token_metadata[token_addr] = {
                            'volume': data.get('volume24h', 0),
                            'liquidity': data.get('liquidity', 0),
                            'last_updated': time.time()
                        }
            await asyncio.sleep(30)  # Actualizar cada 30 segundos
        except Exception as e:
            logger.error(f"Error actualizando precios: {e}")
            await asyncio.sleep(60)

async def helius_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Monitor principal de Helius"""
    if not HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurada")
        return

    logger.info("🚀 Iniciando monitor Helius en Railway...")
    
    # Iniciar actualizador de precios
    asyncio.create_task(update_token_prices())

    subscription_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [{"vote": False, "failed": False}],
    }

    while True:
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=30) as ws:
                await ws.send(json.dumps(subscription_msg))
                logger.info("✅ Conectado a Helius WebSocket")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        tx = data.get("params", {}).get("result", {})
                        
                        if not tx:
                            continue

                        # Extraer tokens de la transacción
                        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                        for key in account_keys:
                            if len(key) == 44:  # Dirección de token
                                await process_token(key, context)
                                
                    except Exception as e:
                        logger.debug(f"Error procesando transacción: {e}")

        except Exception as e:
            logger.error(f"Error WebSocket: {e}. Reconectando en 10s...")
            await asyncio.sleep(10)

async def process_token(token_addr: str, context: ContextTypes.DEFAULT_TYPE):
    """Procesa un token individual"""
    try:
        # Añadir a watchlist si no existe
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 100:
                watchlist.pop(0)
        
        # Obtener datos actuales
        token_data = await price_api.get_token_price(token_addr)
        if not token_data:
            return
            
        # Filtrar por volumen y liquidez
        if (token_data.get('liquidity', 0) < MIN_LIQUIDITY or 
            token_data.get('volume24h', 0) < MIN_VOLUME_USD):
            return
        
        current_price = token_data['price']
        if current_price <= 0:
            return
        
        # Actualizar historial
        hist = price_histories[token_addr]
        hist.append({"ts": time.time(), "price": current_price})
        
        # Detectar tokens planos
        if token_addr not in flat_tokens and is_flat_improved(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0)
            }
            logger.info(f"📊 Token plano detectado: {token_addr}")

        # Detectar breakout
        if token_addr in flat_tokens:
            base_price = flat_tokens[token_addr]["first_price"]
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = flat_tokens[token_addr]["max_alert"]
                
                if current_pct >= last_alert + BREAKOUT_STEP:
                    flat_tokens[token_addr]["max_alert"] = current_pct
                    await send_breakout_alert(context, token_addr, current_pct, token_data)

    except Exception as e:
        logger.error(f"Error procesando token {token_addr}: {e}")

async def send_breakout_alert(context, token_addr, breakout_pct, token_data):
    """Envía alertas de breakout"""
    try:
        emoji = "🚀" if breakout_pct > 20 else "📈"
        risk_level = "ALTO" if breakout_pct > 40 else "MEDIO" if breakout_pct > 20 else "BAJO"
        
        msg = (
            f"{emoji} *BREAKOUT DETECTADO* 🚨\n\n"
            f"*Token:* `{token_addr}`\n"
            f"*Cambio:* {breakout_pct:.2f}%\n"
            f"*Precio Actual:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Nivel de Riesgo:* {risk_level}\n\n"
            f"{link_block(token_addr)}"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
        
        logger.info(f"📈 Alerta enviada: {token_addr} +{breakout_pct:.1f}%")
        
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")

def link_block(addr):
    """Bloque de enlaces de verificación"""
    return (
        "🔍 *Verificación Rápida:*\n"
        f"• [DexScreener](https://dexscreener.com/solana/{addr})\n"
        f"• [Birdeye](https://birdeye.so/token/{addr}?chain=solana)\n"
        f"• [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"• [Jupiter](https://jup.ag/swap/SOL-{addr})\n"
        f"• [Solscan](https://solscan.io/token/{addr})"
    )

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    
    welcome_msg = (
        "🤖 *Breakout Bot - Railway Edition* 🚀\n\n"
        "✅ *Configuración actual:*\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Sensibilidad: {BREAKOUT_STEP}%\n\n"
        "📊 *Comandos:*\n"
        "• `/cazar` - Iniciar monitoreo\n"
        "• `/status` - Estado del sistema\n"
        "• `/ultimos` - Últimos tokens\n"
        "• `/planos` - Tokens planos\n"
        "• `/parar` - Detener monitoreo"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "task" in context.bot_data:
        await update.message.reply_text("⚙️ Ya está monitoreando tokens.")
        return
    await update.message.reply_text("🎯 Iniciando monitor en Railway...")
    context.bot_data["task"] = asyncio.create_task(helius_monitor(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.bot_data.pop("task", None)
    if task:
        task.cancel()
    await update.message.reply_text("🛑 Monitoreo detenido.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 Tokens observados: {len(price_histories)}\n"
        f"📈 Tokens planos: {len(flat_tokens)}\n"
        f"🔔 Alertas emitidas: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}"
    )
    await update.message.reply_text(msg)

async def cmd_ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("📭 No hay tokens vigilados todavía.")
        return
    msg = "👁‍🗨 *Últimos tokens vigilados:*\n\n"
    for i, addr in enumerate(reversed(watchlist[-10:]), 1):
        msg += f"{i}. `{addr}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados.")
        return
    msg = "📊 *Tokens Planos Detectados:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:10], 1):
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        msg += f"{i}. `{addr}` - desde {since}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===================== MAIN =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cazar", cmd_cazar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ultimos", cmd_ultimos))
    app.add_handler(CommandHandler("parar", cmd_parar))
    app.add_handler(CommandHandler("planos", cmd_planos))
    
    logger.info("🚀 Breakout Bot iniciado en Railway")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
