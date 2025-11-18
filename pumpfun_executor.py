# pumpfun_executor.py
"""
Ejecutor para compras en Pump.fun basado en señales de Flintr.

Modo actual:
- SIMULACIÓN PURA (DRY_RUN) → calcula entry_price y tokens con datos de Flintr.
- ESTRUCTURA PREPARADA para:
    - Construir la instrucción `buy` usando la IDL oficial de Pump.fun.
    - Simular la transacción en Helius.
    - Enviarla en modo REAL.

⚠️ IMPORTANTE:
La parte que construye la Instruction real de Pump.fun DEPENDE de la IDL
`pump.json` del repo oficial:
    https://github.com/pump-fun/pump-public-docs/tree/main/idl
y de los avisos en Pump Tech Updates:
    https://t.me/pump_tech_updates
Estos definen el orden exacto de cuentas en `buy`. Esa parte la dejo como
TODO bien marcada, para que no haya falsas expectativas.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

import base58
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction, TransactionInstruction

from config import BotConfig


class PumpFunExecutor:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

        self.rpc_url: Optional[str] = config.helius_rpc_url
        self.wallet_private_key_b58: Optional[str] = config.wallet_private_key

        # DRY_RUN clonado del executor anterior
        # aunque el MODE sea "real", hasta que la parte on-chain esté 100% probada,
        # podemos seguir usando este flag interno para forzar simulación.
        self.force_dry_run: bool = True

        # Lazy init de cliente RPC y keypair
        self._rpc_client: Optional[Client] = None
        self._keypair: Optional[Keypair] = None

        if config.mode == "real":
            print(
                "⚠️ PumpFunExecutor: MODE=real, pero la ejecución on-chain de Pump.fun\n"
                "   aún NO está completa. Se usará DRY_RUN mientras force_dry_run=True\n"
                "   o hasta que completes la construcción de la instrucción buy."
            )

    # ------------------------------------------------------------------
    # API pública principal usada por TradingEngine
    # ------------------------------------------------------------------

    def buy_on_mint(
        self,
        flintr_event: Dict[str, Any],
        size_sol: float,
    ) -> Dict[str, Any]:
        """
        Punto de entrada desde TradingEngine cuando Flintr detecta un mint.

        - En modo simulation → siempre DRY_RUN.
        - En modo real:
            - si `force_dry_run=True` → DRY_RUN (seguro).
            - si force_dry_run=False → intenta construir tx on-chain, simularla
              en Helius y (opcionalmente) enviarla.
        """
        mode = self.config.mode

        # 1) Simulación siempre disponible
        sim_res = self._simulate_buy_from_flintr(flintr_event, size_sol)

        # 2) Si no hay RPC o no hay private key, solo DRY_RUN
        if not self.rpc_url or not self.wallet_private_key_b58:
            print(
                "[PumpFunExecutor] HELIUS_RPC_URL o WALLET_PRIVATE_KEY no configurados. "
                "Usando solo DRY_RUN."
            )
            return sim_res

        # 3) Si estamos en simulation mode → solo DRY_RUN
        if mode != "real":
            return sim_res

        # 4) MODE=real pero seguimos forzando DRY_RUN mientras no completes
        #    la parte de construir la instrucción `buy`.
        if self.force_dry_run:
            print(
                "[PumpFunExecutor] MODE=real, pero force_dry_run=True. "
                "NO se enviarán transacciones a la red todavía."
            )
            return sim_res

        # 5) Si quisieras empezar a probar la parte on-chain:
        #    descomenta el bloque siguiente una vez hayas implementado
        #    `_build_pump_buy_instruction_from_event`.

        # try:
        #     tx_sig = self._buy_onchain_with_helius(flintr_event, size_sol)
        #     sim_res["onchain_signature"] = tx_sig
        #     sim_res["simulated"] = False
        # except NotImplementedError as nie:
        #     print("[PumpFunExecutor] TODO en ejecución on-chain:", nie)
        # except Exception as exc:
        #     print("[PumpFunExecutor] Error al intentar comprar on-chain:", repr(exc))

        return sim_res

    # ------------------------------------------------------------------
    #  SIMULACIÓN PURA (la que usa hoy tu TradingEngine)
    # ------------------------------------------------------------------

    def _simulate_buy_from_flintr(
        self,
        flintr_event: Dict[str, Any],
        size_sol: float,
    ) -> Dict[str, Any]:
        """
        Simula una compra usando únicamente el payload de Flintr.
        Esta es la parte que YA funciona con tu TradingEngine.
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

        return {
            "entry_price_sol": entry_price_sol,
            "amount_tokens": amount_tokens,
            "simulated": True,
        }

    # ------------------------------------------------------------------
    #   INICIALIZACIÓN RPC + KEYPAIR (para Helius)
    # ------------------------------------------------------------------

    def _get_rpc_client(self) -> Client:
        if self._rpc_client is None:
            if not self.rpc_url:
                raise RuntimeError("HELIUS_RPC_URL no configurado")
            self._rpc_client = Client(self.rpc_url)
        return self._rpc_client

    def _get_keypair(self) -> Keypair:
        if self._keypair is None:
            if not self.wallet_private_key_b58:
                raise RuntimeError("WALLET_PRIVATE_KEY no configurado")

            # Esperamos que sea la secretKey en formato base58 (como en web3.js)
            # ver ejemplo oficial: bs58.encode(secretKey):contentReference[oaicite:5]{index=5}
            secret_bytes = base58.b58decode(self.wallet_private_key_b58)
            self._keypair = Keypair.from_secret_key(secret_bytes)
        return self._keypair

    # ------------------------------------------------------------------
    #   ESQUELETO PARA COMPRA REAL EN PUMP.FUN + HELIUS
    # ------------------------------------------------------------------

    def _buy_onchain_with_helius(
        self,
        flintr_event: Dict[str, Any],
        size_sol: float,
        simulate_only: bool = False,
    ) -> str:
        """
        ➜ ESQUELETO para compra real en Pump.fun (NO se llama mientras
          force_dry_run=True).

        Flujo:
          1. Construir Instruction `buy` (Pump.fun) desde Flintr + IDL.
          2. Construir Transaction con compute-budget y blockhash de Helius.
          3. Simularla (simulateTransaction) para ver si pasa.:contentReference[oaicite:6]{index=6}
          4. Si simulate_only=False → enviar con sendTransaction.:contentReference[oaicite:7]{index=7}

        Devuelve:
          - signature (str) de la transacción enviada (o simulada).
        """
        client = self._get_rpc_client()
        kp = self._get_keypair()
        payer_pubkey: Pubkey = kp.pubkey()

        data = flintr_event.get("data", {})
        mint_str: str = data.get("mint")
        if not mint_str:
            raise ValueError("Flintr event sin `data.mint`")

        # 1) Construir instruction `buy` de Pump.fun
        buy_ix = self._build_pump_buy_instruction_from_event(
            flintr_event=flintr_event,
            size_sol=size_sol,
            payer=payer_pubkey,
        )

        # 2) Conseguir blockhash reciente desde Helius
        blockhash_resp = client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        tx = Transaction(recent_blockhash=str(blockhash), fee_payer=payer_pubkey)
        tx.add(buy_ix)

        # 3) Firmar transacción
        tx.sign(kp)

        # 4) Simular primero (DRY_RUN on-chain)
        sim_resp = client.simulate_transaction(tx)
        logs = sim_resp.value.logs
        err = sim_resp.value.err

        print("[PumpFunExecutor] simulateTransaction logs:")
        if logs:
            for line in logs:
                print("   ", line)
        else:
            print("   (sin logs)")

        if err:
            raise RuntimeError(f"simulateTransaction devolvió error: {err}")

        # Si solo queremos DRY_RUN on-chain, devolvemos signature "fake"
        if simulate_only:
            return "SIMULATED_ONLY"

        # 5) Enviar vía Helius (sendTransaction)
        send_resp = client.send_transaction(tx, kp)
        sig = send_resp.value
        print(f"[PumpFunExecutor] sendTransaction signature: {sig}")

        return str(sig)

    # ------------------------------------------------------------------
    #   TODO: construir Instruction `buy` usando la IDL oficial
    # ------------------------------------------------------------------

    def _build_pump_buy_instruction_from_event(
        self,
        flintr_event: Dict[str, Any],
        size_sol: float,
        payer: Pubkey,
    ) -> TransactionInstruction:
        """
        ⚠️ AQUÍ es donde se conecta la IDL oficial de Pump.fun (`pump.json`).

        - Debes descargar la IDL:
          https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json :contentReference[oaicite:8]{index=8}
        - Leer el bloque de la instrucción `buy`:
            { "name": "buy", "accounts": [...], "args": [...] }
        - Construir:
            - `PROGRAM_ID` como Pubkey del programa.
            - Lista de cuentas (AccountMeta) en el orden exacto de la IDL.
            - `data` codificando el ABI de la instrucción `buy` y sus args.

        Debido a que ese orden cambia cuando Pump actualiza el programa
        (ver Pump Tech Updates), aquí lo dejo como NotImplementedError para
        evitar que el bot firme una transacción con cuentas mal ordenadas.
        """
        # Ejemplo de cómo accedes a ammData de Flintr:
        data = flintr_event.get("data", {})
        amm = data.get("ammData") or {}
        mint_str: str = data.get("mint")

        bonding_curve = amm.get("bondingCurve")
        associated_bc = amm.get("associatedBondingCurve")
        vault_creator_ata = amm.get("vaultCreatorATA")
        vault_creator_auth = amm.get("vaultCreatorAuthority")

        # Aquí tendrías algo como (pseudocódigo):
        #
        # program_id = Pubkey.from_string("<PUMP_PROGRAM_ID>")
        # accounts = [
        #     AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        #     AccountMeta(pubkey=Pubkey.from_string(mint_str), is_signer=False, is_writable=True),
        #     AccountMeta(pubkey=Pubkey.from_string(bonding_curve), is_signer=False, is_writable=True),
        #     ...
        # ]
        # data_bytes = encode_idl_instruction("buy", args={ "amount": ..., ... })
        # return TransactionInstruction(program_id=program_id, data=data_bytes, keys=accounts)
        #
        # Donde `encode_idl_instruction` es un helper que tú mismo implementas
        # para transformar la descripción de la IDL en el buffer binario.

        raise NotImplementedError(
            "Implementa `_build_pump_buy_instruction_from_event` usando la IDL "
            "oficial de Pump.fun (pump.json) y el orden de cuentas actualizado."
        )
