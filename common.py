"""
Shared infrastructure for the Polymarket BTC Up/Down bot.
"""

import logging
import sys
import time
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

HOST         = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
WS_URL       = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MARKET_SLUG  = "btc-updown-5m"

# Sniper settings
SNIPE_AMOUNT = 1.0   # USDC per trade
SNIPE_PROB   = 0.95  # midpoint threshold to trigger buy
SNIPE_TIME   = 120   # only trigger if < 2 min remaining

# On-chain redemption (Polygon)
RPC_URL  = "https://polygon-bor-rpc.publicnode.com"
USDC_E   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDR = "0x4D97DCd97eC945f40CF65F87097ACe5EA0476045"


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
