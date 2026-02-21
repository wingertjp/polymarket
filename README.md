# polymarket

A bot for Polymarket's BTC Up/Down 5-minute binary markets on Polygon.

## Modes

**Live order book viewer** (no auth required):
```bash
python main.py data
```
Streams real-time bids and asks for both Up and Down tokens side-by-side in the terminal. Automatically rolls over to the next 5-minute window.

**Sniper — dry run** (no auth required, no orders placed):
```bash
python main.py snipe --dry-run
```
Full signal and snipe logic runs — Polymarket order book + Binance microstructure signal — but orders are simulated. Reports WIN/LOSS outcome at window close based on BTC candle open vs close.

**Sniper — production** (requires wallet):
```bash
python main.py snipe
```
Monitors each 5-minute market via WebSocket. When the midpoint of either the Up or Down token reaches `SNIPE_PROB` and fewer than `SNIPE_TIME` seconds remain, fires a FOK market buy for `SNIPE_AMOUNT` USDC.

After a snipe, monitors the Polymarket mid of the initial bet token. If it drops below `RESCUE_MID_THRESHOLD` (meaning the market has reversed), fires a FOK buy on the opposite token for `SNIPE_RESCUE_AMOUNT` USDC.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:
```
PRIVATE_KEY=<your Polygon wallet private key>
```

`PRIVATE_KEY` is only needed for `snipe` mode (production). Dry run works without it.

## Configuration

All constants live in `common.py` and can be overridden via `.env` or environment variables:

### Sniper

| Variable | Default | Description |
|---|---|---|
| `SNIPE_AMOUNT` | `1.0` | USDC per snipe order |
| `SNIPE_PROB` | `0.95` | Midpoint threshold to trigger a snipe |
| `SNIPE_TIME` | `120` | Only snipe if fewer than this many seconds remain |
| `SNIPE_RESCUE_AMOUNT` | `0.20` | USDC per rescue order |
| `RESCUE_MID_THRESHOLD` | `0.80` | Rescue fires when the initial bet token mid drops below this (opposite token still cheap at ~0.20) |
| `DRY_RUN` | `false` | Simulate orders without placing them (no `PRIVATE_KEY` needed) |

### Market

| Variable | Default | Description |
|---|---|---|
| `MARKET_SLUG` | `btc-updown-5m` | Polymarket market slug prefix |
| `HOST` | `https://clob.polymarket.com` | Polymarket CLOB REST endpoint |
| `GAMMA_API` | `https://gamma-api.polymarket.com` | Polymarket Gamma API endpoint |
| `WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Polymarket order book WebSocket |

### On-chain (Polygon)

| Variable | Default | Description |
|---|---|---|
| `RPC_URL` | `https://polygon-bor-rpc.publicnode.com` | Polygon JSON-RPC endpoint |
| `USDC_E` | `0x2791…` | Bridged USDC.e contract address |
| `CTF_ADDR` | `0x4D97…` | Gnosis CTF contract address (for redemptions) |
