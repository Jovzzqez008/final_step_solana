# jupiter_executor.py
import os
from typing import Any, Dict

from jup_python_sdk.clients.ultra_api_client import UltraApiClient
from jup_python_sdk.models.ultra_api.ultra_order_request_model import UltraOrderRequest

from config import BotConfig

# WSOL mint en Solana mainnet
WSOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterExecutor:
    """
    Executor de ventas usando Jupiter Ultra API (jup-python-sdk).

    - Usa tu WALLET_PRIVATE_KEY (base58 estilo Phantom).
    - Opcionalmente usa JUPITER_API_KEY si existe.
    - Vende un token cualquiera contra SOL (WSOL) con order_and_execute.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config

        api_key = os.getenv("JUPITER_API_KEY")
        kwargs: Dict[str, Any] = {
            # Le decimos al SDK que la private key está en esta env var
            "private_key_env_var": "WALLET_PRIVATE_KEY",
        }
        if api_key:
            kwargs["api_key"] = api_key

        # Ultra API client: no necesitas RPC explícito, lo hace la API por dentro
        self.client = UltraApiClient(**kwargs)

        # Guardamos slippage por si luego lo usamos cuando UltraOrderRequest lo exponga
        self.slippage_bps = config.slippage_bps

    def sell_to_sol(self, mint: str, amount_tokens: float, decimals: int) -> Dict[str, Any]:
        """
        Vende `amount_tokens` del token `mint` contra SOL usando Jupiter Ultra.

        - mint: address del token (string base58).
        - amount_tokens: cantidad de tokens en unidades decimales (ej. 1234.56).
        - decimals: decimales del token (ej. 6 en la mayoría de memecoins).
        """
        if amount_tokens <= 0:
            raise ValueError("amount_tokens debe ser > 0")

        # Normalizar decimales a un rango razonable
        try:
            d = int(decimals)
        except Exception:
            d = 6
        if d < 0:
            d = 0
        if d > 12:
            d = 12

        # Pasar a unidades enteras (ej. 1.23 * 10**6 = 1_230_000)
        amount = int(amount_tokens * (10 ** d))
        if amount <= 0:
            raise ValueError("amount calculado es 0; revisa amount_tokens/decimals")

        taker = self.client._get_public_key()

        order_request = UltraOrderRequest(
            input_mint=mint,
            output_mint=WSOL_MINT,
            amount=amount,
            taker=taker,
            # Cuando el SDK expone más campos (slippage, etc.) los podemos añadir aquí.
        )

        # Esto ya:
        #  - busca la mejor ruta
        #  - construye la tx
        #  - la firma con tu WALLET_PRIVATE_KEY
        #  - la ejecuta on-chain
        resp: Dict[str, Any] = self.client.order_and_execute(order_request)

        # El response suele traer:
        #  - "status" ("Success" / "Failed")
        #  - "signature" (tx id)
        #  - campos extra según versión de Ultra API
        return resp

    def close(self) -> None:
        """Cerrar conexiones HTTP internas del cliente (buena práctica al apagar el bot)."""
        try:
            self.client.close()
        except Exception:
            pass
