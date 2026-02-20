# polymarket

A bot for Polymarket's BTC Up/Down 5-minute binary markets on Polygon.

## Modes

**Live order book viewer** (no auth required):
```bash
python main.py data
```
Streams real-time bids and asks for both Up and Down tokens side-by-side in the terminal. Automatically rolls over to the next 5-minute window.

**Market maker** (requires wallet):
```bash
python main.py mm
```
Posts bid/ask orders around the midpoint, refreshes every 10 seconds, and cancels stale orders on each cycle.

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

`PRIVATE_KEY` is only needed for `mm` mode.

## Configuration

Edit constants at the top of `main.py`:

| Constant | Default | Description |
|---|---|---|
| `MM_SPREAD` | `0.04` | Total spread (2% each side of mid) |
| `MM_SIZE` | `5.0` | Order size in USDC |
| `MM_REFRESH` | `10` | Seconds between order refresh cycles |
