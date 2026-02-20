# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires a `.env` file with:
```
PRIVATE_KEY=<your Polygon wallet private key>
```

## Running

```bash
# Live order book viewer (no auth required)
python main.py data

# Market maker (requires PRIVATE_KEY in .env)
python main.py mm
```

## Architecture

Single-file bot (`main.py`) targeting the Polymarket CLOB (Central Limit Order Book) on Polygon. It operates on 5-minute BTC Up/Down binary markets.

**Two modes:**

- **`data` mode** — No auth. Uses the Gamma API to discover the current 5-minute market window (slug derived from clock: `btc-updown-5m-<unix_ts_rounded_to_300s>`), then opens a WebSocket to `wss://ws-subscriptions-clob.polymarket.com/ws/market` to stream live order book updates. Renders a terminal UI showing bids/asks for both the Up and Down tokens side-by-side. Automatically rolls over to the next window when the countdown hits zero.

- **`mm` mode** — Requires auth. Builds an L2 `ClobClient` using `PRIVATE_KEY`, derives API credentials via `create_or_derive_api_creds()`, then runs a simple market-maker loop: fetch midpoint, post bid/ask `MM_SPREAD` apart with size `MM_SIZE`, sleep `MM_REFRESH` seconds, cancel and repost. Rolls over to new market windows automatically.

**Key constants** (top of file, tune as needed):
- `MM_SPREAD = 0.04` — total spread (2% each side of mid)
- `MM_SIZE = 5.0` — order size in USDC
- `MM_REFRESH = 10` — seconds between order refresh cycles
- `TICK = 0.01` — price tick size

**APIs used:**
- `https://gamma-api.polymarket.com/events` — market discovery by slug
- `https://clob.polymarket.com` — CLOB REST (order placement, midpoint, market info)
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` — real-time order book feed

**WebSocket message types:**
- `event_type == "book"` — full book snapshot for a token
- messages with `"price_changes"` key — incremental book updates (add/remove levels)
