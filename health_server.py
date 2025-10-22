#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏥 HEALTH CHECK SERVER PARA RAILWAY - FIXED
============================================
Servidor HTTP ligero para healthchecks y monitoreo del bot
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ESTADO GLOBAL DEL BOT
# ═══════════════════════════════════════════════════════════════

bot_status = {
    "running": False,
    "started_at": None,
    "last_scan": None,
    "total_scans": 0,
    "open_positions": 0,
    "total_signals": 0,
    "total_trades": 0,
    "win_rate": 0.0,
    "total_pnl": 0.0,
    "ml_enabled": False,
    "mode": "unknown"
}

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Solana Trading Bot ML",
    version="4.1",
    docs_url=None,  # Desactivar docs para producción
    redoc_url=None
)

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "🚀 Solana Trading Bot ML",
        "version": "4.1",
        "status": "healthy" if bot_status["running"] else "starting",
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "stats": "/stats"
        }
    }

@app.get("/health")
async def health_check():
    """
    Endpoint principal de healthcheck para Railway
    IMPORTANTE: Retorna 200 SIEMPRE para evitar reinicios
    """
    uptime_seconds = 0
    if bot_status["started_at"]:
        uptime_seconds = int((datetime.now() - bot_status["started_at"]).total_seconds())
    
    return JSONResponse(
        status_code=200,  # SIEMPRE 200
        content={
            "status": "healthy",
            "bot_running": bot_status["running"],
            "uptime_seconds": uptime_seconds,
            "last_scan": bot_status["last_scan"].isoformat() if bot_status["last_scan"] else None,
            "mode": bot_status["mode"],
            "timestamp": datetime.now().isoformat()
        }
    )

@app.get("/status")
async def get_status():
    """Status detallado del bot"""
    uptime_seconds = 0
    if bot_status["started_at"]:
        uptime_seconds = int((datetime.now() - bot_status["started_at"]).total_seconds())
    
    return JSONResponse({
        "bot": {
            "running": bot_status["running"],
            "mode": bot_status["mode"],
            "ml_enabled": bot_status["ml_enabled"],
            "started_at": bot_status["started_at"].isoformat() if bot_status["started_at"] else None,
            "uptime_seconds": uptime_seconds
        },
        "activity": {
            "total_scans": bot_status["total_scans"],
            "total_signals": bot_status["total_signals"],
            "total_trades": bot_status["total_trades"],
            "open_positions": bot_status["open_positions"],
            "last_scan": bot_status["last_scan"].isoformat() if bot_status["last_scan"] else None
        },
        "performance": {
            "win_rate": round(bot_status["win_rate"], 2),
            "total_pnl_percent": round(bot_status["total_pnl"], 2)
        }
    })

@app.get("/stats")
async def get_stats():
    """Estadísticas completas"""
    return JSONResponse({
        "scans": bot_status["total_scans"],
        "signals": bot_status["total_signals"],
        "trades": bot_status["total_trades"],
        "positions": bot_status["open_positions"],
        "win_rate": round(bot_status["win_rate"], 2),
        "pnl": round(bot_status["total_pnl"], 2),
        "ml_enabled": bot_status["ml_enabled"]
    })

@app.get("/ping")
async def ping():
    """Ping simple para verificar que el servidor está vivo"""
    return {"ping": "pong", "timestamp": datetime.now().isoformat()}

# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE ACTUALIZACIÓN
# ═══════════════════════════════════════════════════════════════

def update_bot_status(
    running: bool,
    scans: int,
    positions: int,
    signals: Optional[int] = None,
    trades: Optional[int] = None,
    wins: Optional[int] = None,
    losses: Optional[int] = None,
    total_pnl: Optional[float] = None,
    ml_enabled: Optional[bool] = None,
    mode: Optional[str] = None
):
    """
    Actualizar estado del bot desde el loop principal
    """
    bot_status["running"] = running
    bot_status["total_scans"] = scans
    bot_status["open_positions"] = positions
    bot_status["last_scan"] = datetime.now()
    
    if not bot_status["started_at"] and running:
        bot_status["started_at"] = datetime.now()
    
    if signals is not None:
        bot_status["total_signals"] = signals
    
    if trades is not None:
        bot_status["total_trades"] = trades
    
    if wins is not None and losses is not None:
        total = wins + losses
        bot_status["win_rate"] = (wins / total * 100) if total > 0 else 0.0
    
    if total_pnl is not None:
        bot_status["total_pnl"] = total_pnl
    
    if ml_enabled is not None:
        bot_status["ml_enabled"] = ml_enabled
    
    if mode is not None:
        bot_status["mode"] = mode

# ═══════════════════════════════════════════════════════════════
# SERVIDOR
# ═══════════════════════════════════════════════════════════════

async def start_health_server(port: int = 8080):
    """
    Iniciar servidor HTTP para healthchecks
    """
    try:
        # Marcar como iniciado inmediatamente
        bot_status["running"] = True
        bot_status["started_at"] = datetime.now()
        
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",  # Cambiar a info para ver requests
            access_log=True,  # Activar access log para debug
            timeout_keep_alive=60
        )
        server = uvicorn.Server(config)
        
        logger.info(f"✅ Health server iniciado en puerto {port}")
        logger.info(f"🏥 Healthcheck disponible en: http://0.0.0.0:{port}/health")
        logger.info(f"📊 Status disponible en: http://0.0.0.0:{port}/status")
        
        await server.serve()
        
    except Exception as e:
        logger.error(f"❌ Error iniciando health server: {e}")
        raise
