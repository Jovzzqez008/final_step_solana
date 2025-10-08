# fixed_breakout_scanner.py
import asyncio
import time
import logging
import os
from typing import Dict, List, Deque, Optional
from collections import deque
import httpx
import statistics

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------- CONFIGURACIÓN ACTUALIZADA --------------------
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    
    PRICE_INTERVAL = 300  # 5 minutos
    CONSOLIDATION_THRESHOLD = 2.0
    BREAKOUT_PERCENT = 5.0
    MIN_CONSOLIDATION_HOURS = 4
    MIN_LIQUIDITY = 25000
    VOLUME_SPIKE_MULTIPLIER = 2.0
    
    # ✅ URLs CORREGIDAS de DexScreener
    DEXSCREENER_URLS = {
        'search_solana': "https://api.dexscreener.com/latest/dex/search?q=solana",
        'tokens': "https://api.dexscreener.com/latest/dex/tokens/",
        'pairs': "https://api.dexscreener.com/latest/dex/pairs/"
    }

class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
    async def send_alert(self, message: str):
        if not self.enabled:
            print(f"\n🔔 {message}\n")
            return
            
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            async with httpx.AsyncClient() as client:
                await client.post(url, json=payload, timeout=10)
                logger.info("📱 Alerta enviada a Telegram")
        except Exception as e:
            logger.error(f"❌ Error Telegram: {e}")

class RobustPatternDetector:
    def __init__(self):
        self.price_history: Dict[str, Deque[Dict]] = {}
        self.consolidation_trackers: Dict[str, Dict] = {}
        self.detected_patterns: Dict[str, float] = {}
        
    def analyze_pattern(self, token: str, price_data: Dict) -> Optional[Dict]:
        current_price = price_data['price']
        symbol = price_data.get('symbol', 'Unknown')
        
        if token not in self.price_history:
            self.price_history[token] = deque(maxlen=36)
            self.consolidation_trackers[token] = {
                'in_consolidation': False,
                'consolidation_start': None,
                'price_range': (0, 0),
                'volume_base': 0,
                'consolidation_hours': 0
            }
        
        self.price_history[token].append(price_data)
        
        if len(self.price_history[token]) < 12:
            return None
        
        prices = [p['price'] for p in self.price_history[token]]
        volumes = [p.get('volume_24h', 0) for p in self.price_history[token]]
        timestamps = [p['timestamp'] for p in self.price_history[token]]
        
        consolidation_signal = self._detect_consolidation(token, prices, volumes, timestamps)
        breakout_signal = self._detect_breakout(token, current_price, volumes[-1] if volumes else 0, consolidation_signal)
        
        if breakout_signal:
            logger.info(f"🚨 BREAKOUT: {symbol} +{breakout_signal['breakout_percent']:.2f}%")
            return breakout_signal
        
        return None
    
    def _detect_consolidation(self, token: str, prices: List[float], volumes: List[float], timestamps: List[float]) -> Dict:
        if len(prices) < 12:
            return {'in_consolidation': False}
        
        recent_prices = prices[-24:] if len(prices) >= 24 else prices
        min_price = min(recent_prices)
        max_price = max(recent_prices)
        price_range_pct = ((max_price - min_price) / min_price) * 100
        
        is_consolidating = price_range_pct <= Config.CONSOLIDATION_THRESHOLD
        
        tracker = self.consolidation_trackers[token]
        
        if is_consolidating:
            if not tracker['in_consolidation']:
                tracker.update({
                    'in_consolidation': True,
                    'consolidation_start': timestamps[0],
                    'price_range': (min_price, max_price),
                    'volume_base': statistics.mean(volumes) if volumes else 0,
                    'consolidation_hours': (timestamps[-1] - timestamps[0]) / 3600
                })
        else:
            tracker['in_consolidation'] = False
        
        return tracker
    
    def _detect_breakout(self, token: str, current_price: float, current_volume: float, consolidation_data: Dict) -> Optional[Dict]:
        if not consolidation_data['in_consolidation']:
            return None
        
        consolidation_high = consolidation_data['price_range'][1]
        consolidation_hours = consolidation_data['consolidation_hours']
        volume_base = consolidation_data['volume_base']
        
        move_from_consolidation = ((current_price - consolidation_high) / consolidation_high) * 100
        volume_spike = current_volume / volume_base if volume_base > 0 else 1
        
        is_valid_breakout = (
            move_from_consolidation >= Config.BREAKOUT_PERCENT and
            consolidation_hours >= Config.MIN_CONSOLIDATION_HOURS and
            volume_spike >= Config.VOLUME_SPIKE_MULTIPLIER
        )
        
        last_alert = self.detected_patterns.get(token, 0)
        time_since_last_alert = time.time() - last_alert
        
        if is_valid_breakout and time_since_last_alert > 7200:
            breakout_signal = {
                'token': token,
                'breakout_percent': move_from_consolidation,
                'consolidation_hours': consolidation_hours,
                'volume_spike': volume_spike,
                'current_price': current_price,
                'consolidation_range': consolidation_data['price_range'],
                'timestamp': time.time()
            }
            
            self.detected_patterns[token] = time.time()
            return breakout_signal
        
        return None

class WorkingTokenFinder:
    """Buscador que funciona con las URLs correctas."""
    
    def __init__(self):
        self.tracked_tokens = set()
        
    async def find_potential_tokens(self) -> List[str]:
        """Encuentra tokens usando búsqueda funcional."""
        logger.info("🔍 Buscando tokens con potencial...")
        
        tokens = set()
        
        # Estrategia 1: Buscar tokens populares en Solana
        popular_tokens = await self._get_popular_solana_tokens()
        tokens.update(popular_tokens)
        
        # Estrategia 2: Tokens con volumen reciente
        volume_tokens = await self._get_volume_tokens()
        tokens.update(volume_tokens)
        
        # Token de referencia
        tokens.add("H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump")
        
        logger.info(f"🎯 Encontrados {len(tokens)} tokens para monitorear")
        return list(tokens)
    
    async def _get_popular_solana_tokens(self) -> List[str]:
        """Obtiene tokens populares de Solana usando search."""
        tokens = []
        try:
            url = Config.DEXSCREENER_URLS['search_solana']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    # DexScreener search devuelve pairs, tomamos los primeros
                    pairs = data.get('pairs', [])[:50]
                    for pair in pairs:
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        if liquidity >= Config.MIN_LIQUIDITY:
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address:
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error buscando tokens Solana: {e}")
        return tokens
    
    async def _get_volume_tokens(self) -> List[str]:
        """Obtiene tokens con volumen usando el endpoint de pairs."""
        tokens = []
        try:
            # Usamos el endpoint de pairs con algunos tokens conocidos como base
            base_tokens = [
                "So11111111111111111111111111111111111111112",  # SOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump",  # TOKABU
            ]
            
            for base_token in base_tokens:
                url = f"{Config.DEXSCREENER_URLS['pairs']}{base_token}"
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        pairs = data.get('pairs', [])[:20]
                        for pair in pairs:
                            liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                            volume_24h = float(pair.get('volume', {}).get('h24', 0))
                            
                            if (liquidity >= Config.MIN_LIQUIDITY and 
                                volume_24h >= 5000):
                                
                                token_address = pair.get('baseToken', {}).get('address')
                                if token_address and token_address != base_token:
                                    tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error volumen tokens: {e}")
        return tokens

class FixedBreakoutScanner:
    def __init__(self):
        self.token_finder = WorkingTokenFinder()
        self.pattern_detector = RobustPatternDetector()
        self.telegram = TelegramNotifier()
        self.is_running = False
        self.alert_count = 0
        self.cycle_count = 0
        
    async def start_scanning(self):
        self.is_running = True
        
        # Mensaje de inicio
        start_msg = (
            "🚀 <b>BREAKOUT SCANNER INICIADO</b>\n\n"
            "✅ <b>Configuración corregida</b>\n"
            "• URLs de DexScreener actualizadas\n"
            "• Búsqueda funcional de tokens\n"
            "• Telegram conectado\n\n"
            "🎯 <b>Parámetros activos:</b>\n"
            "• Breakout: 5% mínimo\n"
            "• Consolidación: ±2%\n"
            "• Liquidez: $25K+\n"
            "• Volumen spike: 2x\n\n"
            "<i>Escaneo cada 5 minutos...</i>"
        )
        await self.telegram.send_alert(start_msg)
        
        logger.info("🚀 SCANNER CORREGIDO INICIADO")
        
        while self.is_running:
            try:
                self.cycle_count += 1
                
                tokens_to_monitor = await self.token_finder.find_potential_tokens()
                cycle_alerts = 0
                
                for token in tokens_to_monitor:
                    try:
                        price_data = await self.get_token_data(token)
                        if price_data and price_data['price'] > 0:
                            pattern_signal = self.pattern_detector.analyze_pattern(token, price_data)
                            
                            if pattern_signal:
                                cycle_alerts += 1
                                self.alert_count += 1
                                await self.send_alert(token, price_data, pattern_signal)
                        
                        await asyncio.sleep(0.3)
                        
                    except Exception as e:
                        continue
                
                if cycle_alerts > 0:
                    logger.info(f"🚨 Ciclo {self.cycle_count}: {cycle_alerts} alertas")
                else:
                    logger.info(f"📊 Ciclo {self.cycle_count}: {len(tokens_to_monitor)} tokens - 0 alertas")
                
                # Reporte cada 6 ciclos (30 minutos)
                if self.cycle_count % 6 == 0:
                    await self.send_status_report(len(tokens_to_monitor))
                
                await asyncio.sleep(Config.PRICE_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error en ciclo: {e}")
                await asyncio.sleep(30)
    
    async def get_token_data(self, token_address: str) -> Optional[Dict]:
        try:
            url = f"{Config.DEXSCREENER_URLS['tokens']}{token_address}"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    pairs = data.get('pairs', [])
                    
                    if pairs:
                        main_pair = max(pairs, key=lambda x: float(x.get('liquidity', {}).get('usd', 0)))
                        return {
                            'price': float(main_pair.get('priceUsd', 0)),
                            'volume_24h': float(main_pair.get('volume', {}).get('h24', 0)),
                            'liquidity': float(main_pair.get('liquidity', {}).get('usd', 0)),
                            'symbol': main_pair.get('baseToken', {}).get('symbol', 'Unknown'),
                            'name': main_pair.get('baseToken', {}).get('name', 'Unknown'),
                            'timestamp': time.time()
                        }
        except Exception as e:
            logger.debug(f"Error datos token {token_address[:8]}: {e}")
        return None
    
    async def send_alert(self, token: str, price_data: Dict, pattern: Dict):
        symbol = price_data.get('symbol', 'N/A')
        name = price_data.get('name', 'N/A')
        
        alert_message = (
            f"🚀 <b>BREAKOUT DETECTADO</b>\n\n"
            f"<b>Token:</b> {symbol}\n"
            f"<b>Nombre:</b> {name}\n"
            f"<b>Address:</b> <code>{token}</code>\n\n"
            f"📊 <b>Métricas:</b>\n"
            f"• <b>Breakout:</b> +{pattern['breakout_percent']:.2f}% 🚀\n"
            f"• <b>Consolidación:</b> {pattern['consolidation_hours']:.1f} horas\n"
            f"• <b>Volumen:</b> {pattern['volume_spike']:.1f}x spike\n"
            f"• <b>Precio:</b> ${pattern['current_price']:.6f}\n"
            f"• <b>Liquidez:</b> ${price_data.get('liquidity', 0):,.2f}\n\n"
            f"🔗 <b>Enlaces:</b>\n"
            f"• <a href='https://dexscreener.com/solana/{token}'>DexScreener</a>\n"
            f"• <a href='https://birdeye.so/token/{token}?chain=solana'>Birdeye</a>\n\n"
            f"⚡ <b>Estrategia:</b>\n"
            f"• Entrada: Breakout confirmado\n"
            f"• Stop Loss: -3%\n"
            f"• Take Profit: +10-15%"
        )
        
        logger.info(f"🚀 ALERTA: {symbol} +{pattern['breakout_percent']:.2f}%")
        await self.telegram.send_alert(alert_message)
    
    async def send_status_report(self, token_count: int):
        status_msg = (
            f"📊 <b>Reporte de Estado</b>\n\n"
            f"• <b>Ciclos completados:</b> {self.cycle_count}\n"
            f"• <b>Alertas totales:</b> {self.alert_count}\n"
            f"• <b>Tokens monitoreados:</b> {token_count}\n"
            f"• <b>Estado:</b> ✅ Activo\n\n"
            f"<i>Siguiente reporte en 30 minutos</i>"
        )
        await self.telegram.send_alert(status_msg)
    
    def stop_scanning(self):
        self.is_running = False
        logger.info("🛑 Scanner detenido")

# -------------------- COMANDOS DE CONTROL --------------------
async def handle_commands(scanner: FixedBreakoutScanner):
    """Maneja comandos simples para controlar el scanner."""
    while scanner.is_running:
        try:
            command = await asyncio.get_event_loop().run_in_executor(None, input, ">>> ")
            command = command.strip().lower()
            
            if command == 'stop':
                scanner.stop_scanning()
                break
            elif command == 'status':
                print(f"📊 Estado: {scanner.cycle_count} ciclos, {scanner.alert_count} alertas")
            elif command == 'help':
                print("Comandos: stop, status, help")
            else:
                print("Comando no reconocido. Usa 'help' para ver comandos.")
                
        except (KeyboardInterrupt, EOFError):
            scanner.stop_scanning()
            break

# -------------------- EJECUCIÓN MEJORADA --------------------
async def main():
    scanner = FixedBreakoutScanner()
    
    print("🚀 BREAKOUT SCANNER - VERSIÓN CORREGIDA")
    print("=" * 50)
    print("✅ CONFIGURACIÓN:")
    print(f"   • Telegram: {'✅' if scanner.telegram.enabled else '❌'}")
    print(f"   • URLs DexScreener: ✅ Corregidas")
    print("=" * 50)
    print("🎯 PARÁMETROS:")
    print(f"   • Breakout: {Config.BREAKOUT_PERCENT}%")
    print(f"   • Consolidación: ±{Config.CONSOLIDATION_THRESHOLD}%") 
    print(f"   • Liquidez mínima: ${Config.MIN_LIQUIDITY:,}")
    print("=" * 50)
    print("⚡ COMANDOS DISPONIBLES:")
    print("   • stop - Detener scanner")
    print("   • status - Ver estado")
    print("   • help - Ver ayuda")
    print("=" * 50)
    
    # Ejecutar scanner y comandos en paralelo
    try:
        await asyncio.gather(
            scanner.start_scanning(),
            handle_commands(scanner)
        )
    except KeyboardInterrupt:
        logger.info("🛑 Detenido por usuario")
    finally:
        scanner.stop_scanning()

if __name__ == "__main__":
    asyncio.run(main())
