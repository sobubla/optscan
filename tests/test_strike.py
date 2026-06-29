"""
Tests for backend/strike_selector.py.

All tests use a synthetic enriched chain — no HTTP calls, no DB, no mocking required.
The chain format mirrors get_option_greeks() output (see openalgo_client._parse_greeks_row).

Expected to FAIL before backend/strike_selector.py exists.
"""

import math
from datetime import date

import pytest

from backend.strike_selector import (
    EntrySuggestion,
    _dir_to_option_type,
    compute_lots,
    evaluate,
    iv_percentile,
    next_weekly_expiry,
    select_strike,
)

# ──────────────────────────── synthetic chain ─────────────────────────────────

NEAR = "25-Jan-2024"   # DTE = 1  (below min_dte=2 → triggers expiry roll)
FAR  = "01-Feb-2024"   # DTE = 8  (used for all strike-selection tests)

# fmt: off
_FAR_ROWS = [
    # strike 44000 — ITM (delta > 0.50, outside band upper)
    {"strike": 44000, "expiry": FAR,
     "call_ltp": 600.0, "call_oi": 70_000, "call_iv": 0.20,
     "call_delta":  0.68, "call_gamma": 0.0003, "call_theta": -50.0, "call_vega": 22.0,
     "put_ltp":  55.0, "put_oi": 55_000, "put_iv": 0.22,
     "put_delta": -0.32, "put_gamma": 0.0003, "put_theta": -48.0, "put_vega": 20.0},

    # strike 44500 — in band; call_delta=0.48, put_delta=-0.49
    {"strike": 44500, "expiry": FAR,
     "call_ltp": 320.0, "call_oi": 95_000, "call_iv": 0.185,
     "call_delta":  0.48, "call_gamma": 0.0005, "call_theta": -60.0, "call_vega": 26.0,
     "put_ltp": 280.0, "put_oi": 88_000, "put_iv": 0.188,
     "put_delta": -0.49, "put_gamma": 0.0005, "put_theta": -58.0, "put_vega": 25.0},

    # strike 45000 — in band; call_delta=0.40 → nearest midpoint 0.425
    {"strike": 45000, "expiry": FAR,
     "call_ltp": 120.0, "call_oi": 75_000, "call_iv": 0.185,
     "call_delta":  0.40, "call_gamma": 0.0005, "call_theta": -55.0, "call_vega": 24.0,
     "put_ltp": 600.0, "put_oi": 65_000, "put_iv": 0.19,
     "put_delta": -0.62, "put_gamma": 0.0004, "put_theta": -45.0, "put_vega": 19.0},

    # strike 45500 — OTM + low OI (below band)
    {"strike": 45500, "expiry": FAR,
     "call_ltp":  25.0, "call_oi":  8_000, "call_iv": 0.20,
     "call_delta":  0.15, "call_gamma": 0.0002, "call_theta": -30.0, "call_vega": 10.0,
     "put_ltp": 900.0, "put_oi":  7_500, "put_iv": 0.22,
     "put_delta": -0.85, "put_gamma": 0.0002, "put_theta": -28.0, "put_vega":  9.0},
]

_NEAR_ROW = {
    "strike": 44500, "expiry": NEAR,
    "call_ltp": 200.0, "call_oi": 90_000, "call_iv": 0.185,
    "call_delta":  0.50, "call_gamma": 0.0006, "call_theta": -120.0, "call_vega": 28.0,
    "put_ltp": 190.0, "put_oi": 85_000, "put_iv": 0.188,
    "put_delta": -0.50, "put_gamma": 0.0006, "put_theta": -115.0, "put_vega": 27.0,
}
# fmt: on

FAR_CHAIN = _FAR_ROWS
FULL_CHAIN = [_NEAR_ROW] + _FAR_ROWS   # both expiries

# Minimal settings stub — only the fields evaluate() reads
class _Cfg:
    ENTRY_DELTA_BAND_MIN       = 0.35
    ENTRY_DELTA_BAND_MAX       = 0.50
    ENTRY_MIN_OI               = 50_000
    ENTRY_MAX_SPREAD_PCT       = 5.0
    ENTRY_IV_PERCENTILE_REJECT = 70
    ENTRY_RISK_PCT             = 0.02
    ENTRY_STOP_PCT             = 0.30
    ENTRY_TARGET_PCT           = 0.50
    ENTRY_EOD_SQUAREOFF        = "15:15"
    EQUITY_RUPEES              = 200_000
    ENTRY_MIN_DTE              = 2
    INDICES = {"BANKNIFTY": {"lot_size": 30}}

CFG = _Cfg()


# ──────────────────────── 1. direction → option type ──────────────────────────

def test_dir_long_maps_to_ce():
    assert _dir_to_option_type("long") == "CE"


def test_dir_short_maps_to_pe():
    assert _dir_to_option_type("short") == "PE"


# ──────────────────────── 2. expiry roll ──────────────────────────────────────

def test_expiry_rolls_when_dte_below_min():
    # 2024-01-25 is a Thursday (DTE=0 to itself, so min_dte=2 forces next Thursday)
    today = date(2024, 1, 25)
    expiry = next_weekly_expiry(today, min_dte=2)
    # next eligible Thursday is 2024-02-01
    assert expiry == "01-Feb-2024"


# ──────────────────────── 3. strike selection ─────────────────────────────────

def test_select_strike_long_picks_nearest_delta():
    """For long, picks CE with abs(delta) nearest band midpoint 0.425."""
    row = select_strike(
        FAR_CHAIN, FAR, "long",
        delta_band=(0.35, 0.50),
        min_oi=50_000,
        max_spread_pct=5.0,
    )
    assert row is not None
    # 45000 CE delta=0.40 → |0.40-0.425|=0.025 beats 44500 CE delta=0.48 → 0.055
    assert row["strike"] == 45000


def test_select_strike_short_picks_nearest_delta():
    """For short, picks PE with abs(delta) nearest midpoint; only 44500 PE in band."""
    row = select_strike(
        FAR_CHAIN, FAR, "short",
        delta_band=(0.35, 0.50),
        min_oi=50_000,
        max_spread_pct=5.0,
    )
    assert row is not None
    assert row["strike"] == 44500


# ──────────────────────── 4. liquidity filters ────────────────────────────────

def test_liquidity_low_oi_rejected():
    """All in-band CEs have oi < min_oi → None."""
    chain = [
        {**row, "call_oi": 1_000, "put_oi": 1_000}
        for row in FAR_CHAIN
    ]
    assert select_strike(chain, FAR, "long", min_oi=50_000) is None


def test_liquidity_wide_spread_rejected():
    """Winning CE row has a spread_pct field above threshold → None."""
    chain = [
        {**row, "call_spread_pct": 8.0}
        if row["strike"] == 45000 else row
        for row in FAR_CHAIN
    ]
    # 45000 CE would normally win on delta, but its spread_pct=8.0 > 5.0
    # 44500 CE also has no spread_pct field → spread filter skipped for it
    row = select_strike(chain, FAR, "long", max_spread_pct=5.0)
    # 45000 is rejected; fallback is 44500 (next nearest delta)
    assert row is not None
    assert row["strike"] == 44500


# ──────────────────────── 5. IV-rich check ────────────────────────────────────

def test_iv_rich_above_threshold_returns_none():
    """
    Selected strike has call_iv=0.185. History = [0.10]*30.
    Percentile of 0.185 vs 30 observations of 0.10 is 100 → above threshold=70 → None.
    """
    history = [0.10] * 30
    result = evaluate(
        sym="BANKNIFTY",
        direction="long",
        regime="trending",
        spot=44_500.0,
        atr=150.0,
        chain=FAR_CHAIN,
        iv_history=history,
        today=date(2024, 1, 30),   # FAR expiry DTE=2 ≥ min_dte=2
        mode="intraday",
        config=CFG,
    )
    assert result is None


# ──────────────────────── 6. sizing ───────────────────────────────────────────

def test_lots_from_premium_at_risk():
    """lots = max(1, floor(equity * risk_pct / (premium * lot_size)))"""
    # 200_000 * 0.02 = 4_000; 120 * 30 = 3_600 → floor(4000/3600)=1
    assert compute_lots(120.0, 30, 200_000.0, 0.02) == 1
    # 500_000 * 0.02 = 10_000; 20 * 75 = 1_500 → floor(10000/1500)=6
    assert compute_lots(20.0, 75, 500_000.0, 0.02) == 6
    # minimum is 1 even when premium is enormous
    assert compute_lots(50_000.0, 75, 10_000.0, 0.02) == 1


# ──────────────────── 7. lotsize from chain row ───────────────────────────────

def test_evaluate_uses_lotsize_from_chain_row():
    """
    When chain rows carry lotsize=50, evaluate() uses that for sizing,
    ignoring _Cfg.INDICES["BANKNIFTY"]["lot_size"] = 30.

    equity=200_000, risk_pct=0.02 → risk_capital=4_000
    strike 44500, call_ltp=320.0 (nearest in-band delta=0.48)
    cost_per_lot = 320 * 50 = 16_000 → lots = floor(4_000/16_000) = 0 → min 1
    If lot_size=30 instead: cost = 320*30=9_600 → lots = floor(4_000/9_600) = 0 → min 1

    Both round down to 1 here, so we instead test a premium where the two lot sizes
    produce *different* lots.  Use a small premium (20.0) on a high-lotsize row:
    cost_per_lot=20*50=1_000 → lots=4  (50-row wins)
    cost_per_lot=20*30=600  → lots=6  (30-row wins)

    We inject lotsize=50 into the chain rows and assert lots==4 (not 6).
    """
    # Build a chain identical to FAR_CHAIN but with lotsize=50 on every row.
    chain_with_lotsize = [
        {**row, "lotsize": 50, "call_ltp": 20.0, "put_ltp": 20.0}
        for row in FAR_CHAIN
    ]

    class _CfgLotsize(_Cfg):
        EQUITY_RUPEES = 200_000
        ENTRY_RISK_PCT = 0.02

    # iv_history values must be above 0.185 so the current IV percentile is low (<70)
    # and the IV-rich check doesn't reject the candidate.
    result = evaluate(
        sym="BANKNIFTY",
        direction="long",
        regime="trending",
        spot=44_500.0,
        atr=150.0,
        chain=chain_with_lotsize,
        iv_history=[0.25] * 30,
        today=date(2024, 1, 30),
        mode="intraday",
        config=_CfgLotsize(),
    )
    # With lotsize=50: cost_per_lot=20*50=1000 → lots=floor(4000/1000)=4
    assert result is not None
    assert result.lots == 4
