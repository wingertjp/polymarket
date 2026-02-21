"""Tests for snipe rescue logic — pure functions only."""

from snipe import should_rescue, _bet_result


def test_rescue_fires_when_direction_opposes_up_bet():
    assert should_rescue("Up", "DOWN", remaining=10.0, rescue_time=15.0) is True


def test_rescue_fires_when_direction_opposes_down_bet():
    assert should_rescue("Down", "UP", remaining=10.0, rescue_time=15.0) is True


def test_no_rescue_when_direction_confirms_initial_bet():
    assert should_rescue("Up", "UP", remaining=10.0, rescue_time=15.0) is False


def test_no_rescue_when_direction_is_none():
    assert should_rescue("Up", None, remaining=10.0, rescue_time=15.0) is False


def test_no_rescue_when_too_much_time_remaining():
    assert should_rescue("Up", "DOWN", remaining=20.0, rescue_time=15.0) is False


def test_rescue_fires_exactly_at_time_boundary():
    assert should_rescue("Up", "DOWN", remaining=15.0, rescue_time=15.0) is True


# ── Bet result ───────────────────────────────────────────────────────────────

def test_bet_result_up_wins_when_price_rises():
    assert _bet_result("Up", open_price=50000.0, close_price=50100.0) == "WIN"


def test_bet_result_up_loses_when_price_falls():
    assert _bet_result("Up", open_price=50000.0, close_price=49900.0) == "LOSS"


def test_bet_result_down_wins_when_price_falls():
    assert _bet_result("Down", open_price=50000.0, close_price=49900.0) == "WIN"


def test_bet_result_down_loses_when_price_rises():
    assert _bet_result("Down", open_price=50000.0, close_price=50100.0) == "LOSS"
