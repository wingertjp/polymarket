"""
Binance BTC/USDT price signal — microstructure for Polymarket rescue strategy.

Subscribes to two Binance combined WebSocket streams:
  - btcusdt@depth5@100ms  →  Order Book Imbalance (OBI)
  - btcusdt@aggTrade      →  Kalman-filtered price + velocity

Runs as a background asyncio task; reads are safe from any coroutine.
"""

import asyncio
import json

import websockets

_STREAM_URL = (
    "wss://stream.binance.com:9443/stream"
    "?streams=btcusdt@depth5@100ms/btcusdt@aggTrade/btcusdt@kline_5m"
)

# Kalman tuning
_KF_Q = 0.0005   # process noise  — how fast the true price can drift
_KF_R = 0.05     # measurement noise — how noisy individual trades are

OBI_THRESHOLD = 0.30  # abs(obi) must exceed this to emit UP/DOWN


class BinancePriceSignal:
    """
    Real-time BTC/USDT microstructure signal from Binance order book.

    Usage (inside an asyncio context):
        signal = BinancePriceSignal()
        await signal.start()
        ...
        print(signal.obi, signal.direction, signal.price, signal.velocity)
        ...
        await signal.stop()
    """

    def __init__(self, obi_threshold: float = OBI_THRESHOLD) -> None:
        self._obi_threshold = obi_threshold

        # Kalman filter state
        self._kf_x: float = 0.0   # filtered price estimate
        self._kf_p: float = 1.0   # error covariance

        # Exposed state (written only from the asyncio task or test helpers)
        self._obi: float = 0.0
        self._velocity: float = 0.0   # USD / second
        self._last_trade_ts_ms: float = 0.0
        self._candle_open: float = 0.0

        self._task: asyncio.Task | None = None

    # ── Public read-only properties ──────────────────────────────────────────

    @property
    def obi(self) -> float:
        """Order Book Imbalance ∈ [-1, 1].  Positive = buy pressure."""
        return self._obi

    @property
    def price(self) -> float:
        """Kalman-filtered BTC price (USD)."""
        return self._kf_x

    @property
    def velocity(self) -> float:
        """Rate of price change (USD / second). Positive = rising."""
        return self._velocity

    @property
    def candle_open(self) -> float:
        """Open price of the current 5-minute BTC candle (0.0 until first kline received)."""
        return self._candle_open

    @property
    def direction(self) -> str | None:
        """
        'UP'   if OBI >  threshold  (buy pressure dominates)
        'DOWN' if OBI < -threshold  (sell pressure dominates)
        None   if signal too weak
        """
        if self._obi > self._obi_threshold:
            return "UP"
        if self._obi < -self._obi_threshold:
            return "DOWN"
        return None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the background WebSocket listener."""
        self._task = asyncio.create_task(self._run(), name="binance-signal")

    async def stop(self) -> None:
        """Cancel the background task cleanly."""
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── Internal: WebSocket loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                async with websockets.connect(_STREAM_URL) as ws:
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream: str = msg.get("stream", "")
                        data: dict = msg.get("data", {})
                        if "depth5" in stream:
                            self._on_depth(data)
                        elif "aggTrade" in stream:
                            self._on_trade(data)
                        elif "kline" in stream:
                            self._on_kline(data)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(1)   # brief pause before reconnect

    # ── Internal: signal processing ─────────────────────────────────────────

    def _on_depth(self, data: dict) -> None:
        """Recompute OBI from top-N bid/ask levels."""
        bid_vol = sum(float(q) for _, q in data.get("bids", []))
        ask_vol = sum(float(q) for _, q in data.get("asks", []))
        total = bid_vol + ask_vol
        self._obi = (bid_vol - ask_vol) / total if total else 0.0

    def _on_trade(self, data: dict) -> None:
        """Update Kalman filter and velocity on each aggregate trade.

        data keys used:
          p  — price  (str)
          T  — trade time in ms (int)
        """
        price = float(data["p"])
        ts_ms = float(data["T"])

        if self._kf_x == 0.0:          # cold-start: seed the filter
            self._kf_x = price
            self._last_trade_ts_ms = ts_ms
            return

        old_x = self._kf_x
        self._kf_x = self._kalman_update(price)

        dt_s = (ts_ms - self._last_trade_ts_ms) / 1000.0
        self._velocity = (self._kf_x - old_x) / max(dt_s, 0.001)
        self._last_trade_ts_ms = ts_ms

    def _on_kline(self, data: dict) -> None:
        """Track the 5-minute candle open price."""
        k = data.get("k", {})
        self._candle_open = float(k.get("o", 0))

    def _kalman_update(self, measurement: float) -> float:
        self._kf_p += _KF_Q
        k = self._kf_p / (self._kf_p + _KF_R)
        estimate = self._kf_x + k * (measurement - self._kf_x)
        self._kf_p = (1.0 - k) * self._kf_p
        return estimate
