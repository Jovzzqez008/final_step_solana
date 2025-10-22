#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏥 HEALTH CHECK SERVER PARA RAILWAY - FIXED V2
===============================================
✅ Responde inmediatamente (no espera al bot)
✅ Usa PORT dinámico de Railway
✅ Siempre retorna 200 OK
"""

import asyncio
import logging
import os
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
    "started_at": datetime.now(),  # ✅ Marcar como iniciado INMEDIATAMENTE
    "last_scan": None,
    "total_scans": 0,
    "open_positions": 0,
    "total_signals": 0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "win_rate": 0.0,
    "total_pnl": 0.0,
    "ml_enabled": False,
    "mode": "starting"
}

# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Solana Trading Bot ML",
    version="4.2",
    docs_url=None,
    redoc_url=None
)

@app.get("/")
async def root():
    """Endpoint raíz"""
    return {
        "message": "🚀 Solana Trading Bot ML",
        "version": "4.2",
        "status": "healthy",  # ✅ SIEMPRE healthy
        "bot_status": bot_status["mode"],
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "stats": "/stats",
            "ping": "/ping"
        }
    }

@app.get("/health")
async def health_check():
    """
    ✅ CRÍTICO: Siempre retorna 200 OK para Railway
    Railway reinicia el servicio si recibe != 200
    """
    uptime_seconds = 0
    if bot_status["started_at"]:
        uptime_seconds = int((datetime.now() - bot_status["started_at"]).total_seconds())
    
    # ✅ SIEMPRE retornar 200, incluso si el bot no ha empezado
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",  # ✅ Siempre healthy
            "server": "online",
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
        "server": {
            "status": "online",
            "started_at": bot_status["started_at"].isoformat() if bot_status["started_at"] else None,
            "uptime_seconds": uptime_seconds
        },
        "bot": {
            "running": bot_status["running"],
            "mode": bot_status["mode"],
            "ml_enabled": bot_status["ml_enabled"]
        },
        "activity": {
            "total_scans": bot_status["total_scans"],
            "total_signals": bot_status["total_signals"],
            "total_trades": bot_status["total_trades"],
            "open_positions": bot_status["open_positions"],
            "last_scan": bot_status["last_scan"].isoformat() if bot_status["last_scan"] else None
        },
        "performance": {
            "wins": bot_status["wins"],
            "losses": bot_status["losses"],
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
        "wins": bot_status["wins"],
        "losses": bot_status["losses"],
        "win_rate": round(bot_status["win_rate"], 2),
        "pnl": round(bot_status["total_pnl"], 2),
        "ml_enabled": bot_status["ml_enabled"]
    })

@app.get("/ping")
async def ping():
    """Ping simple"""
    return {
        "ping": "pong",
        "timestamp": datetime.now().isoformat(),
        "uptime": int((datetime.now() - bot_status["started_at"]).total_seconds()) if bot_status["started_at"] else 0
    }

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
    """Actualizar estado del bot"""
    bot_status["running"] = running
    bot_status["total_scans"] = scans
    bot_status["open_positions"] = positions
    bot_status["last_scan"] = datetime.now()
    
    if signals is not None:
        bot_status["total_signals"] = signals
    
    if trades is not None:
        bot_status["total_trades"] = trades
    
    if wins is not None:
        bot_status["wins"] = wins
    
    if losses is not None:
        bot_status["losses"] = losses
    
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

async def start_health_server(port: Optional[int] = None):
    """
    ✅ Iniciar servidor HTTP INMEDIATAMENTE
    """
    try:
        # ✅ Usar PORT de Railway o 8080 por defecto
        if port is None:
            port = int(os.getenv('PORT', '8080'))
        
        # ✅ Marcar servidor como iniciado ANTES de uvicorn
        bot_status["started_at"] = datetime.now()
        bot_status["mode"] = "server_starting"
        
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=True,
            timeout_keep_alive=75,  # ✅ Aumentar timeout
            limit_concurrency=100,
            backlog=2048
        )
        server = uvicorn.Server(config)
        
        logger.info(f"✅ Health server iniciado en 0.0.0.0:{port}")
        logger.info(f"🏥 Healthcheck: http://0.0.0.0:{port}/health")
        
        await server.serve()
        
    except Exception as e:
        logger.error(f"❌ Error health server: {e}", exc_info=True)
        raise

# ═══════════════════════════════════════════════════════════════
# STARTUP EVENT (ejecutar ANTES de cualquier request)
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    """Ejecutado cuando FastAPI inicia"""
    logger.info("🚀 FastAPI startup event - Health server READY")
    bot_status["mode"] = "ready"
