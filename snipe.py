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
import requests
import websockets

from common import (
    HOST, WS_URL,
    SNIPE_AMOUNT, SNIPE_PROB, SNIPE_TIME,
    RPC_URL, USDC_E, CTF_ADDR,
    log, log_ws, log_book, log_order, log_redeem,
    configure_logging, fetch_active_market,
    sorted_bids, sorted_asks, compute_mid,
    ClobClient,
)
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


# ── Auth client ────────────────────────────────────────────────────────────────

def build_client_l2() -> ClobClient:
    key = os.getenv("PRIVATE_KEY")
    if not key:
        raise ValueError("PRIVATE_KEY not set in .env")
    l1    = ClobClient(HOST, chain_id=POLYGON, key=key)
    creds = l1.create_or_derive_api_creds()
    log.info("API key derived: %s", creds.api_key)
    return ClobClient(HOST, chain_id=POLYGON, key=key, creds=creds)


# ── On-chain helpers ───────────────────────────────────────────────────────────

def _rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    res = r.json()
    if "error" in res:
        raise RuntimeError(f"RPC error: {res['error']['message']}")
    return res["result"]

def _eth_call(to, data):
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])

def _sel(sig: str) -> str:
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode())[:4].hex()

def _find_index_set(cid: str, asset_id: int) -> int | None:
    sel_gc = _sel("getCollectionId(bytes32,bytes32,uint256)")
    sel_gp = _sel("getPositionId(address,bytes32)")
    cid_padded  = cid[2:].zfill(64)
    addr_padded = "000000000000000000000000" + USDC_E[2:].lower()
    for i in range(8):
        index_set    = 1 << i
        idx_padded   = hex(index_set)[2:].zfill(64)
        coll_result  = _eth_call(CTF_ADDR, sel_gc + "0" * 64 + cid_padded + idx_padded)
        if not coll_result:
            continue
        pos_result = _eth_call(CTF_ADDR, sel_gp + addr_padded + coll_result[2:].zfill(64))
        if pos_result and int(pos_result, 16) == asset_id:
            return index_set
    return None

def _usdc_e_balance(wallet: str) -> float:
    result = _eth_call(USDC_E, "0x70a08231" + "000000000000000000000000" + wallet[2:].lower())
    return int(result, 16) / 1e6

def _wait_receipt(tx_hash: str, timeout: int = 90) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            return receipt
        time.sleep(3)
    return None


# ── CTF redemption ─────────────────────────────────────────────────────────────

def redeem_pending_positions(client: ClobClient, private_key: str) -> None:
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

    sel_pd    = _sel("payoutDenominator(bytes32)")
    redeemable = []

    for t in candidates:
        cid      = t["market"]
        asset_id = int(t["asset_id"])
        outcome  = t.get("outcome", "?")

        try:
            denom = int(_eth_call(CTF_ADDR, sel_pd + cid[2:].zfill(64)), 16)
        except Exception as e:
            log_redeem.debug("payoutDenominator(%s…): %s", cid[:12], e)
            continue

        if denom == 0:
            continue

        try:
            addr_padded = "000000000000000000000000" + wallet[2:].lower()
            balance = int(_eth_call(CTF_ADDR, "0x00fdd58e" + addr_padded + hex(asset_id)[2:].zfill(64)), 16)
        except Exception as e:
            log_redeem.debug("balanceOf(%s…): %s", cid[:12], e)
            continue

        if balance == 0:
            continue

        try:
            index_set = _find_index_set(cid, asset_id)
        except Exception as e:
            log_redeem.warning("_find_index_set(%s…): %s", cid[:12], e)
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
    balance_before = _usdc_e_balance(wallet)

    try:
        nonce     = int(_rpc("eth_getTransactionCount", [wallet, "latest"]), 16)
        gas_price = int(int(_rpc("eth_gasPrice", []), 16) * 1.2)
    except Exception as e:
        log_redeem.error("failed to get nonce/gas: %s", e)
        return

    from eth_utils import to_checksum_address
    sel_rp = _sel("redeemPositions(address,bytes32,bytes32,uint256[])")

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
            tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
            nonce  += 1
            log_redeem.info("[%d/%d] %-5s  tx=%s…  waiting", i, len(redeemable), pos["outcome"], tx_hash[:18])
            receipt = _wait_receipt(tx_hash)
            if receipt and receipt.get("status") == "0x1":
                log_redeem.critical("[%d/%d] REDEEMED  %-5s  +%.4f USDC.e  tx=%s…",
                                    i, len(redeemable), pos["outcome"], pos["balance"], tx_hash[:18])
            else:
                log_redeem.error("[%d/%d] tx FAILED  %-5s  tx=%s…  receipt=%s",
                                 i, len(redeemable), pos["outcome"], tx_hash[:18], receipt)
        except Exception as e:
            log_redeem.error("[%d/%d] redeem error: %s: %s", i, len(redeemable), type(e).__name__, e)

    balance_after = _usdc_e_balance(wallet)
    if balance_after > balance_before:
        log_redeem.critical("redemption complete  gained=+%.4f USDC.e  balance=%.4f",
                            balance_after - balance_before, balance_after)
    else:
        log_redeem.info("redemption complete  balance=%.4f", balance_after)


# ── Sniper ─────────────────────────────────────────────────────────────────────

async def snipe_market(client: ClobClient, mkt) -> None:
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
                        return

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
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
                                for attempt in range(1, 4):
                                    try:
                                        order = client.create_market_order(
                                            MarketOrderArgs(token_id=token_id, amount=SNIPE_AMOUNT, side=BUY)
                                        )
                                        resp   = client.post_order(order, OrderType.FOK)
                                        status = resp.get("status", "") if isinstance(resp, dict) else ""
                                        if status == "matched":
                                            log_order.critical("WIN  %-4s  filled  attempt=%d/3  orderID=%s",
                                                               outcome, attempt, resp.get("orderID", "?"))
                                        else:
                                            log_order.critical("LOSS  %-4s  not filled  attempt=%d/3  status=%s  resp=%s",
                                                               outcome, attempt, status or "?", resp)
                                        break
                                    except Exception as e:
                                        log_order.error("order failed (attempt %d/3): %s: %s", attempt, type(e).__name__, e)
                                        if getattr(e, "status_code", None) == 400:
                                            break
                                else:
                                    log_order.critical("LOSS  %-4s  all 3 attempts failed", outcome)
                                fired = True
                                return

        except Exception as e:
            remaining = mkt.end_ts - time.time()
            if remaining <= 0:
                return
            log_ws.warning("WS disconnected (%s: %s) — reconnecting in 2s  (%.0fs left)",
                           type(e).__name__, e, remaining)
            await asyncio.sleep(2)


def run_snipe_mode(client: ClobClient) -> None:
    private_key = os.getenv("PRIVATE_KEY")
    last_cid: str | None = None

    log.info("snipe mode started  SNIPE_PROB=%.2f  SNIPE_TIME=%ds  SNIPE_AMOUNT=%s USDC",
             SNIPE_PROB, SNIPE_TIME, SNIPE_AMOUNT)

    redeem_pending_positions(client, private_key)

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

            remaining = mkt.end_ts - time.time()
            if remaining > 0:
                log.info("window not yet expired — waiting %.0fs for next window", remaining)
                time.sleep(remaining)

            redeem_pending_positions(client, private_key)

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
