# price_monitor.py
#
# Monitor de precios para tus posiciones abiertas.
# - Lee las posiciones del TradingEngine
# - Para cada mint OPEN consulta DexScreener
# - Convierte el precio a SOL (priceNative)
# - Llama a engine.update_price(mint, price_sol)

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from trading_engine import TradingEngine

logger = logging.getLogger(__name__)

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"


class PriceMonitor:
    def __init__(
        self,
        engine: TradingEngine,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """
        :param engine: instancia de TradingEngine
        :param poll_interval_seconds: cada cuántos segundos actualizar precios
        """
        self.engine = engine
        self.poll_interval_seconds = poll_interval_seconds
        self._running: bool = False

    async def _fetch_price_for_mint(
        self,
        client: httpx.AsyncClient,
        mint: str,
    ) -> Optional[float]:
        """
        Devuelve el precio en SOL para un token (mint) usando DexScreener.

        - Busca el par con mayor liquidez.usd
        - Usa priceNative como precio en SOL (si está disponible)
        """
        url = DEXSCREENER_URL.format(mint)
        try:
            resp = await client.get(url, timeout=10)
        except Exception as e:
            logger.debug(f"[PriceMonitor] Error HTTP para {mint}: {e}")
            return None

        if resp.status_code != 200:
            logger.debug(
                f"[PriceMonitor] DexScreener {mint} status={resp.status_code}"
            )
            return None

        try:
            data = resp.json()
        except Exception as e:
            logger.debug(f"[PriceMonitor] Error parseando JSON para {mint}: {e}")
            return None

        pairs: List[Dict[str, Any]] = data.get("pairs") or []
        if not pairs:
            logger.debug(f"[PriceMonitor] Sin pares para {mint}")
            return None

        # Escoger el par con mayor liquidez USD
        def liquidity_usd(pair: Dict[str, Any]) -> float:
            try:
                liq = pair.get("liquidity", {}) or {}
                return float(liq.get("usd", 0.0))
            except Exception:
                return 0.0

        best_pair = max(pairs, key=liquidity_usd)
        price_native = best_pair.get("priceNative")

        if price_native is None:
            # A veces DexScreener no trae priceNative; podríamos intentar
            # priceUsd con un precio de SOL, pero de momento lo dejamos así.
            logger.debug(f"[PriceMonitor] priceNative no disponible para {mint}")
            return None

        try:
            price_sol = float(price_native)
        except (TypeError, ValueError):
            return None

        return price_sol

    async def run_forever(self) -> None:
        """
        Loop principal:
        - Obtiene snapshot de posiciones
        - Para cada posición OPEN pide precio y actualiza engine.update_price
        """
        self._running = True
        logger.info(
            f"[PriceMonitor] Iniciando monitor de precios "
            f"cada {self.poll_interval_seconds:.1f}s"
        )

        async with httpx.AsyncClient() as client:
            while self._running:
                try:
                    snapshot = self.engine.get_positions_snapshot()
                    # Sólo posiciones OPEN
                    open_mints = [
                        p["mint"]
                        for p in snapshot
                        if p.get("status") == "OPEN"
                    ]

                    if not open_mints:
                        await asyncio.sleep(self.poll_interval_seconds)
                        continue

                    logger.debug(
                        f"[PriceMonitor] Actualizando precios de {len(open_mints)} "
                        f"tokens..."
                    )

                    for mint in open_mints:
                        price = await self._fetch_price_for_mint(client, mint)
                        if price is None:
                            continue

                        # Actualiza PnL / SL / Trailing en TradingEngine
                        self.engine.update_price(mint, price)

                        logger.debug(
                            f"[PriceMonitor] {mint} → {price:.10f} SOL actualizado"
                        )

                        # peq. pausa para no spamear DexScreener
                        await asyncio.sleep(0.2)

                except Exception as e:
                    logger.error(f"[PriceMonitor] Error en loop principal: {e}")

                await asyncio.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        self._running = False
        logger.info("[PriceMonitor] Detenido por stop()")
