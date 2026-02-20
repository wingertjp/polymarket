"""
Polymarket BTC Up/Down — wallet mode (list on-chain CTF positions).

Usage:
  python wallet.py [--log-level LEVEL]
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from common import (
    CTF_ADDR,
    log,
    configure_logging, build_client_l2,
    eth_call, abi_sel, usdc_e_balance,
)
from eth_account import Account


def _ctf_balance(wallet: str, asset_id: int) -> int:
    addr_padded = "000000000000000000000000" + wallet[2:].lower()
    tid_padded  = hex(asset_id)[2:].zfill(64)
    return int(eth_call(CTF_ADDR, "0x00fdd58e" + addr_padded + tid_padded), 16)


def _position_status(cid: str, outcome: str, balance: int, denom: int) -> tuple[str, bool | None]:
    """
    Returns (status, won).
    won = True/False for resolved positions, None for active ones.
    """
    if denom == 0:
        return "active", None

    # Market resolved — check if this outcome won
    try:
        sel_pn = abi_sel("payoutNumerators(bytes32,uint256)")
        up_num = int(eth_call(CTF_ADDR, sel_pn + cid[2:].zfill(64) + "0" * 64), 16)
        dn_num = int(eth_call(CTF_ADDR, sel_pn + cid[2:].zfill(64) + hex(1)[2:].zfill(64)), 16)
        won = (outcome == "Up" and up_num == denom) or (outcome == "Down" and dn_num == denom)
    except Exception as e:
        log.warning("payoutNumerators failed  cid=%s…  outcome=%s: %s", cid[:12], outcome, e)
        won = False

    if balance > 0 and won:
        return "redeemable ✓", True
    elif balance > 0:
        return "lost", False
    elif won:
        return "redeemed", True
    else:
        return "lost", False


def run_wallet_mode() -> None:
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        sys.exit("PRIVATE_KEY not set in .env")

    account = Account.from_key(private_key)
    wallet  = account.address

    print(f"\nwallet: {wallet}\n")

    client = build_client_l2()

    try:
        resp   = client.get_trades()
        trades = resp if isinstance(resp, list) else resp.get("data", [])
    except Exception as e:
        sys.exit(f"get_trades failed: {e}")

    # ── Today's trade log ──────────────────────────────────────────────────────
    today_start = int(time.time()) // 86400 * 86400  # midnight UTC

    def _trade_ts(t) -> float:
        raw = t.get("match_time") or t.get("created_at") or 0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    today_trades = [t for t in trades if _trade_ts(t) >= today_start]
    today_trades.sort(key=_trade_ts)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"  today ({today_str} UTC) — {len(today_trades)} trade(s)")
    if today_trades:
        print(f"  {'time':>5}  {'outcome':<7}  {'price':>6}  {'tokens':>8}  {'cost':>8}")
        print("  " + "─" * 44)
        today_cost = 0.0
        for t in today_trades:
            ts      = datetime.fromtimestamp(_trade_ts(t), tz=timezone.utc).strftime("%H:%M")
            outcome = t.get("outcome", "?")
            price   = float(t.get("price", 0) or 0)
            size    = float(t.get("size",  0) or 0)
            cost    = price * size
            today_cost += cost
            print(f"  {ts:>5}  {outcome:<7}  {price:>6.4f}  {size:>8.4f}  {cost:>8.4f}")
        print(f"  {'':>5}  {'':7}  {'':6}  {'total:':>8}  {today_cost:>8.4f}")
    print()

    # Aggregate cost and total tokens per (cid, asset_id) across ALL trades
    # cost = sum(price * size)  — USDC spent
    # tokens = sum(size)        — outcome tokens acquired (each redeems for 1 USDC if won)
    cost_map   = defaultdict(float)  # key -> total USDC spent
    tokens_map = defaultdict(float)  # key -> total outcome tokens acquired
    for t in trades:
        key = (t.get("market", ""), t.get("asset_id", ""))
        if not all(key):
            continue
        price = float(t.get("price", 0) or 0)
        size  = float(t.get("size",  0) or 0)
        cost_map[key]   += price * size
        tokens_map[key] += size

    # Deduplicate by (cid, asset_id) for on-chain queries
    seen, candidates = set(), []
    for t in trades:
        key = (t.get("market", ""), t.get("asset_id", ""))
        if key in seen or not all(key):
            continue
        seen.add(key)
        candidates.append(t)

    if not candidates:
        print("no trades found")
        return

    sel_pd = abi_sel("payoutDenominator(bytes32)")
    rows   = []

    print(f"checking {len(candidates)} position(s) on-chain…\n")

    for t in candidates:
        cid      = t["market"]
        asset_id = int(t["asset_id"])
        outcome  = t.get("outcome", "?")
        key      = (cid, t["asset_id"])  # string key, matches cost_map

        try:
            balance = _ctf_balance(wallet, asset_id)
        except Exception as e:
            log.warning("balanceOf failed  cid=%s…: %s", cid[:12], e)
            continue

        try:
            denom = int(eth_call(CTF_ADDR, sel_pd + cid[2:].zfill(64)), 16)
        except Exception as e:
            log.warning("payoutDenominator failed  cid=%s…: %s", cid[:12], e)
            denom = 0

        status, won  = _position_status(cid, outcome, balance, denom)
        balance_usdc = balance / 1e6
        cost         = cost_map.get(key, 0.0)
        total_tokens = tokens_map.get(key, 0.0)

        # PnL calculation
        if won is None:
            pnl = None                              # active — unrealized
        elif won and balance > 0:
            pnl = balance_usdc - cost               # redeemable: current value minus cost
        elif won and balance == 0:
            pnl = total_tokens - cost               # redeemed: tokens acquired minus cost
        else:
            pnl = -cost                             # lost: full loss

        rows.append({
            "cid":         cid,
            "outcome":     outcome,
            "balance":     balance_usdc,
            "balance_raw": balance,
            "cost":        cost,
            "pnl":         pnl,
            "won":         won,
            "status":      status,
        })

    # Sort: redeemable first, then active, then lost, then redeemed
    _order = {"redeemable ✓": 0, "active": 1, "lost": 2, "redeemed": 3}
    rows.sort(key=lambda r: _order.get(r["status"], 9))

    # Only show positions with a non-zero balance (skip fully-settled zero-balance)
    visible = [r for r in rows if r["balance_raw"] > 0]

    def _pnl_str(pnl: float | None) -> str:
        if pnl is None:
            return "—"
        return f"{pnl:+.4f}"

    if not visible:
        print("no open positions")
    else:
        cid_w = 14
        print(f"  {'cid':<{cid_w}}  {'outcome':<7}  {'balance':>9}  {'cost':>8}  {'pnl':>9}  status")
        print("  " + "─" * (cid_w + 52))
        for r in visible:
            cid_short = r["cid"][:cid_w - 1] + "…"
            print(f"  {cid_short:<{cid_w}}  {r['outcome']:<7}  {r['balance']:>9.4f}"
                  f"  {r['cost']:>8.4f}  {_pnl_str(r['pnl']):>9}  {r['status']}")

    # Summary counts
    n_redeemable = sum(1 for r in rows if r["status"] == "redeemable ✓")
    n_active     = sum(1 for r in rows if r["status"] == "active")
    n_lost       = sum(1 for r in rows if r["status"] == "lost")
    n_redeemed   = sum(1 for r in rows if r["status"] == "redeemed")
    n_resolved   = n_redeemable + n_lost + n_redeemed

    realized_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    winrate_str  = (
        f"{n_redeemable + n_redeemed}/{n_resolved} "
        f"({(n_redeemable + n_redeemed) / n_resolved * 100:.0f}%)"
        if n_resolved else "n/a"
    )

    print()
    print(f"  {len(rows)} position(s)  "
          f"({n_redeemable} redeemable  {n_active} active  {n_lost} lost  {n_redeemed} redeemed)")
    print(f"  winrate: {winrate_str}  |  realized PnL: {realized_pnl:+.4f} USDC")
    print(f"  USDC.e balance: {usdc_e_balance(wallet):.4f}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket wallet — list CTF positions")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "WARNING"), metavar="LEVEL")
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_wallet_mode()
