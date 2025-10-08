# simple_breakout_scanner.py
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

# -------------------- CONFIGURACIÓN SIMPLE --------------------
class Config:
    # Solo Telegram desde environment
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    
    # Scanner parameters optimizados
    PRICE_INTERVAL = 300  # 5 minutos
    CONSOLIDATION_THRESHOLD = 2.0
    BREAKOUT_PERCENT = 5.0
    MIN_CONSOLIDATION_HOURS = 4
    MIN_LIQUIDITY = 25000
    VOLUME_SPIKE_MULTIPLIER = 2.0
    
    # Fuentes DexScreener
    DEXSCREENER_URLS = {
        'volume': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=volume24h&order=desc&limit=100",
        'new': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=age&order=asc&limit=80",
        'trending': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=priceChange24h&order=desc&limit=100"
    }

class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
    async def send_alert(self, message: str):
        """Envía alerta a Telegram."""
        if not self.enabled:
            logger.info("📱 Telegram no configurado - Mostrando en consola")
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
                response = await client.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    logger.info("📱 Alerta enviada a Telegram")
                else:
                    logger.error(f"❌ Error Telegram: {response.status_code}")
        except Exception as e:
            logger.error(f"❌ Error enviando a Telegram: {e}")

class EfficientPatternDetector:
    """Detector eficiente sin APIs externas complejas."""
    
    def __init__(self):
        self.price_history: Dict[str, Deque[Dict]] = {}
        self.consolidation_trackers: Dict[str, Dict] = {}
        self.detected_patterns: Dict[str, float] = {}
        self.token_metadata: Dict[str, Dict] = {}
        
    def analyze_pattern(self, token: str, price_data: Dict) -> Optional[Dict]:
        """Análisis simple y eficiente del patrón."""
        current_price = price_data['price']
        symbol = price_data.get('symbol', 'Unknown')
        
        # Inicializar historial si es nuevo token
        if token not in self.price_history:
            self.price_history[token] = deque(maxlen=36)  # 3 horas de datos
            self.consolidation_trackers[token] = {
                'in_consolidation': False,
                'consolidation_start': None,
                'price_range': (0, 0),
                'volume_base': 0,
                'consolidation_hours': 0
            }
        
        # Agregar nuevo dato
        self.price_history[token].append(price_data)
        
        # Solo analizar si tenemos suficientes datos
        if len(self.price_history[token]) < 12:  # 1 hora mínima
            return None
        
        prices = [p['price'] for p in self.price_history[token]]
        volumes = [p.get('volume_24h', 0) for p in self.price_history[token]]
        timestamps = [p['timestamp'] for p in self.price_history[token]]
        
        # Detectar consolidación
        consolidation_signal = self._detect_consolidation(token, prices, volumes, timestamps)
        
        # Detectar breakout
        breakout_signal = self._detect_breakout(token, current_price, volumes[-1] if volumes else 0, consolidation_signal)
        
        if breakout_signal:
            logger.info(f"🚨 BREAKOUT: {symbol} +{breakout_signal['breakout_percent']:.2f}%")
            return breakout_signal
        
        return None
    
    def _detect_consolidation(self, token: str, prices: List[float], volumes: List[float], timestamps: List[float]) -> Dict:
        if len(prices) < 12:
            return {'in_consolidation': False}
        
        # Analizar últimas 2-3 horas
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
                logger.info(f"🔍 Consolidación detectada: {token[:8]}... - {price_range_pct:.2f}% rango")
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
        
        # Prevenir alertas duplicadas (2 horas mínimo entre alertas)
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

class SmartTokenFinder:
    """Buscador inteligente solo con DexScreener."""
    
    def __init__(self):
        self.tracked_tokens = set()
        
    async def find_potential_tokens(self) -> List[str]:
        """Encuentra tokens con potencial usando solo DexScreener."""
        logger.info("🔍 Buscando tokens con potencial...")
        
        tokens = set()
        
        # Estrategia 1: Tokens con buen volumen
        volume_tokens = await self._get_volume_tokens()
        tokens.update(volume_tokens)
        
        # Estrategia 2: Tokens en crecimiento moderado
        trending_tokens = await self._get_trending_tokens()
        tokens.update(trending_tokens)
        
        # Estrategia 3: Tokens con liquidez sólida
        liquidity_tokens = await self._get_liquidity_tokens()
        tokens.update(liquidity_tokens)
        
        # Token de referencia
        tokens.add("H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump")
        
        logger.info(f"🎯 Encontrados {len(tokens)} tokens para monitorear")
        return list(tokens)
    
    async def _get_volume_tokens(self) -> List[str]:
        """Tokens con volumen decente."""
        tokens = []
        try:
            url = Config.DEXSCREENER_URLS['volume']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:80]:
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        volume_24h = float(pair.get('volume', {}).get('h24', 0))
                        
                        # Filtros básicos de calidad
                        if (liquidity >= Config.MIN_LIQUIDITY and 
                            volume_24h >= 10000 and  # $10K volumen mínimo
                            volume_24h <= 5000000):  # $5M volumen máximo (evitar los masivos)
                            
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address and token_address != 'unknown':
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error volumen tokens: {e}")
        return tokens
    
    async def _get_trending_tokens(self) -> List[str]:
        """Tokens con crecimiento saludable."""
        tokens = []
        try:
            url = Config.DEXSCREENER_URLS['trending']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:60]:
                        price_change_24h = float(pair.get('priceChange', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        
                        # Crecimiento moderado, no pump extremo
                        if (15 <= price_change_24h <= 500 and  # 15% - 500% crecimiento
                            liquidity >= Config.MIN_LIQUIDITY):
                            
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address:
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error trending tokens: {e}")
        return tokens
    
    async def _get_liquidity_tokens(self) -> List[str]:
        """Tokens con buena liquidez."""
        tokens = []
        try:
            # Usamos el endpoint de volumen pero filtramos por liquidez
            url = "https://api.dexscreener.com/latest/dex/pairs/solana?sort=volume24h&order=desc&limit=60"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:40]:
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        
                        # Buena liquidez pero no masiva
                        if (50000 <= liquidity <= 2000000):  # $50K - $2M liquidez
                            
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address:
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error liquidez tokens: {e}")
        return tokens

class SimpleBreakoutScanner:
    """Scanner simple y eficiente."""
    
    def __init__(self):
        self.token_finder = SmartTokenFinder()
        self.pattern_detector = EfficientPatternDetector()
        self.telegram = TelegramNotifier()
        self.is_running = False
        self.alert_count = 0
        self.cycle_count = 0
        
    async def start_scanning(self):
        """Inicia el scanner simple."""
        self.is_running = True
        
        # Mensaje de inicio
        if self.telegram.enabled:
            start_msg = (
                "🚀 <b>BREAKOUT SCANNER INICIADO</b>\n\n"
                "✅ <b>Configuración simple y eficiente</b>\n"
                "• Solo DexScreener API\n"
                "• Sin APIs complejas\n"
                "• Detección temprana\n\n"
                "🎯 <b>Parámetros:</b>\n"
                "• Breakout: 5% mínimo\n"
                "• Consolidación: ±2%\n"
                "• Liquidez: $25K+\n"
                "• Volumen spike: 2x\n\n"
                "<i>Escaneo activo cada 5 minutos...</i>"
            )
            await self.telegram.send_alert(start_msg)
        
        logger.info("🚀 SCANNER SIMPLE INICIADO")
        
        while self.is_running:
            try:
                self.cycle_count += 1
                
                # Buscar tokens
                tokens_to_monitor = await self.token_finder.find_potential_tokens()
                cycle_alerts = 0
                
                # Monitorear cada token
                for token in tokens_to_monitor:
                    try:
                        price_data = await self.get_token_data(token)
                        if price_data and price_data['price'] > 0:
                            pattern_signal = self.pattern_detector.analyze_pattern(token, price_data)
                            
                            if pattern_signal:
                                cycle_alerts += 1
                                self.alert_count += 1
                                await self.send_simple_alert(token, price_data, pattern_signal)
                        
                        await asyncio.sleep(0.2)  # Rate limiting muy suave
                        
                    except Exception as e:
                        continue
                
                # Log del ciclo
                if cycle_alerts > 0:
                    logger.info(f"🚨 Ciclo {self.cycle_count}: {cycle_alerts} alertas")
                else:
                    logger.info(f"📊 Ciclo {self.cycle_count}: {len(tokens_to_monitor)} tokens - 0 alertas")
                
                # Reporte cada 12 ciclos (1 hora)
                if self.cycle_count % 12 == 0:
                    await self.send_status_report(len(tokens_to_monitor))
                
                await asyncio.sleep(Config.PRICE_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error en ciclo: {e}")
                await asyncio.sleep(30)  # Esperar y reintentar
    
    async def get_token_data(self, token_address: str) -> Optional[Dict]:
        """Obtiene datos simples del token."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=8)
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
    
    async def send_simple_alert(self, token: str, price_data: Dict, pattern: Dict):
        """Envía alerta simple y efectiva."""
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
            f"⚡ <b>Estrategia Rápida:</b>\n"
            f"• Entrada: Ahora\n"
            f"• Stop Loss: -3%\n"
            f"• Take Profit: +10-15%"
        )
        
        logger.info(f"🚀 ALERTA: {symbol} +{pattern['breakout_percent']:.2f}%")
        await self.telegram.send_alert(alert_message)
    
    async def send_status_report(self, token_count: int):
        """Envía reporte de estado simple."""
        status_msg = (
            f"📊 <b>Reporte de Estado</b>\n\n"
            f"• <b>Ciclos completados:</b> {self.cycle_count}\n"
            f"• <b>Alertas totales:</b> {self.alert_count}\n"
            f"• <b>Tokens monitoreados:</b> {token_count}\n"
            f"• <b>Estado:</b> ✅ Activo\n\n"
            f"<i>Siguiente reporte en 1 hora</i>"
        )
        await self.telegram.send_alert(status_msg)
    
    def stop_scanning(self):
        self.is_running = False
        logger.info("🛑 Scanner detenido")

# -------------------- EJECUCIÓN --------------------
async def main():
    scanner = SimpleBreakoutScanner()
    
    print("🚀 SIMPLE BREAKOUT SCANNER")
    print("=" * 50)
    print("✅ CONFIGURACIÓN:")
    print(f"   • Telegram: {'✅' if scanner.telegram.enabled else '❌'}")
    print(f"   • APIs externas: ❌ (solo DexScreener)")
    print("=" * 50)
    print("🎯 PARÁMETROS:")
    print(f"   • Breakout: {Config.BREAKOUT_PERCENT}%")
    print(f"   • Consolidación: ±{Config.CONSOLIDATION_THRESHOLD}%") 
    print(f"   • Liquidez mínima: ${Config.MIN_LIQUIDITY:,}")
    print("=" * 50)
    print("⚡ INICIANDO EN 3 SEGUNDOS...")
    
    await asyncio.sleep(3)
    
    try:
        await scanner.start_scanning()
    except KeyboardInterrupt:
        logger.info("🛑 Detenido por usuario")
    finally:
        scanner.stop_scanning()

if __name__ == "__main__":
    asyncio.run(main())
