# bot_pump_alert_final.py
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import aiohttp
import redis
import websockets
from sqlalchemy import create_engine, Column, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

class Config:
    # PumpPortal WebSocket
    PUMP_PORTAL_WSS = os.getenv('PUMP_PORTAL_WSS', 'wss://pumpportal.fun/api/data')
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    # Database
    DATABASE_URL = os.getenv('DATABASE_URL')
    REDIS_URL = os.getenv('REDIS_URL')
    
    # Alert Configuration
    ALERT_THRESHOLD_PERCENT = float(os.getenv('ALERT_THRESHOLD_PERCENT', '300'))
    MONITORING_WINDOW_MINUTES = int(os.getenv('MONITORING_WINDOW_MINUTES', '30'))
    DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', '-30'))
    CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '5'))
    
    # URLs
    DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/"
    PUMP_FUN_BASE = "https://pump.fun/token/"
    RUGCHECK_BASE = "https://rugcheck.xyz/tokens/"
    BIRDEYE_BASE = "https://birdeye.so/token/"

# =============================================================================
# BASE DE DATOS
# =============================================================================

Base = declarative_base()

class TokenAlert(Base):
    __tablename__ = 'token_alerts'
    
    id = Column(String, primary_key=True)
    mint_address = Column(String, index=True)
    token_name = Column(String)
    symbol = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    initial_price = Column(Float)
    current_price = Column(Float)
    peak_price = Column(Float)
    alert_triggered = Column(Boolean, default=False)
    alert_sent_at = Column(DateTime)
    pattern_detected = Column(Text)
    gain_percent = Column(Float, default=0.0)

class DatabaseManager:
    def __init__(self):
        # Redis (in-memory watchlist)
        self.redis_client = None
        if Config.REDIS_URL:
            try:
                self.redis_client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)
                print("✅ Redis conectado correctamente")
            except Exception as e:
                print(f"❌ Error conectando Redis: {e}")
                self.redis_client = None

        # SQLAlchemy for history
        self.engine = None
        self.session = None
        if Config.DATABASE_URL:
            try:
                self.engine = create_engine(Config.DATABASE_URL, echo=False)
                Base.metadata.create_all(self.engine)
                Session = sessionmaker(bind=self.engine)
                self.session = Session()
                print("✅ PostgreSQL conectado correctamente")
            except Exception as e:
                print(f"❌ Error conectando PostgreSQL: {e}")
                self.engine = None
                self.session = None

    def add_token_to_watch(self, mint_address: str, token_data: Dict[str, Any]):
        """Agrega token a la lista de vigilancia"""
        try:
            # Guardar en Redis con TTL de 30 minutos
            if self.redis_client:
                key = f"token:{mint_address}"
                token_data['mint_address'] = mint_address
                token_data['created_at'] = datetime.utcnow().isoformat()
                token_data['alert_triggered'] = False
                
                self.redis_client.setex(
                    key, 
                    timedelta(minutes=Config.MONITORING_WINDOW_MINUTES), 
                    json.dumps(token_data)
                )
            
            # Guardar en PostgreSQL para historial
            if self.session:
                token_alert = TokenAlert(
                    id=mint_address,
                    mint_address=mint_address,
                    token_name=token_data.get('token_name', 'Unknown'),
                    symbol=token_data.get('symbol', 'Unknown'),
                    initial_price=token_data.get('initial_price', 0.0),
                    current_price=token_data.get('initial_price', 0.0),
                    peak_price=token_data.get('initial_price', 0.0)
                )
                self.session.merge(token_alert)
                self.session.commit()
            
            print(f"✅ Token agregado a vigilancia: {token_data.get('symbol', 'Unknown')} ({mint_address})")
            return True
            
        except Exception as e:
            print(f"❌ Error agregando token a vigilancia: {e}")
            if self.session:
                self.session.rollback()
            return False

    def update_token_price(self, mint_address: str, current_price: float):
        """Actualiza precio del token"""
        try:
            if not self.redis_client:
                return None
                
            key = f"token:{mint_address}"
            token_data_str = self.redis_client.get(key)
            if not token_data_str:
                return None
                
            token_data = json.loads(token_data_str)
            initial_price = float(token_data.get('initial_price', 0.0))
            peak_price = float(token_data.get('peak_price', initial_price))
            
            # Calcular ganancia porcentual
            gain_percent = ((current_price - initial_price) / initial_price * 100) if initial_price > 0 else 0.0
            peak_price = max(peak_price, current_price)
            
            token_data.update({
                'current_price': current_price,
                'peak_price': peak_price,
                'gain_percent': gain_percent
            })
            
            self.redis_client.setex(
                key, 
                timedelta(minutes=Config.MONITORING_WINDOW_MINUTES), 
                json.dumps(token_data)
            )
            
            # Actualizar PostgreSQL
            if self.session:
                token_alert = self.session.query(TokenAlert).filter_by(mint_address=mint_address).first()
                if token_alert:
                    token_alert.current_price = current_price
                    token_alert.peak_price = peak_price
                    token_alert.gain_percent = gain_percent
                    self.session.commit()
            
            return token_data
            
        except Exception as e:
            print(f"❌ Error actualizando precio: {e}")
            return None

    def get_all_watched_tokens(self) -> List[Dict[str, Any]]:
        """Obtiene todos los tokens bajo vigilancia"""
        tokens = []
        if not self.redis_client:
            return tokens
            
        try:
            for key in self.redis_client.scan_iter("token:*"):
                token_data_str = self.redis_client.get(key)
                if token_data_str:
                    token_data = json.loads(token_data_str)
                    tokens.append(token_data)
            return tokens
        except Exception as e:
            print(f"❌ Error obteniendo tokens vigilados: {e}")
            return []

    def remove_token(self, mint_address: str):
        """Elimina token de la vigilancia"""
        try:
            if self.redis_client:
                self.redis_client.delete(f"token:{mint_address}")
            print(f"✅ Token removido de vigilancia: {mint_address}")
        except Exception as e:
            print(f"❌ Error removiendo token: {e}")

    def mark_token_alerted(self, mint_address: str, pattern_detected: str):
        """Marca token como alertado"""
        try:
            if self.session:
                token_alert = self.session.query(TokenAlert).filter_by(mint_address=mint_address).first()
                if token_alert:
                    token_alert.alert_triggered = True
                    token_alert.alert_sent_at = datetime.utcnow()
                    token_alert.pattern_detected = pattern_detected
                    self.session.commit()
                    return True
            return False
        except Exception as e:
            print(f"❌ Error marcando token como alertado: {e}")
            return False

# =============================================================================
# TELEGRAM NOTIFIER
# =============================================================================

class TelegramNotifier:
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def send_alert(self, token_data: Dict[str, Any], pattern_detected: str):
        """Envía alerta a Telegram"""
        if not self.token or not self.chat_id:
            print("⚠️ Telegram no configurado, omitiendo envío")
            return False

        try:
            mint_address = token_data.get('mint_address', '')
            symbol = token_data.get('symbol', 'Unknown')
            name = token_data.get('token_name', 'Unknown')
            gain_percent = token_data.get('gain_percent', 0)
            current_price = token_data.get('current_price', 0)
            initial_price = token_data.get('initial_price', 0)
            
            # Calcular tiempo desde creación
            created_at = token_data.get('created_at')
            if isinstance(created_at, str):
                created_dt = datetime.fromisoformat(created_at)
            else:
                created_dt = datetime.utcnow()
            time_min = (datetime.utcnow() - created_dt).total_seconds() / 60

            # Construir mensaje
            message = f"🚨 **ALERTA DE MOMENTUM PUMP.FUN** 🚨\n\n"
            message += f"**Token:** {name} ({symbol})\n"
            message += f"**Patrón Detectado:** {pattern_detected}\n"
            message += f"**Ganancia:** +{gain_percent:.2f}%\n"
            message += f"**Tiempo desde creación:** {time_min:.1f} minutos\n"
            message += f"**Precio Inicial:** ${initial_price:.8f}\n"
            message += f"**Precio Actual:** ${current_price:.8f}\n\n"

            # Enlaces
            pump_fun_url = f"{Config.PUMP_FUN_BASE}{mint_address}"
            dexscreener_url = f"https://dexscreener.com/solana/{mint_address}"
            rugcheck_url = f"{Config.RUGCHECK_BASE}{mint_address}"

            message += f"🔗 **Enlaces:**\n"
            message += f"• [Pump.fun]({pump_fun_url})\n"
            message += f"• [DexScreener]({dexscreener_url})\n"
            message += f"• [RugCheck]({rugcheck_url})"

            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False
            }
            
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status == 200:
                    print(f"✅ Alerta Telegram enviada para {symbol}")
                    return True
                else:
                    error_text = await response.text()
                    print(f"❌ Error enviando alerta Telegram: {response.status} - {error_text}")
                    return False
                    
        except Exception as e:
            print(f"❌ Error en send_alert: {e}")
            return False

    async def close(self):
        """Cierra sesión HTTP"""
        if self.session:
            await self.session.close()

# =============================================================================
# PUMP PORTAL CLIENT
# =============================================================================

class PumpPortalClient:
    def __init__(self, db: DatabaseManager):
        self.uri = Config.PUMP_PORTAL_WSS
        self.db = db
        self.websocket = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        self.subscribed_tokens = set()

    async def connect_and_listen(self):
        """Conecta y escucha eventos de PumpPortal"""
        while True:
            try:
                print("🔌 Conectando a PumpPortal WebSocket...")
                async with websockets.connect(
                    self.uri, 
                    ping_interval=20, 
                    ping_timeout=10,
                    max_size=None
                ) as websocket:
                    self.websocket = websocket
                    self.reconnect_delay = 5
                    print("✅ Conectado a PumpPortal")

                    # Suscribirse a nuevos tokens
                    subscribe_payload = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_payload))
                    print("📝 Suscrito a nuevos tokens")

                    # Escuchar mensajes
                    async for message in websocket:
                        await self.handle_message(message)
                        
            except websockets.exceptions.ConnectionClosed:
                print(f"❌ Conexión WebSocket cerrada. Reconectando en {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
                
            except Exception as e:
                print(f"❌ Error en conexión PumpPortal: {e}")
                print(f"🔄 Reconectando en {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def handle_message(self, message):
        """Procesa mensajes de PumpPortal"""
        try:
            data = json.loads(message)
            print(f"📨 Mensaje recibido: {json.dumps(data)[:200]}...")
            
            # Detectar tipo de mensaje
            if isinstance(data, dict):
                # Mensaje de nuevo token
                if 'mint' in data:
                    await self.handle_new_token(data)
                # Mensaje de trade (actualizar precio)
                elif 'price' in data and 'mint' in data:
                    await self.handle_token_trade(data)
                # Mensaje de migración (graduación a Raydium)
                elif 'method' in data and data['method'] == 'migration':
                    await self.handle_migration(data)
                        
        except json.JSONDecodeError as e:
            print(f"❌ Error decodificando JSON: {e}")
        except Exception as e:
            print(f"❌ Error procesando mensaje: {e}")

    async def handle_new_token(self, token_data):
        """Procesa un nuevo token detectado"""
        try:
            mint_address = token_data.get('mint', '').strip()
            token_name = token_data.get('name', 'Unknown')
            symbol = token_data.get('symbol', 'Unknown')
            
            if not mint_address:
                print("⚠️ Token detectado sin dirección mint")
                return

            print(f"🎯 Nuevo token detectado: {token_name} ({symbol}) - {mint_address}")
            
            # Extraer precio inicial
            initial_price = self._extract_initial_price(token_data)
            
            # Crear payload para vigilancia
            token_payload = {
                "token_name": token_name,
                "symbol": symbol,
                "initial_price": initial_price,
                "current_price": initial_price,
                "peak_price": initial_price,
                "gain_percent": 0.0,
                "alert_triggered": False
            }
            
            # Agregar a la base de datos para vigilancia
            self.db.add_token_to_watch(mint_address, token_payload)
            
            # Suscribirse a trades de este token
            await self.subscribe_to_token_trades([mint_address])
            
        except Exception as e:
            print(f"❌ Error procesando nuevo token: {e}")

    async def handle_token_trade(self, trade_data):
        """Procesa un trade de token para actualizar precio"""
        try:
            mint_address = trade_data.get('mint', '').strip()
            price = trade_data.get('price')
            
            if mint_address and price is not None:
                # Actualizar precio en la base de datos
                self.db.update_token_price(mint_address, float(price))
                
        except Exception as e:
            print(f"❌ Error procesando trade: {e}")

    async def handle_migration(self, migration_data):
        """Procesa migración de token (graduación a Raydium)"""
        try:
            mint_address = migration_data.get('mint', '').strip()
            if mint_address:
                print(f"🎓 Token graduado a Raydium: {mint_address}")
                # Podrías agregar lógica adicional aquí si necesitas
                # hacer algo cuando un token se gradúa
                
        except Exception as e:
            print(f"❌ Error procesando migración: {e}")

    def _extract_initial_price(self, token_data):
        """Extrae el precio inicial del token"""
        price_fields = ['initialPrice', 'price', 'initial_price', 'startPrice', 'initialLiquidity']
        
        for field in price_fields:
            if field in token_data and token_data[field] is not None:
                try:
                    return float(token_data[field])
                except (ValueError, TypeError):
                    continue
        
        # Valor por defecto si no se encuentra precio
        return 0.000001

    async def subscribe_to_token_trades(self, mint_addresses):
        """Suscribe a trades de tokens específicos"""
        if not self.websocket:
            return
            
        try:
            # Filtrar tokens ya suscritos
            to_subscribe = [mint for mint in mint_addresses if mint not in self.subscribed_tokens]
            if not to_subscribe:
                return
                
            payload = {
                "method": "subscribeTokenTrade",
                "keys": to_subscribe
            }
            await self.websocket.send(json.dumps(payload))
            
            # Agregar a la lista de suscritos
            self.subscribed_tokens.update(to_subscribe)
            print(f"📊 Suscrito a trades de {len(to_subscribe)} tokens")
            
        except Exception as e:
            print(f"❌ Error suscribiendo a trades: {e}")

# =============================================================================
# TOKEN TRACKER
# =============================================================================

class TokenTracker:
    def __init__(self, db: DatabaseManager, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.analysis_count = 0

    async def analyze_tokens(self):
        """Analiza todos los tokens bajo vigilancia"""
        print("🔍 Iniciando análisis de tokens...")
        
        while True:
            try:
                tokens = self.db.get_all_watched_tokens()
                current_time = datetime.utcnow()
                self.analysis_count += 1
                
                if tokens:
                    print(f"📊 Analizando {len(tokens)} tokens (análisis #{self.analysis_count})...")
                
                for token_data in tokens:
                    await self._check_token_conditions(token_data, current_time)
                
                await asyncio.sleep(Config.CHECK_INTERVAL_SECONDS)
                
            except Exception as e:
                print(f"❌ Error en análisis de tokens: {e}")
                await asyncio.sleep(5)

    async def _check_token_conditions(self, token_data, current_time):
        """Verifica las condiciones para un token específico"""
        try:
            mint_address = token_data.get('mint_address', '')
            if not mint_address:
                return

            # Obtener timestamp de creación
            created_at_str = token_data.get('created_at')
            created_at = datetime.fromisoformat(created_at_str) if isinstance(created_at_str, str) else datetime.utcnow()
                
            time_since_creation = (current_time - created_at).total_seconds() / 60  # en minutos

            # Verificar si ha expirado (30 minutos)
            if time_since_creation > Config.MONITORING_WINDOW_MINUTES:
                print(f"🕒 Token {mint_address} expirado, eliminando...")
                self.db.remove_token(mint_address)
                return

            # Obtener datos de precio
            initial_price = float(token_data.get('initial_price', 0))
            current_price = float(token_data.get('current_price', initial_price))
            peak_price = float(token_data.get('peak_price', current_price))
            gain_percent = float(token_data.get('gain_percent', 0))
            alerted = token_data.get('alert_triggered', False)

            # Verificar dump (caída del umbral desde el pico)
            if peak_price > 0:
                dump_percent = ((current_price - peak_price) / peak_price) * 100
                if dump_percent <= Config.DUMP_THRESHOLD_PERCENT:
                    print(f"📉 Token {mint_address} en dump ({dump_percent:.2f}%), eliminando...")
                    self.db.remove_token(mint_address)
                    return

            # Verificar condición de alerta (umbral en ventana de tiempo)
            if (not alerted and 
                gain_percent >= Config.ALERT_THRESHOLD_PERCENT and 
                time_since_creation <= 15):
                
                pattern_detected = f"+{gain_percent:.0f}% en {time_since_creation:.1f} minutos"
                print(f"🚨 ALERTA DISPARADA: {token_data.get('symbol', 'Unknown')} - {pattern_detected}")
                
                # Enviar alerta
                success = await self.notifier.send_alert(token_data, pattern_detected)
                
                if success:
                    # Marcar como alertado
                    self.db.mark_token_alerted(mint_address, pattern_detected)
                    print(f"✅ Alerta procesada correctamente para {mint_address}")
                else:
                    print(f"❌ Falló el envío de alerta para {mint_address}")
            
            # Log periódico para tokens con ganancia significativa
            elif gain_percent > 100 and self.analysis_count % 10 == 0:
                symbol = token_data.get('symbol', 'Unknown')
                print(f"📈 Token {symbol} con +{gain_percent:.1f}% ({time_since_creation:.1f}m)")
                
        except Exception as e:
            print(f"❌ Error verificando condiciones del token: {e}")

# =============================================================================
# MAIN APPLICATION
# =============================================================================

class BotManager:
    def __init__(self):
        self.db = DatabaseManager()
        self.notifier = TelegramNotifier()
        self.pump_portal_client = PumpPortalClient(self.db)
        self.token_tracker = TokenTracker(self.db, self.notifier)
        self.is_running = True
        self.tasks = []

    def _check_config(self):
        """Verifica que todas las variables de entorno estén configuradas"""
        required_vars = [
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_CHAT_ID', 
            'DATABASE_URL',
            'REDIS_URL'
        ]
        
        missing_vars = []
        for var in required_vars:
            if not getattr(Config, var, None):
                missing_vars.append(var)
        
        if missing_vars:
            print(f"❌ ERROR: Variables de entorno faltantes: {', '.join(missing_vars)}")
            print("💡 Asegúrate de configurar estas variables en Railway")
            return False
        
        print("✅ Configuración verificada correctamente")
        return True

    async def start_services(self):
        """Inicia todos los servicios del bot"""
        print("🤖 Iniciando Bot de Alertas Pump.fun...")
        print("=" * 50)
        
        if not self._check_config():
            return False
        
        try:
            # Iniciar servicios en paralelo
            pump_portal_task = asyncio.create_task(self.pump_portal_client.connect_and_listen())
            tracker_task = asyncio.create_task(self.token_tracker.analyze_tokens())
            
            self.tasks = [pump_portal_task, tracker_task]
            
            print("✅ Todos los servicios iniciados correctamente")
            print("🔄 Bot en funcionamiento...")
            return True
            
        except Exception as e:
            print(f"❌ Error iniciando servicios: {e}")
            return False

    async def shutdown(self):
        """Apaga el bot limpiamente"""
        print("\n🛑 Apagando bot...")
        self.is_running = False
        
        # Cancelar todas las tareas
        for task in self.tasks:
            if not task.done():
                task.cancel()
        
        # Esperar a que las tareas se cancelen
        await asyncio.gather(*self.tasks, return_exceptions=True)
        
        # Cerrar conexiones
        await self.notifier.close()
        
        print("✅ Bot apagado correctamente")

def signal_handler(signum, frame):
    """Maneja señales de apagado"""
    print(f"\n📡 Señal {signum} recibida, iniciando apagado...")
    sys.exit(0)

async def main():
    # Configurar manejo de señales
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    bot_manager = BotManager()
    
    try:
        success = await bot_manager.start_services()
        if not success:
            print("❌ No se pudieron iniciar los servicios. Saliendo...")
            return
        
        # Mantener el bot corriendo
        while bot_manager.is_running:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🛑 Interrupción por teclado recibida")
    except Exception as e:
        print(f"❌ Error crítico: {e}")
    finally:
        await bot_manager.shutdown()

if __name__ == "__main__":
    # Configurar logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    print("🚀 Iniciando Bot de Alertas Pump.fun...")
    asyncio.run(main())
