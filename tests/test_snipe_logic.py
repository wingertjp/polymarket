"""Tests for snipe rescue logic — pure functions only."""

from snipe import should_rescue, _bet_result


# ── Rescue (Polymarket mid-based) ────────────────────────────────────────────

def test_rescue_fires_when_initial_mid_drops_below_threshold():
    assert should_rescue(initial_mid=0.15, rescue_mid_threshold=0.20) is True


def test_no_rescue_when_initial_mid_stays_above_threshold():
    assert should_rescue(initial_mid=0.60, rescue_mid_threshold=0.20) is False


def test_rescue_fires_exactly_at_mid_threshold():
    assert should_rescue(initial_mid=0.20, rescue_mid_threshold=0.20) is True


def test_no_rescue_when_mid_just_above_threshold():
    assert should_rescue(initial_mid=0.21, rescue_mid_threshold=0.20) is False


# ── Bet result ───────────────────────────────────────────────────────────────

def test_bet_result_up_wins_when_price_rises():
    assert _bet_result("Up", open_price=50000.0, close_price=50100.0) == "WIN"


def test_bet_result_up_loses_when_price_falls():
    assert _bet_result("Up", open_price=50000.0, close_price=49900.0) == "LOSS"


def test_bet_result_down_wins_when_price_falls():
    assert _bet_result("Down", open_price=50000.0, close_price=49900.0) == "WIN"


def test_bet_result_down_loses_when_price_rises():
    assert _bet_result("Down", open_price=50000.0, close_price=50100.0) == "LOSS"
