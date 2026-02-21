"""
Polymarket BTC Up/Down 5-minute bot.

  python main.py data                        -- live order book TUI (no auth)
  python main.py snipe [--log-level LEVEL]   -- sniper (requires PRIVATE_KEY in .env)
  python main.py wallet [--log-level LEVEL]  -- list on-chain CTF positions
  python main.py redeem [--log-level LEVEL]  -- auto-redeem resolved positions (polls every 10s)

Or run modes directly:
  python data.py
  python snipe.py [--log-level LEVEL]
  python wallet.py [--log-level LEVEL]
  python redeem.py [--log-level LEVEL]
"""

import argparse
import os

from common import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down bot")
    parser.add_argument("mode", choices=["data", "snipe", "wallet", "redeem"])
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="DEBUG INFO WARNING ERROR CRITICAL (default: INFO, env: LOG_LEVEL)",
    )
    args = parser.parse_args()

    if args.mode == "data":
        from data import run_data_mode
        run_data_mode()

    elif args.mode == "snipe":
        configure_logging(args.log_level)
        from common import build_client_l2
        from snipe import run_snipe_mode
        run_snipe_mode(build_client_l2())

    elif args.mode == "wallet":
        configure_logging(args.log_level)
        from wallet import run_wallet_mode
        run_wallet_mode()

    elif args.mode == "redeem":
        configure_logging(args.log_level)
        from redeem import run_redeem_mode
        run_redeem_mode()


if __name__ == "__main__":
    main()
