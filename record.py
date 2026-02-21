"""
Polymarket BTC Up/Down — record mode.

Streams live order book + Binance price and writes JSONL ticks to
recordings/YYYY-MM-DDTHH-MM-SS_<slug>.jsonl.

Usage:
  python main.py record [--log-level LEVEL]
  python record.py [--log-level LEVEL]
"""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

from binance_signal import BinancePriceSignal
from common import (
    WS_URL,
    log, log_ws, log_book,
    configure_logging, fetch_active_market,
    sorted_bids, sorted_asks, compute_mid,
    ClobClient,
    build_client_l1,
)

RECORDINGS_DIR = Path(__file__).parent / "recordings"


def _recording_path(slug: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return RECORDINGS_DIR / f"{ts}_{slug}.jsonl"


async def record_market(mkt) -> None:
    """Stream one market window, writing JSONL ticks to disk."""
    RECORDINGS_DIR.mkdir(exist_ok=True)

    # Derive slug from end_ts (same logic as common.fetch_active_market)
    window_ts = mkt.end_ts - 300
    slug = f"btc-updown-5m-{window_ts}"
    path = _recording_path(slug)

    log.info("recording  %r  →  %s", mkt.title, path.name)

    book = {
        "Up":   {"bids": {}, "asks": {}},
        "Down": {"bids": {}, "asks": {}},
    }
    token_to_outcome  = {mkt.up_token: "Up", mkt.down_token: "Down"}
    msg_count         = 0
    book_snapshots    = 0
    price_change_msgs = 0
    ticks_written     = 0

    signal = BinancePriceSignal()
    await signal.start()
    log.info("binance signal started")

    sub = {"assets_ids": [mkt.up_token, mkt.down_token], "type": "market", "custom_feature_enabled": True}

    with open(path, "w") as f:
        while True:
            remaining = mkt.end_ts - time.time()
            if remaining <= 0:
                break

            try:
                async with websockets.connect(WS_URL) as ws:
                    book["Up"]   = {"bids": {}, "asks": {}}
                    book["Down"] = {"bids": {}, "asks": {}}
                    log_ws.info("connected  assets=%s…,%s…", mkt.up_token[:8], mkt.down_token[:8])
                    await ws.send(json.dumps(sub))

                    while True:
                        remaining = mkt.end_ts - time.time()
                        if remaining <= 0:
                            break

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break

                        msg_count += 1
                        msg     = json.loads(raw)
                        updated = False

                        if isinstance(msg, dict) and msg.get("event_type") == "book":
                            outcome = token_to_outcome.get(msg.get("asset_id", ""))
                            if outcome:
                                book[outcome]["bids"] = {b["price"]: b["size"] for b in msg.get("bids", [])}
                                book[outcome]["asks"] = {a["price"]: a["size"] for a in msg.get("asks", [])}
                                updated        = True
                                book_snapshots += 1
                            else:
                                log_book.warning("snapshot for unknown asset_id=%s…", msg.get("asset_id", "?")[:16])

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
                                else:
                                    book[outcome][side][price] = size
                                updated = True

                        if updated:
                            up_bids   = sorted_bids(book["Up"]["bids"])
                            up_asks   = sorted_asks(book["Up"]["asks"])
                            down_bids = sorted_bids(book["Down"]["bids"])
                            down_asks = sorted_asks(book["Down"]["asks"])

                            up_mid,   _ = compute_mid(up_bids,   up_asks,   down_bids, down_asks)
                            down_mid, _ = compute_mid(down_bids, down_asks, up_bids,   up_asks)

                            tick = {
                                "ts":        time.time(),
                                "remaining": max(0.0, mkt.end_ts - time.time()),
                                "up_mid":    round(up_mid,   4) if up_mid   is not None else None,
                                "down_mid":  round(down_mid, 4) if down_mid is not None else None,
                                "btc":       round(signal.price, 2) if signal.price else None,
                            }
                            f.write(json.dumps(tick) + "\n")
                            f.flush()
                            ticks_written += 1

                            log.debug(
                                "tick  remaining=%.1fs  up_mid=%s  down_mid=%s  btc=%s",
                                tick["remaining"], tick["up_mid"], tick["down_mid"], tick["btc"],
                            )

            except Exception as e:
                remaining = mkt.end_ts - time.time()
                if remaining <= 0:
                    break
                log_ws.warning("WS disconnected (%s: %s) — reconnecting in 2s  (%.0fs left)",
                               type(e).__name__, e, remaining)
                await asyncio.sleep(2)

    await signal.stop()
    log.info(
        "window done  ticks=%d  msgs=%d (snapshots=%d updates=%d)  file=%s",
        ticks_written, msg_count, book_snapshots, price_change_msgs, path.name,
    )


def run_record_mode() -> None:
    client: ClobClient = build_client_l1()
    last_cid: str | None = None

    log.info("record mode started")

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
            asyncio.run(record_market(mkt))

            remaining = mkt.end_ts - time.time()
            if remaining > 0:
                log.info("window not yet expired — waiting %.0fs for next window", remaining)
                time.sleep(remaining)

        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.error("unexpected error: %s: %s — retrying in 2s", type(e).__name__, e)
            last_cid = None
            time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down recorder")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="DEBUG INFO WARNING ERROR CRITICAL (default: INFO)",
    )
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_record_mode()
