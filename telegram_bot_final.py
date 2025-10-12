# pumpfun_detector_live.py
"""
Pump.fun early detector (Helius + Helius-RPC + Moralis + Solscan) -> Telegram alerts
- Detects mints via Helius logsSubscribe (Pump.fun program)
- Enriches via Helius RPC, Solscan and Moralis bonding endpoints
- Scores each mint for "risk" and sends informative alerts to Telegram
- Tracks tokens until pre-graduation/graduation thresholds
- Creates Postgres tables automatically

Environment variables (Railway):
  HELIUS_WSS_URL        (required) wss://.../?api-key=KEY
  HELIUS_RPC_URL        (optional) https://.../?api-key=KEY (fallback derived from WSS)
  MORALIS_API_KEY       (required for bonding / market cap enrichment)
  TELEGRA M_BOT_TOKEN   (required)
  TELEGRAM_CHAT_ID      (required)
  DATABASE_URL          (required) postgres://...
  UMBRAL_MCAP           (optional, default 55000)
  GRADUATION_MC_TARGET  (optional, default 69000)
  TOKEN_POLL_INTERVAL   (optional, default 7)
  PUMP_FUN_PROGRAM_ID   (optional, default program id)
  SOLSCAN_API_BASE      (optional, default https://public-api.solscan.io)
"""

import os
import re
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import aiohttp
import websockets
import asyncpg

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pumpfun_detector")

# ---------------- env / config ----------------
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL") or (HELIUS_WSS_URL.replace("wss://", "https://") if HELIUS_WSS_URL else None)
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
UMBRAL_MCAP = float(os.getenv("UMBRAL_MCAP", "55000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
TOKEN_POLL_INTERVAL = int(os.getenv("TOKEN_POLL_INTERVAL", "7"))
PUMP_FUN_PROGRAM_ID = os.getenv("PUMP_FUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SOLSCAN_API_BASE = os.getenv("SOLSCAN_API_BASE", "https://public-api.solscan.io")

# sanity checks
required = {
    "HELIUS_WSS_URL": HELIUS_WSS_URL,
    "HELIUS_RPC_URL": HELIUS_RPC_URL,
    "MORALIS_API_KEY": MORALIS_API_KEY,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "DATABASE_URL": DATABASE_URL
}
missing = [k for k, v in required.items() if not v]
if missing:
    log.error(f"Faltan variables de entorno requeridas: {missing}")
    # raise SystemExit so user sees it immediately
    raise SystemExit(1)

# mint pattern heuristic (base58-ish)
MINT_PATTERN = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# ---------------- helpers ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def generate_links(mint: str) -> Dict[str,str]:
    return {
        "pumpfun": f"https://pump.fun/coin/{mint}",
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "rugcheck": f"https://rugcheck.xyz/tokens/{mint}",
        "solscan": f"https://solscan.io/token/{mint}"
    }

# ---------------- DATABASE ----------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=6)
        log.info("Connected to Postgres")
        await self._ensure_tables()

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def _ensure_tables(self):
        q_tokens = """
        CREATE TABLE IF NOT EXISTS tokens (
            mint TEXT PRIMARY KEY,
            symbol TEXT,
            creator TEXT,
            first_seen TIMESTAMP WITH TIME ZONE,
            last_seen TIMESTAMP WITH TIME ZONE,
            market_cap DOUBLE PRECISION,
            price DOUBLE PRECISION,
            score INTEGER,
            risk_reasons JSONB,
            notified_pre_graduation BOOLEAN DEFAULT FALSE,
            graduated BOOLEAN DEFAULT FALSE,
            raw JSONB
        );
        """
        q_events = """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            mint TEXT,
            type TEXT,
            payload JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        );
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q_tokens)
            await conn.execute(q_events)
        log.info("DB: ensured tokens and events tables")

    async def upsert_token(self, mint: str, fields: Dict[str, Any]):
        # fields may include symbol, creator, market_cap, price, score, risk_reasons, raw
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tokens (mint, symbol, creator, first_seen, last_seen, market_cap, price, score, risk_reasons, raw)
                VALUES ($1,$2,$3,NOW(),NOW(),$4,$5,$6,$7,$8)
                ON CONFLICT (mint) DO UPDATE
                SET last_seen = NOW(),
                    market_cap = COALESCE($4, tokens.market_cap),
                    price = COALESCE($5, tokens.price),
                    symbol = COALESCE($2, tokens.symbol),
                    creator = COALESCE($3, tokens.creator),
                    score = COALESCE($6, tokens.score),
                    risk_reasons = COALESCE($7, tokens.risk_reasons),
                    raw = COALESCE($8, tokens.raw)
            """, mint,
                 fields.get("symbol"),
                 fields.get("creator"),
                 fields.get("market_cap"),
                 fields.get("price"),
                 fields.get("score"),
                 json.dumps(fields.get("risk_reasons") or []),
                 json.dumps(fields.get("raw") or {}))

    async def record_event(self, mint: str, etype: str, payload: Dict[str, Any]):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO events (mint, type, payload) VALUES ($1,$2,$3)", mint, etype, json.dumps(payload))

    async def mark_notified_pre(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tokens SET notified_pre_graduation = TRUE WHERE mint = $1", mint)

    async def was_notified_pre(self, mint: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT notified_pre_graduation FROM tokens WHERE mint = $1", mint)
            return bool(row and row.get("notified_pre_graduation"))

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tokens SET graduated = TRUE WHERE mint = $1", mint)

# ---------------- Helius RPC & utilities ----------------
class HeliusRPC:
    def __init__(self, rpc_url: str, session: aiohttp.ClientSession):
        self.rpc_url = rpc_url
        self.session = session

    async def _rpc(self, method: str, params: list, timeout=10):
        payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
        try:
            async with self.session.post(self.rpc_url, json=payload, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    log.debug(f"Helius RPC {method} returned {resp.status}")
        except Exception as e:
            log.debug(f"Helius RPC error {e}")
        return None

    async def get_parsed_account_info(self, address: str):
        # use "getAccountInfo" with jsonParsed
        res = await self._rpc("getAccountInfo", [address, {"encoding":"jsonParsed","commitment":"confirmed"}])
        if not res:
            return None
        return res.get("result")

    async def get_token_supply(self, mint: str):
        res = await self._rpc("getTokenSupply", [mint, {"commitment":"confirmed"}])
        if not res:
            return None
        return res.get("result")

    async def get_transaction(self, signature: str):
        res = await self._rpc("getTransaction", [signature, "jsonParsed"])
        if not res:
            return None
        return res.get("result")

# ---------------- Moralis client (bonding) ----------------
class MoralisClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base = "https://solana-gateway.moralis.io"
        self.headers = {"x-api-key": MORALIS_API_KEY}

    async def get_bonding(self, mint: str) -> Optional[Dict[str,Any]]:
        url = f"{self.base}/token/mainnet/exchange/pumpfun/{mint}/bonding"
        try:
            async with self.session.get(url, headers=self.headers, timeout=8) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    log.debug(f"Moralis bonding {resp.status} for {mint}")
        except Exception as e:
            log.debug(f"Moralis error: {e}")
        return None

# ---------------- Solscan client ----------------
class SolscanClient:
    def __init__(self, session: aiohttp.ClientSession, base: str = SOLSCAN_API_BASE):
        self.s = session
        self.base = base.rstrip("/")

    async def token_meta(self, mint: str) -> Optional[Dict[str,Any]]:
        url = f"{self.base}/token/meta?tokenAddress={mint}"
        try:
            async with self.s.get(url, timeout=8) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.debug(f"Solscan meta error: {e}")
        return None

    async def token_holders(self, mint: str) -> Optional[Dict[str,Any]]:
        url = f"{self.base}/token/holders?tokenAddress={mint}&limit=20"
        try:
            async with self.s.get(url, timeout=8) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.debug(f"Solscan holders error: {e}")
        return None

# ---------------- Telegram notifier (simple) ----------------
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session: aiohttp.ClientSession):
        self.token = token
        self.chat_id = chat_id
        self.session = session

    async def send(self, text: str, buttons: Optional[List[List[Dict[str,str]]]] = None):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        if buttons:
            payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        try:
            async with self.session.post(url, data=payload, timeout=10) as resp:
                if resp.status != 200:
                    log.warning(f"Telegram send returned {resp.status}")
                else:
                    return await resp.json()
        except Exception as e:
            log.error(f"Telegram send error: {e}")

    async def alert_token(self, mint: str, symbol: str, market_cap: float, score: int, reasons: List[str], links: Dict[str,str]):
        title = f"🚨 Pump.fun detection — {symbol} ({mint[:8]}...)"
        body = (
            f"<b>{symbol}</b>\n"
            f"Mint: <code>{mint}</code>\n"
            f"Market Cap: ${market_cap:,.0f}\n"
            f"Score: {score}/100\n"
            f"Reasons: {', '.join(reasons) if reasons else 'None'}\n\n"
            f"Links below."
        )
        text = f"{title}\n\n{body}"
        buttons = [
            [{"text": "Open Pump.fun", "url": links["pumpfun"]}, {"text": "DexScreener", "url": links["dexscreener"]}],
            [{"text": "RugCheck", "url": links["rugcheck"]}, {"text": "Solscan", "url": links["solscan"]}]
        ]
        await self.send(text, buttons=buttons)

# ---------------- Scoring engine ----------------
def compute_score_and_reasons(market_cap: float,
                              holders_info: Optional[Dict[str,Any]],
                              bonding_info: Optional[Dict[str,Any]]) -> (int, List[str]):
    reasons = []
    score = 50  # base
    # market cap heuristics
    if market_cap is None:
        reasons.append("No market_cap")
        score -= 20
    else:
        if market_cap < 1000:
            score += 5
        elif market_cap < 5000:
            score += 2
        elif market_cap < 20000:
            score += 0
        else:
            # larger mcap -> risk of being expensive / already pumped
            score -= 5

    # holders heuristics
    if holders_info and isinstance(holders_info, dict):
        total = holders_info.get("total", 0) or 0
        top_account = None
        try:
            rows = holders_info.get("data") or []
            if rows:
                top = rows[0]
                top_balance = float(top.get("amount") or top.get("balance") or 0)
                # if supply known we could compute percentage; approximate rule:
                top_pct = float(top.get("percent") or 0)
                if top_pct and top_pct > 40:
                    reasons.append(f"Top holder {top_pct}%")
                    score -= 20
                elif top_pct and top_pct > 20:
                    reasons.append(f"Top holder {top_pct}%")
                    score -= 10
                if total >= 50:
                    score += 10
                elif total >= 10:
                    score += 3
                else:
                    reasons.append("Pocos holders")
                    score -= 15
        except Exception:
            pass
    else:
        reasons.append("No holders data")
        score -= 10

    # bonding info heuristics
    if bonding_info and isinstance(bonding_info, dict):
        try:
            mc = float(bonding_info.get("market_cap") or bonding_info.get("mcap") or 0)
            # if bonding shows good backing -> safe-ish
            if mc and mc > 1000:
                score += 10
            elif mc and mc < 500:
                score -= 10
        except Exception:
            pass
    else:
        reasons.append("No bonding info")
        score -= 5

    # clamp
    score = max(0, min(100, score))
    return score, reasons

# ---------------- Helius logs listener ----------------
class HeliusListener:
    def __init__(self, wss_url: str, program_id: str, on_detect):
        self.wss = wss_url
        self.program_id = program_id
        self.on_detect = on_detect
        self._running = False

    async def _subscribe(self, ws):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.program_id]},
                {"commitment": "confirmed"}
            ]
        }
        await ws.send(json.dumps(msg))

    async def run(self):
        if not self.wss:
            log.warning("No HELIUS_WSS_URL configured - HeliusListener disabled")
            return
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.wss, ping_interval=30, ping_timeout=10, max_size=None) as ws:
                    log.info("Connected to Helius WebSocket")
                    await self._subscribe(ws)
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        # parse logs
                        params = payload.get("params", {})
                        result = params.get("result", {})
                        logs = result.get("logs", []) or []
                        signature = result.get("signature")
                        for line in logs:
                            # detect mints in logs heuristically
                            m = MINT_PATTERN.search(line)
                            if m:
                                mint = m.group(0)
                                # dispatch detection: include signature for extra enrichment
                                await self.on_detect(mint=mint, source="helius_logs", signature=signature, raw_line=line)
                    backoff = 1
            except Exception as e:
                log.warning(f"Helius WS error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def stop(self):
        self._running = False

# ---------------- Orchestrator / Monitor ----------------
class PumpFunDetector:
    def __init__(self, db: Database, helius_rpc: HeliusRPC, moralis: MoralisClient, solscan: SolscanClient,
                 notifier: TelegramNotifier):
        self.db = db
        self.helius_rpc = helius_rpc
        self.moralis = moralis
        self.solscan = solscan
        self.notifier = notifier
        self.tracked: Dict[str, Dict[str,Any]] = {}  # mint -> meta
        self._poll_task = None
        self._running = False

    async def handle_detect(self, mint: str, source: str, signature: Optional[str]=None, raw_line: Optional[str]=None):
        """
        Called by HeliusListener when a potential mint is seen in logs.
        We then enrich using RPC, Solscan and Moralis.
        """
        try:
            log.info(f"Detected candidate mint {mint} from {source} (sig={signature})")
            # quick dedupe: if already tracked and seen recently, skip
            if mint in self.tracked:
                log.debug("Already tracked; updating timestamp")
                self.tracked[mint]["last_seen"] = now_iso()
                return

            # Enrichment steps
            async with aiohttp.ClientSession() as s:
                # RPC parsed account info (token mint data)
                rpc = self.helius_rpc
                parsed = await rpc.get_parsed_account_info(mint)
                # token supply
                supply_info = await rpc.get_token_supply(mint)
                # transaction details if signature present
                tx_info = None
                if signature:
                    tx_info = await rpc.get_transaction(signature)

                # solscan metadata and holders
                sol_meta = await self.solscan.token_meta(mint)
                sol_holders = await self.solscan.token_holders(mint)

                # moralis bonding / market_cap
                bonding = await self.moralis.get_bonding(mint)

            # extract core fields
            symbol = None
            creator = None
            market_cap = None
            price = None
            raw_payload = {
                "parsed": parsed,
                "supply": supply_info,
                "tx": tx_info,
                "sol_meta": sol_meta,
                "sol_holders": sol_holders,
                "bonding": bonding,
                "raw_line": raw_line
            }
            # symbol extraction attempts
            if sol_meta and isinstance(sol_meta, dict):
                symbol = sol_meta.get("symbol") or sol_meta.get("data", {}).get("symbol")
            elif parsed and isinstance(parsed, dict):
                # parsed account info might contain token info under value.data.parsed.info
                try:
                    val = parsed.get("value", {})
                    data = val.get("data", {})
                    # sometimes token metadata not present; skip
                except Exception:
                    pass

            # creator extract: transaction or parsed
            if tx_info and isinstance(tx_info, dict):
                # heuristics: find signer that created mint or first account
                try:
                    meta = tx_info.get("meta") or {}
                    # sometimes preTokenBalances / postTokenBalances contain info
                    creator = tx_info.get("transaction", {}).get("message", {}).get("accountKeys", [None])[0]
                except Exception:
                    pass

            # market cap extraction
            if bonding and isinstance(bonding, dict):
                try:
                    market_cap = float(bonding.get("market_cap") or bonding.get("mcap") or 0)
                    price = float(bonding.get("price") or 0)
                except Exception:
                    pass

            # compute score
            score, reasons = compute_score_and_reasons(market_cap, sol_holders, bonding)

            # upsert DB
            await self.db.upsert_token(mint, {
                "symbol": symbol,
                "creator": creator,
                "market_cap": market_cap,
                "price": price,
                "score": score,
                "risk_reasons": reasons,
                "raw": raw_payload
            })

            # record detection event
            await self.db.record_event(mint, "detected", {"source": source, "signature": signature, "raw_line": raw_line})

            # track for monitoring
            self.tracked[mint] = {
                "symbol": symbol or "UNKNOWN",
                "creator": creator,
                "market_cap": market_cap or 0,
                "price": price or 0,
                "score": score,
                "reasons": reasons,
                "last_seen": now_iso()
            }

            # alert policies:
            # - early alert: if score >= 60 and market_cap > 1000 -> pump_early
            # - pre-graduation alert: if market_cap >= UMBRAL_MCAP
            if score >= 60 and (market_cap or 0) >= 1000 and not await self.db.was_notified_pre(mint):
                links = generate_links(mint)
                await self.notifier.alert_token(mint, self.tracked[mint]["symbol"], market_cap or 0, score, reasons, links)
                await self.db.mark_notified_pre(mint)

        except Exception as e:
            log.exception(f"Error in handle_detect for {mint}: {e}")

    async def poll_tracked(self):
        self._running = True
        async with aiohttp.ClientSession() as s:
            moralis_local = MoralisClient(s)
            solscan_local = SolscanClient(s)
            while self._running:
                try:
                    if not self.tracked:
                        await asyncio.sleep(TOKEN_POLL_INTERVAL)
                        continue
                    for mint in list(self.tracked.keys()):
                        try:
                            bonding = await moralis_local.get_bonding(mint)
                            sol_holders = await solscan_local.token_holders(mint)
                            market_cap = None
                            if bonding and isinstance(bonding, dict):
                                market_cap = float(bonding.get("market_cap") or bonding.get("mcap") or 0)
                            # update tracked data & DB
                            score, reasons = compute_score_and_reasons(market_cap, sol_holders, bonding)
                            self.tracked[mint].update({"market_cap": market_cap or 0, "score": score, "reasons": reasons, "last_seen": now_iso()})
                            await self.db.upsert_token(mint, {
                                "symbol": self.tracked[mint].get("symbol"),
                                "creator": self.tracked[mint].get("creator"),
                                "market_cap": market_cap,
                                "price": self.tracked[mint].get("price"),
                                "score": score,
                                "risk_reasons": reasons,
                                "raw": {"bonding": bonding, "holders": sol_holders}
                            })
                            # Pre-graduation alert
                            if (market_cap or 0) >= UMBRAL_MCAP and not await self.db.was_notified_pre(mint):
                                links = generate_links(mint)
                                await self.notifier.alert_token(mint, self.tracked[mint]["symbol"], market_cap or 0, score, reasons, links)
                                await self.db.mark_notified_pre(mint)
                            # Graduation: mark and stop tracking
                            if (market_cap or 0) >= GRADUATION_MC_TARGET:
                                await self.db.mark_graduated(mint)
                                log.info(f"Token {mint} reached graduation target; stopping tracking")
                                self.tracked.pop(mint, None)
                        except Exception as e:
                            log.debug(f"Error polling individual mint {mint}: {e}")
                    await asyncio.sleep(TOKEN_POLL_INTERVAL)
                except Exception as e:
                    log.error(f"Error in poll_tracked loop: {e}")
                    await asyncio.sleep(TOKEN_POLL_INTERVAL)

    async def stop(self):
        self._running = False

# ---------------- Entrypoint ----------------
async def main():
    log.info("Starting Pump.fun detector (live)")
    # DB
    db = Database(DATABASE_URL)
    await db.connect()

    # shared HTTP session
    session = aiohttp.ClientSession()

    # low-level clients
    helius_rpc = HeliusRPC(HELIUS_RPC_URL, session)
    moralis = MoralisClient(session)
    solscan = SolscanClient(session)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, session)

    # detector
    detector = PumpFunDetector(db, helius_rpc, moralis, solscan, notifier)

    # Helius logs listener
    helius_listener = HeliusListener(HELIUS_WSS_URL, PUMP_FUN_PROGRAM_ID, detector.handle_detect)

    # start listener and poller
    listener_task = asyncio.create_task(helius_listener.run())
    poll_task = asyncio.create_task(detector.poll_tracked())

    # keep running until Ctrl+C
    try:
        await asyncio.gather(listener_task, poll_task)
    except asyncio.CancelledError:
        log.info("Cancelled, shutting down")
    except KeyboardInterrupt:
        log.info("Keyboard interrupt; shutting down")
    finally:
        await detector.stop()
        await helius_listener.stop()
        await db.close()
        await session.close()
        log.info("Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())

