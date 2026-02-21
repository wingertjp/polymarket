"""
Polymarket BTC Up/Down — redeem mode (auto-claims resolved CTF positions).

Polls every POLL_INTERVAL seconds and redeems any winning positions found
in the trade history that still have on-chain balance.

Usage:
  python redeem.py [--log-level LEVEL]
"""

import argparse
import os
import time

from common import (
    log, log_redeem,
    configure_logging, build_client_l2,
    redeem_pending_positions,
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds between checks


def run_redeem_mode() -> None:
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise ValueError("PRIVATE_KEY not set in .env")

    client = build_client_l2()
    log.info("redeem mode started  poll_interval=%ds", POLL_INTERVAL)

    while True:
        try:
            redeem_pending_positions(client, private_key)
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:
            log_redeem.error("unexpected error: %s: %s", type(e).__name__, e)

        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("stopped by user")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket — auto-redeem resolved positions")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="DEBUG INFO WARNING ERROR CRITICAL (default: INFO)",
    )
    args = parser.parse_args()
    configure_logging(args.log_level)
    run_redeem_mode()
