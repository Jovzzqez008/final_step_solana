# bot_corregido.py - VERSIÓN QUE SÍ DETECTA TOKENS
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")

# Parámetros optimizados
FLAT_STD_THRESHOLD = 0.2
BREAKOUT_STEP = 12.0
MIN_VOLUME_USD = 10000.0
MIN_LIQUIDITY = 5000.0
MIN_SAMPLES = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("breakout_bot_fixed")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=30))
flat_tokens = {}
watchlist = []
token_metadata = {}
bot_active = False

# ===================== API CLIENT MEJORADO =====================
class PriceAPI:
    def __init__(self):
        self.session = None
        self.request_count = 0
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def get_token_price(self, token_address: str):
        """Obtiene precio REAL desde DexScreener"""
        try:
            self.request_count += 1
            session = await self.get_session()
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
            
            async with session.get(url, timeout=8) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        pair = data['pairs'][0]
                        
                        # Validar que el token sea real y tenga datos
                        price = float(pair.get('priceUsd', 0))
                        volume = float(pair.get('volume', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        
                        # Solo retornar si es un token válido
                        if price > 0 and volume >= MIN_VOLUME_USD and liquidity >= MIN_LIQUIDITY:
                            return {
                                'price': price,
                                'volume24h': volume,
                                'liquidity': liquidity,
                                'price_change24h': float(pair.get('priceChange', {}).get('h24', 0)),
                                'dex': pair.get('dexId'),
                                'pair_address': pair.get('pairAddress'),
                                'valid': True
                            }
            
            return None
        except Exception as e:
            logger.debug(f"Error obteniendo precio para {token_address}: {e}")
            return None

price_api = PriceAPI()

# ===================== DETECCIÓN MEJORADA =====================
def is_flat(hist):
    """Detección mejorada de tokens planos"""
    if len(hist) < MIN_SAMPLES:
        return False
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 3:
        return False
        
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
            returns.append(ret)
    
    if not returns:
        return False
        
    sd = pstdev(returns) if len(returns) > 1 else 0
    max_abs = max(abs(x) for x in returns) if returns else 0
    
    return sd < FLAT_STD_THRESHOLD and max_abs < 1.0

# ===================== PROCESAMIENTO DE TRANSACCIONES MEJORADO =====================
async def extract_tokens_from_transaction(tx_data):
    """Extrae tokens REALES de una transacción de Helius"""
    tokens_found = set()
    
    try:
        # Método 1: Buscar en accountKeys
        account_keys = tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])
        for key in account_keys:
            if isinstance(key, str) and len(key) >= 32:  # Más flexible con la longitud
                tokens_found.add(key)
        
        # Método 2: Buscar en meta información
        meta = tx_data.get("meta", {})
        if meta:
            # Buscar en preTokenBalances y postTokenBalances
            for balance_type in ["preTokenBalances", "postTokenBalances"]:
                balances = meta.get(balance_type, [])
                for balance in balances:
                    mint = balance.get("mint")
                    if mint and isinstance(mint, str):
                        tokens_found.add(mint)
        
        # Método 3: Buscar en logs
        logs = meta.get("logMessages", [])
        for log in logs:
            if isinstance(log, str) and "mint" in log.lower():
                # Intentar extraer dirección del log
                words = log.split()
                for word in words:
                    if len(word) >= 32 and len(word) <= 44:
                        tokens_found.add(word)
        
        return list(tokens_found)
        
    except Exception as e:
        logger.debug(f"Error extrayendo tokens: {e}")
        return []

async def process_real_token(token_addr: str, context: ContextTypes.DEFAULT_TYPE):
    """Procesa un token con datos REALES"""
    try:
        logger.info(f"🔍 Procesando token: {token_addr}")
        
        # Obtener datos REALES del token
        token_data = await price_api.get_token_price(token_addr)
        
        if not token_data or not token_data.get('valid'):
            logger.debug(f"Token no válido o sin datos: {token_addr}")
            return
        
        # Añadir a watchlist
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 50:
                watchlist.pop(0)
        
        current_price = token_data['price']
        hist = price_histories[token_addr]
        hist.append({
            "ts": time.time(), 
            "price": current_price,
            "volume": token_data.get('volume24h', 0)
        })
        
        logger.info(f"✅ Token válido: {token_addr} - Precio: ${current_price:.6f} - Vol: ${token_data.get('volume24h', 0):,.0f}")
        
        # Detectar tokens planos
        if token_addr not in flat_tokens and is_flat(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0),
                "history_length": len(hist)
            }
            logger.info(f"📊 TOKEN PLANO DETECTADO: {token_addr}")

        # Detectar breakout
        if token_addr in flat_tokens:
            base_price = flat_tokens[token_addr]["first_price"]
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = flat_tokens[token_addr]["max_alert"]
                
                if current_pct >= last_alert + BREAKOUT_STEP:
                    flat_tokens[token_addr]["max_alert"] = current_pct
                    await send_breakout_alert(context, token_addr, current_pct, token_data)
                    logger.info(f"🚀 BREAKOUT ALERTADO: {token_addr} +{current_pct:.1f}%")

    except Exception as e:
        logger.error(f"Error procesando token real {token_addr}: {e}")

# ===================== MONITOR HELIUS CORREGIDO =====================
async def helius_monitor_fixed(context: ContextTypes.DEFAULT_TYPE):
    """Monitor corregido que SÍ detecta tokens"""
    if not HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurada")
        return

    logger.info("🎯 Iniciando monitor CORREGIDO...")
    
    # Suscripción más específica para tokens
    subscription_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {
                "accountInclude": [
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # Programa de tokens de Solana
                ],
                "vote": False,
                "failed": False
            }
        ],
    }

    while bot_active:
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=30, ping_timeout=10) as ws:
                await ws.send(json.dumps(subscription_msg))
                logger.info("✅ Conectado a Helius WebSocket - Buscando tokens...")

                async for message in ws:
                    if not bot_active:
                        break
                        
                    try:
                        data = json.loads(message)
                        tx = data.get("params", {}).get("result", {})
                        
                        if not tx:
                            continue
                        
                        # EXTRAER TOKENS de la transacción
                        tokens = await extract_tokens_from_transaction(tx)
                        
                        if tokens:
                            logger.info(f"📨 Transacción con {len(tokens)} tokens potenciales")
                            
                            # Procesar cada token encontrado
                            for token_addr in tokens[:5]:  # Límite para no saturar
                                if bot_active:
                                    await process_real_token(token_addr, context)
                        
                    except Exception as e:
                        logger.debug(f"Error procesando mensaje: {e}")

        except Exception as e:
            if bot_active:
                logger.error(f"Error WebSocket: {e}. Reconectando en 5s...")
                await asyncio.sleep(5)

# ===================== ALERTAS =====================
async def send_breakout_alert(context, token_addr, breakout_pct, token_data):
    """Envía alertas de breakout"""
    try:
        short_addr = token_addr[:8] + "..." + token_addr[-8:]
        
        msg = (
            f"🚀 *BREAKOUT DETECTADO* 🎯\n\n"
            f"*Token:* `{short_addr}`\n"
            f"*Cambio:* +{breakout_pct:.2f}%\n"
            f"*Precio:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n\n"
            f"🔍 *Verificación:*\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token_addr})\n"
            f"- [Birdeye](https://birdeye.so/token/{token_addr}?chain=solana)\n"
            f"- [Jupiter](https://jup.ag/swap/SOL-{token_addr})"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
        logger.info(f"📤 Alerta enviada: {short_addr} +{breakout_pct:.1f}%")
        
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Breakout Bot - CORREGIDO* 🚀\n\n"
        "✅ *Ahora SÍ detecta tokens reales:*\n"
        "• Precios reales desde DexScreener\n"
        "• Filtros por volumen y liquidez\n"
        "• Detección de tokens planos\n"
        "• Alertas de breakout\n\n"
        f"⚙️ *Configuración:*\n"
        f"• Breakout: +{BREAKOUT_STEP}%\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n\n"
        "📊 *Comandos:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener\n"
        "• /status - Estado\n"
        "• /tokens - Ver tokens\n"
        "• /planos - Tokens planos"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Ya está monitoreando.")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🎯 *INICIANDO DETECCIÓN DE TOKENS*\n\n"
        "🔍 Buscando tokens reales en Solana...\n"
        "✅ Usando datos REALES de DexScreener\n"
        "📊 Filtros activos por volumen/liquidez",
        parse_mode="Markdown"
    )
    
    # Iniciar el monitor CORREGIDO
    asyncio.create_task(helius_monitor_fixed(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text("🛑 Monitoreo detenido.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del sistema"""
    status_msg = (
        f"🤖 *ESTADO DEL BOT*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"📊 Tokens observados: {len(price_histories)}\n"
        f"📈 Tokens planos: {len(flat_tokens)}\n"
        f"👁️ En watchlist: {len(watchlist)}\n"
        f"📞 Requests API: {price_api.request_count}\n\n"
        f"💡 Comandos: /cazar /parar /tokens /planos"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens detectados"""
    if not watchlist:
        await update.message.reply_text("📭 No hay tokens en la lista.")
        return
        
    msg = "👁️ *Últimos Tokens Detectados:*\n\n"
    for i, addr in enumerate(reversed(watchlist[-10:]), 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        status = "📊" if addr in flat_tokens else "🔍"
        msg += f"{i}. `{short_addr}` {status}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados.")
        return
        
    msg = "📊 *Tokens Planos Detectados:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:10], 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        samples = info.get("history_length", 0)
        alert_pct = info.get("max_alert", 0)
        status = f"🚀 +{alert_pct:.1f}%" if alert_pct > 0 else "⏳ Plano"
        msg += f"{i}. `{short_addr}`\n   ⏰ {since} | 📈 {status} | 📊 {samples} datos\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===================== MAIN =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cazar", cmd_cazar))
    app.add_handler(CommandHandler("parar", cmd_parar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tokens", cmd_tokens))
    app.add_handler(CommandHandler("planos", cmd_planos))
    
    logger.info("🚀 Breakout Bot Corregido - Listo para detectar tokens REALES")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
