"""Tests for BinancePriceSignal — pure signal logic (no network)."""

import time
import pytest
from binance_signal import BinancePriceSignal


# ── OBI (Order Book Imbalance) ──────────────────────────────────────────────

def test_obi_is_zero_when_bid_ask_volumes_equal():
    signal = BinancePriceSignal()
    signal._on_depth({"bids": [["50000", "1.0"], ["49900", "1.0"]],
                      "asks": [["50100", "1.0"], ["50200", "1.0"]]})
    assert signal.obi == pytest.approx(0.0)


def test_obi_positive_when_bid_volume_dominates():
    signal = BinancePriceSignal()
    signal._on_depth({"bids": [["50000", "3.0"]],
                      "asks": [["50100", "1.0"]]})
    assert signal.obi == pytest.approx(0.5)   # (3-1)/(3+1)


def test_obi_negative_when_ask_volume_dominates():
    signal = BinancePriceSignal()
    signal._on_depth({"bids": [["50000", "1.0"]],
                      "asks": [["50100", "3.0"]]})
    assert signal.obi == pytest.approx(-0.5)  # (1-3)/(1+3)


def test_obi_is_zero_when_book_empty():
    signal = BinancePriceSignal()
    signal._on_depth({"bids": [], "asks": []})
    assert signal.obi == 0.0


# ── Direction ───────────────────────────────────────────────────────────────

def test_direction_up_when_obi_exceeds_threshold():
    signal = BinancePriceSignal(obi_threshold=0.30)
    signal._on_depth({"bids": [["50000", "4.0"]],
                      "asks": [["50100", "1.0"]]})   # obi = 0.6
    assert signal.direction == "UP"


def test_direction_down_when_obi_below_negative_threshold():
    signal = BinancePriceSignal(obi_threshold=0.30)
    signal._on_depth({"bids": [["50000", "1.0"]],
                      "asks": [["50100", "4.0"]]})   # obi = -0.6
    assert signal.direction == "DOWN"


def test_direction_none_when_obi_within_threshold():
    signal = BinancePriceSignal(obi_threshold=0.30)
    signal._on_depth({"bids": [["50000", "1.1"]],
                      "asks": [["50100", "1.0"]]})   # obi ≈ 0.047
    assert signal.direction is None


# ── Kalman filter ───────────────────────────────────────────────────────────

def test_price_initialises_to_first_trade():
    signal = BinancePriceSignal()
    signal._on_trade({"p": "50000.0", "q": "0.1", "m": False, "T": int(time.time() * 1000)})
    assert signal.price == pytest.approx(50000.0)


def test_price_converges_toward_series_of_identical_trades():
    signal = BinancePriceSignal()
    t0 = int(time.time() * 1000)
    for i in range(50):
        signal._on_trade({"p": "50000.0", "q": "0.1", "m": False, "T": t0 + i * 100})
    assert signal.price == pytest.approx(50000.0, rel=1e-3)


def test_kalman_smooths_out_price_spike():
    signal = BinancePriceSignal()
    t0 = int(time.time() * 1000)
    # Establish baseline at 50000
    for i in range(20):
        signal._on_trade({"p": "50000.0", "q": "0.1", "m": False, "T": t0 + i * 100})
    # Single spike
    signal._on_trade({"p": "51000.0", "q": "0.1", "m": False, "T": t0 + 2100})
    # Filtered price should move less than the raw spike
    assert signal.price < 51000.0
    assert signal.price > 50000.0


# ── Velocity ────────────────────────────────────────────────────────────────

def test_velocity_positive_when_price_rising():
    signal = BinancePriceSignal()
    t0 = int(time.time() * 1000)
    signal._on_trade({"p": "50000.0", "q": "0.1", "m": False, "T": t0})
    signal._on_trade({"p": "50100.0", "q": "0.1", "m": False, "T": t0 + 1000})
    assert signal.velocity > 0


def test_velocity_negative_when_price_falling():
    signal = BinancePriceSignal()
    t0 = int(time.time() * 1000)
    signal._on_trade({"p": "50100.0", "q": "0.1", "m": False, "T": t0})
    signal._on_trade({"p": "50000.0", "q": "0.1", "m": False, "T": t0 + 1000})
    assert signal.velocity < 0
