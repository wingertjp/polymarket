"""
Polymarket BTC Up/Down — wallet mode (list on-chain CTF positions).

Usage:
  python wallet.py [--log-level LEVEL]
"""

import argparse
import os
import sys

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


def _position_status(cid: str, outcome: str, balance: int, denom: int) -> str:
    """Determine the status string for a position."""
    if denom == 0:
        return "active"

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
        return "redeemable ✓"
    elif balance > 0:
        return "lost"
    elif won:
        return "redeemed"
    else:
        return "lost"


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

    # Deduplicate by (cid, asset_id)
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

        status = _position_status(cid, outcome, balance, denom)
        rows.append({
            "cid":         cid,
            "outcome":     outcome,
            "balance":     balance / 1e6,
            "balance_raw": balance,
            "status":      status,
        })

    # Sort: redeemable first, then active, then lost, then redeemed
    _order = {"redeemable ✓": 0, "active": 1, "lost": 2, "redeemed": 3}
    rows.sort(key=lambda r: _order.get(r["status"], 9))

    # Only show positions with a non-zero balance (skip fully-settled zero-balance)
    visible = [r for r in rows if r["balance_raw"] > 0]

    if not visible:
        print("no open positions")
    else:
        cid_w = 14
        print(f"  {'cid':<{cid_w}}  {'outcome':<7}  {'balance':>9}  status")
        print("  " + "─" * (cid_w + 32))
        for r in visible:
            cid_short = r["cid"][:cid_w - 1] + "…"
            print(f"  {cid_short:<{cid_w}}  {r['outcome']:<7}  {r['balance']:>9.4f}  {r['status']}")

    # Summary counts
    n_redeemable = sum(1 for r in rows if r["status"] == "redeemable ✓")
    n_active     = sum(1 for r in rows if r["status"] == "active")
    n_lost       = sum(1 for r in rows if r["status"] == "lost")
    n_redeemed   = sum(1 for r in rows if r["status"] == "redeemed")

    print()
    print(f"  {len(rows)} position(s)  "
          f"({n_redeemable} redeemable  {n_active} active  {n_lost} lost  {n_redeemed} redeemed)")
    print(f"  USDC.e balance: {usdc_e_balance(wallet):.4f}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket wallet — list CTF positions")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "WARNING"), metavar="LEVEL")
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_wallet_mode()
