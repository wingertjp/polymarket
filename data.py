"""
Polymarket BTC Up/Down — data mode (live order book TUI, no auth).

Usage:
  python data.py
"""

import asyncio
import json
import os
import sys
import time
import websockets
from datetime import datetime, timezone

from common import HOST, WS_URL, fetch_active_market, ClobClient
from py_clob_client.constants import POLYGON

# ── Display constants ──────────────────────────────────────────────────────────
#   Each cell: "  " + price(6) + "  " + size(9) = 21 chars
_PW = 6   # " 54.0%"
_SW = 9   # "$  1,434"
_CW = 2 + _PW + 2 + _SW   # 21 — full cell width
_G  = "   "               # 3-space gap between columns


def _pct(v) -> str:
    return f"{float(v)*100:5.1f}%"

def _usd(v) -> str:
    return f"${float(v):>8,.0f}"

def _cell(p, s) -> str:
    return f"  {_pct(p)}  {_usd(s)}"

def _empty() -> str:
    return " " * _CW

def _sep(ch: str = "─") -> str:
    return "  " + ch * (_PW + 2 + _SW)


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

    up_mid      = mid(up_bids, up_asks)
    down_mid    = mid(down_bids, down_asks)
    now         = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    countdown   = max(0, int(end_ts - time.time()))
    mins, secs  = divmod(countdown, 60)
    W           = _CW * 2 + len(_G)
    BORDER      = "  " + "═" * W

    up_hdr   = f"  ▲ Up    mid {_pct(up_mid)   if up_mid   is not None else '  n/a'}"
    down_hdr = f"  ▼ Down  mid {_pct(down_mid) if down_mid is not None else '  n/a'}"
    col_hdr  = f"  {'Price':>{_PW}}  {'Size':>{_SW}}"

    lines = [
        "\033[H\033[2J",
        BORDER,
        f"  {title}",
        f"  ● LIVE  ·  {now} UTC  ·  closes in {mins}:{secs:02d}",
        BORDER,
        f"{up_hdr:<{_CW}}{_G}{down_hdr:<{_CW}}",
        f"{_sep()}{_G}{_sep()}",
        f"{col_hdr}{_G}{col_hdr}",
    ]

    n_asks = max(len(up_asks), len(down_asks), 1)
    for i in range(n_asks - 1, -1, -1):
        left  = _cell(*up_asks[i])   if i < len(up_asks)   else _empty()
        right = _cell(*down_asks[i]) if i < len(down_asks) else _empty()
        lines.append(f"{left}{_G}{right}")

    lines.append(f"{_sep('┄')}{_G}{_sep('┄')}")

    n_bids = max(len(up_bids), len(down_bids), 1)
    for i in range(n_bids):
        left  = _cell(*up_bids[i])   if i < len(up_bids)   else _empty()
        right = _cell(*down_bids[i]) if i < len(down_bids) else _empty()
        lines.append(f"{left}{_G}{right}")

    lines.append(f"{_sep()}{_G}{_sep()}")
    lines.append(BORDER)

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


async def stream_order_book(mkt) -> None:
    """Stream order book for one market window. Returns when the window expires."""
    book = {
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
                return

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return

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
    clob     = ClobClient(HOST, chain_id=POLYGON)
    last_cid = None

    while True:
        try:
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

        except KeyboardInterrupt:
            sys.stdout.write("\n  Stopped.\n")
            sys.stdout.flush()
            break
        except Exception as e:
            sys.stdout.write(f"\033[H\033[2J  Error: {e}\n  Reconnecting …\n")
            sys.stdout.flush()
            time.sleep(2)


if __name__ == "__main__":
    run_data_mode()
