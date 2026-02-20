"""
Polymarket BTC Up/Down 5-minute bot.

Modes:
  python main.py data    -- Phase A: live order book via WebSocket (no auth)
  python main.py mm      -- Phase C: run market maker (requires auth)
"""

import asyncio
import json
import os
import sys
import time
import requests
import websockets
from datetime import datetime, timezone
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

HOST         = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
WS_URL       = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MARKET_SLUG  = "btc-updown-5m"

# Market-maker settings (USDC)
MM_SPREAD  = 0.04
MM_SIZE    = 5.0
MM_REFRESH = 10
TICK       = 0.01


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


# ── Phase C: Market maker ──────────────────────────────────────────────────────

def build_client_l2() -> ClobClient:
    key = os.getenv("PRIVATE_KEY")
    if not key:
        raise ValueError("PRIVATE_KEY not set in .env")
    l1    = ClobClient(HOST, chain_id=POLYGON, key=key)
    creds = l1.create_or_derive_api_creds()
    print(f"  API key: {creds.api_key}")
    return ClobClient(HOST, chain_id=POLYGON, key=key, creds=creds)


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 4)


def post_mm_orders(client: ClobClient, mkt: Market) -> list[str]:
    half      = MM_SPREAD / 2
    order_ids = []
    for token_id, label in [(mkt.up_token, "Up"), (mkt.down_token, "Down")]:
        mid       = float(client.get_midpoint(token_id)["mid"])
        bid_price = round_tick(max(0.01, mid - half))
        ask_price = round_tick(min(0.99, mid + half))
        print(f"  [{label}] mid={mid:.3f}  bid={bid_price}  ask={ask_price}")
        for price, side in [(bid_price, BUY), (ask_price, SELL)]:
            args = OrderArgs(token_id=token_id, price=price, size=MM_SIZE, side=side)
            resp = client.create_and_post_order(args)
            oid  = resp.get("orderID") or resp.get("order_id", "")
            print(f"    {side} posted: {oid}")
            if oid:
                order_ids.append(oid)
    return order_ids


def run_market_maker(client: ClobClient) -> None:
    active_ids: list[str] = []
    mkt: Market | None    = None

    print("\n  Market maker started. Ctrl-C to stop.\n")
    while True:
        try:
            if active_ids:
                print(f"  Cancelling {len(active_ids)} stale orders …")
                client.cancel_orders(active_ids)
                active_ids = []

            fresh = fetch_active_market(client)
            if mkt is None or fresh.condition_id != mkt.condition_id:
                mkt = fresh
                print(f"  Market: {mkt.title}")

            active_ids = post_mm_orders(client, mkt)
            print(f"\n  Sleeping {MM_REFRESH}s …\n")
            time.sleep(MM_REFRESH)

        except KeyboardInterrupt:
            print("\n  Shutting down …")
            if active_ids:
                client.cancel_orders(active_ids)
                print("  Orders cancelled.")
            break


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "data"

    if mode == "data":
        run_data_mode()

    elif mode == "mm":
        client = build_client_l2()
        run_market_maker(client)

    else:
        print(f"Unknown mode '{mode}'. Use: data | mm")
        sys.exit(1)


if __name__ == "__main__":
    main()
