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
Full signal and snipe logic runs — Polymarket order book + Binance microstructure signal — but orders are simulated. Useful for validating strategy before risking real funds.

**Sniper — production** (requires wallet):
```bash
python main.py snipe
```
Monitors each 5-minute market via WebSocket. When the midpoint of either the Up or Down token reaches 0.95 and less than 2 minutes remain, fires a FOK market buy for 1 USDC.

Additionally monitors the Binance BTC/USDT order book for rescue signals (Strategy B): if the Binance order book imbalance strongly contradicts the open bet in the last 15 seconds, fires a FOK buy on the opposite token.

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

All constants are in `common.py` and can be overridden via `.env` or environment variables:

| Variable | Default | Description |
|---|---|---|
| `SNIPE_AMOUNT` | `1.0` | USDC per trade |
| `SNIPE_PROB` | `0.95` | Midpoint threshold to trigger buy |
| `SNIPE_TIME` | `120` | Only trigger if fewer than this many seconds remain |
| `RESCUE_TIME` | `15` | Rescue window: last N seconds before market close |
| `DRY_RUN` | `false` | Set to `true` to simulate orders without PRIVATE_KEY |
