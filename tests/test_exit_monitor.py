"""
Tests for backend/exit_monitor.py.

Each test isolates exactly one trigger by ensuring all others stay in safe territory
via the default fixtures. No DB, no HTTP, no real clock.

Expected to FAIL before backend/exit_monitor.py exists.
"""

from datetime import datetime, timedelta

import pytest

from backend.exit_monitor import OpenPosition, MarketState, ExitSignal, check_exit

# ─────────────────────────── fixtures ────────────────────────────────────────

ENTRY_TIME = datetime(2024, 1, 30, 10, 0, 0)   # 10:00 IST, non-expiry day


def _pos(**overrides) -> OpenPosition:
    """Base open position — all triggers in safe zone by default."""
    base = dict(
        position_id="BNFTEST001",
        sym="BANKNIFTY",
        expiry="01-Feb-2024",     # Feb 1 is the expiry; test date is Jan 30, not expiry day
        strike=45000,
        option_type="CE",
        direction="long",
        entry_premium=120.0,
        stop_premium=84.0,        # entry * 0.70
        target_premium=180.0,     # entry * 1.50
        entry_iv=0.185,
        entry_regime="trending",
        entry_time=ENTRY_TIME,
        time_stop="15:15",
        mode="intraday",
        peak_premium=120.0,       # no move yet
    )
    base.update(overrides)
    return OpenPosition(**base)


def _state(**overrides) -> MarketState:
    """Base market state — no trigger fires."""
    base = dict(
        current_premium=125.0,    # modestly up; well away from stop (84) and target (180)
        current_iv=0.185,         # same as entry; no IV change
        current_regime="trending",
        now=datetime(2024, 1, 30, 10, 15, 0),   # 15 min in; < MAX_HOLD_MINUTES=45
    )
    base.update(overrides)
    return MarketState(**base)


class _Cfg:
    EXIT_TRAIL_PCT           = 0.20
    EXIT_IV_CRUSH_DELTA      = 0.05
    EXIT_HOLD_MINUTES_EXPIRY = 30
    MAX_HOLD_MINUTES         = 45
    ENTRY_EOD_SQUAREOFF      = "15:15"


CFG = _Cfg()


# ─────────────────────────── trigger tests ───────────────────────────────────

def test_premium_stop_fires():
    """current_premium drops below stop_premium → premium_stop fires."""
    signal = check_exit(_pos(), _state(current_premium=83.0), CFG)
    assert signal is not None
    assert signal.trigger == "premium_stop"
    assert signal.pnl_pct < 0


def test_premium_target_fires():
    """current_premium reaches or exceeds target_premium → premium_target fires."""
    signal = check_exit(_pos(), _state(current_premium=181.0), CFG)
    assert signal is not None
    assert signal.trigger == "premium_target"
    assert signal.pnl_pct > 0


def test_premium_trail_fires():
    """
    peak_premium already exceeded target; current drops > EXIT_TRAIL_PCT from peak.
    peak=200, trail_level = 200 * (1 - 0.20) = 160.0; current=155 → fires.
    """
    pos = _pos(peak_premium=200.0)
    # Ensure higher-priority triggers do NOT fire at current=155:
    #   stop=84 → safe; target=180 → 155 < 180, so target not fired; held=15 min → safe
    signal = check_exit(pos, _state(current_premium=155.0), CFG)
    assert signal is not None
    assert signal.trigger == "premium_trail"


def test_time_stop_fires():
    """Held for > MAX_HOLD_MINUTES → time_stop fires."""
    pos = _pos()
    now = ENTRY_TIME + timedelta(minutes=46)
    # Ensure premium is safe: 125 well between 84 (stop) and 180 (target)
    signal = check_exit(pos, _state(now=now), CFG)
    assert signal is not None
    assert signal.trigger == "time_stop"


def test_iv_crush_fires():
    """IV drops by more than EXIT_IV_CRUSH_DELTA → iv_crush fires."""
    # entry_iv=0.185, current_iv=0.120 → drop=0.065 > 0.05
    signal = check_exit(_pos(), _state(current_iv=0.120), CFG)
    assert signal is not None
    assert signal.trigger == "iv_crush"


def test_regime_flip_fires():
    """current_regime differs from entry_regime → regime_flip fires."""
    # All other triggers in safe zone: premium 125, IV same, 15 min in, not yet 15:15
    signal = check_exit(_pos(), _state(current_regime="ranging"), CFG)
    assert signal is not None
    assert signal.trigger == "regime_flip"


def test_eod_squareoff_fires():
    """Past EOD time with mode=intraday → eod_squareoff fires."""
    # Entry at 15:00 so held=16 min < MAX_HOLD_MINUTES=45; only EOD fires at 15:16.
    pos = _pos(mode="intraday", entry_time=datetime(2024, 1, 30, 15, 0, 0))
    now = datetime(2024, 1, 30, 15, 16, 0)
    signal = check_exit(pos, _state(now=now), CFG)
    assert signal is not None
    assert signal.trigger == "eod_squareoff"


def test_eod_no_fire_positional():
    """EOD trigger suppressed for positional mode, even past 15:15."""
    # Entry at 15:00 so held=16 min < MAX_HOLD_MINUTES=45; EOD suppressed for positional.
    pos = _pos(mode="positional", entry_time=datetime(2024, 1, 30, 15, 0, 0))
    now = datetime(2024, 1, 30, 15, 16, 0)
    signal = check_exit(pos, _state(now=now), CFG)
    # No trigger fires: premium=125 safe, IV same, regime same, 16 min held, EOD suppressed.
    assert signal is None


def test_hold_case_no_trigger():
    """All default safe values → no trigger fires."""
    signal = check_exit(_pos(), _state(), CFG)
    assert signal is None
