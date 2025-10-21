#!/usr/bin/env python3
"""
Health Check Server para Railway
"""
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
import logging

logger = logging.getLogger(__name__)

app = FastAPI()

bot_status = {
    "running": False,
    "last_scan": None,
    "total_scans": 0,
    "positions": 0
}

@app.get("/health")
async def health_check():
    """Endpoint para Railway healthcheck"""
    return JSONResponse({
        "status": "healthy" if bot_status["running"] else "starting",
        "bot_running": bot_status["running"],
        "last_scan": bot_status["last_scan"],
        "total_scans": bot_status["total_scans"],
        "open_positions": bot_status["positions"]
    })

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "Solana Trading Bot ML", "status": "running"}

def update_bot_status(running: bool, scans: int, positions: int):
    """Actualizar estado del bot"""
    bot_status["running"] = running
    bot_status["total_scans"] = scans
    bot_status["positions"] = positions
    bot_status["last_scan"] = asyncio.get_event_loop().time()

async def start_health_server(port: int = 8080):
    """Iniciar servidor de health"""
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    logger.info(f"✅ Health server iniciado en puerto {port}")
    await server.serve()
