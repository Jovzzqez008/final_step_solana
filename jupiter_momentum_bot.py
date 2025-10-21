#!/usr/bin/env python3
"""
🚀 JUPITER MOMENTUM TRADING BOT
Estrategia: Detectar tokens con momentum real y tradear automáticamente
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

import aiohttp
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.transaction import Transaction
from spl.token.instructions import get_associated_token_address
import base58

# Telegram
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot no instalado - Notificaciones deshabilitadas")

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    # Wallet & RPC
    PRIVATE_KEY: str = os.getenv('WALLET_PRIVATE_KEY', '')
    RPC_ENDPOINT: str = os.getenv('RPC_ENDPOINT', 'https://api.mainnet-beta.solana.com')
    
    # Telegram
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    
    # Trading
    DRY_RUN: bool = os.getenv('DRY_RUN', 'true').lower() == 'true'
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '300'))  # 3%
    
    # Filtros - Tokens de Calidad
    MIN_ORGANIC_SCORE: float = float(os.getenv('MIN_ORGANIC_SCORE', '60'))
    MIN_LIQUIDITY_USD: float = float(os.getenv('MIN_LIQUIDITY_USD', '10000'))
    MIN_HOLDER_COUNT: int = int(os.getenv('MIN_HOLDER_COUNT', '100'))
    MIN_MARKET_CAP_USD: float = float(os.getenv('MIN_MARKET_CAP_USD', '50000'))
    
    # Señales de Momentum
    MIN_PRICE_CHANGE_5M: float = float(os.getenv('MIN_PRICE_CHANGE_5M', '3'))  # %
    MIN_VOLUME_5M_USD: float = float(os.getenv('MIN_VOLUME_5M_USD', '5000'))
    MIN_NET_BUYERS_5M: int = int(os.getenv('MIN_NET_BUYERS_5M', '50'))
    
    # Confirmación Multi-Timeframe
    MIN_PRICE_CHANGE_1H: float = float(os.getenv('MIN_PRICE_CHANGE_1H', '5'))  # %
    MIN_NET_BUYERS_1H: int = int(os.getenv('MIN_NET_BUYERS_1H', '100'))
    
    # Risk Management
    STOP_LOSS_PERCENT: float = float(os.getenv('STOP_LOSS_PERCENT', '-15'))
    TAKE_PROFIT_1: float = float(os.getenv('TAKE_PROFIT_1', '25'))
    TAKE_PROFIT_2: float = float(os.getenv('TAKE_PROFIT_2', '50'))
    TRAILING_ACTIVATION: float = float(os.getenv('TRAILING_ACTIVATION', '30'))
    TRAILING_PERCENT: float = float(os.getenv('TRAILING_PERCENT', '-10'))
    
    # Posiciones
    MAX_POSITIONS: int = int(os.getenv('MAX_POSITIONS', '3'))
    MAX_HOLD_TIME_MIN: int = int(os.getenv('MAX_HOLD_TIME_MIN', '180'))
    
    # Timing
    SCAN_INTERVAL_SEC: int = int(os.getenv('SCAN_INTERVAL_SEC', '30'))
    POSITION_CHECK_SEC: int = int(os.getenv('POSITION_CHECK_SEC', '10'))

config = Config()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# MODELOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class TokenData:
    mint: str
    symbol: str
    name: str
    price_usd: float
    liquidity: float
    mcap: float
    holder_count: int
    organic_score: float
    
    # Stats 5m
    price_change_5m: float
    volume_5m: float
    net_buyers_5m: int
    
    # Stats 1h
    price_change_1h: float
    volume_1h: float
    net_buyers_1h: int
    
    # Stats 6h
    price_change_6h: float
    
    # Metadata
    first_seen: float = 0
    
    def __post_init__(self):
        if self.first_seen == 0:
            self.first_seen = time.time()

@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    amount_sol: float
    highest_price: float
    trailing_active: bool = False
    tp1_taken: bool = False
    tp2_taken: bool = False
    
    def current_pnl(self, current_price: float) -> float:
        return ((current_price - self.entry_price) / self.entry_price) * 100
    
    def hold_time_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60

# ═══════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.wallet: Optional[Keypair] = None
        self.solana_client: Optional[AsyncClient] = None
        self.telegram_bot: Optional[Bot] = None
        
        self.positions: Dict[str, Position] = {}
        self.watchlist: Dict[str, TokenData] = {}
        
        self.stats = {
            'scans': 0,
            'signals': 0,
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0
        }
        
        self.running = True

state = BotState()

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def send_telegram(message: str):
    """Enviar notificación a Telegram"""
    if not TELEGRAM_AVAILABLE or not state.telegram_bot:
        return
    
    try:
        await state.telegram_bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.debug(f"Error Telegram: {e}")

# ═══════════════════════════════════════════════════════════════
# JUPITER API
# ═══════════════════════════════════════════════════════════════

async def fetch_top_tokens(category: str = 'toporganicscore', interval: str = '5m') -> List[dict]:
    """Obtener tokens top de Jupiter API v2"""
    url = f'https://lite-api.jup.ag/tokens/v2/{category}/{interval}?limit=50'
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Jupiter API error: {resp.status}")
                return []
    except Exception as e:
        logger.error(f"Error fetching tokens: {e}")
        return []

def parse_token_data(token_json: dict) -> Optional[TokenData]:
    """Parsear respuesta de Jupiter API a TokenData"""
    try:
        # Extraer stats
        stats_5m = token_json.get('stats5m', {})
        stats_1h = token_json.get('stats1h', {})
        stats_6h = token_json.get('stats6h', {})
        
        return TokenData(
            mint=token_json['id'],
            symbol=token_json.get('symbol', 'UNKNOWN'),
            name=token_json.get('name', 'Unknown'),
            price_usd=token_json.get('usdPrice', 0),
            liquidity=token_json.get('liquidity', 0),
            mcap=token_json.get('mcap', 0),
            holder_count=token_json.get('holderCount', 0),
            organic_score=token_json.get('organicScore', 0),
            
            # 5m stats
            price_change_5m=stats_5m.get('priceChange', 0),
            volume_5m=(stats_5m.get('buyVolume', 0) + stats_5m.get('sellVolume', 0)),
            net_buyers_5m=stats_5m.get('numNetBuyers', 0),
            
            # 1h stats
            price_change_1h=stats_1h.get('priceChange', 0),
            volume_1h=(stats_1h.get('buyVolume', 0) + stats_1h.get('sellVolume', 0)),
            net_buyers_1h=stats_1h.get('numNetBuyers', 0),
            
            # 6h stats
            price_change_6h=stats_6h.get('priceChange', 0)
        )
    except Exception as e:
        logger.error(f"Error parsing token: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# JUPITER SWAP
# ═══════════════════════════════════════════════════════════════

async def get_quote(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
    """Obtener quote de Jupiter"""
    url = 'https://quote-api.jup.ag/v6/quote'
    params = {
        'inputMint': input_mint,
        'outputMint': output_mint,
        'amount': amount_lamports,
        'slippageBps': config.SLIPPAGE_BPS,
        'onlyDirectRoutes': False
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Quote API error: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"Error getting quote: {e}")
        return None

async def execute_swap(quote: dict) -> Optional[str]:
    """Ejecutar swap en Jupiter"""
    if config.DRY_RUN:
        logger.info("🧪 [DRY RUN] Swap simulado")
        return "dry-run-signature"
    
    url = 'https://quote-api.jup.ag/v6/swap'
    
    try:
        payload = {
            'quoteResponse': quote,
            'userPublicKey': str(state.wallet.pubkey()),
            'wrapAndUnwrapSol': True,
            'dynamicComputeUnitLimit': True,
            'prioritizationFeeLamports': 'auto'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    swap_data = await resp.json()
                    
                    # Deserializar y firmar transacción
                    tx_bytes = base58.b58decode(swap_data['swapTransaction'])
                    tx = Transaction.deserialize(tx_bytes)
                    tx.sign(state.wallet)
                    
                    # Enviar transacción
                    result = await state.solana_client.send_transaction(tx)
                    signature = result['result']
                    
                    logger.info(f"✅ Swap ejecutado: {signature}")
                    return signature
                else:
                    logger.error(f"Swap API error: {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Error ejecutando swap: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# TRADING LOGIC
# ═══════════════════════════════════════════════════════════════

def passes_quality_filters(token: TokenData) -> bool:
    """Filtros de calidad de token"""
    
    # Organic score
    if token.organic_score < config.MIN_ORGANIC_SCORE:
        return False
    
    # Liquidez
    if token.liquidity < config.MIN_LIQUIDITY_USD:
        return False
    
    # Holders
    if token.holder_count < config.MIN_HOLDER_COUNT:
        return False
    
    # Market cap
    if token.mcap < config.MIN_MARKET_CAP_USD:
        return False
    
    return True

def has_momentum_signal(token: TokenData) -> bool:
    """Detectar señal de momentum"""
    
    # Señal 5m
    signal_5m = (
        token.price_change_5m >= config.MIN_PRICE_CHANGE_5M and
        token.volume_5m >= config.MIN_VOLUME_5M_USD and
        token.net_buyers_5m >= config.MIN_NET_BUYERS_5M
    )
    
    # Confirmación 1h
    confirmation_1h = (
        token.price_change_1h >= config.MIN_PRICE_CHANGE_1H and
        token.net_buyers_1h >= config.MIN_NET_BUYERS_1H
    )
    
    # Tendencia 6h positiva (opcional pero ayuda)
    trend_6h_ok = token.price_change_6h >= 0
    
    return signal_5m and confirmation_1h and trend_6h_ok

async def buy_token(token: TokenData):
    """Comprar token"""
    logger.info(f"💰 Comprando {token.symbol} @ ${token.price_usd:.6f}")
    
    # SOL mint
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    amount_lamports = int(config.TRADE_AMOUNT_SOL * 1e9)
    
    # Get quote
    quote = await get_quote(SOL_MINT, token.mint, amount_lamports)
    if not quote:
        logger.error(f"❌ No se pudo obtener quote para {token.symbol}")
        return
    
    # Execute swap
    signature = await execute_swap(quote)
    if not signature:
        logger.error(f"❌ Swap fallido para {token.symbol}")
        return
    
    # Crear posición
    position = Position(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=token.price_usd,
        entry_time=time.time(),
        amount_sol=config.TRADE_AMOUNT_SOL,
        highest_price=token.price_usd
    )
    
    state.positions[token.mint] = position
    state.stats['trades'] += 1
    
    # Notificar
    message = (
        f"🟢 <b>POSICIÓN ABIERTA</b>\n\n"
        f"<b>{token.name}</b> ({token.symbol})\n"
        f"💰 {config.TRADE_AMOUNT_SOL} SOL @ ${token.price_usd:.6f}\n\n"
        f"📊 Señales:\n"
        f"├ 5m: +{token.price_change_5m:.1f}% | {token.net_buyers_5m} buyers\n"
        f"├ 1h: +{token.price_change_1h:.1f}% | {token.net_buyers_1h} buyers\n"
        f"└ 6h: +{token.price_change_6h:.1f}%\n\n"
        f"🎯 Score: {token.organic_score:.0f} | Holders: {token.holder_count}\n"
        f"💵 Liq: ${token.liquidity:,.0f} | MC: ${token.mcap:,.0f}\n\n"
        f"Tx: https://solscan.io/tx/{signature}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Posición abierta: {token.symbol}")

async def sell_token(position: Position, current_price: float, reason: str, percentage: int = 100):
    """Vender token"""
    logger.info(f"💸 Vendiendo {percentage}% de {position.symbol} - {reason}")
    
    # SOL mint
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    
    # Calcular cantidad a vender (simplificado - en producción obtener balance real)
    # Aquí asumimos vender todo por simplicidad
    amount_lamports = int(position.amount_sol * 1e9 * (percentage / 100))
    
    # Get quote (vendiendo token por SOL)
    quote = await get_quote(position.mint, SOL_MINT, amount_lamports)
    if not quote:
        logger.error(f"❌ No se pudo obtener quote de venta para {position.symbol}")
        return
    
    # Execute swap
    signature = await execute_swap(quote)
    if not signature:
        logger.error(f"❌ Venta fallida para {position.symbol}")
        return
    
    # Calcular P&L
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    
    # Actualizar stats
    if percentage == 100:
        if pnl > 0:
            state.stats['wins'] += 1
        else:
            state.stats['losses'] += 1
        state.stats['total_pnl'] += pnl
        
        # Eliminar posición
        del state.positions[position.mint]
    else:
        # Venta parcial - actualizar posición
        position.amount_sol *= (100 - percentage) / 100
    
    # Notificar
    emoji = '🟢' if pnl > 0 else '🔴'
    message = (
        f"{emoji} <b>POSICIÓN CERRADA</b> ({percentage}%)\n\n"
        f"<b>{position.symbol}</b>\n"
        f"📊 P&L: {pnl:+.2f}%\n"
        f"💰 Entry: ${position.entry_price:.6f}\n"
        f"💰 Exit: ${current_price:.6f}\n"
        f"⏱️ Hold: {hold_time:.1f} min\n"
        f"🎯 {reason}\n\n"
        f"Tx: https://solscan.io/tx/{signature}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Venta ejecutada: {position.symbol} ({pnl:+.2f}%)")

async def monitor_position(position: Position):
    """Monitorear y gestionar posición"""
    
    # Obtener precio actual
    tokens = await fetch_top_tokens('toporganicscore', '5m')
    current_token = None
    
    for t in tokens:
        if t['id'] == position.mint:
            current_token = parse_token_data(t)
            break
    
    if not current_token:
        logger.warning(f"⚠️ No se encontró {position.symbol} en top tokens")
        return
    
    current_price = current_token.price_usd
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    
    # Actualizar highest price
    if current_price > position.highest_price:
        position.highest_price = current_price
    
    # Activar trailing stop
    if not position.trailing_active and pnl >= config.TRAILING_ACTIVATION:
        position.trailing_active = True
        logger.info(f"🛡️ Trailing activado para {position.symbol} (+{pnl:.1f}%)")
        await send_telegram(f"🛡️ Trailing stop activado: {position.symbol} (+{pnl:.1f}%)")
    
    # ═══ REGLAS DE SALIDA ═══
    
    # 1. Stop Loss
    if pnl <= config.STOP_LOSS_PERCENT:
        await sell_token(position, current_price, f"Stop Loss ({pnl:.1f}%)")
        return
    
    # 2. Max Hold Time
    if hold_time >= config.MAX_HOLD_TIME_MIN:
        await sell_token(position, current_price, f"Max Hold Time ({hold_time:.0f}min)")
        return
    
    # 3. Trailing Stop
    if position.trailing_active:
        trailing_stop_price = position.highest_price * (1 + config.TRAILING_PERCENT / 100)
        if current_price <= trailing_stop_price:
            await sell_token(position, current_price, f"Trailing Stop ({pnl:.1f}%)")
            return
    
    # 4. Take Profit 1 (50% de posición)
    if pnl >= config.TAKE_PROFIT_1 and not position.tp1_taken:
        position.tp1_taken = True
        await sell_token(position, current_price, f"TP1 ({pnl:.1f}%)", percentage=50)
        return
    
    # 5. Take Profit 2 (resto de posición)
    if pnl >= config.TAKE_PROFIT_2 and not position.tp2_taken:
        position.tp2_taken = True
        await sell_token(position, current_price, f"TP2 ({pnl:.1f}%)", percentage=100)
        return

# ═══════════════════════════════════════════════════════════════
# LOOPS PRINCIPALES
# ═══════════════════════════════════════════════════════════════

async def scan_loop():
    """Loop principal de escaneo"""
    logger.info("🔄 Scanner iniciado")
    
    while state.running:
        try:
            state.stats['scans'] += 1
            
            # Verificar límite de posiciones
            if len(state.positions) >= config.MAX_POSITIONS:
                logger.debug(f"⏸️ Límite de posiciones alcanzado ({config.MAX_POSITIONS})")
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            # Obtener tokens top
            tokens = await fetch_top_tokens('toporganicscore', '5m')
            
            if not tokens:
                logger.warning("⚠️ No se obtuvieron tokens")
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            logger.info(f"📊 Escaneando {len(tokens)} tokens...")
            
            # Analizar cada token
            for token_json in tokens:
                token = parse_token_data(token_json)
                
                if not token:
                    continue
                
                # Skip si ya tenemos posición
                if token.mint in state.positions:
                    continue
                
                # Filtros de calidad
                if not passes_quality_filters(token):
                    continue
                
                # Señal de momentum
                if has_momentum_signal(token):
                    state.stats['signals'] += 1
                    
                    logger.info(
                        f"⚡ SEÑAL: {token.symbol} | "
                        f"5m: +{token.price_change_5m:.1f}% | "
                        f"1h: +{token.price_change_1h:.1f}% | "
                        f"Score: {token.organic_score:.0f}"
                    )
                    
                    # Ejecutar compra
                    await buy_token(token)
                    break  # Solo una compra por ciclo
            
            await asyncio.sleep(config.SCAN_INTERVAL_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error en scan loop: {e}", exc_info=True)
            await asyncio.sleep(10)

async def position_monitor_loop():
    """Loop de monitoreo de posiciones"""
    logger.info("🔄 Monitor de posiciones iniciado")
    
    while state.running:
        try:
            if state.positions:
                logger.debug(f"📊 Monitoreando {len(state.positions)} posiciones...")
                
                # Monitorear cada posición
                for mint in list(state.positions.keys()):
                    if mint in state.positions:  # Re-check en caso de venta
                        await monitor_position(state.positions[mint])
                        await asyncio.sleep(2)  # Delay entre checks
            
            await asyncio.sleep(config.POSITION_CHECK_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error en monitor loop: {e}", exc_info=True)
            await asyncio.sleep(10)

async def stats_loop():
    """Loop de estadísticas"""
    logger.info("🔄 Stats loop iniciado")
    
    while state.running:
        await asyncio.sleep(300)  # Cada 5 min
        
        try:
            win_rate = (state.stats['wins'] / state.stats['trades'] * 100) if state.stats['trades'] > 0 else 0
            
            logger.info(
                f"📊 Stats | Scans: {state.stats['scans']} | "
                f"Señales: {state.stats['signals']} | "
                f"Trades: {state.stats['trades']} | "
                f"Win Rate: {win_rate:.1f}% | "
                f"P&L: {state.stats['total_pnl']:+.2f}%"
            )
            
        except Exception as e:
            logger.error(f"Error en stats: {e}")

# ═══════════════════════════════════════════════════════════════
# SETUP & MAIN
# ═══════════════════════════════════════════════════════════════

async def setup():
    """Inicializar bot"""
    logger.info("🚀 Inicializando Jupiter Momentum Bot...")
    
    # Wallet
    if not config.PRIVATE_KEY:
        raise ValueError("❌ WALLET_PRIVATE_KEY no configurada")
    
    try:
        private_key_bytes = base58.b58decode(config.PRIVATE_KEY)
        state.wallet = Keypair.from_bytes(private_key_bytes)
        logger.info(f"✅ Wallet: {str(state.wallet.pubkey())[:8]}...")
    except Exception as e:
        raise ValueError(f"❌ Error cargando wallet: {e}")
    
    # Solana RPC
    state.solana_client = AsyncClient(config.RPC_ENDPOINT)
    logger.info(f"✅ RPC: {config.RPC_ENDPOINT}")
    
    # Verificar balance
    try:
        balance_resp = await state.solana_client.get_balance(state.wallet.pubkey())
        balance_sol = balance_resp['result']['value'] / 1e9
        logger.info(f"💰 Balance: {balance_sol:.4f} SOL")
        
        if balance_sol < config.TRADE_AMOUNT_SOL:
            logger.warning(f"⚠️ Balance bajo para trading")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo verificar balance: {e}")
    
    # Telegram
    if TELEGRAM_AVAILABLE and config.TELEGRAM_TOKEN:
        state.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
        logger.info("✅ Telegram configurado")
        
        await send_telegram(
            f"🚀 <b>Jupiter Momentum Bot</b>\n\n"
            f"{'🧪 DRY RUN' if config.DRY_RUN else '💰 REAL TRADING'}\n\n"
            f"<b>Config:</b>\n"
            f"├ Trade: {config.TRADE_AMOUNT_SOL} SOL\n"
            f"├ Max Pos: {config.MAX_POSITIONS}\n"
            f"├ Stop Loss: {config.STOP_LOSS_PERCENT}%\n"
            f"├ TP1: {config.TAKE_PROFIT_1}%\n"
            f"└ TP2: {config.TAKE_PROFIT_2}%\n\n"
            f"<b>Filtros:</b>\n"
            f"├ Organic Score: {config.MIN_ORGANIC_SCORE}+\n"
            f"├ Liquidity: ${config.MIN_LIQUIDITY_USD:,.0f}+\n"
            f"├ Holders: {config.MIN_HOLDER_COUNT}+\n"
            f"└ Market Cap: ${config.MIN_MARKET_CAP_USD:,.0f}+\n\n"
            f"✅ Bot iniciado correctamente"
        )
    else:
        logger.warning("⚠️ Telegram no disponible")
    
    logger.info("=" * 60)
    logger.info(f"✅ SETUP COMPLETO - {'🧪 DRY RUN' if config.DRY_RUN else '💰 REAL'}")
    logger.info("=" * 60)

async def main():
    """Función principal"""
    try:
        await setup()
        
        # Crear tareas
        tasks = [
            asyncio.create_task(scan_loop()),
            asyncio.create_task(position_monitor_loop()),
            asyncio.create_task(stats_loop())
        ]
        
        # Ejecutar todas las tareas
        await asyncio.gather(*tasks)
        
    except KeyboardInterrupt:
        logger.info("🛑 Deteniendo bot...")
        state.running = False
        await send_telegram("🛑 <b>Bot Detenido</b> (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
        await send_telegram(f"❌ <b>Error Fatal</b>\n\n{str(e)}")
        raise
    finally:
        if state.solana_client:
            await state.solana_client.close()
        logger.info("👋 Bot cerrado")

if __name__ == '__main__':
    asyncio.run(main())
