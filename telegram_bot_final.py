# tokabu_pattern_scanner.py
import asyncio
import time
import logging
from typing import Dict, List, Deque, Optional
from collections import deque
import httpx
import statistics

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------- CONFIGURACIÓN BASADA EN ANÁLISIS DE TOKABU --------------------
class TokabuBasedConfig:
    # PARÁMETROS OPTIMIZADOS BASADOS EN ANÁLISIS DE TOKABU
    PRICE_INTERVAL = 600  # 10 minutos (balance entre detección y eficiencia)
    CONSOLIDATION_THRESHOLD = 2.5  # ±2.5% para consolidación (recomendado)
    BREAKOUT_PERCENT = 7.0  # 7% mínimo para alerta (punto medio recomendado)
    MIN_CONSOLIDATION_HOURS = 6  # 6 horas mínimas de consolidación
    MIN_LIQUIDITY = 100000  # $100K mínimo (basado en Tokabu)
    VOLUME_SPIKE_MULTIPLIER = 2.5  # 2.5x volumen para confirmación
    
    # Fuentes para tokens maduros similares a Tokabu
    DEXSCREENER_SOURCES = {
        'high_liquidity': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=liquidity&order=desc&limit=100",
        'established_tokens': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=age&order=desc&limit=80"
    }

# -------------------- DETECTOR ESPECÍFICO PARA PATRÓN TOKABU --------------------
class TokabuPatternDetector:
    """
    Detector especializado en el patrón mostrado por Tokabu:
    - Token maduro (100+ días)
    - Alta liquidez ($100K+)
    - Consolidación prolongada seguida de breakout 6-8%
    - Volumen significativo para confirmación
    """
    
    def __init__(self):
        self.price_history: Dict[str, Deque[Dict]] = {}
        self.consolidation_trackers: Dict[str, Dict] = {}
        self.detected_patterns: Dict[str, float] = {}  # Para evitar duplicados
        
    def analyze_tokabu_pattern(self, token: str, price_data: Dict) -> Optional[Dict]:
        """
        Analiza específicamente el patrón tipo Tokabu:
        Consolidación + Breakout en token maduro
        """
        current_price = price_data['price']
        current_time = time.time()
        symbol = price_data.get('symbol', 'Unknown')
        
        # Inicializar historial si es nuevo token
        if token not in self.price_history:
            self.price_history[token] = deque(maxlen=72)  # 12 horas de datos (10min intervals)
            self.consolidation_trackers[token] = {
                'in_consolidation': False,
                'consolidation_start': None,
                'price_range': (0, 0),
                'volume_base': 0,
                'consolidation_hours': 0
            }
        
        # Agregar nuevo dato al historial
        self.price_history[token].append(price_data)
        
        # Solo analizar si tenemos suficientes datos
        if len(self.price_history[token]) < 18:  # 3 horas mínimas
            return None
        
        prices = [p['price'] for p in self.price_history[token]]
        volumes = [p.get('volume_24h', 0) for p in self.price_history[token]]
        timestamps = [p['timestamp'] for p in self.price_history[token]]
        
        # 1. Detectar fase de consolidación
        consolidation_signal = self._detect_tokabu_consolidation(token, prices, volumes, timestamps)
        
        # 2. Detectar breakout específico
        breakout_signal = self._detect_tokabu_breakout(token, current_price, volumes[-1] if volumes else 0, consolidation_signal)
        
        if breakout_signal:
            logger.info(f"🚨 PATRÓN TOKABU DETECTADO: {symbol} - Breakout +{breakout_signal['breakout_percent']:.2f}%")
            return breakout_signal
        
        return None
    
    def _detect_tokabu_consolidation(self, token: str, prices: List[float], volumes: List[float], timestamps: List[float]) -> Dict:
        """Detecta consolidación al estilo Tokabu."""
        if len(prices) < 18:  # Mínimo 3 horas
            return {'in_consolidation': False}
        
        # Analizar las últimas 6 horas para consolidación
        recent_prices = prices[-36:] if len(prices) >= 36 else prices
        recent_volumes = volumes[-36:] if len(volumes) >= 36 else volumes
        
        min_price = min(recent_prices)
        max_price = max(recent_prices)
        price_range_pct = ((max_price - min_price) / min_price) * 100
        
        # Está en consolidación si el rango de precio es < threshold
        is_consolidating = price_range_pct <= TokabuBasedConfig.CONSOLIDATION_THRESHOLD
        
        tracker = self.consolidation_trackers[token]
        
        if is_consolidating:
            if not tracker['in_consolidation']:
                # Nueva consolidación detectada
                tracker.update({
                    'in_consolidation': True,
                    'consolidation_start': timestamps[0],
                    'price_range': (min_price, max_price),
                    'volume_base': statistics.mean(recent_volumes) if recent_volumes else 0,
                    'consolidation_hours': (timestamps[-1] - timestamps[0]) / 3600
                })
            else:
                # Actualizar duración existente
                tracker['consolidation_hours'] = (timestamps[-1] - tracker['consolidation_start']) / 3600
        else:
            tracker['in_consolidation'] = False
        
        return tracker
    
    def _detect_tokabu_breakout(self, token: str, current_price: float, current_volume: float, consolidation_data: Dict) -> Optional[Dict]:
        """Detecta breakout específico siguiendo el patrón Tokabu."""
        if not consolidation_data['in_consolidation']:
            return None
        
        consolidation_high = consolidation_data['price_range'][1]
        consolidation_hours = consolidation_data['consolidation_hours']
        volume_base = consolidation_data['volume_base']
        
        # Calcular movimiento desde el máximo de consolidación
        move_from_consolidation = ((current_price - consolidation_high) / consolidation_high) * 100
        
        # Verificar volumen spike (confirmación)
        volume_spike = current_volume / volume_base if volume_base > 0 else 1
        
        # Criterios de breakout al estilo Tokabu
        is_valid_breakout = (
            move_from_consolidation >= TokabuBasedConfig.BREAKOUT_PERCENT and
            consolidation_hours >= TokabuBasedConfig.MIN_CONSOLIDATION_HOURS and
            volume_spike >= TokabuBasedConfig.VOLUME_SPIKE_MULTIPLIER
        )
        
        # Prevenir alertas duplicadas (4 horas mínimo entre alertas)
        last_alert = self.detected_patterns.get(token, 0)
        time_since_last_alert = time.time() - last_alert
        
        if is_valid_breakout and time_since_last_alert > 14400:  # 4 horas
            breakout_signal = {
                'token': token,
                'breakout_percent': move_from_consolidation,
                'consolidation_hours': consolidation_hours,
                'volume_spike': volume_spike,
                'current_price': current_price,
                'consolidation_range': consolidation_data['price_range'],
                'timestamp': time.time(),
                'pattern': 'tokabu_breakout'
            }
            
            self.detected_patterns[token] = time.time()
            return breakout_signal
        
        return None

# -------------------- BUSCADOR DE TOKENS SIMILARES A TOKABU --------------------
class MatureTokenFinder:
    """Encuentra tokens con características similares a Tokabu."""
    
    def __init__(self):
        self.mature_tokens = set()
    
    async def find_tokabu_like_tokens(self) -> List[str]:
        """Encuentra tokens maduros con buen volumen y liquidez."""
        logger.info("🔍 Buscando tokens similares a Tokabu...")
        
        tokens = set()
        
        # 1. Tokens con alta liquidez (como Tokabu)
        high_liquidity_tokens = await self._get_high_liquidity_tokens()
        tokens.update(high_liquidity_tokens)
        
        # 2. Tokens establecidos (edad similar)
        established_tokens = await self._get_established_tokens()
        tokens.update(established_tokens)
        
        # 3. Siempre incluir Tokabu para monitoreo
        tokens.add("H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump")
        
        self.mature_tokens = tokens
        logger.info(f"🎯 Encontrados {len(tokens)} tokens maduros para monitorear")
        return list(tokens)
    
    async def _get_high_liquidity_tokens(self) -> List[str]:
        """Obtiene tokens con alta liquidez similar a Tokabu."""
        tokens = []
        try:
            url = TokabuBasedConfig.DEXSCREENER_SOURCES['high_liquidity']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:60]:  # Top 60 por liquidez
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        if liquidity >= TokabuBasedConfig.MIN_LIQUIDITY:
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address and token_address != 'unknown':
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error obteniendo tokens por liquidez: {e}")
        
        return tokens
    
    async def _get_established_tokens(self) -> List[str]:
        """Obtiene tokens establecidos (no nuevos)."""
        tokens = []
        try:
            url = "https://api.dexscreener.com/latest/dex/pairs/solana?sort=volume24h&order=desc&limit=80"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:50]:
                        # Filtrar por edad aproximada (pair creation)
                        created_at = pair.get('pairCreatedAt')
                        if created_at:
                            age_days = (time.time() * 1000 - created_at) / (1000 * 86400)
                            if age_days > 30:  # Mínimo 30 días
                                token_address = pair.get('baseToken', {}).get('address')
                                if token_address:
                                    tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error obteniendo tokens establecidos: {e}")
        
        return tokens

# -------------------- SISTEMA PRINCIPAL TOKABU --------------------
class TokabuPatternScanner:
    """
    Sistema principal especializado en detectar el patrón Tokabu
    en tokens maduros con alta liquidez.
    """
    
    def __init__(self):
        self.token_finder = MatureTokenFinder()
        self.pattern_detector = TokabuPatternDetector()
        self.is_running = False
        self.alert_count = 0
    
    async def start_tokabu_scan(self, telegram_context=None):
        """Inicia el escaneo especializado para patrones Tokabu."""
        self.is_running = True
        logger.info("🚀 INICIANDO TOKABU PATTERN SCANNER")
        logger.info("🎯 Objetivo: Tokens maduros + Consolidación + Breakout 7%+")
        
        while self.is_running:
            try:
                # 1. Obtener lista de tokens maduros
                mature_tokens = await self.token_finder.find_tokabu_like_tokens()
                
                # 2. Monitorear cada token
                cycle_alerts = 0
                for token in mature_tokens:
                    try:
                        price_data = await self.get_token_data(token)
                        if price_data and price_data['price'] > 0:
                            # Analizar patrón específico Tokabu
                            pattern_signal = self.pattern_detector.analyze_tokabu_pattern(token, price_data)
                            
                            if pattern_signal:
                                cycle_alerts += 1
                                self.alert_count += 1
                                await self.send_tokabu_alert(token, price_data, pattern_signal, telegram_context)
                        
                        # Rate limiting respetuoso
                        await asyncio.sleep(1.5)
                        
                    except Exception as e:
                        logger.debug(f"Error monitoreando {token}: {e}")
                        continue
                
                if cycle_alerts > 0:
                    logger.info(f"🚨 Ciclo completado: {cycle_alerts} alertas de patrón Tokabu")
                
                logger.info(f"📊 Estadísticas: {self.alert_count} alertas totales - Monitoreando {len(mature_tokens)} tokens")
                
                # Esperar hasta próximo ciclo
                await asyncio.sleep(TokabuBasedConfig.PRICE_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error en ciclo principal: {e}")
                await asyncio.sleep(30)
    
    async def get_token_data(self, token_address: str) -> Optional[Dict]:
        """Obtiene datos de precio y volumen para un token."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
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
            logger.debug(f"Error obteniendo datos para {token_address}: {e}")
        
        return None
    
    async def send_tokabu_alert(self, token: str, price_data: Dict, pattern: Dict, context=None):
        """Envía alerta específica del patrón Tokabu."""
        symbol = price_data.get('symbol', 'N/A')
        name = price_data.get('name', 'N/A')
        liquidity = price_data.get('liquidity', 0)
        
        alert_message = (
            f"🎯 *PATRÓN TOKABU DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} ({name})\n"
            f"*Address:* `{token}`\n"
            f"*Breakout:* +{pattern['breakout_percent']:.2f}% 🚀\n"
            f"*Consolidación previa:* {pattern['consolidation_hours']:.1f} horas\n"
            f"*Spike de volumen:* {pattern['volume_spike']:.1f}x\n\n"
            f"*Rango de consolidación:*\n"
            f"• Mínimo: ${pattern['consolidation_range'][0]:.6f}\n"
            f"• Máximo: ${pattern['consolidation_range'][1]:.6f}\n"
            f"• Precio actual: ${pattern['current_price']:.6f}\n\n"
            f"*Métricas de calidad:*\n"
            f"• Liquidez: ${liquidity:,.2f}\n"
            f"• Volumen 24h: ${price_data.get('volume_24h', 0):,.2f}\n\n"
            f"🔗 *Análisis rápido:*\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token})\n"
            f"- [Chart 6h](https://dexscreener.com/solana/{token}?chart=interval=6h)\n"
            f"- [Birdeye](https://birdeye.so/token/{token}?chain=solana)\n\n"
            f"⚡ *Patrón: Token maduro con breakout después de consolidación prolongada*\n"
            f"📊 *Basado en análisis de Tokabu (123 días, $1M+ liquidez)*"
        )
        
        logger.info(f"🎯 ALERTA TOKABU: {symbol} +{pattern['breakout_percent']:.2f}% después de {pattern['consolidation_hours']:.1f}h consolidación")
        
        # Enviar a Telegram si hay contexto
        if context:
            try:
                await context.bot.send_message(
                    chat_id=context.job.chat_id,
                    text=alert_message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Error enviando alerta Telegram: {e}")
        else:
            # Solo log si no hay Telegram
            print(f"\n{'='*60}")
            print("🎯 ALERTA PATRÓN TOKABU:")
            print(f"Token: {symbol} ({name})")
            print(f"Breakout: +{pattern['breakout_percent']:.2f}%")
            print(f"Consolidación: {pattern['consolidation_hours']:.1f} horas")
            print(f"Volumen: {pattern['volume_spike']:.1f}x spike")
            print(f"{'='*60}\n")
    
    def stop_scanning(self):
        """Detiene el escaneo."""
        self.is_running = False
        logger.info("🛑 Tokabu Pattern Scanner detenido")

# -------------------- EJECUCIÓN INMEDIATA --------------------
async def main():
    """Función principal para ejecutar el scanner."""
    scanner = TokabuPatternScanner()
    
    print("🎯 TOKABU PATTERN SCANNER")
    print("=" * 50)
    print("OBJETIVO: Detectar tokens maduros con patrón de")
    print("consolidación + breakout (basado en análisis de Tokabu)")
    print("=" * 50)
    print("PARÁMETROS OPTIMIZADOS:")
    print(f"• Consolidación: ±{TokabuBasedConfig.CONSOLIDATION_THRESHOLD}%")
    print(f"• Breakout: {TokabuBasedConfig.BREAKOUT_PERCENT}% mínimo")
    print(f"• Liquidez mínima: ${TokabuBasedConfig.MIN_LIQUIDITY:,}")
    print(f"• Consolidación mínima: {TokabuBasedConfig.MIN_CONSOLIDATION_HOURS}h")
    print("=" * 50)
    
    try:
        # Ejecutar por 30 minutos para prueba
        await asyncio.wait_for(
            scanner.start_tokabu_scan(),
            timeout=1800  # 30 minutos
        )
    except asyncio.TimeoutError:
        logger.info("⏰ Prueba completada (30 minutos)")
    except KeyboardInterrupt:
        logger.info("🛑 Detenido por usuario")
    finally:
        scanner.stop_scanning()

if __name__ == "__main__":
    # Ejecutar el scanner
    asyncio.run(main())
