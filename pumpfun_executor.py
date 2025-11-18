# pumpfun_executor.py
#
# Executor para comprar en Pump.fun usando la misma estructura
# que bots tipo listen-rs (PUMP_BUY_METHOD + bonding curve).
#
# Modo simulation:
#   - construye la TX
#   - la simula contra Helius (no gasta SOL)
#
# Modo real:
#   - construye y envía la TX firmada con tu WALLET_PRIVATE_KEY

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Any, Dict, Optional

import base58
from solana.publickey import PublicKey
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.system_program import SYS_PROGRAM_ID
from solana.sysvar import SYSVAR_RENT_PUBKEY
from solana.transaction import Transaction, TransactionInstruction, AccountMeta

from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

from solders.keypair import Keypair
from solders.pubkey import Pubkey as SPubkey

from config import BotConfig

logger = logging.getLogger(__name__)

# ----------------- CONSTANTES PUMP.FUN -----------------
# Tomadas de listen-rs pump.rs (pump sniper) :contentReference[oaicite:4]{index=4}

PUMP_FUN_PROGRAM_ID = PublicKey("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL_ADDRESS = PublicKey("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_FEE_ADDRESS = PublicKey("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
EVENT_AUTHORITY = PublicKey("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
ASSOCIATED_TOKEN_PROGRAM_ID = PublicKey(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Método BUY: bytes que van al principio de la instrucción
PUMP_BUY_METHOD = bytes(
    [0x66, 0x06, 0x3D, 0x12, 0x01, 0xDA, 0xEB, 0xEA]
)


# ----------------- BONDING CURVE LAYOUT -----------------
# Mismo layout que en listen-rs (49 bytes) :contentReference[oaicite:5]{index=5}

@dataclass
class BondingCurveLayout:
    blob1: int
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    blob4: int
    complete: bool

    @classmethod
    def parse(cls, data: bytes) -> "BondingCurveLayout":
        if len(data) < 49:
            raise ValueError(f"Bonding curve data length inválido: {len(data)}")
        blob1 = int.from_bytes(data[0:8], "little")
        v_token = int.from_bytes(data[8:16], "little")
        v_sol = int.from_bytes(data[16:24], "little")
        r_token = int.from_bytes(data[24:32], "little")
        r_sol = int.from_bytes(data[32:40], "little")
        blob4 = int.from_bytes(data[40:48], "little")
        complete = data[48] != 0
        return cls(
            blob1=blob1,
            virtual_token_reserves=v_token,
            virtual_sol_reserves=v_sol,
            real_token_reserves=r_token,
            real_sol_reserves=r_sol,
            blob4=blob4,
            complete=complete,
        )


def get_token_amount(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    real_token_reserves: int,
    lamports: int,
) -> int:
    """
    Misma fórmula que get_token_amount en listen-rs. :contentReference[oaicite:6]{index=6}
    Calcula cuántos tokens te dan por 'lamports' en la bonding curve.
    """
    vs = int(virtual_sol_reserves)
    vt = int(virtual_token_reserves)
    amount_in = int(lamports)

    vs_u = vs
    vt_u = vt
    amount_in_u = amount_in

    reserves_product = vs_u * vt_u
    if vt_u == 0:
        return 0

    new_vs = vs_u + amount_in_u
    if new_vs == 0:
        return 0

    new_vt = reserves_product // new_vs + 1
    amount_out = vt_u - new_vt
    final_amount_out = min(amount_out, int(real_token_reserves))
    if final_amount_out < 0:
        final_amount_out = 0
    return int(final_amount_out)


# ----------------- EXECUTOR -----------------

class PumpFunExecutor:
    """
    Executor para Pump.fun que:
    - Usa Helius como RPC (config.helius_rpc_url)
    - Usa WALLET_PRIVATE_KEY (bs58) para firmar
    - Construye la instrucción buy a partir del evento de Flintr
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        if not config.helius_rpc_url:
            logger.warning("HELIUS_RPC_URL no configurado, PumpFunExecutor limitado.")
        if not config.wallet_private_key:
            logger.warning("WALLET_PRIVATE_KEY no configurado, no se podrá firmar TX.")
        self._client: Optional[AsyncClient] = None
        self._owner_kp: Optional[Keypair] = None
        self._owner_pubkey: Optional[PublicKey] = None

    # ------------- Helpers internos -------------

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            if not self._config.helius_rpc_url:
                raise RuntimeError("HELIUS_RPC_URL requerido para PumpFunExecutor.")
            self._client = AsyncClient(self._config.helius_rpc_url)
        return self._client

    def _get_owner(self) -> tuple[Keypair, PublicKey]:
        if self._owner_kp is not None and self._owner_pubkey is not None:
            return self._owner_kp, self._owner_pubkey

        if not self._config.wallet_private_key:
            raise RuntimeError("WALLET_PRIVATE_KEY requerido para PumpFunExecutor.")

        # Private key como bs58 (como en TypeScript: bs58.decode(...))
        secret_bytes = base58.b58decode(self._config.wallet_private_key)
        kp = Keypair.from_bytes(secret_bytes)
        pub = PublicKey(bytes(kp.pubkey()))
        self._owner_kp = kp
        self._owner_pubkey = pub
        return kp, pub

    async def _fetch_bonding_curve_layout(
        self, bonding_curve: PublicKey
    ) -> BondingCurveLayout:
        client = await self._get_client()
        resp = await client.get_account_info(bonding_curve)
        if resp.value is None or resp.value.data is None:
            raise RuntimeError("No se pudo leer la cuenta de bonding curve.")

        # resp.value.data es un tuple (data, encoding) en solana-py >=0.30
        data_raw = resp.value.data[0]
        if isinstance(data_raw, str):
            # viene en base64
            import base64 as b64

            data_bytes = b64.b64decode(data_raw)
        else:
            data_bytes = bytes(data_raw)

        # El layout es de 49 bytes; tomamos los primeros 49
        layout = BondingCurveLayout.parse(data_bytes[:49])
        return layout

    # ------------- Construcción de la instrucción BUY -------------

    def _build_pump_buy_instruction_from_event(
        self,
        event: Dict[str, Any],
        owner: PublicKey,
        user_ata: PublicKey,
        bonding_curve: PublicKey,
        associated_bonding_curve: PublicKey,
        token_amount: int,
        lamports: int,
    ) -> TransactionInstruction:
        """
        Construye la Instruction de BUY de Pump.fun a partir de:
        - evento Flintr (para el mint)
        - cuentas de bonding curve
        - token_amount calculado
        - lamports a gastar

        Equivalente a make_pump_swap_ix(...) en listen-rs. :contentReference[oaicite:7]{index=7}
        """

        mint_str = event["data"]["mint"]
        mint = PublicKey(mint_str)

        accounts = [
            AccountMeta(pubkey=PUMP_GLOBAL_ADDRESS, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FEE_ADDRESS, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=associated_bonding_curve, is_signer=False, is_writable=True
            ),
            AccountMeta(pubkey=user_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSVAR_RENT_PUBKEY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FUN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]

        # Datos: PumpFunSwapInstructionData { method_id: [u8;8], token_amount: u64, lamports: u64 }
        data = PUMP_BUY_METHOD + struct.pack("<QQ", int(token_amount), int(lamports))

        ix = TransactionInstruction(
            program_id=PUMP_FUN_PROGRAM_ID,
            data=data,
            keys=accounts,
        )
        return ix

    # ------------- API pública del executor -------------

    async def build_buy_tx_from_event(
        self,
        event: Dict[str, Any],
    ) -> Transaction:
        """
        Construye una Transaction para comprar en Pump.fun el mint del evento Flintr.
        Usa config.invest_amount_sol como cantidad fija de SOL.
        """

        client = await self._get_client()
        wallet_kp, owner = self._get_owner()

        # 1) Mint del evento
        mint_str = event["data"]["mint"]
        mint = PublicKey(mint_str)

        # 2) Bonding curve & associated bonding curve
        #    Si Flintr trae ammData.*, úsalo. Si no, derivar como en listen-rs. :contentReference[oaicite:8]{index=8}
        amm_data = event.get("data", {}).get("ammData", {}) or {}
        bonding_curve_str = amm_data.get("bondingCurve")
        associated_bonding_curve_str = amm_data.get("associatedBondingCurve")

        if bonding_curve_str:
            bonding_curve = PublicKey(bonding_curve_str)
        else:
            # Derivar PDA: [b"bonding-curve", mint]
            from solana.publickey import PublicKey as SdkPK

            bonding_curve, _ = PublicKey.find_program_address(
                [b"bonding-curve", bytes(mint)],
                PUMP_FUN_PROGRAM_ID,
            )

        if associated_bonding_curve_str:
            associated_bonding_curve = PublicKey(associated_bonding_curve_str)
        else:
            associated_bonding_curve = get_associated_token_address(
                bonding_curve, mint
            )

        # 3) ATA del usuario para ese mint
        user_ata = get_associated_token_address(owner, mint)

        # 4) Leer bonding curve y calcular token_amount según get_token_amount
        layout = await self._fetch_bonding_curve_layout(bonding_curve)

        invest_sol = self._config.invest_amount_sol
        lamports = int(invest_sol * 1_000_000_000)

        raw_token_amount = get_token_amount(
            layout.virtual_sol_reserves,
            layout.virtual_token_reserves,
            layout.real_token_reserves,
            lamports,
        )

        # aplicar "slippage tonto" como listen-rs (90% del amount) :contentReference[oaicite:9]{index=9}
        token_amount = int(raw_token_amount * 0.9)

        if token_amount <= 0:
            raise RuntimeError("token_amount calculado es 0; no tiene sentido comprar.")

        # 5) Construir Instruction BUY
        ix = self._build_pump_buy_instruction_from_event(
            event=event,
            owner=owner,
            user_ata=user_ata,
            bonding_curve=bonding_curve,
            associated_bonding_curve=associated_bonding_curve,
            token_amount=token_amount,
            lamports=lamports,
        )

        # 6) Construir Transaction
        recent_blockhash_resp = await client.get_latest_blockhash()
        blockhash = recent_blockhash_resp.value.blockhash

        tx = Transaction(fee_payer=owner, recent_blockhash=blockhash)
        tx.add(ix)

        # No la firmo aquí todavía; dejo que el caller decida
        tx.sign(solana_keypair_from_solders(wallet_kp))

        return tx

    async def simulate_buy_from_event(
        self,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Simula la compra en Helius (modo DRY_RUN real on-chain).
        No gasta SOL, pero te da logs y si pasaría o no.
        """
        client = await self._get_client()
        tx = await self.build_buy_tx_from_event(event)

        # simulate_transaction espera base64 en algunas versiones;
        # solana-py se encarga del encode.
        resp = await client.simulate_transaction(tx, sig_verify=False)
        return resp.to_dict() if hasattr(resp, "to_dict") else resp

    async def send_buy_from_event(
        self,
        event: Dict[str, Any],
    ) -> str:
        """
        Envía la TX real a la red (usa Helius). Debes usarlo SOLO en MODE=real.
        Devuelve la signature.
        """
        if self._config.mode != "real":
            raise RuntimeError("send_buy_from_event llamado en MODE != real")

        client = await self._get_client()
        tx = await self.build_buy_tx_from_event(event)

        opts = TxOpts(skip_preflight=True, max_retries=2)
        resp = await client.send_transaction(tx, opts=opts)
        # resp.value es signature en algunos casos; en otros resp["result"]
        # simplifiquemos:
        try:
            sig = resp.value  # solana-py 0.30+
        except AttributeError:
            sig = resp["result"]
        logger.info(f"Pump.fun BUY enviado, signature={sig}")
        return str(sig)


# Helper para firmar con solders.Keypair en Transaction de solana-py
def solana_keypair_from_solders(kp: Keypair):
    """
    Convierte una solders.Keypair a un objeto compatible con solana-py Transaction.sign.
    Truco: solana-py usa su propia Keypair, pero acepta objetos con .public_key() y .secret_key().
    """
    from solana.keypair import Keypair as SPyKeypair

    secret = bytes(kp)
    return SPyKeypair.from_secret_key(secret)
