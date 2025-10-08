# bot_combinado.py - DETECTA TOKENS PLANOS + RECUPERACIONES DESDE DIPS
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

# PARÁMETROS PARA AMBAS ESTRATEGIAS
# Estrategia 1: Tokens planos + breakout
FLAT_STD_THRESHOLD = 0.15
FLAT_MAX_ABS_RETURN = 0.5
BREAKOUT_STEP = 15.0

# Estrategia 2: Recuperación desde dips
MIN_DIP_PERCENT = -40.0
MAX_DIP_PERCENT = -30.0
RECOVERY_THRESHOLD = 5.0

# Filtros generales
MIN_VOLUME_USD = 50000.0
MIN_LIQUIDITY = 10000.0
PRICE_UPDATE_INTERVAL = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("combo_bot")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=30))
flat_tokens = {}           # Estrategia 1: tokens planos
dip_tokens = {}            # Estrategia 2: tokens en dip
watchlist = []
token_metadata = {}
bot_active = False         # Control global del bot

# ===================== DETECCIÓN ESTRATEGIA 1: TOKENS PLANOS =====================
def calculate_metrics(hist):
    """Calcula métricas para tokens planos"""
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

def is_flat(hist):
    """Detecta si un token está plano"""
    metrics = calculate_metrics(hist)
    if not metrics:
        return False
        
    return (metrics['std_dev'] < FLAT_STD_THRESHOLD and 
            abs(metrics['max_return']) < FLAT_MAX_ABS_RETURN and 
            abs(metrics['min_return']) < FLAT_MAX_ABS_RETURN and
            abs(metrics['avg_return']) < 0.1)

# ===================== DETECCIÓN ESTRATEGIA 2: RECUPERACIÓN DESDE DIPS =====================
def is_in_dip_zone(price_change_24h):
    """Verifica si está en -30% a -40%"""
    return MIN_DIP_PERCENT <= price_change_24h <= MAX_DIP_PERCENT

def is_recovering(hist):
    """Detecta recuperación reciente"""
    if len(hist) < 3:
        return False, 0
    
    recent_prices = [point["price"] for point in hist[-3:]]
    
    if len(recent_prices) >= 2:
        price_changes = []
        for i in range(1, len(recent_prices)):
            if recent_prices[i-1] > 0:
                change = (recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1] * 100
                price_changes.append(change)
        
        if price_changes and all(change > 0 for change in price_changes):
            total_recovery = sum(price_changes)
            return True, total_recovery
    
    return False, 0

# ===================== MONITOR PRINCIPAL =====================
class PriceAPI:
    def __init__(self):
        self.session = None
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def get_token_price(self, token_address: str):
        try:
            session = await self.get_session()
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
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
            logger.debug(f"Error obteniendo precio: {e}")
            return None

price_api = PriceAPI()

async def update_token_prices(context: ContextTypes.DEFAULT_TYPE):
    """Actualiza precios y verifica ambas estrategias"""
    while bot_active:
        try:
            if watchlist:
                for token_addr in list(watchlist)[:30]:  # Copia para evitar modificación durante iteración
                    if not bot_active:  # Verificar si el bot sigue activo
                        break
                        
                    data = await price_api.get_token_price(token_addr)
                    if data and data.get('price', 0) > 0:
                        # Actualizar historial
                        hist = price_histories[token_addr]
                        hist.append({
                            "ts": time.time(),
                            "price": data['price']
                        })
                        
                        # ESTRATEGIA 1: Verificar tokens planos
                        await check_flat_tokens(token_addr, data, hist, context)
                        
                        # ESTRATEGIA 2: Verificar recuperación desde dips
                        await check_dip_recovery(token_addr, data, hist, context)
                        
            await asyncio.sleep(PRICE_UPDATE_INTERVAL)
        except Exception as e:
            logger.error(f"Error actualizando precios: {e}")
            await asyncio.sleep(30)

async def check_flat_tokens(token_addr, token_data, hist, context):
    """Estrategia 1: Detectar tokens planos y breakouts"""
    current_price = token_data['price']
    
    # Si no está en flat_tokens pero ahora es plano, añadirlo
    if token_addr not in flat_tokens and is_flat(hist):
        flat_tokens[token_addr] = {
            "first_price": current_price,
            "flat_since": time.time(),
            "max_alert": 0,
            "volume": token_data.get('volume24h', 0),
            "liquidity": token_data.get('liquidity', 0)
        }
        logger.info(f"📊 Token plano detectado: {token_addr}")

    # Si está en flat_tokens, verificar breakout
    if token_addr in flat_tokens:
        base_price = flat_tokens[token_addr]["first_price"]
        if base_price > 0:
            current_pct = (current_price - base_price) / base_price * 100
            last_alert = flat_tokens[token_addr]["max_alert"]
            
            if current_pct >= last_alert + BREAKOUT_STEP:
                flat_tokens[token_addr]["max_alert"] = current_pct
                await send_breakout_alert(context, token_addr, current_pct, token_data)

async def check_dip_recovery(token_addr, token_data, hist, context):
    """Estrategia 2: Detectar recuperación desde dips"""
    current_price = token_data['price']
    price_change_24h = token_data.get('price_change24h', 0)
    
    # Verificar si está en dip y añadir a monitoreo
    if token_addr not in dip_tokens and is_in_dip_zone(price_change_24h):
        dip_tokens[token_addr] = {
            'dip_price': current_price,
            'dip_since': time.time(),
            'price_change_24h': price_change_24h,
            'last_alert': 0,
            'volume': token_data.get('volume24h', 0),
            'liquidity': token_data.get('liquidity', 0)
        }
        logger.info(f"📉 Token en dip detectado: {token_addr} ({price_change_24h:.1f}%)")

    # Verificar recuperación en tokens en dip
    if token_addr in dip_tokens:
        is_recovering_flag, recovery_pct = is_recovering(hist)
        if is_recovering_flag and recovery_pct >= RECOVERY_THRESHOLD:
            # Solo alertar si no hemos alertado recientemente (evitar spam)
            last_alert = dip_tokens[token_addr].get('last_alert', 0)
            if time.time() - last_alert > 300:  # 5 minutos entre alertas
                dip_info = dip_tokens[token_addr]
                total_recovery = ((current_price - dip_info['dip_price']) / dip_info['dip_price']) * 100
                
                await send_recovery_alert(context, token_addr, token_data, recovery_pct, total_recovery, dip_info)
                dip_tokens[token_addr]['last_alert'] = time.time()

async def helius_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Monitor principal de Helius"""
    if not HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurada")
        return

    logger.info("🎯 Iniciando monitor combinado...")
    
    # Iniciar actualizador de precios
    asyncio.create_task(update_token_prices(context))

    subscription_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [{"vote": False, "failed": False}],
    }

    while bot_active:
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=30) as ws:
                await ws.send(json.dumps(subscription_msg))
                logger.info("✅ Conectado a Helius WebSocket")

                async for message in ws:
                    if not bot_active:
                        break
                    try:
                        data = json.loads(message)
                        tx = data.get("params", {}).get("result", {})
                        
                        if not tx:
                            continue

                        # Extraer tokens de la transacción
                        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                        for key in account_keys:
                            if len(key) == 44:  # Dirección de token
                                await process_token(key)
                                
                    except Exception as e:
                        logger.debug(f"Error procesando transacción: {e}")

        except Exception as e:
            if bot_active:  # Solo reconectar si el bot sigue activo
                logger.error(f"Error WebSocket: {e}. Reconectando en 10s...")
                await asyncio.sleep(10)

async def process_token(token_addr: str):
    """Procesa un nuevo token"""
    try:
        # Añadir a watchlist si no existe
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 100:
                watchlist.pop(0)
                
    except Exception as e:
        logger.error(f"Error procesando token {token_addr}: {e}")

# ===================== ALERTAS MEJORADAS =====================
async def send_breakout_alert(context, token_addr, breakout_pct, token_data):
    """Alerta para tokens planos que hacen breakout"""
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
            f"💡 *Estrategia:* Token plano con breakout alcista\n\n"
            f"{link_block(token_addr)}"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
        
        logger.info(f"📈 Alerta breakout: {token_addr} +{breakout_pct:.1f}%")
        
    except Exception as e:
        logger.error(f"Error enviando alerta breakout: {e}")

async def send_recovery_alert(context, token_addr, token_data, recovery_pct, total_recovery, dip_info):
    """Alerta para tokens en dip que se recuperan"""
    try:
        emoji = "🚀" if recovery_pct > 10 else "📈"
        
        msg = (
            f"{emoji} *RECUPERACIÓN DESDE DIP DETECTADA* 🎯\n\n"
            f"*Token:* `{token_addr}`\n"
            f"*Recuperación reciente:* +{recovery_pct:.2f}%\n"
            f"*Recuperación total desde dip:* +{total_recovery:.2f}%\n"
            f"*Caída original 24h:* {dip_info['price_change_24h']:.1f}%\n"
            f"*Precio Actual:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n\n"
            f"💡 *Estrategia:* Posible entrada temprana en recuperación\n\n"
            f"{link_block(token_addr)}"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
        
        logger.info(f"🎯 Alerta recuperación: {token_addr} +{recovery_pct:.1f}%")
        
    except Exception as e:
        logger.error(f"Error enviando alerta recuperación: {e}")

def link_block(addr):
    return (
        "🔍 *Verificación Rápida:*\n"
        f"• [DexScreener](https://dexscreener.com/solana/{addr})\n"
        f"• [Birdeye](https://birdeye.so/token/{addr}?chain=solana)\n"
        f"• [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"• [Jupiter](https://jup.ag/swap/SOL-{addr})\n"
        f"• [Solscan](https://solscan.io/token/{addr})"
    )

# ===================== COMANDOS TELEGRAM MEJORADOS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando start mejorado con botones de acción"""
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Dual Strategy Bot - Mejorado* 🚀\n\n"
        "✅ *Dos estrategias activas:*\n"
        "1. 📊 *Tokens Planos + Breakout*\n"
        "   - Detecta tokens con precio estable\n"
        "   - Alerta cuando rompen +15%\n\n"
        "2. 📉 *Recuperación desde Dips*\n"  
        "   - Tokens en -30% a -40%\n"
        "   - Alerta cuando empiezan a recuperarse\n\n"
        f"⚙️ *Configuración actual:*\n"
        f"• Breakout: {BREAKOUT_STEP}%\n"
        f"• Dips: {MIN_DIP_PERCENT}% a {MAX_DIP_PERCENT}%\n"
        f"• Recovery: {RECOVERY_THRESHOLD}%\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n\n"
        "📊 *Comandos rápidos:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener monitoreo\n"
        "• /status - Estado del sistema\n"
        "• /planos - Ver tokens planos\n"
        "• /dips - Ver tokens en dip\n"
        "• /limpiar - Limpiar listas\n"
        "• /config - Ver configuración"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ El bot ya está monitoreando activamente.")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🎯 *INICIANDO MONITOREO COMBINADO*\n\n"
        "🔍 Buscando:\n"
        "• Tokens planos con breakout potencial\n" 
        "• Tokens en dip con recuperación temprana\n\n"
        "📡 Conectando a Helius...",
        parse_mode="Markdown"
    )
    
    # Iniciar el monitor en segundo plano
    asyncio.create_task(helius_monitor(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text(
        "🛑 *MONITOREO DETENIDO*\n\n"
        "El bot ha dejado de buscar nuevas señales.\n"
        "Usa /cazar para reiniciar.",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estado completo del sistema"""
    status_msg = (
        f"🤖 *ESTADO DEL SISTEMA*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"📊 Tokens observados: {len(price_histories)}\n"
        f"📈 Tokens planos: {len(flat_tokens)}\n" 
        f"📉 Tokens en dip: {len(dip_tokens)}\n"
        f"👁️ En watchlist: {len(watchlist)}\n\n"
        f"💡 Usa /cazar para iniciar o /parar para detener"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos detectados"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados aún.")
        return
        
    msg = "📊 *Tokens Planos Detectados:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:10], 1):
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        alert_pct = info.get("max_alert", 0)
        status = f"🚀 +{alert_pct:.1f}%" if alert_pct > 0 else "⏳ Esperando"
        msg += f"{i}. `{addr}`\n   ⏰ {since} | {status}\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_dips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens en dip monitoreados"""
    if not dip_tokens:
        await update.message.reply_text("📉 No hay tokens en dip monitoreados aún.")
        return
        
    msg = "📉 *Tokens en Dip Monitoreados:*\n\n"
    for i, (addr, info) in enumerate(list(dip_tokens.items())[:10], 1):
        since = datetime.fromtimestamp(info["dip_since"]).strftime("%H:%M")
        dip_pct = info.get('price_change_24h', 0)
        alerted = "🔔" if info.get('last_alert', 0) > 0 else "⏳"
        msg += f"{i}. `{addr}`\n   📉 {dip_pct:.1f}% | ⏰ {since} | {alerted}\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra últimos tokens vistos"""
    if not watchlist:
        await update.message.reply_text("📭 No hay tokens en la lista de vigilancia.")
        return
        
    msg = "👁️ *Últimos Tokens Detectados:*\n\n"
    for i, addr in enumerate(reversed(watchlist[-10:]), 1):
        # Verificar si está en alguna lista especial
        status = ""
        if addr in flat_tokens:
            status = "📊"
        elif addr in dip_tokens:
            status = "📉"
        msg += f"{i}. `{addr}` {status}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpia las listas"""
    flat_tokens.clear()
    dip_tokens.clear()
    watchlist.clear()
    price_histories.clear()
    
    await update.message.reply_text(
        "🧹 *LISTAS LIMPIAS*\n\n"
        "Se han limpiado todas las listas de tokens.\n"
        "El bot empezará desde cero.",
        parse_mode="Markdown"
    )

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la configuración actual"""
    config_msg = (
        f"⚙️ *CONFIGURACIÓN ACTUAL*\n\n"
        f"**Estrategia 1 - Breakout:**\n"
        f"• Sensibilidad: {BREAKOUT_STEP}%\n"
        f"• Desviación máxima: {FLAT_STD_THRESHOLD}%\n"
        f"• Movimiento máximo: {FLAT_MAX_ABS_RETURN}%\n\n"
        f"**Estrategia 2 - Recuperación:**\n"
        f"• Rango de dip: {MIN_DIP_PERCENT}% a {MAX_DIP_PERCENT}%\n"
        f"• Umbral recuperación: {RECOVERY_THRESHOLD}%\n\n"
        f"**Filtros generales:**\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Actualización: {PRICE_UPDATE_INTERVAL}s"
    )
    await update.message.reply_text(config_msg, parse_mode="Markdown")

# ===================== MAIN MEJORADO =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Registrar todos los comandos
    commands = [
        ("start", cmd_start),
        ("cazar", cmd_cazar),
        ("parar", cmd_parar),
        ("status", cmd_status),
        ("planos", cmd_planos),
        ("dips", cmd_dips),
        ("ultimos", cmd_ultimos),
        ("limpiar", cmd_limpiar),
        ("config", cmd_config),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🚀 Bot Combinado Iniciado - 2 Estrategias")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
