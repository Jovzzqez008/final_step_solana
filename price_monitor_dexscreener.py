# price_monitor_dexscreener.py
import time
import threading
from typing import Optional

import httpx

from trading_engine import TradingEngine

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"


class DexscreenerPriceMonitor:
    """
    Monitor de precios REAL para tokens en Solana usando DexScreener.

    Para cada posición abierta en el engine:
      - Llama a /latest/dex/tokens/<mint>
      - Escoge el par con mayor liquidez en USD
      - Usa priceNative (precio del token en SOL)
      - Llama a engine.update_price(mint, price_sol)
    """

    def __init__(self, engine: TradingEngine, interval_sec: float = 5.0) -> None:
        self.engine = engine
        self.interval_sec = interval_sec
        self.running = False

    def start(self) -> None:
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        print("📡 DexScreener Price Monitor iniciado.")
        while self.running:
            try:
                self._tick()
            except Exception as e:
                print("[DEX] Error en price monitor:", repr(e))
            time.sleep(self.interval_sec)

    def _tick(self) -> None:
        positions = self.engine.get_positions_snapshot()
        if not positions:
            return

        # Solo nos interesan posiciones abiertas
        mints = [p["mint"] for p in positions if p["status"] == "OPEN"]
        if not mints:
            return

        for mint in mints:
            price_sol = self._fetch_price_for_mint(mint)
            if price_sol is not None and price_sol > 0:
                updated = self.engine.update_price(mint, price_sol)
                if updated:
                    print(
                        f"[DEX] {updated.symbol} ({mint[:6]}…): "
                        f"precio={price_sol:.10f} SOL"
                    )

    def _fetch_price_for_mint(self, mint: str) -> Optional[float]:
        url = f"{DEXSCREENER_URL}/{mint}"
        try:
            with httpx.Client(timeout=10) as client:
                res = client.get(url)
            if res.status_code != 200:
                print(f"[DEX] {mint}: status {res.status_code}")
                return None

            data = res.json()
            pairs = data.get("pairs") or []
            if not pairs:
                # Aún sin pool en DEX (solo bonding curve en Pump.fun)
                return None

            # Escoger par con mayor liquidez en USD
            best = max(
                pairs,
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
            )
            price_native = best.get("priceNative")
            if not price_native:
                return None

            return float(price_native)
        except Exception as e:
            print(f"[DEX] Error obteniendo precio para {mint}:", repr(e))
            return None
