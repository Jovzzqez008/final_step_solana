# main.py
import os
import asyncio
import logging
from datetime import datetime
from typing import Dict

import aiohttp
import asyncpg
import websockets
import json
from fastapi import FastAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuración
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
PUMP_PORTAL_WSS = "wss://pumpportal.fun/api/data"

# Parámetros de trading
MIN_GAIN_PERCENT = 300    # +300% en 15min
MAX_TIME_WINDOW = 15      # 15 minutos  
STOP_LOSS_PERCENT = 50    # -50%
MAX_MONITOR_TIME = 30     # 30 minutos máximo

# FastAPI app
app = FastAPI(title="Pump.fun Monitor Bot")

class PumpFunMonitor:
    def __init__(self):
        self.monitoring_tasks = {}
        self.db_pool = None
        self.telegram_app = None
        self.running = False
        
    async def initialize(self):
        """Inicializar todos los componentes"""
        # Base de datos
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        
        # Telegram Bot
        self.telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Comandos
        self.telegram_app.add_handler(CommandHandler("start", self.start_command))
        self.telegram_app.add_handler(CommandHandler("iniciar", self.iniciar_command))
        self.telegram_app.add_handler(CommandHandler("detener", self.detener_command))
        self.telegram_app.add_handler(CommandHandler("status", self.status_command))
        
        # Inicializar bot
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        
        logging.info("✅ Monitor inicializado correctamente")
        
    async def _create_tables(self):
        """Crear tablas necesarias"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS token_monitoring (
                    mint TEXT PRIMARY KEY,
                    symbol TEXT,
                    initial_price DECIMAL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'monitoring'
                )
            """)
    
    async def start_monitoring(self):
        """Iniciar monitoreo de tokens"""
        self.running = True
        logging.info("🎯 Iniciando monitoreo de Pump.fun...")
        
        while self.running:
            try:
                async with websockets.connect(PUMP_PORTAL_WSS) as ws:
                    # Suscribirse a nuevos tokens
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    logging.info("✅ Conectado a PumpPortal WebSocket")
                    
                    async for message in ws:
                        if not self.running:
                            break
                        await self.handle_new_token(message)
                        
            except Exception as e:
                logging.error(f"❌ WebSocket error: {e}")
                await asyncio.sleep(5)
    
    async def handle_new_token(self, message: str):
        """Procesar nuevo token detectado"""
        try:
            data = json.loads(message)
            mint = data.get('mint')
            
            if not mint or mint in self.monitoring_tasks:
                return
                
            # Obtener precio inicial
            initial_price = await self.get_token_price(mint)
            if not initial_price or initial_price <= 0:
                return
                
            symbol = f"TOKEN_{mint[:6]}"
            
            # Guardar en base de datos
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO token_monitoring (mint, symbol, initial_price)
                    VALUES ($1, $2, $3)
                """, mint, symbol, initial_price)
            
            # Iniciar monitoreo individual
            task = asyncio.create_task(
                self.monitor_token(mint, symbol, initial_price)
            )
            self.monitoring_tasks[mint] = task
            
            logging.info(f"🎯 Nuevo token en monitoreo: {symbol}")
            
        except Exception as e:
            logging.error(f"❌ Error procesando token: {e}")
    
    async def monitor_token(self, mint: str, symbol: str, initial_price: float):
        """Monitorear token individualmente"""
        start_time = datetime.now()
        max_price = initial_price
        
        while self.running:
            try:
                # Verificar tiempo máximo
                elapsed = (datetime.now() - start_time).total_seconds() / 60
                if elapsed > MAX_MONITOR_TIME:
                    await self.cleanup_token(mint, "timeout")
                    return
                
                # Obtener precio actual
                current_data = await self.get_token_data(mint)
                if not current_data:
                    await asyncio.sleep(10)
                    continue
                    
                current_price = current_data['price']
                
                # Actualizar precio máximo
                if current_price > max_price:
                    max_price = current_price
                
                # Calcular ganancia
                gain_percent = ((current_price - initial_price) / initial_price) * 100
                
                # ⚡ REGLA PRINCIPAL: +300% en 15min
                if gain_percent >= MIN_GAIN_PERCENT and elapsed <= MAX_TIME_WINDOW:
                    await self.send_alert(mint, symbol, gain_percent, elapsed, current_data)
                    await self.cleanup_token(mint, "alert_sent")
                    return
                
                # 🛑 STOP LOSS: -50% desde máximo
                current_loss = ((current_price - max_price) / max_price) * 100
                if current_loss <= -STOP_LOSS_PERCENT:
                    await self.cleanup_token(mint, "stop_loss")
                    return
                
                await asyncio.sleep(10)  # Verificar cada 10 segundos
                
            except Exception as e:
                logging.error(f"❌ Error monitoreando {symbol}: {e}")
                await asyncio.sleep(30)
    
    async def send_alert(self, mint: str, symbol: str, gain: float, elapsed: float, data: Dict):
        """Enviar alerta a Telegram"""
        message = (
            f"🚀 <b>ALERTA DE PUMP DETECTADA!</b> 🚀\n\n"
            f"<b>{symbol}</b>\n"
            f"• Ganancia: <b>+{gain:.1f}%</b>\n"
            f"• Tiempo: {elapsed:.1f} minutos\n"
            f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>"
        )
        
        keyboard = [
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}")]
        ]
        
        try:
            await self.telegram_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True
            )
            logging.info(f"✅ Alerta enviada: {symbol} +{gain:.1f}%")
        except Exception as e:
            logging.error(f"❌ Error enviando alerta: {e}")
    
    async def get_token_price(self, mint: str) -> float:
        """Obtener precio de token desde DexScreener"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('pairs') and len(data['pairs']) > 0:
                            return float(data['pairs'][0]['priceUsd'])
        except Exception as e:
            logging.error(f"❌ Error obteniendo precio: {e}")
        return 0
    
    async def get_token_data(self, mint: str) -> Dict:
        """Obtener datos completos del token"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('pairs') and len(data['pairs']) > 0:
                            pair = data['pairs'][0]
                            return {
                                'price': float(pair['priceUsd']),
                                'market_cap': float(pair.get('marketCap', 0)),
                                'liquidity': float(pair.get('liquidity', {}).get('usd', 0))
                            }
        except Exception as e:
            logging.error(f"❌ Error obteniendo datos: {e}")
        return {}
    
    async def cleanup_token(self, mint: str, reason: str):
        """Limpiar token del monitoreo"""
        if mint in self.monitoring_tasks:
            del self.monitoring_tasks[mint]
        
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE token_monitoring SET status = $1 WHERE mint = $2", 
                reason, mint
            )
        
        logging.info(f"🛑 Token {mint} eliminado: {reason}")
    
    async def stop_monitoring(self):
        """Detener todo el monitoreo"""
        self.running = False
        
        # Cancelar todas las tareas
        for mint, task in self.monitoring_tasks.items():
            task.cancel()
        self.monitoring_tasks.clear()
        
        logging.info("⛔ Monitoreo detenido")
    
    # Comandos de Telegram
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 <b>PUMP.FUN MONITOR BOT</b>\n\n"
            "• Detección en tiempo real via PumpPortal\n"
            "• Alerta: +300% en 15 minutos\n"
            "• Stop Loss: -50%\n" 
            "• Máximo: 30 minutos por token\n\n"
            "Usa /iniciar para comenzar el monitoreo",
            parse_mode="HTML"
        )
    
    async def iniciar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.running:
            asyncio.create_task(self.start_monitoring())
            await update.message.reply_text("✅ Monitor iniciado")
        else:
            await update.message.reply_text("⚠️ El monitor ya está en ejecución")
    
    async def detener_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.running:
            await self.stop_monitoring()
            await update.message.reply_text("⛔ Monitor detenido")
        else:
            await update.message.reply_text("⚠️ El monitor no está en ejecución")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = "🟢 ACTIVO" if self.running else "🔴 INACTIVO"
        tokens = len(self.monitoring_tasks)
        
        await update.message.reply_text(
            f"📊 <b>ESTADO DEL MONITOR</b>\n\n"
            f"• Estado: {status}\n"
            f"• Tokens monitoreados: {tokens}\n"
            f"• Regla: +{MIN_GAIN_PERCENT}% en {MAX_TIME_WINDOW}min\n"
            f"• Stop Loss: {STOP_LOSS_PERCENT}%",
            parse_mode="HTML"
        )

# Instancia global del monitor
monitor = PumpFunMonitor()

@app.on_event("startup")
async def startup():
    """Inicializar al iniciar la app"""
    try:
        await monitor.initialize()
        logging.info("🚀 Aplicación iniciada correctamente")
    except Exception as e:
        logging.error(f"❌ Error en startup: {e}")

@app.on_event("shutdown")
async def shutdown():
    """Limpiar al apagar la app"""
    await monitor.stop_monitoring()
    if monitor.telegram_app:
        await monitor.telegram_app.stop()
        await monitor.telegram_app.shutdown()
    logging.info("✅ Aplicación apagada correctamente")

@app.get("/")
async def root():
    return {
        "message": "Pump.fun Monitor Bot", 
        "status": "running",
        "version": "1.0"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "monitor_running": monitor.running,
        "tokens_monitored": len(monitor.monitoring_tasks)
    }

@app.post("/start-monitor")
async def start_monitor():
    """Endpoint para iniciar monitoreo manualmente"""
    if not monitor.running:
        asyncio.create_task(monitor.start_monitoring())
        return {"status": "monitor_started"}
    return {"status": "already_running"}

@app.post("/stop-monitor")
async def stop_monitor():
    """Endpoint para detener monitoreo manualmente"""
    if monitor.running:
        await monitor.stop_monitoring()
        return {"status": "monitor_stopped"}
    return {"status": "already_stopped"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
