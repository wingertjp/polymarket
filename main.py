"""
Polymarket BTC Up/Down 5-minute bot.

  python main.py data                        -- live order book TUI (no auth)
  python main.py snipe [--log-level LEVEL]   -- sniper (requires PRIVATE_KEY in .env)

Or run modes directly:
  python data.py
  python snipe.py [--log-level LEVEL]
"""

import argparse
import os

from common import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down bot")
    parser.add_argument("mode", choices=["data", "snipe"])
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
        from snipe import build_client_l2, run_snipe_mode
        run_snipe_mode(build_client_l2())


if __name__ == "__main__":
    main()
