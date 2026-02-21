"""
Polymarket BTC Up/Down — snipe mode (requires PRIVATE_KEY in .env).

Usage:
  python snipe.py [--log-level LEVEL]
"""

import argparse
import asyncio
import json
import os
import time
import websockets

from binance_signal import BinancePriceSignal
from common import (
    WS_URL,
    SNIPE_AMOUNT, SNIPE_PROB, SNIPE_TIME, RESCUE_TIME, RESCUE_MID_THRESHOLD, SNIPE_RESCUE_AMOUNT, DRY_RUN,
    log, log_ws, log_book, log_order,
    configure_logging, fetch_active_market,
    sorted_bids, sorted_asks, compute_mid,
    ClobClient,
    build_client_l1, build_client_l2,
)
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


# ── Dry run outcome helpers ─────────────────────────────────────────────────────

def _bet_result(initial_outcome: str, open_price: float, close_price: float) -> str:
    """Return 'WIN' or 'LOSS' for the simulated bet."""
    actual_up = close_price > open_price
    won = actual_up if initial_outcome == "Up" else not actual_up
    return "WIN" if won else "LOSS"


def _log_dry_run_outcome(
    initial_outcome: str | None,
    rescued: bool,
    signal: "BinancePriceSignal",
) -> None:
    if initial_outcome is None:
        log_order.warning("[DRY RUN] window ended — no snipe was triggered")
        return

    open_price  = signal.candle_open
    close_price = signal.price

    if open_price == 0.0:
        log_order.warning("[DRY RUN] outcome unavailable — candle open price not received yet")
        return

    actual = "UP" if close_price > open_price else "DOWN"
    initial_result = _bet_result(initial_outcome, open_price, close_price)

    if not rescued:
        log_order.critical(
            "[DRY RUN] OUTCOME  bet=%-4s  open=%.2f  close=%.2f  actual=%-4s  result=%s",
            initial_outcome, open_price, close_price, actual, initial_result,
        )
    else:
        rescue_outcome = "Down" if initial_outcome == "Up" else "Up"
        rescue_result  = _bet_result(rescue_outcome, open_price, close_price)
        log_order.critical(
            "[DRY RUN] OUTCOME  bet=%-4s  open=%.2f  close=%.2f  actual=%-4s"
            "  initial=%s  rescue=%s",
            initial_outcome, open_price, close_price, actual,
            initial_result, rescue_result,
        )


# ── Rescue helper ──────────────────────────────────────────────────────────────

def should_rescue(
    initial_mid: float,
    remaining: float,
    rescue_time: float,
    rescue_mid_threshold: float,
) -> bool:
    """Return True when Polymarket mid of the initial bet token has collapsed below threshold."""
    if remaining > rescue_time:
        return False
    return initial_mid <= rescue_mid_threshold


# ── Sniper ─────────────────────────────────────────────────────────────────────

async def snipe_market(client: ClobClient, mkt, *, dry_run: bool = False) -> None:
    """Monitor one market window. Fire FOK buy when mid >= SNIPE_PROB and < SNIPE_TIME remaining."""
    book = {
        "Up":   {"bids": {}, "asks": {}},
        "Down": {"bids": {}, "asks": {}},
    }
    token_to_outcome  = {mkt.up_token: "Up", mkt.down_token: "Down"}
    msg_count         = 0
    book_snapshots    = 0
    price_change_msgs = 0
    fired             = False
    initial_outcome:  str | None = None   # "Up" or "Down" — what we bought
    rescue_token_id:  str | None = None   # opposite token to buy on rescue
    rescued           = False

    signal = BinancePriceSignal()
    await signal.start()
    log.info("binance signal started  mode=%s", "DRY RUN" if dry_run else "LIVE")

    log.info(
        "watching  %r  end_ts=%s  (~%ds)  trigger: mid>=%.2f AND remaining<%ds",
        mkt.title, mkt.end_ts, int(mkt.end_ts - time.time()), SNIPE_PROB, SNIPE_TIME,
    )

    sub = {"assets_ids": [mkt.up_token, mkt.down_token], "type": "market", "custom_feature_enabled": True}

    while True:
        remaining = mkt.end_ts - time.time()
        if remaining <= 0:
            log_ws.info("window expired  msgs=%d (snapshots=%d updates=%d)",
                        msg_count, book_snapshots, price_change_msgs)
            if dry_run:
                _log_dry_run_outcome(initial_outcome, rescued, signal)
            return

        try:
            async with websockets.connect(WS_URL) as ws:
                book["Up"]   = {"bids": {}, "asks": {}}
                book["Down"] = {"bids": {}, "asks": {}}
                log_ws.info("connected  subscribing assets=%s…,%s…", mkt.up_token[:8], mkt.down_token[:8])
                await ws.send(json.dumps(sub))

                while True:
                    remaining = mkt.end_ts - time.time()
                    if remaining <= 0:
                        log_ws.info("window expired  msgs=%d (snapshots=%d updates=%d)",
                                    msg_count, book_snapshots, price_change_msgs)
                        if dry_run:
                            _log_dry_run_outcome(initial_outcome, rescued, signal)
                        return

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        if dry_run:
                            _log_dry_run_outcome(initial_outcome, rescued, signal)
                        return

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
                            bids_top = sorted(book[outcome]["bids"].items(), key=lambda x: float(x[0]), reverse=True)[:1]
                            asks_top = sorted(book[outcome]["asks"].items(), key=lambda x: float(x[0]))[:1]
                            log_book.debug("snapshot #%d  %-4s  bids=%d  asks=%d  best_bid=%s  best_ask=%s",
                                           msg_count, outcome,
                                           len(book[outcome]["bids"]), len(book[outcome]["asks"]),
                                           bids_top[0] if bids_top else None,
                                           asks_top[0] if asks_top else None)
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
                                log_book.debug("#%d  %-4s  %-4s  remove  %s", msg_count, outcome, side, price)
                            else:
                                book[outcome][side][price] = size
                                log_book.debug("#%d  %-4s  %-4s  set     %s → %s", msg_count, outcome, side, price, size)
                            updated = True

                    elif isinstance(msg, dict) and ("market" in msg or "list" in msg):
                        log_ws.debug("#%d  ack/keep-alive: %s", msg_count, msg)
                    elif isinstance(msg, list):
                        log_ws.debug("#%d  ack/keep-alive (list): %s", msg_count, msg)
                    else:
                        log_ws.warning("unhandled msg #%d  keys=%s", msg_count,
                                       list(msg.keys()) if isinstance(msg, dict) else type(msg).__name__)

                    if updated and remaining < SNIPE_TIME and not fired:
                        up_bids   = sorted_bids(book["Up"]["bids"])
                        up_asks   = sorted_asks(book["Up"]["asks"])
                        down_bids = sorted_bids(book["Down"]["bids"])
                        down_asks = sorted_asks(book["Down"]["asks"])
                        for outcome, token_id, bids, asks, cb, ca in (
                            ("Up",   mkt.up_token,   up_bids,   up_asks,   down_bids, down_asks),
                            ("Down", mkt.down_token, down_bids, down_asks, up_bids,   up_asks),
                        ):
                            mid, src = compute_mid(bids, asks, cb, ca)
                            if mid is None:
                                log.warning("snipe check  %-4s  no price data — skipping", outcome)
                                continue
                            log.debug("snipe check  %-4s  mid=%.4f  src=%s", outcome, mid, src)
                            if mid >= SNIPE_PROB:
                                log_order.critical(
                                    "FIRE  %-4s  mid=%.4f (src=%s) >= %.2f  remaining=%.1fs  amount=%s USDC  token=%s…",
                                    outcome, mid, src, SNIPE_PROB, remaining, SNIPE_AMOUNT, token_id[:16],
                                )
                                if dry_run:
                                    log_order.warning("[DRY RUN] order skipped")
                                else:
                                    for attempt in range(1, 4):
                                        try:
                                            order = client.create_market_order(
                                                MarketOrderArgs(token_id=token_id, amount=SNIPE_AMOUNT, side=BUY)
                                            )
                                            resp   = client.post_order(order, OrderType.FOK)
                                            status = resp.get("status", "") if isinstance(resp, dict) else ""
                                            if status == "matched":
                                                log_order.critical("FILLED  %-4s  attempt=%d/3  orderID=%s",
                                                                   outcome, attempt, resp.get("orderID", "?"))
                                            else:
                                                log_order.critical("NOT_FILLED  %-4s  attempt=%d/3  status=%s  resp=%s",
                                                                   outcome, attempt, status or "?", resp)
                                            break
                                        except Exception as e:
                                            log_order.error("order failed (attempt %d/3): %s: %s", attempt, type(e).__name__, e)
                                            if getattr(e, "status_code", None) == 400:
                                                break
                                    else:
                                        log_order.critical("FAILED  %-4s  all 3 attempts failed", outcome)
                                fired          = True
                                initial_outcome = outcome
                                rescue_token_id = (
                                    mkt.down_token if outcome == "Up" else mkt.up_token
                                )
                                break  # keep watching for rescue

                    if fired and not rescued and updated and remaining < RESCUE_TIME:
                        _ib = sorted_bids(book[initial_outcome]["bids"])
                        _ia = sorted_asks(book[initial_outcome]["asks"])
                        _cb = sorted_bids(book["Down" if initial_outcome == "Up" else "Up"]["bids"])
                        _ca = sorted_asks(book["Down" if initial_outcome == "Up" else "Up"]["asks"])
                        initial_mid, _ = compute_mid(_ib, _ia, _cb, _ca)
                        rescue_outcome  = "Down" if initial_outcome == "Up" else "Up"
                        if initial_mid is not None and should_rescue(
                            initial_mid, remaining, RESCUE_TIME, RESCUE_MID_THRESHOLD
                        ):
                            log_order.critical(
                                "RESCUE  %s→%s  mid=%.4f<=%.2f  remaining=%.1fs  amount=%s USDC  token=%s…",
                                initial_outcome, rescue_outcome, initial_mid, RESCUE_MID_THRESHOLD,
                                remaining, SNIPE_RESCUE_AMOUNT, rescue_token_id[:16],
                            )
                            if dry_run:
                                log_order.warning("[DRY RUN] rescue order skipped")
                            else:
                                for attempt in range(1, 4):
                                    try:
                                        order = client.create_market_order(
                                            MarketOrderArgs(token_id=rescue_token_id, amount=SNIPE_RESCUE_AMOUNT, side=BUY)
                                        )
                                        resp   = client.post_order(order, OrderType.FOK)
                                        status = resp.get("status", "") if isinstance(resp, dict) else ""
                                        if status == "matched":
                                            log_order.critical("RESCUE_FILLED  attempt=%d/3  orderID=%s",
                                                               attempt, resp.get("orderID", "?"))
                                        else:
                                            log_order.critical("RESCUE_NOT_FILLED  attempt=%d/3  status=%s  resp=%s",
                                                               attempt, status or "?", resp)
                                        break
                                    except Exception as e:
                                        log_order.error("rescue order failed (attempt %d/3): %s: %s",
                                                        attempt, type(e).__name__, e)
                                        if getattr(e, "status_code", None) == 400:
                                            break
                                else:
                                    log_order.critical("RESCUE_FAILED  all 3 attempts failed")
                            rescued = True
                            if dry_run:
                                _log_dry_run_outcome(initial_outcome, rescued, signal)
                            return

        except Exception as e:
            remaining = mkt.end_ts - time.time()
            if remaining <= 0:
                return
            log_ws.warning("WS disconnected (%s: %s) — reconnecting in 2s  (%.0fs left)",
                           type(e).__name__, e, remaining)
            await asyncio.sleep(2)


def run_snipe_mode(client: ClobClient, *, dry_run: bool = False) -> None:
    last_cid: str | None = None

    log.info(
        "snipe mode started  mode=%s  SNIPE_PROB=%.2f  SNIPE_TIME=%ds  SNIPE_AMOUNT=%s USDC",
        "DRY RUN" if dry_run else "LIVE", SNIPE_PROB, SNIPE_TIME, SNIPE_AMOUNT,
    )

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
            asyncio.run(snipe_market(client, mkt, dry_run=dry_run))

            remaining = mkt.end_ts - time.time()
            if remaining > 0:
                log.info("window not yet expired — waiting %.0fs for next window", remaining)
                time.sleep(remaining)

        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log.error("unexpected error: %s: %s — reconnecting in 2s", type(e).__name__, e)
            last_cid = None
            time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down sniper")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="DEBUG INFO WARNING ERROR CRITICAL (default: INFO)",
    )
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_snipe_mode(build_client_l2())
