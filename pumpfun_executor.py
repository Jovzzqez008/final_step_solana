# pumpfun_executor.py
"""
Ejecutor para compras en Pump.fun basado en señales de Flintr.

Por ahora SOLO implementa DRY_RUN (simulación realista):
- Usa el payload de Flintr (latestPrice si existe).
- Calcula cuántos tokens "compraríamos" con X SOL.
- Devuelve entry_price_sol y amount_tokens para que el TradingEngine
  registre la posición simulada.

Más adelante:
- Aquí se construirá y enviará la transacción REAL usando Helius + Solana.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from config import BotConfig


class PumpFunExecutor:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

        # En el futuro:
        # - usar config.helius_rpc_url
        # - usar config.wallet_private_key
        self.rpc_url: Optional[str] = config.helius_rpc_url
        self.wallet_private_key: Optional[str] = config.wallet_private_key

        # De momento, aunque el MODE sea "real", vamos a seguir en DRY_RUN
        # hasta implementar la parte on-chain de forma segura.
        self.dry_run: bool = True

        if config.mode == "real":
            print(
                "⚠️ PumpFunExecutor: MODE=real pero la ejecución on-chain aún no está "
                "implementada. Funciona SOLO en DRY_RUN (simulación)."
            )

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def buy_on_mint(
        self,
        flintr_event: Dict[str, Any],
        size_sol: float,
    ) -> Dict[str, Any]:
        """
        Simula una compra en el momento del mint de Pump.fun usando la
        información de Flintr.

        Devuelve:
          {
            "entry_price_sol": float,   # precio por token en SOL
            "amount_tokens": float,     # cantidad de tokens "comprados"
            "simulated": bool,
          }
        """
        data = flintr_event.get("data", {})
        mint = data.get("mint")
        meta = data.get("metaData") or {}
        token_data = data.get("tokenData") or {}

        symbol = meta.get("symbol") or ""
        name = meta.get("name") or ""

        latest_price_raw = token_data.get("latestPrice")
        try:
            latest_price = float(latest_price_raw) if latest_price_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            latest_price = 0.0

        # Si no tenemos precio todavía, dejamos que el engine lo fije luego
        # en el primer update_price real (DexScreener / Jupiter).
        if latest_price <= 0:
            entry_price_sol = 0.0
            amount_tokens = 0.0
        else:
            entry_price_sol = latest_price
            amount_tokens = size_sol / latest_price

        print(
            f"[PumpFunExecutor] (DRY_RUN) buy_on_mint → {symbol} ({name}) mint={mint}\n"
            f"   size={size_sol} SOL, entry_price={entry_price_sol}, "
            f"tokens≈{amount_tokens}"
        )

        # En el futuro, cuando implementemos on-chain:
        # - construiríamos la tx contra el programa de Pump.fun
        # - firmaríamos con wallet_private_key
        # - enviaríamos por Helius RPC
        # - devolveríamos signature y precio real fill.

        return {
            "entry_price_sol": entry_price_sol,
            "amount_tokens": amount_tokens,
            "simulated": True,
        }
