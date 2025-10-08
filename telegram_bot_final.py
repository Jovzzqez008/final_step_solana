# early_breakout_scanner.py
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

# -------------------- CONFIGURACIÓN PARA DETECCIÓN TEMPRANA --------------------
class EarlyBreakoutConfig:
    # PARÁMETROS OPTIMIZADOS PARA DETECCIÓN TEMPRANA
    PRICE_INTERVAL = 300  # 5 minutos (más frecuente para capturar movimientos tempranos)
    CONSOLIDATION_THRESHOLD = 2.0  # ±2% para consolidación (más sensible)
    BREAKOUT_PERCENT = 5.0  # 5% mínimo para alerta (más temprano)
    MIN_CONSOLIDATION_HOURS = 4  # 4 horas mínimas de consolidación
    MIN_LIQUIDITY = 25000  # $25K mínimo (para tokens más jóvenes)
    VOLUME_SPIKE_MULTIPLIER = 2.0  # 2.0x volumen para confirmación
    
    # Fuentes para tokens con potencial (no solo los más populares)
    DEXSCREENER_SOURCES = {
        'rising': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=priceChange24h&order=desc&limit=100",
        'new_pairs': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=age&order=asc&limit=80",
        'volume_spikes': "https://api.dexscreener.com/latest/dex/pairs/solana?sort=volume24h&order=desc&limit=100"
    }

# -------------------- DETECTOR PARA PATRÓN TEMPRANO --------------------
class EarlyPatternDetector:
    """
    Detector especializado en capturar el patrón INICIAL de consolidación + breakout
    en tokens que están empezando a moverse.
    """
    
    def __init__(self):
        self.price_history: Dict[str, Deque[Dict]] = {}
        self.consolidation_trackers: Dict[str, Dict] = {}
        self.detected_patterns: Dict[str, float] = {}
        self.potential_tokens: Dict[str, Dict] = {}  # Tokens en fase de consolidación
        
    def analyze_early_pattern(self, token: str, price_data: Dict) -> Optional[Dict]:
        """
        Analiza específicamente el patrón INICIAL de consolidación + breakout
        """
        current_price = price_data['price']
        current_time = time.time()
        symbol = price_data.get('symbol', 'Unknown')
        
        # Inicializar historial si es nuevo token
        if token not in self.price_history:
            self.price_history[token] = deque(maxlen=48)  # 4 horas de datos (5min intervals)
            self.consolidation_trackers[token] = {
                'in_consolidation': False,
                'consolidation_start': None,
                'price_range': (0, 0),
                'volume_base': 0,
                'consolidation_hours': 0,
                'breakout_attempts': 0
            }
        
        # Agregar nuevo dato al historial
        self.price_history[token].append(price_data)
        
        # Solo analizar si tenemos suficientes datos
        if len(self.price_history[token]) < 12:  # 1 hora mínima
            return None
        
        prices = [p['price'] for p in self.price_history[token]]
        volumes = [p.get('volume_24h', 0) for p in self.price_history[token]]
        timestamps = [p['timestamp'] for p in self.price_history[token]]
        
        # 1. Detectar fase de consolidación TEMPRANA
        consolidation_signal = self._detect_early_consolidation(token, prices, volumes, timestamps)
        
        # 2. Detectar breakout INICIAL (no necesariamente masivo)
        breakout_signal = self._detect_early_breakout(token, current_price, volumes[-1] if volumes else 0, consolidation_signal)
        
        # 3. Seguimiento de tokens con potencial (aún en consolidación)
        potential_signal = self._track_potential_tokens(token, price_data, consolidation_signal)
        
        if breakout_signal:
            logger.info(f"🚨 BREAKOUT TEMPRANO DETECTADO: {symbol} - Breakout +{breakout_signal['breakout_percent']:.2f}%")
            # Remover de potenciales si ya hizo breakout
            if token in self.potential_tokens:
                del self.potential_tokens[token]
            return breakout_signal
        
        return None
    
    def _detect_early_consolidation(self, token: str, prices: List[float], volumes: List[float], timestamps: List[float]) -> Dict:
        """Detecta consolidación en etapas tempranas."""
        if len(prices) < 12:  # Mínimo 1 hora
            return {'in_consolidation': False}
        
        # Analizar las últimas 2-4 horas para consolidación
        recent_prices = prices[-24:] if len(prices) >= 24 else prices
        recent_volumes = volumes[-24:] if len(volumes) >= 24 else volumes
        
        min_price = min(recent_prices)
        max_price = max(recent_prices)
        price_range_pct = ((max_price - min_price) / min_price) * 100
        
        # Está en consolidación si el rango de precio es < threshold
        is_consolidating = price_range_pct <= EarlyBreakoutConfig.CONSOLIDATION_THRESHOLD
        
        tracker = self.consolidation_trackers[token]
        
        if is_consolidating:
            if not tracker['in_consolidation']:
                # Nueva consolidación detectada - POTENCIAL ALTO
                tracker.update({
                    'in_consolidation': True,
                    'consolidation_start': timestamps[0],
                    'price_range': (min_price, max_price),
                    'volume_base': statistics.mean(recent_volumes) if recent_volumes else 0,
                    'consolidation_hours': (timestamps[-1] - timestamps[0]) / 3600,
                    'breakout_attempts': 0
                })
                logger.info(f"🔍 NUEVA CONSOLIDACIÓN DETECTADA: {token} - {price_range_pct:.2f}% rango")
            else:
                # Actualizar duración existente
                tracker['consolidation_hours'] = (timestamps[-1] - tracker['consolidation_start']) / 3600
        else:
            tracker['in_consolidation'] = False
        
        return tracker
    
    def _detect_early_breakout(self, token: str, current_price: float, current_volume: float, consolidation_data: Dict) -> Optional[Dict]:
        """Detecta breakout INICIAL (no necesariamente masivo)."""
        if not consolidation_data['in_consolidation']:
            return None
        
        consolidation_high = consolidation_data['price_range'][1]
        consolidation_hours = consolidation_data['consolidation_hours']
        volume_base = consolidation_data['volume_base']
        
        # Calcular movimiento desde el máximo de consolidación
        move_from_consolidation = ((current_price - consolidation_high) / consolidation_high) * 100
        
        # Verificar volumen spike (confirmación)
        volume_spike = current_volume / volume_base if volume_base > 0 else 1
        
        # Criterios MÁS FLEXIBLES para detección temprana
        is_valid_breakout = (
            move_from_consolidation >= EarlyBreakoutConfig.BREAKOUT_PERCENT and
            consolidation_hours >= EarlyBreakoutConfig.MIN_CONSOLIDATION_HOURS and
            volume_spike >= EarlyBreakoutConfig.VOLUME_SPIKE_MULTIPLIER
        )
        
        # Prevenir alertas duplicadas (2 horas mínimo entre alertas)
        last_alert = self.detected_patterns.get(token, 0)
        time_since_last_alert = time.time() - last_alert
        
        if is_valid_breakout and time_since_last_alert > 7200:  # 2 horas
            breakout_signal = {
                'token': token,
                'breakout_percent': move_from_consolidation,
                'consolidation_hours': consolidation_hours,
                'volume_spike': volume_spike,
                'current_price': current_price,
                'consolidation_range': consolidation_data['price_range'],
                'timestamp': time.time(),
                'pattern': 'early_breakout',
                'stage': 'initial_breakout'  # ¡Esta es la clave!
            }
            
            self.detected_patterns[token] = time.time()
            return breakout_signal
        
        return None
    
    def _track_potential_tokens(self, token: str, price_data: Dict, consolidation_data: Dict) -> None:
        """Hace seguimiento de tokens que están en consolidación (potencial futuro)."""
        if consolidation_data['in_consolidation']:
            consolidation_hours = consolidation_data['consolidation_hours']
            
            # Solo trackear si lleva al menos 2 horas en consolidación
            if consolidation_hours >= 2 and token not in self.potential_tokens:
                self.potential_tokens[token] = {
                    'symbol': price_data.get('symbol', 'Unknown'),
                    'consolidation_start': consolidation_data['consolidation_start'],
                    'consolidation_hours': consolidation_hours,
                    'price_range': consolidation_data['price_range'],
                    'liquidity': price_data.get('liquidity', 0),
                    'first_detected': time.time()
                }
                logger.info(f"🎯 NUEVO POTENCIAL: {price_data.get('symbol', 'Unknown')} - {consolidation_hours:.1f}h consolidación")
        
        # Reporte periódico de tokens en consolidación
        if int(time.time()) % 1800 == 0:  # Cada 30 minutos
            self._report_potential_tokens()
    
    def _report_potential_tokens(self):
        """Reporta tokens que están en fase de consolidación (potenciales futuros)."""
        if self.potential_tokens:
            logger.info(f"🔍 TOKENS EN CONSOLIDACIÓN ({len(self.potential_tokens)}):")
            for token, data in list(self.potential_tokens.items()):
                # Limpiar tokens que ya no están en consolidación
                if time.time() - data['first_detected'] > 86400:  # 24 horas máximo
                    del self.potential_tokens[token]
                    continue
                
                logger.info(f"   • {data['symbol']}: {data['consolidation_hours']:.1f}h consolidación | "
                           f"Rango: ±{((data['price_range'][1]-data['price_range'][0])/data['price_range'][0]*100):.2f}%")

# -------------------- BUSCADOR DE TOKENS CON POTENCIAL --------------------
class PotentialTokenFinder:
    """Encuentra tokens con potencial de breakout temprano."""
    
    def __init__(self):
        self.tracked_tokens = set()
    
    async def find_potential_tokens(self) -> List[str]:
        """Encuentra tokens con características de potencial breakout."""
        logger.info("🔍 Buscando tokens con potencial de breakout temprano...")
        
        tokens = set()
        
        # 1. Tokens con crecimiento reciente (pero no masivo)
        rising_tokens = await self._get_rising_tokens()
        tokens.update(rising_tokens)
        
        # 2. Tokens nuevos con buena liquidez
        new_tokens = await self._get_new_tokens_with_potential()
        tokens.update(new_tokens)
        
        # 3. Tokens con spikes de volumen reciente
        volume_tokens = await self._get_volume_spike_tokens()
        tokens.update(volume_tokens)
        
        # 4. Siempre incluir Tokabu para referencia
        tokens.add("H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump")
        
        self.tracked_tokens = tokens
        logger.info(f"🎯 Encontrados {len(tokens)} tokens con potencial")
        return list(tokens)
    
    async def _get_rising_tokens(self) -> List[str]:
        """Obtiene tokens con crecimiento reciente pero no extremo."""
        tokens = []
        try:
            url = EarlyBreakoutConfig.SOURCES['rising']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:80]:
                        price_change_24h = float(pair.get('priceChange', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        
                        # Buscar tokens con crecimiento moderado (10%-100%)
                        if (10 <= price_change_24h <= 200 and
                            liquidity >= EarlyBreakoutConfig.MIN_LIQUIDITY):
                            
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address and token_address != 'unknown':
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error obteniendo tokens en crecimiento: {e}")
        
        return tokens
    
    async def _get_new_tokens_with_potential(self) -> List[str]:
        """Obtiene tokens nuevos pero con métricas prometedoras."""
        tokens = []
        try:
            url = EarlyBreakoutConfig.SOURCES['new_pairs']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:60]:
                        created_at = pair.get('pairCreatedAt')
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        volume_24h = float(pair.get('volume', {}).get('h24', 0))
                        
                        # Tokens de 1-30 días con buena liquidez
                        if created_at:
                            age_days = (time.time() * 1000 - created_at) / (1000 * 86400)
                            if (1 <= age_days <= 30 and
                                liquidity >= EarlyBreakoutConfig.MIN_LIQUIDITY and
                                volume_24h >= 5000):  # $5K volumen mínimo
                                
                                token_address = pair.get('baseToken', {}).get('address')
                                if token_address:
                                    tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error obteniendo tokens nuevos: {e}")
        
        return tokens
    
    async def _get_volume_spike_tokens(self) -> List[str]:
        """Obtiene tokens con spikes de volumen reciente."""
        tokens = []
        try:
            url = EarlyBreakoutConfig.SOURCES['volume_spikes']
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for pair in data.get('pairs', [])[:70]:
                        volume_24h = float(pair.get('volume', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        
                        # Tokens con volumen decente pero no masivo
                        if (5000 <= volume_24h <= 500000 and  # $5K - $500K volumen
                            liquidity >= 10000):  # $10K liquidez mínima
                            
                            token_address = pair.get('baseToken', {}).get('address')
                            if token_address:
                                tokens.append(token_address)
        except Exception as e:
            logger.error(f"Error obteniendo tokens con volumen: {e}")
        
        return tokens

# -------------------- SISTEMA PRINCIPAL MEJORADO --------------------
class EarlyBreakoutScanner:
    """
    Sistema especializado en detectar breakouts TEMPRANOS
    en tokens que están empezando a moverse.
    """
    
    def __init__(self):
        self.token_finder = PotentialTokenFinder()
        self.pattern_detector = EarlyPatternDetector()
        self.is_running = False
        self.alert_count = 0
    
    async def start_early_scan(self, telegram_context=None):
        """Inicia el escaneo para breakouts tempranos."""
        self.is_running = True
        logger.info("🚀 INICIANDO EARLY BREAKOUT SCANNER")
        logger.info("🎯 Objetivo: Tokens que empiezan patrón consolidación + breakout")
        
        while self.is_running:
            try:
                # 1. Obtener lista de tokens con potencial
                potential_tokens = await self.token_finder.find_potential_tokens()
                
                # 2. Monitorear cada token
                cycle_alerts = 0
                for token in potential_tokens:
                    try:
                        price_data = await self.get_token_data(token)
                        if price_data and price_data['price'] > 0:
                            # Analizar patrón de breakout TEMPRANO
                            pattern_signal = self.pattern_detector.analyze_early_pattern(token, price_data)
                            
                            if pattern_signal:
                                cycle_alerts += 1
                                self.alert_count += 1
                                await self.send_early_alert(token, price_data, pattern_signal, telegram_context)
                        
                        # Rate limiting más agresivo
                        await asyncio.sleep(0.5)
                        
                    except Exception as e:
                        logger.debug(f"Error monitoreando {token}: {e}")
                        continue
                
                if cycle_alerts > 0:
                    logger.info(f"🚨 Ciclo completado: {cycle_alerts} alertas de breakout temprano")
                
                # Reporte de estado cada ciclo
                logger.info(f"📊 Estadísticas: {self.alert_count} alertas totales - "
                           f"Monitoreando {len(potential_tokens)} tokens - "
                           f"{len(self.pattern_detector.potential_tokens)} en consolidación")
                
                # Esperar hasta próximo ciclo
                await asyncio.sleep(EarlyBreakoutConfig.PRICE_INTERVAL)
                
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
    
    async def send_early_alert(self, token: str, price_data: Dict, pattern: Dict, context=None):
        """Envía alerta específica de breakout TEMPRANO."""
        symbol = price_data.get('symbol', 'N/A')
        name = price_data.get('name', 'N/A')
        liquidity = price_data.get('liquidity', 0)
        
        alert_message = (
            f"🚀 *BREAKOUT TEMPRANO DETECTADO* 🚀\n\n"
            f"*Token:* {symbol} ({name})\n"
            f"*Address:* `{token}`\n"
            f"*Breakout:* +{pattern['breakout_percent']:.2f}% 📈\n"
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
            f"⚡ *ESTRATEGIA RECOMENDADA:*\n"
            f"• Entrada temprana en breakout inicial\n"
            f"• Stop loss: -3% desde entrada\n"
            f"• Take profit: +10-15% objetivo\n\n"
            f"🎯 *PATRÓN: BREAKOUT INICIAL DETECTADO*"
        )
        
        logger.info(f"🚀 ALERTA BREAKOUT TEMPRANO: {symbol} +{pattern['breakout_percent']:.2f}% después de {pattern['consolidation_hours']:.1f}h consolidación")
        
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
            print("🚀 ALERTA BREAKOUT TEMPRANO:")
            print(f"Token: {symbol} ({name})")
            print(f"Breakout: +{pattern['breakout_percent']:.2f}%")
            print(f"Consolidación: {pattern['consolidation_hours']:.1f} horas")
            print(f"Volumen: {pattern['volume_spike']:.1f}x spike")
            print(f"ESTRATEGIA: Entrada temprana | Stop loss -3% | TP +10-15%")
            print(f"{'='*60}\n")
    
    def stop_scanning(self):
        """Detiene el escaneo."""
        self.is_running = False
        logger.info("🛑 Early Breakout Scanner detenido")

# -------------------- EJECUCIÓN INMEDIATA --------------------
async def main():
    """Función principal para ejecutar el scanner temprano."""
    scanner = EarlyBreakoutScanner()
    
    print("🚀 EARLY BREAKOUT SCANNER")
    print("=" * 50)
    print("OBJETIVO: Detectar tokens que EMPIEZAN patrón")
    print("consolidación + breakout (CAPTURAR DESPEGUE)")
    print("=" * 50)
    print("PARÁMETROS PARA DETECCIÓN TEMPRANA:")
    print(f"• Consolidación: ±{EarlyBreakoutConfig.CONSOLIDATION_THRESHOLD}%")
    print(f"• Breakout: {EarlyBreakoutConfig.BREAKOUT_PERCENT}% mínimo (MÁS TEMPRANO)")
    print(f"• Liquidez mínima: ${EarlyBreakoutConfig.MIN_LIQUIDITY:,}")
    print(f"• Consolidación mínima: {EarlyBreakoutConfig.MIN_CONSOLIDATION_HOURS}h")
    print("=" * 50)
    
    try:
        # Ejecutar por 30 minutos para prueba
        await asyncio.wait_for(
            scanner.start_early_scan(),
            timeout=1800  # 30 minutos
        )
    except asyncio.TimeoutError:
        logger.info("⏰ Prueba completada (30 minutos)")
    except KeyboardInterrupt:
        logger.info("🛑 Detenido por usuario")
    finally:
        scanner.stop_scanning()

if __name__ == "__main__":
    # Ejecutar el scanner temprano
    asyncio.run(main())
