# price_monitor.py
"""
Monitor de precios para tus posiciones abiertas.

- Intenta primero DexScreener: https://api.dexscreener.com/latest/dex/tokens/{mint}
- Si falla o no hay pares válidos, hace fallback a Jupiter Price API v3:
  https://lite-api.jup.ag/price/v3?ids={mint},{So1111...}
- Actualiza last_price_sol en TradingEngine.update_price(mint, price_sol)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from trading_engine import TradingEngine

logger = logging.getLogger(__name__)

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
# Según la doc oficial de Price API v3 (lite): 
JUPITER_PRICE_URL_LITE = "https://lite-api.jup.ag/price/v3"
# Mint de SOL "wrapped" estándar en Solana, usado por Jupiter como referencia: 
SOL_MINT = "So11111111111111111111111111111111111111112"


async def _fetch_price_from_dexscreener(
    client: httpx.AsyncClient,
    mint: str,
) -> Optional[float]:
    """
    Devuelve el precio del token EN SOL usando DexScreener (priceNative de un par donde
    el quote sea SOL). Si no lo encuentra, devuelve None.
    """
    url = f"{DEXSCREENER_TOKENS_URL}/{mint}"
    try:
        resp = await client.get(url, timeout=8)
    except Exception as exc:
        logger.debug("[PriceMonitor] DexScreener error de red: %r", exc)
        return None

    if resp.status_code != 200:
        logger.debug(
            "[PriceMonitor] DexScreener status %s para %s",
            resp.status_code,
            mint,
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug("[PriceMonitor] DexScreener JSON inválido: %r", exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Filtrar pares en Solana (opcional, pero ayuda) y donde el quote sea SOL
    # En DexScreener, para Solana típicamente: chainId="solana" y quoteToken.symbol="SOL" 
    sol_pairs = [
        p
        for p in pairs
        if p.get("chainId") == "solana"
        and p.get("quoteToken", {}).get("symbol") == "SOL"
    ]
    if not sol_pairs:
        sol_pairs = pairs  # fallback: usar cualquier par

    # Elegir el par con mayor liquidez en USD
    def _liq_usd(pair: dict) -> float:
        liq = pair.get("liquidity") or {}
        usd = liq.get("usd")
        try:
            return float(usd) if usd is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    best_pair = max(sol_pairs, key=_liq_usd)

    price_native = best_pair.get("priceNative")
    if price_native is None:
        return None

    try:
        price_sol = float(price_native)
        if price_sol <= 0:
            return None
        return price_sol
    except (TypeError, ValueError):
        return None


async def _fetch_price_from_jupiter(
    client: httpx.AsyncClient,
    mint: str,
    base_url: str = JUPITER_PRICE_URL_LITE,
) -> Optional[float]:
    """
    Fallback: usa Jupiter Price API v3 para obtener precio en USD del token y de SOL
    y devuelve token_price_usd / sol_price_usd = precio del token en SOL.
    Doc: https://lite-api.jup.ag/price/v3?ids=... 
    """
    # ids = token, SOL
    url = f"{base_url}?ids={mint},{SOL_MINT}"
    try:
        resp = await client.get(url, timeout=8)
    except Exception as exc:
        logger.debug("[PriceMonitor] Jupiter error de red: %r", exc)
        return None

    if resp.status_code != 200:
        logger.debug(
            "[PriceMonitor] Jupiter status %s para %s",
            resp.status_code,
            mint,
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug("[PriceMonitor] Jupiter JSON inválido: %r", exc)
        return None

    token_info = data.get(mint)
    sol_info = data.get(SOL_MINT)
    if not token_info or not sol_info:
        return None

    try:
        token_usd = float(token_info.get("usdPrice"))
        sol_usd = float(sol_info.get("usdPrice"))
        if token_usd <= 0 or sol_usd <= 0:
            return None
        return token_usd / sol_usd
    except (TypeError, ValueError):
        return None


async def _fetch_price_for_mint(
    client: httpx.AsyncClient,
    mint: str,
    jupiter_base_url: str,
) -> Optional[float]:
    """
    Lógica unificada: primero DexScreener, si no hay precio, fallback Jupiter.
    Devuelve precio en SOL (float) o None.
    """
    # 1) DexScreener
    price = await _fetch_price_from_dexscreener(client, mint)
    if price is not None:
        return price

    # 2) Jupiter Price v3 (lite/pro según base_url que pases en config)
    price = await _fetch_price_from_jupiter(client, mint, base_url=jupiter_base_url)
    return price


async def price_monitor_loop(
    engine: TradingEngine,
    poll_interval_sec: float = 3.0,
) -> None:
    """
    Bucle principal para mantener last_price_sol lo más real posible.

    - Cada `poll_interval_sec` revisa todas las posiciones OPEN.
    - Para cada mint, pide precio (DexScreener + fallback Jupiter).
    - Llama engine.update_price(mint, price_sol).
    """
    logger.info("[PriceMonitor] Iniciado bucle de precios...")

    # Usamos la base de Jupiter desde tu config (ej: https://lite-api.jup.ag)
    jupiter_price_base = engine.config.jupiter_api_url.rstrip("/") + "/price/v3"

    async with httpx.AsyncClient() as client:
        while True:
            try:
                snapshot = engine.get_positions_snapshot()
                open_positions = [
                    p for p in snapshot if p.get("status") == "OPEN"
                ]

                if not open_positions:
                    await asyncio.sleep(poll_interval_sec)
                    continue

                for pos in open_positions:
                    mint = pos.get("mint")
                    if not mint:
                        continue

                    price_sol = await _fetch_price_for_mint(
                        client,
                        mint,
                        jupiter_base_url=jupiter_price_base,
                    )

                    if price_sol is None:
                        logger.debug(
                            "[PriceMonitor] Sin precio para mint %s (DexScreener+Jupiter)",
                            mint,
                        )
                        await asyncio.sleep(0.2)
                        continue

                    updated_pos = engine.update_price(mint, price_sol)
                    if updated_pos is not None:
                        logger.debug(
                            "[PriceMonitor] %s price=%.10f SOL (max=%.10f)",
                            updated_pos.symbol,
                            updated_pos.last_price_sol,
                            updated_pos.max_price_sol,
                        )

                    # Pequeño delay entre tokens para no abusar del rate limit
                    await asyncio.sleep(0.25)

                await asyncio.sleep(poll_interval_sec)

            except asyncio.CancelledError:
                logger.info("[PriceMonitor] Cancelado, saliendo del bucle.")
                break
            except Exception as exc:
                logger.exception("[PriceMonitor] Error en bucle: %r", exc)
                # Esperar un poco antes de reintentar
                await asyncio.sleep(5.0)
