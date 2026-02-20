"""
Polymarket BTC Up/Down 5-minute bot.

Modes:
  python main.py data                          -- live order book via WebSocket (no auth)
  python main.py snipe [--log-level LEVEL]     -- sniper (requires auth)

Log levels (--log-level or LOG_LEVEL env):
  DEBUG    every price-change tick, all mid calculations
  INFO     lifecycle: market found, WS connected, book snapshots, mids in snipe window  (default)
  WARNING  unusual: incomplete book, unknown WS msgs, retry loops
  ERROR    recoverable failures: order rejected, connection lost
  CRITICAL trade fired
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import requests
import websockets
from datetime import datetime, timezone
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

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


# ── Book helpers ───────────────────────────────────────────────────────────────

def _sorted_bids(side: dict) -> list:
    return sorted(side.items(), key=lambda x: float(x[0]), reverse=True)

def _sorted_asks(side: dict) -> list:
    return sorted(side.items(), key=lambda x: float(x[0]))

def _compute_mid(
    bids: list,
    asks: list,
    comp_bids: list | None = None,
    comp_asks: list | None = None,
) -> tuple[float | None, str]:
    """
    Best estimate of midpoint for a binary-market token, given sorted bid/ask lists.
    Falls back gracefully when one side is missing, using the complementary token's
    book (up_price + down_price = 1) before giving up entirely.

    Returns (mid, source) where source describes how mid was computed:
      "full"       — normal (best_bid + best_ask) / 2
      "bid_only"   — best_bid used as lower-bound estimate
      "ask_only"   — best_ask used as upper-bound estimate
      "cross_bid"  — inferred from complementary token's best bid
      "cross_ask"  — inferred from complementary token's best ask
      "cross_full" — inferred from complementary token's full mid
      None         — no price data at all
    """
    if bids and asks:
        return (float(bids[0][0]) + float(asks[0][0])) / 2, "full"

    # Single-sided own book — try to fill the missing side from complementary token
    # In a binary market: price_up + price_down = 1
    # → ask_up  = 1 - bid_down,  bid_up  = 1 - ask_down
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


# ── Logging ────────────────────────────────────────────────────────────────────
#
#  Named sub-loggers — can be silenced individually at runtime:
#    logging.getLogger("polymarket.book").setLevel(logging.WARNING)
#
#  polymarket          general lifecycle & mid calculations
#  polymarket.fetch    market discovery (Gamma API + CLOB REST)
#  polymarket.ws       raw WebSocket connection & message handling
#  polymarket.book     order book snapshots and incremental updates
#  polymarket.order    order construction, submission, response

log       = logging.getLogger("polymarket")
log_fetch = logging.getLogger("polymarket.fetch")
log_ws    = logging.getLogger("polymarket.ws")
log_book  = logging.getLogger("polymarket.book")
log_order = logging.getLogger("polymarket.order")

# Third-party loggers that flood output if left uncapped
_DEPS = [
    "websockets",
    "websockets.client",
    "websockets.connection",
    "websockets.protocol",
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "asyncio",
    "py_clob_client",
]


def configure_logging(level: str = "INFO") -> None:
    """
    Configure the polymarket logger hierarchy and silence noisy dependencies.

    All polymarket.* output goes to stderr so it doesn't interfere with the
    data-mode TUI on stdout.
    """
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"Invalid log level: {level!r}. Choose: DEBUG INFO WARNING ERROR CRITICAL")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)-20s  %(message)s",
        datefmt="%H:%M:%S",
    ))

    # App root — all polymarket.* loggers inherit this level
    app_root = logging.getLogger("polymarket")
    app_root.setLevel(numeric)
    app_root.handlers.clear()
    app_root.addHandler(handler)
    app_root.propagate = False

    # Dependency loggers — cap at WARNING so DEBUG/INFO noise is suppressed even
    # when the app runs at DEBUG level; WARNING+ from deps still surfaces.
    for name in _DEPS:
        dep = logging.getLogger(name)
        dep.setLevel(logging.WARNING)
        if not dep.handlers:
            dep.addHandler(handler)
        dep.propagate = False

    log.info("logging ready  app_level=%s  deps_capped_at=WARNING", level.upper())


# ── Market discovery ───────────────────────────────────────────────────────────

class Market:
    def __init__(self, condition_id: str, up_token: str, down_token: str, title: str, end_ts: int):
        self.condition_id = condition_id
        self.up_token     = up_token
        self.down_token   = down_token
        self.title        = title
        self.end_ts       = end_ts   # Unix timestamp when this window closes


def fetch_active_market(clob: ClobClient, exclude_cid: str | None = None) -> Market:
    """
    Derive the current market slug from the system clock.
    Raises RuntimeError if the new window isn't ready yet on CLOB.
    Never returns the market identified by exclude_cid (the one that just closed).
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
    log_fetch.debug("condition_id=%s", condition_id)

    if condition_id == exclude_cid:
        raise RuntimeError("Same market window still active")

    clob_info = clob.get_market(condition_id)
    log_fetch.debug(
        "clob_info  enable_order_book=%s  accepting_orders=%s",
        clob_info.get("enable_order_book"),
        clob_info.get("accepting_orders"),
    )
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


# ── Display helpers ────────────────────────────────────────────────────────────

# ── Display constants ──────────────────────────────────────────────────────────
#   Each cell: "  " + price(6) + "  " + size(9) = 19 content + 2 prefix = 21 chars
_PW = 6   # " 54.0%"  ← f"{v*100:5.1f}%"
_SW = 9   # "$  1,434" ← f"${v:>8,.0f}"
_CW = 2 + _PW + 2 + _SW   # 21 — full cell width including leading spaces
_G  = "   "               # 3-space gap between the two columns

def _pct(v) -> str:
    return f"{float(v)*100:5.1f}%"   # always 6 chars

def _usd(v) -> str:
    return f"${float(v):>8,.0f}"     # always 9 chars

def _cell(p, s) -> str:
    return f"  {_pct(p)}  {_usd(s)}"   # exactly _CW chars

def _empty() -> str:
    return " " * _CW

def _sep(ch: str = "─") -> str:
    return "  " + ch * (_PW + 2 + _SW)  # "  " + 17 dashes = _CW chars

def render_book(title: str, book: dict, end_ts: int) -> None:
    up   = book.get("Up",   {})
    down = book.get("Down", {})

    up_bids   = sorted(up.get("bids",   {}).items(), key=lambda x: float(x[0]), reverse=True)[:5]
    up_asks   = sorted(up.get("asks",   {}).items(), key=lambda x: float(x[0]))[:5]
    down_bids = sorted(down.get("bids", {}).items(), key=lambda x: float(x[0]), reverse=True)[:5]
    down_asks = sorted(down.get("asks", {}).items(), key=lambda x: float(x[0]))[:5]

    def mid(bids, asks):
        if bids and asks:
            return (float(bids[0][0]) + float(asks[0][0])) / 2
        return None

    up_mid   = mid(up_bids, up_asks)
    down_mid = mid(down_bids, down_asks)
    now       = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    countdown = max(0, int(end_ts - time.time()))
    mins, secs = divmod(countdown, 60)
    countdown_str = f"{mins}:{secs:02d}"

    W      = _CW * 2 + len(_G)          # total content width = 45
    BORDER = "  " + "═" * W

    # column sub-headers (left-padded to _CW so the gap aligns)
    up_hdr   = f"  ▲ Up    mid {_pct(up_mid)   if up_mid   is not None else '  n/a'}"
    down_hdr = f"  ▼ Down  mid {_pct(down_mid) if down_mid is not None else '  n/a'}"
    col_hdr  = f"  {'Price':>{_PW}}  {'Size':>{_SW}}"  # _CW chars

    lines = [
        "\033[H\033[2J",
        BORDER,
        f"  {title}",
        f"  ● LIVE  ·  {now} UTC  ·  closes in {countdown_str}",
        BORDER,
        f"{up_hdr:<{_CW}}{_G}{down_hdr:<{_CW}}",
        f"{_sep()}{_G}{_sep()}",
        f"{col_hdr}{_G}{col_hdr}",
    ]

    # asks — closest to mid at bottom
    n_asks = max(len(up_asks), len(down_asks), 1)
    for i in range(n_asks - 1, -1, -1):
        left  = _cell(*up_asks[i])   if i < len(up_asks)   else _empty()
        right = _cell(*down_asks[i]) if i < len(down_asks) else _empty()
        lines.append(f"{left}{_G}{right}")

    lines.append(f"{_sep('┄')}{_G}{_sep('┄')}")

    # bids — best first
    n_bids = max(len(up_bids), len(down_bids), 1)
    for i in range(n_bids):
        left  = _cell(*up_bids[i])   if i < len(up_bids)   else _empty()
        right = _cell(*down_bids[i]) if i < len(down_bids) else _empty()
        lines.append(f"{left}{_G}{right}")

    lines.append(f"{_sep()}{_G}{_sep()}")
    lines.append(BORDER)

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# ── Phase A: WebSocket live feed ───────────────────────────────────────────────

async def stream_order_book(mkt: Market) -> None:
    """
    Stream order book for one market window.
    Returns as soon as the window's end timestamp is reached.
    """
    book: dict = {
        "Up":   {"bids": {}, "asks": {}},
        "Down": {"bids": {}, "asks": {}},
    }
    token_to_outcome = {mkt.up_token: "Up", mkt.down_token: "Down"}

    sys.stdout.write(f"\033[H\033[2J  Connecting …  {mkt.title}\n")
    sys.stdout.flush()

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "assets_ids": [mkt.up_token, mkt.down_token],
            "type": "market",
            "custom_feature_enabled": True,
        }))

        while True:
            remaining = mkt.end_ts - time.time()
            if remaining <= 0:
                return  # window time elapsed → switch to next market

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return  # deadline reached mid-wait

            msg     = json.loads(raw)
            updated = False

            if isinstance(msg, dict) and msg.get("event_type") == "book":
                outcome = token_to_outcome.get(msg.get("asset_id", ""))
                if outcome:
                    book[outcome]["bids"] = {b["price"]: b["size"] for b in msg.get("bids", [])}
                    book[outcome]["asks"] = {a["price"]: a["size"] for a in msg.get("asks", [])}
                    updated = True

            elif isinstance(msg, dict) and "price_changes" in msg:
                for change in msg["price_changes"]:
                    outcome = token_to_outcome.get(change.get("asset_id", ""))
                    if not outcome:
                        continue
                    side  = "bids" if change["side"] == "BUY" else "asks"
                    price = change["price"]
                    size  = change["size"]
                    if float(size) == 0:
                        book[outcome][side].pop(price, None)
                    else:
                        book[outcome][side][price] = size
                    updated = True

            # subscription ack / keep-alive — no action needed
            elif isinstance(msg, dict) and "market" in msg:
                pass

            if updated:
                render_book(mkt.title, book, mkt.end_ts)


def run_data_mode() -> None:
    clob        = ClobClient(HOST, chain_id=POLYGON)
    last_cid    = None   # condition_id of the market we just streamed

    while True:
        try:
            # Retry until a NEW market window is open on CLOB
            while True:
                try:
                    mkt = fetch_active_market(clob, exclude_cid=last_cid)
                    break
                except RuntimeError as e:
                    sys.stdout.write(f"\033[H\033[2J  {e} — retrying in 2s …\n")
                    sys.stdout.flush()
                    time.sleep(2)

            last_cid = mkt.condition_id
            asyncio.run(stream_order_book(mkt))
            # stream returned (timeout or WS close) → loop to find next window

        except KeyboardInterrupt:
            sys.stdout.write("\n  Stopped.\n")
            sys.stdout.flush()
            break
        except Exception as e:
            sys.stdout.write(f"\033[H\033[2J  Error: {e}\n  Reconnecting …\n")
            sys.stdout.flush()
            time.sleep(2)


# ── Auth client ────────────────────────────────────────────────────────────────

def build_client_l2() -> ClobClient:
    key = os.getenv("PRIVATE_KEY")
    if not key:
        raise ValueError("PRIVATE_KEY not set in .env")
    l1    = ClobClient(HOST, chain_id=POLYGON, key=key)
    creds = l1.create_or_derive_api_creds()
    log.info("API key derived: %s", creds.api_key)
    return ClobClient(HOST, chain_id=POLYGON, key=key, creds=creds)


# ── Phase D: Sniper ────────────────────────────────────────────────────────────

async def snipe_market(client: ClobClient, mkt: Market) -> None:
    """
    Monitor one market window via WebSocket.
    Fire a FOK market buy for SNIPE_AMOUNT USDC when:
      - midpoint of either token >= SNIPE_PROB
      - AND less than SNIPE_TIME seconds remain
    """
    book: dict = {
        "Up":   {"bids": {}, "asks": {}},
        "Down": {"bids": {}, "asks": {}},
    }
    token_to_outcome  = {mkt.up_token: "Up", mkt.down_token: "Down"}
    msg_count         = 0
    book_snapshots    = 0
    price_change_msgs = 0

    log.info(
        "watching  %r  end_ts=%s  (~%ds)  trigger: mid>=%.2f AND remaining<%ds",
        mkt.title, mkt.end_ts, int(mkt.end_ts - time.time()), SNIPE_PROB, SNIPE_TIME,
    )
    log_ws.debug("up_token=%s…  down_token=%s…", mkt.up_token[:16], mkt.down_token[:16])

    async with websockets.connect(WS_URL) as ws:
        sub = {
            "assets_ids": [mkt.up_token, mkt.down_token],
            "type": "market",
            "custom_feature_enabled": True,
        }
        log_ws.info("connected  subscribing assets=%s…,%s…",
                    mkt.up_token[:8], mkt.down_token[:8])
        log_ws.debug("subscription payload: %s", sub)
        await ws.send(json.dumps(sub))

        while True:
            remaining = mkt.end_ts - time.time()
            if remaining <= 0:
                log_ws.info("window expired  msgs=%d (snapshots=%d updates=%d)",
                            msg_count, book_snapshots, price_change_msgs)
                return

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                log_ws.info("recv timeout — window done  msgs=%d", msg_count)
                return

            msg_count += 1
            msg     = json.loads(raw)
            updated = False

            # ── Full book snapshot ──────────────────────────────────────────
            if isinstance(msg, dict) and msg.get("event_type") == "book":
                outcome = token_to_outcome.get(msg.get("asset_id", ""))
                if outcome:
                    book[outcome]["bids"] = {b["price"]: b["size"] for b in msg.get("bids", [])}
                    book[outcome]["asks"] = {a["price"]: a["size"] for a in msg.get("asks", [])}
                    updated = True
                    book_snapshots += 1
                    bids_top = sorted(book[outcome]["bids"].items(), key=lambda x: float(x[0]), reverse=True)[:1]
                    asks_top = sorted(book[outcome]["asks"].items(), key=lambda x: float(x[0]))[:1]
                    log_book.info(
                        "snapshot #%d  %-4s  bids=%d  asks=%d  best_bid=%s  best_ask=%s",
                        msg_count, outcome,
                        len(book[outcome]["bids"]), len(book[outcome]["asks"]),
                        bids_top[0] if bids_top else None,
                        asks_top[0] if asks_top else None,
                    )
                else:
                    log_book.warning("snapshot for unknown asset_id=%s…", msg.get("asset_id", "?")[:16])

            # ── Incremental price changes ───────────────────────────────────
            elif isinstance(msg, dict) and "price_changes" in msg:
                price_change_msgs += 1
                for change in msg["price_changes"]:
                    outcome = token_to_outcome.get(change.get("asset_id", ""))
                    if not outcome:
                        continue
                    side  = "bids" if change["side"] == "BUY" else "asks"
                    price = change["price"]
                    size  = change["size"]
                    if float(size) == 0:
                        book[outcome][side].pop(price, None)
                        log_book.debug("#%d  %-4s  %-4s  remove  %s", msg_count, outcome, side, price)
                    else:
                        book[outcome][side][price] = size
                        log_book.debug("#%d  %-4s  %-4s  set     %s → %s", msg_count, outcome, side, price, size)
                    updated = True

            # ── Subscription ack / keep-alive ──────────────────────────────
            elif isinstance(msg, dict) and "market" in msg:
                log_ws.debug("#%d  market ack/keep-alive: %s", msg_count, msg)

            else:
                log_ws.warning("unhandled msg #%d  keys=%s",
                               msg_count,
                               list(msg.keys()) if isinstance(msg, dict) else type(msg).__name__)

            # ── Mid calculation ─────────────────────────────────────────────
            if updated:
                in_window = remaining < SNIPE_TIME
                up_bids   = _sorted_bids(book["Up"]["bids"])
                up_asks   = _sorted_asks(book["Up"]["asks"])
                down_bids = _sorted_bids(book["Down"]["bids"])
                down_asks = _sorted_asks(book["Down"]["asks"])
                for outcome, bids, asks, cb, ca in (
                    ("Up",   up_bids,   up_asks,   down_bids, down_asks),
                    ("Down", down_bids, down_asks, up_bids,   up_asks),
                ):
                    mid, src = _compute_mid(bids, asks, cb, ca)
                    if mid is not None:
                        # INFO when inside snipe window (actionable); DEBUG otherwise
                        mid_log = log.info if in_window else log.debug
                        mid_log(
                            "mid  %-4s  %.4f  src=%-10s  remaining=%.1fs  in_snipe_window=%s",
                            outcome, mid, src, remaining, in_window,
                        )
                    else:
                        log.warning("mid  %-4s  no price data at all", outcome)

            # ── Snipe trigger ───────────────────────────────────────────────
            if updated and remaining < SNIPE_TIME:
                up_bids   = _sorted_bids(book["Up"]["bids"])
                up_asks   = _sorted_asks(book["Up"]["asks"])
                down_bids = _sorted_bids(book["Down"]["bids"])
                down_asks = _sorted_asks(book["Down"]["asks"])
                for outcome, token_id, bids, asks, cb, ca in (
                    ("Up",   mkt.up_token,   up_bids,   up_asks,   down_bids, down_asks),
                    ("Down", mkt.down_token, down_bids, down_asks, up_bids,   up_asks),
                ):
                    mid, src = _compute_mid(bids, asks, cb, ca)
                    if mid is None:
                        log.warning("snipe check  %-4s  no price data — skipping", outcome)
                        continue
                    log.debug("snipe check  %-4s  mid=%.4f  src=%s", outcome, mid, src)
                    if mid >= SNIPE_PROB:
                        log_order.critical(
                            "FIRE  %-4s  mid=%.4f (src=%s) >= %.2f  remaining=%.1fs  amount=%s USDC  token=%s…",
                            outcome, mid, src, SNIPE_PROB, remaining, SNIPE_AMOUNT, token_id[:16],
                        )
                        try:
                            order = client.create_market_order(
                                MarketOrderArgs(token_id=token_id, amount=SNIPE_AMOUNT, side=BUY)
                            )
                            log_order.info("order created: %s", order)
                            resp = client.post_order(order, OrderType.FOK)
                            log_order.info("order response: %s", resp)
                        except Exception as e:
                            log_order.error("order failed: %s: %s", type(e).__name__, e)


def run_snipe_mode(client: ClobClient) -> None:
    last_cid: str | None = None

    log.info("snipe mode started  SNIPE_PROB=%.2f  SNIPE_TIME=%ds  SNIPE_AMOUNT=%s USDC",
             SNIPE_PROB, SNIPE_TIME, SNIPE_AMOUNT)
    while True:
        try:
            while True:
                try:
                    mkt = fetch_active_market(client, exclude_cid=last_cid)
                    break
                except RuntimeError as e:
                    log.warning("%s — retrying in 2s …", e)
                    time.sleep(2)

            last_cid = mkt.condition_id
            asyncio.run(snipe_market(client, mkt))

        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.error("unexpected error: %s: %s — reconnecting in 2s", type(e).__name__, e)
            time.sleep(2)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down bot")
    parser.add_argument("mode", choices=["data", "snipe"], help="Operating mode")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="Logging level for snipe mode: DEBUG INFO WARNING ERROR CRITICAL (default: INFO, env: LOG_LEVEL)",
    )
    args = parser.parse_args()

    if args.mode == "data":
        run_data_mode()

    elif args.mode == "snipe":
        configure_logging(args.log_level)
        client = build_client_l2()
        run_snipe_mode(client)


if __name__ == "__main__":
    main()
