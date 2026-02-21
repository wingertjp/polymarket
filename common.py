"""
Shared infrastructure for the Polymarket BTC Up/Down bot.
"""

import logging
import os
import sys
import time
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

HOST        = os.getenv("HOST",        "https://clob.polymarket.com")
GAMMA_API   = os.getenv("GAMMA_API",   "https://gamma-api.polymarket.com")
WS_URL      = os.getenv("WS_URL",      "wss://ws-subscriptions-clob.polymarket.com/ws/market")
MARKET_SLUG = os.getenv("MARKET_SLUG", "btc-updown-5m")

# Sniper settings
SNIPE_AMOUNT  = float(os.getenv("SNIPE_AMOUNT",  "1.0"))    # USDC per trade
SNIPE_PROB    = float(os.getenv("SNIPE_PROB",   "0.95"))   # midpoint threshold to trigger buy
SNIPE_TIME    = int(os.getenv("SNIPE_TIME",     "120"))    # only trigger if < 2 min remaining
RESCUE_MID_THRESHOLD = float(os.getenv("RESCUE_MID_THRESHOLD", "0.80"))  # rescue when initial bet token mid drops below this → rescue token still cheap (~0.20)
SNIPE_RESCUE_AMOUNT  = float(os.getenv("SNIPE_RESCUE_AMOUNT",  "0.20"))  # USDC for rescue order (smaller to limit whipsaw cost)
DRY_RUN              = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# On-chain redemption (Polygon)
RPC_URL  = os.getenv("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
USDC_E   = os.getenv("USDC_E",   "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDR = os.getenv("CTF_ADDR", "0x4D97DCd97eC945f40CF65F87097ACe5EA0476045")


# ── Logging ────────────────────────────────────────────────────────────────────

log        = logging.getLogger("polymarket")
log_fetch  = logging.getLogger("polymarket.fetch")
log_ws     = logging.getLogger("polymarket.ws")
log_book   = logging.getLogger("polymarket.book")
log_order  = logging.getLogger("polymarket.order")
log_redeem = logging.getLogger("polymarket.redeem")

_DEPS = [
    "websockets", "websockets.client", "websockets.connection", "websockets.protocol",
    "urllib3", "urllib3.connectionpool", "requests", "asyncio", "py_clob_client",
]


def configure_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"Invalid log level: {level!r}. Choose: DEBUG INFO WARNING ERROR CRITICAL")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)-20s  %(message)s",
        datefmt="%H:%M:%S",
    ))

    app_root = logging.getLogger("polymarket")
    app_root.setLevel(numeric)
    app_root.handlers.clear()
    app_root.addHandler(handler)
    app_root.propagate = False

    for name in _DEPS:
        dep = logging.getLogger(name)
        dep.setLevel(logging.WARNING)
        if not dep.handlers:
            dep.addHandler(handler)
        dep.propagate = False

    log.info("logging ready  app_level=%s  deps_capped_at=WARNING", level.upper())


# ── Market ─────────────────────────────────────────────────────────────────────

class Market:
    def __init__(self, condition_id: str, up_token: str, down_token: str, title: str, end_ts: int):
        self.condition_id = condition_id
        self.up_token     = up_token
        self.down_token   = down_token
        self.title        = title
        self.end_ts       = end_ts


def fetch_active_market(clob: ClobClient, exclude_cid: str | None = None) -> Market:
    """
    Derive the current market slug from the system clock.
    Raises RuntimeError if the new window isn't ready yet on CLOB.
    """
    ts   = (int(time.time()) // 300) * 300
    slug = f"{MARKET_SLUG}-{ts}"
    log_fetch.debug("slug=%s  ts=%s  exclude_cid=%s", slug, ts, exclude_cid)

    resp = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
    resp.raise_for_status()
    events = resp.json()
    if not events:
        raise RuntimeError(f"Market not found: {slug}")

    condition_id = events[0]["markets"][0]["conditionId"]
    if condition_id == exclude_cid:
        raise RuntimeError("Same market window still active")

    clob_info = clob.get_market(condition_id)
    if not clob_info.get("enable_order_book") or not clob_info.get("accepting_orders"):
        raise RuntimeError(f"Market not open yet: {slug}")

    tokens = clob_info.get("tokens", [])
    up   = next((t["token_id"] for t in tokens if t["outcome"] == "Up"),   None)
    down = next((t["token_id"] for t in tokens if t["outcome"] == "Down"), None)
    if not up or not down:
        raise RuntimeError("Token IDs missing")

    log_fetch.info(
        "market ready  %r  cid=%s  up=%s…  down=%s…  end_ts=%s  (~%ds)",
        events[0]["title"], condition_id, up[:16], down[:16], ts + 300, (ts + 300) - int(time.time()),
    )
    return Market(condition_id=condition_id, up_token=up, down_token=down,
                  title=events[0]["title"], end_ts=ts + 300)


# ── Book helpers ───────────────────────────────────────────────────────────────

def sorted_bids(side: dict) -> list:
    return sorted(side.items(), key=lambda x: float(x[0]), reverse=True)

def sorted_asks(side: dict) -> list:
    return sorted(side.items(), key=lambda x: float(x[0]))

def compute_mid(
    bids: list,
    asks: list,
    comp_bids: list | None = None,
    comp_asks: list | None = None,
) -> tuple[float | None, str]:
    """
    Best estimate of midpoint for a binary-market token.
    Falls back to the complementary token's book (up_price + down_price = 1).
    Returns (mid, source).
    """
    if bids and asks:
        return (float(bids[0][0]) + float(asks[0][0])) / 2, "full"

    if bids and not asks:
        if comp_bids and comp_asks:
            comp_mid = (float(comp_bids[0][0]) + float(comp_asks[0][0])) / 2
            return (float(bids[0][0]) + (1 - comp_mid)) / 2, "cross_full"
        if comp_bids:
            synthetic_ask = 1 - float(comp_bids[0][0])
            return (float(bids[0][0]) + synthetic_ask) / 2, "cross_bid"
        return float(bids[0][0]), "bid_only"

    if asks and not bids:
        if comp_bids and comp_asks:
            comp_mid = (float(comp_bids[0][0]) + float(comp_asks[0][0])) / 2
            return ((1 - comp_mid) + float(asks[0][0])) / 2, "cross_full"
        if comp_asks:
            synthetic_bid = 1 - float(comp_asks[0][0])
            return (synthetic_bid + float(asks[0][0])) / 2, "cross_ask"
        return float(asks[0][0]), "ask_only"

    return None, None


# ── On-chain helpers ───────────────────────────────────────────────────────────

def rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    res = r.json()
    if "error" in res:
        raise RuntimeError(f"RPC error: {res['error']['message']}")
    return res["result"]


def eth_call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def abi_sel(sig: str) -> str:
    """Return the 4-byte ABI selector for a function signature."""
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode())[:4].hex()


def find_index_set(cid: str, asset_id: int) -> int | None:
    """Return the indexSet for this asset_id in this condition, or None."""
    sel_gc = abi_sel("getCollectionId(bytes32,bytes32,uint256)")
    sel_gp = abi_sel("getPositionId(address,bytes32)")
    cid_padded  = cid[2:].zfill(64)
    addr_padded = "000000000000000000000000" + USDC_E[2:].lower()
    for i in range(8):
        index_set    = 1 << i
        idx_padded   = hex(index_set)[2:].zfill(64)
        coll_result  = eth_call(CTF_ADDR, sel_gc + "0" * 64 + cid_padded + idx_padded)
        if not coll_result:
            continue
        pos_result = eth_call(CTF_ADDR, sel_gp + addr_padded + coll_result[2:].zfill(64))
        if pos_result and int(pos_result, 16) == asset_id:
            return index_set
    return None


def usdc_e_balance(wallet: str) -> float:
    result = eth_call(USDC_E, "0x70a08231" + "000000000000000000000000" + wallet[2:].lower())
    return int(result, 16) / 1e6


def wait_receipt(tx_hash: str, timeout: int = 90) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            return receipt
        time.sleep(3)
    return None


def build_client_l1() -> ClobClient:
    """Unauthenticated client — GET calls only. No PRIVATE_KEY required."""
    from py_clob_client.constants import POLYGON
    return ClobClient(HOST, chain_id=POLYGON)


def build_client_l2() -> ClobClient:
    from py_clob_client.constants import POLYGON
    key = os.getenv("PRIVATE_KEY")
    if not key:
        raise ValueError("PRIVATE_KEY not set in .env")
    l1    = ClobClient(HOST, chain_id=POLYGON, key=key)
    creds = l1.create_or_derive_api_creds()
    log.info("API key derived: %s", creds.api_key)
    return ClobClient(HOST, chain_id=POLYGON, key=key, creds=creds)


def redeem_pending_positions(client: ClobClient, private_key: str) -> None:
    """
    Scan trade history, find resolved CTF positions still held in the wallet,
    and redeem them on-chain via redeemPositions() on the Gnosis CTF contract.
    """
    from eth_account import Account
    from eth_utils import to_checksum_address

    account = Account.from_key(private_key)
    wallet  = account.address
    log_redeem.info("scanning for redeemable positions  wallet=%s…", wallet[:12])

    try:
        resp   = client.get_trades()
        trades = resp if isinstance(resp, list) else resp.get("data", [])
    except Exception as e:
        log_redeem.warning("get_trades failed: %s", e)
        return

    seen, candidates = set(), []
    for t in trades:
        key = (t.get("market", ""), t.get("asset_id", ""))
        if key in seen or not all(key):
            continue
        seen.add(key)
        candidates.append(t)

    sel_pd     = abi_sel("payoutDenominator(bytes32)")
    redeemable = []

    for t in candidates:
        cid      = t["market"]
        asset_id = int(t["asset_id"])
        outcome  = t.get("outcome", "?")

        try:
            denom = int(eth_call(CTF_ADDR, sel_pd + cid[2:].zfill(64)), 16)
        except Exception as e:
            log_redeem.debug("payoutDenominator(%s…): %s", cid[:12], e)
            continue

        if denom == 0:
            continue

        try:
            addr_padded = "000000000000000000000000" + wallet[2:].lower()
            balance = int(eth_call(CTF_ADDR, "0x00fdd58e" + addr_padded + hex(asset_id)[2:].zfill(64)), 16)
        except Exception as e:
            log_redeem.debug("balanceOf(%s…): %s", cid[:12], e)
            continue

        if balance == 0:
            continue

        try:
            index_set = find_index_set(cid, asset_id)
        except Exception as e:
            log_redeem.warning("find_index_set(%s…): %s", cid[:12], e)
            continue

        if index_set is None:
            log_redeem.warning("indexSet not found  cid=%s…  asset_id=%s", cid[:12], asset_id)
            continue

        redeemable.append({"cid": cid, "outcome": outcome, "balance": balance / 1e6, "index_set": index_set})
        log_redeem.info("  redeemable  %-5s  cid=%s…  balance=%.4f  indexSet=%d",
                        outcome, cid[:12], balance / 1e6, index_set)

    if not redeemable:
        log_redeem.info("no redeemable positions")
        return

    log_redeem.info("redeeming %d position(s)", len(redeemable))
    balance_before = usdc_e_balance(wallet)

    try:
        nonce     = int(rpc("eth_getTransactionCount", [wallet, "latest"]), 16)
        gas_price = int(int(rpc("eth_gasPrice", []), 16) * 1.2)
    except Exception as e:
        log_redeem.error("failed to get nonce/gas: %s", e)
        return

    sel_rp = abi_sel("redeemPositions(address,bytes32,bytes32,uint256[])")

    for i, pos in enumerate(redeemable, 1):
        try:
            calldata = (
                sel_rp
                + "000000000000000000000000" + USDC_E[2:].lower()
                + "0" * 64
                + pos["cid"][2:].zfill(64)
                + hex(0x80)[2:].zfill(64)
                + hex(1)[2:].zfill(64)
                + hex(pos["index_set"])[2:].zfill(64)
            )
            tx = {
                "nonce": nonce, "gasPrice": gas_price, "gas": 200000,
                "to": to_checksum_address(CTF_ADDR), "data": calldata, "value": 0, "chainId": 137,
            }
            signed  = account.sign_transaction(tx)
            tx_hash = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
            nonce  += 1
            log_redeem.info("[%d/%d] %-5s  tx=%s…  waiting", i, len(redeemable), pos["outcome"], tx_hash[:18])
            receipt = wait_receipt(tx_hash)
            if receipt and receipt.get("status") == "0x1":
                log_redeem.critical("[%d/%d] REDEEMED  %-5s  +%.4f USDC.e  tx=%s…",
                                    i, len(redeemable), pos["outcome"], pos["balance"], tx_hash[:18])
            else:
                log_redeem.error("[%d/%d] tx FAILED  %-5s  tx=%s…  receipt=%s",
                                 i, len(redeemable), pos["outcome"], tx_hash[:18], receipt)
        except Exception as e:
            log_redeem.error("[%d/%d] redeem error: %s: %s", i, len(redeemable), type(e).__name__, e)

    balance_after = usdc_e_balance(wallet)
    if balance_after > balance_before:
        log_redeem.critical("redemption complete  gained=+%.4f USDC.e  balance=%.4f",
                            balance_after - balance_before, balance_after)
    else:
        log_redeem.info("redemption complete  balance=%.4f", balance_after)
