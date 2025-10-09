import os
import asyncio
import json
import time
import logging
from datetime import datetime, timedelta
from statistics import pstdev, mean
from collections import deque, defaultdict

import aiohttp
import asyncpg
import websockets
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ------------------ CONFIGURACIÓN ------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")

# Configuración para tokens flat
MIN_FLAT_DURATION_HOURS = 1
MAX_VOLATILITY = 1.5  # Desviación estándar máxima en porcentaje
MAX_AVG_VOLUME_PER_CANDLE = 150  # Volumen promedio máximo por vela (en USD)
MIN_LOW_VOLUME_CANDLES = 6  # Mínimo de velas con volumen bajo
CANDLES_TO_ANALYZE = 24  # Número de velas a analizar (velas de 5 minutos -> 2 horas)

# Configuración para Pump.fun
PUMPFUN_PROGRAM_ID = os.getenv("PUMPFUN_PROGRAM_ID", "pumpfun1Mt11111111111111111111111111111111")
PUMP_PRE_GRADUATION_THRESHOLD = 60000  # Alerta a $60k
PUMP_GRADUATION_TARGET = 69000  # Graduación a $69k

# Configuración de liquidez y volumen mínimos
MIN_LIQUIDITY = 30000
MIN_VOLUME_24H = 10000

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("pump_flat_bot")

# ------------------ BASE DE DATOS ------------------
class DatabaseManager:
    def __init__(self):
        self.pool = None

    async def init(self):
        if DATABASE_URL:
            self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            await self._create_tables()

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_tokens (
                    mint TEXT PRIMARY KEY,
                    alert_type TEXT,
                    first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                    last_alert TIMESTAMP WITH TIME ZONE DEFAULT now(),
                    alert_count INTEGER DEFAULT 1
                )
            ''')

    async def is_notified(self, mint: str, alert_type: str = None) -> bool:
        if not self.pool:
            return False
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT mint FROM notified_tokens WHERE mint = $1 AND alert_type = $2",
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow(
                    "SELECT mint FROM notified_tokens WHERE mint = $1",
                    mint
                )
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str):
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_tokens (mint, alert_type) 
                VALUES ($1, $2)
                ON CONFLICT (mint) 
                DO UPDATE SET 
                    last_alert = now(),
                    alert_count = notified_tokens.alert_count + 1
            ''', mint, alert_type)

db = DatabaseManager()

# ------------------ CLIENTES API ------------------
class APIClient:
    def __init__(self):
        self.session = None
        self.jupiter_base = JUPITER_BASE

    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def jupiter_request(self, endpoint: str):
        session = await self.get_session()
        url = f"{self.jupiter_base}{endpoint}" if endpoint.startswith("/") else f"{self.jupiter_base}/{endpoint}"
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Jupiter API error: {resp.status} for {url}")
                    return None
        except Exception as e:
            logger.error(f"Error in jupiter_request: {e}")
            return None

    async def get_jupiter_tokens(self):
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=80",
            "/tokens/v2/toptraded/1h?limit=80",
            "/tokens/v2/tag?query=verified",
            "/tokens/v2/recent?limit=50"
        ]
        all_tokens = []
        for endpoint in endpoints:
            data = await self.jupiter_request(endpoint)
            if data:
                all_tokens.extend(data)
        return all_tokens

    async def get_token_metadata(self, mint: str):
        return await self.jupiter_request(f"/tokens/v2/search?query={mint}")

    async def get_dexscreener_candles(self, mint: str, limit: int = 24):
        session = await self.get_session()
        try:
            search_url = f"{DEXSCREENER_API}/search?q={mint}"
            async with session.get(search_url, timeout=8) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return []
                pair_address = pairs[0].get('pairAddress')
                if not pair_address:
                    return []
                pair_url = f"{DEXSCREENER_API}/pairs/{pair_address}"
                async with session.get(pair_url, timeout=8) as pair_resp:
                    if pair_resp.status != 200:
                        return []
                    pair_data = await pair_resp.json()
                    candles = pair_data.get('candles', [])
                    normalized = []
                    for candle in candles[-limit:]:
                        if isinstance(candle, dict):
                            normalized.append({
                                'time': candle.get('timestamp', 0),
                                'open': float(candle.get('open', 0)),
                                'high': float(candle.get('high', 0)),
                                'low': float(candle.get('low', 0)),
                                'close': float(candle.get('close', 0)),
                                'volume': float(candle.get('volume', 0))
                            })
                    return normalized
        except Exception as e:
            logger.error(f"Error getting DexScreener candles for {mint}: {e}")
            return []

api_client = APIClient()

# ------------------ DETECCIÓN FLAT ------------------
class FlatDetector:
    async def analyze_flat_pattern(self, mint: str) -> dict:
        candles = await api_client.get_dexscreener_candles(mint, CANDLES_TO_ANALYZE)
        if len(candles) < 12:
            return {'is_flat': False, 'reason': 'insufficient_data'}

        # Calcular volatilidad
        prices = [c['close'] for c in candles if c['close'] > 0]
        if len(prices) < 8:
            return {'is_flat': False, 'reason': 'not_enough_prices'}

        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
                returns.append(ret)

        if not returns:
            return {'is_flat': False, 'reason': 'no_returns'}

        std_dev = pstdev(returns) if len(returns) > 1 else 100
        price_range = (max(prices) - min(prices)) / min(prices) * 100

        # Análisis de volumen
        volumes = [c['volume'] for c in candles]
        avg_volume = mean(volumes) if volumes else 0
        low_volume_count = sum(1 for v in volumes if v < 20)

        # Condiciones para flat
        is_flat = (
            std_dev < MAX_VOLATILITY and
            price_range < 3.0 and
            avg_volume < MAX_AVG_VOLUME_PER_CANDLE and
            low_volume_count >= MIN_LOW_VOLUME_CANDLES
        )

        if is_flat:
            estimated_hours = len(candles) * 5 / 60  # Asumiendo velas de 5min
            return {
                'is_flat': True,
                'duration_hours': estimated_hours,
                'volatility': std_dev,
                'price_range_pct': price_range,
                'avg_volume': avg_volume
            }
        else:
            return {'is_flat': False, 'reason': 'not_flat_pattern'}

flat_detector = FlatDetector()

# ------------------ MONITOR PUMP.FUN ------------------
class PumpFunMonitor:
    def __init__(self):
        self.active = False

    async def start_monitoring(self):
        self.active = True
        logger.info("Starting Pump.fun monitor...")
        if not HELIUS_WSS_URL:
            logger.error("HELIUS_WSS_URL not set")
            return

        while self.active:
            try:
                async with websockets.connect(HELIUS_WSS_URL) as ws:
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [PUMPFUN_PROGRAM_ID]},
                            {"commitment": "processed"}
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("Subscribed to Pump.fun logs")

                    while self.active:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(message)
                            await self._process_message(data)
                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")
                            break
            except Exception as e:
                logger.error(f"Pump.fun monitor connection error: {e}")
                await asyncio.sleep(5)

    async def _process_message(self, data):
        try:
            params = data.get('params', {})
            result = params.get('result', {})
            logs = result.get('value', {}).get('logs', [])
            log_text = "\n".join(logs)

            # Buscar market cap en los logs
            import re
            market_cap_match = re.search(r"market_cap\W*[:=]\W*(\d+[.,]?\d*)", log_text, re.IGNORECASE)
            if market_cap_match:
                market_cap = float(market_cap_match.group(1).replace(',', ''))
                if market_cap >= PUMP_PRE_GRADUATION_THRESHOLD:
                    # Intentar extraer el mint address
                    mint_match = re.search(r"mint\W*[:=]\W*([A-Za-z0-9]{32,44})", log_text, re.IGNORECASE)
                    if mint_match:
                        mint = mint_match.group(1)
                        if not await db.is_notified(mint, "PUMPFUN_PRE_GRAD"):
                            await alert_system.send_pumpfun_alert(mint, market_cap)
        except Exception as e:
            logger.error(f"Error in _process_message: {e}")

    def stop(self):
        self.active = False

pumpfun_monitor = PumpFunMonitor()

# ------------------ SISTEMA DE ALERTAS ------------------
class AlertSystem:
    def __init__(self):
        self.bot = None

    async def get_bot(self):
        if not self.bot and TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        return self.bot

    def format_links(self, mint: str) -> str:
        return (
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
        )

    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        if await db.is_notified(mint, "FLAT_DETECTED"):
            return

        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        volume24h = (token_data.get('stats24h', {}).get('buyVolume', 0) + 
                    token_data.get('stats24h', {}).get('sellVolume', 0))

        message = (
            f"🎯 *TOKEN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} — {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Liquidez:* ${liquidity:,.0f}\n"
            f"*Volumen 24h:* ${volume24h:,.0f}\n\n"
            f"*Detalles Flat:*\n"
            f"• Tiempo en flat: {flat_analysis['duration_hours']:.1f} horas\n"
            f"• Volatilidad: {flat_analysis['volatility']:.2f}%\n"
            f"• Rango de precio: {flat_analysis['price_range_pct']:.2f}%\n"
            f"• Volumen promedio: ${flat_analysis['avg_volume']:.2f}\n\n"
            f"🔗 *Enlaces:*\n{self.format_links(mint)}\n"
            f"💡 *Estrategia:* Token en acumulación - Posible breakout próximo"
        )

        await self._send_message(message)
        await db.mark_notified(mint, "FLAT_DETECTED")

    async def send_pumpfun_alert(self, mint: str, market_cap: float):
        if await db.is_notified(mint, "PUMPFUN_PRE_GRAD"):
            return

        message = (
            f"🚀 *PUMP.FUN - PRE-GRADUACIÓN* 🚀\n\n"
            f"*Mint:* `{mint}`\n"
            f"*Market Cap Actual:* ${market_cap:,.0f}\n"
            f"*Graduación en:* ${PUMP_GRADUATION_TARGET - market_cap:,.0f}\n\n"
            f"🔗 *Enlaces:*\n{self.format_links(mint)}\n"
            f"⚡ *Acción Inminente:* Liquidez se bloqueará automáticamente en ~${PUMP_GRADUATION_TARGET:,.0f}"
        )

        await self._send_message(message)
        await db.mark_notified(mint, "PUMPFUN_PRE_GRAD")

    async def _send_message(self, message: str):
        try:
            bot = await self.get_bot()
            if bot and TELEGRAM_CHAT_ID:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False
                )
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")

alert_system = AlertSystem()

# ------------------ SCANNER FLAT ------------------
class FlatScanner:
    def __init__(self):
        self.active = False

    async def start_scanning(self):
        self.active = True
        logger.info("Starting flat scanner...")

        while self.active:
            try:
                tokens = await api_client.get_jupiter_tokens()
                logger.info(f"Flat scanner: analyzing {len(tokens)} tokens")

                for token in tokens[:50]:  # Limitar para no saturar
                    if not self.active:
                        break

                    mint = token.get('id')
                    if not mint:
                        continue

                    # Filtros básicos de liquidez y volumen
                    liquidity = token.get('liquidity', 0)
                    volume24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                                token.get('stats24h', {}).get('sellVolume', 0))
                    if liquidity < MIN_LIQUIDITY or volume24h < MIN_VOLUME_24H:
                        continue

                    flat_analysis = await flat_detector.analyze_flat_pattern(mint)
                    if flat_analysis['is_flat']:
                        logger.info(f"Flat token detected: {token.get('symbol')} - {mint}")
                        await alert_system.send_flat_alert(mint, token, flat_analysis)

                    await asyncio.sleep(1)  # Rate limiting

                await asyncio.sleep(60)  # Esperar 1 minuto entre scans

            except Exception as e:
                logger.error(f"Flat scanner error: {e}")
                await asyncio.sleep(30)

    def stop(self):
        self.active = False

flat_scanner = FlatScanner()

# ------------------ COMANDOS TELEGRAM ------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *BOT PUMP.FUN + FLAT DETECTOR* 🚀\n\n"
        "🎯 *OBJETIVO:*\n"
        "• Tokens FLAT: 1+ hora en línea recta\n"
        "• Pump.fun: Alerta pre-graduación ($60k+)\n\n"
        "📋 *COMANDOS:*\n"
        "• /iniciar - Activar scanners\n"
        "• /detener - Parar scanners\n"
        "• /status - Estado del sistema\n\n"
        "⚡ *Inicia manualmente cuando quieras!*",
        parse_mode=ParseMode.MARKDOWN
    )

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(flat_scanner.start_scanning())
    asyncio.create_task(pumpfun_monitor.start_monitoring())
    await update.message.reply_text("✅ Scanners activados")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flat_scanner.stop()
    pumpfun_monitor.stop()
    await update.message.reply_text("🛑 Scanners detenidos")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Estado: Activo")

# ------------------ MAIN ------------------
async def main():
    await db.init()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("iniciar", iniciar_command))
    application.add_handler(CommandHandler("detener", detener_command))
    application.add_handler(CommandHandler("status", status_command))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info("Bot iniciado. Esperando comandos...")

    # Mantener el bot corriendo
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Bot detenido por el usuario")
    finally:
        flat_scanner.stop()
        pumpfun_monitor.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
