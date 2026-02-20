# polymarket

A bot for Polymarket's BTC Up/Down 5-minute binary markets on Polygon.

## Modes

**Live order book viewer** (no auth required):
```bash
python main.py data
```
Streams real-time bids and asks for both Up and Down tokens side-by-side in the terminal. Automatically rolls over to the next 5-minute window.

**Sniper** (requires wallet):
```bash
python main.py snipe
```
Monitors each 5-minute market via WebSocket. When the midpoint of either the Up or Down token reaches 0.95 and less than 2 minutes remain, fires a FOK market buy for 1 USDC.

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

`PRIVATE_KEY` is only needed for `snipe` mode.

## Configuration

Edit constants at the top of `main.py`:

| Constant | Default | Description |
|---|---|---|
| `SNIPE_AMOUNT` | `1.0` | USDC per trade |
| `SNIPE_PROB` | `0.95` | Midpoint threshold to trigger buy |
| `SNIPE_TIME` | `120` | Only trigger if fewer than this many seconds remain |
