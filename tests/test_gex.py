"""
GEX analytics tests.

Covers per-strike GEX sign, net-GEX aggregation, gamma-flip detection,
and the GexRegimeProvider regime mapping.

All tests use synthetic chain data so no Fyers connection is needed.
Expected to FAIL before backend/gex.py and GexRegimeProvider exist.
"""

import pytest

from backend.gex import strike_gex, net_gex, gamma_flip
from backend.gate import GexRegimeProvider
from backend.models import OptScanPayload


# ─────────────────────────── helpers ────────────────────────────────────────


def _dummy_payload(**overrides) -> OptScanPayload:
    """Minimal valid payload. GexRegimeProvider ignores all fields in it."""
    base = dict(
        secret="test", v="optscan-v13", sym="BANKNIFTY", tf="9", dir="long",
        bar_time=1_700_000_000_000, price=44_500.0, atr=100.0, adx=30.0, filters=10,
        f_ema=True, f_rsi=True, f_vol=True, f_vwap=True, f_mvwap=True,
        f_band=True, f_cvd=True, f_st=True, f_macd=True, f_poc=True,
        f_mss=True, f_adx=False,
        z=0.5, z_long_zone=True, z_short_zone=False, z_bull_pa=True, z_bear_pa=False,
        fvg_ok=True, pb_ok=True, vol_ok=True, range_ratio=1.2, bars_since=20,
        hh=True, ll=False, ext_long=False, ext_short=False, mss_state=0,
    )
    base.update(overrides)
    return OptScanPayload(**base)


def _row(strike, opt_type, oi, gamma):
    return {"strike": strike, "type": opt_type, "oi": oi, "gamma": gamma}


# ──────────────────── per-strike GEX sign ───────────────────────────────────


def test_call_only_strike_is_positive():
    """Calls-only strike → positive GEX (dealers long call gamma = stabilizing)."""
    gex = strike_gex(call_oi=1000, put_oi=0, call_gamma=0.001, put_gamma=0.0,
                     spot=100.0, lot_size=1.0)
    assert gex > 0


def test_put_only_strike_is_negative():
    """Puts-only strike → negative GEX (dealers short put gamma = destabilizing)."""
    gex = strike_gex(call_oi=0, put_oi=1000, call_gamma=0.0, put_gamma=0.001,
                     spot=100.0, lot_size=1.0)
    assert gex < 0


# ──────────────────── net-GEX aggregation ───────────────────────────────────


def test_net_gex_sums_all_strikes():
    """
    Two-strike chain:
      Strike 100: call_OI=100, call_γ=0.0001, put=none → +100
      Strike 110: call=none, put_OI=300, put_γ=0.0001 → -300
    With spot=100, lot_size=1: GEX = oi × γ × spot² = oi × γ × 10000
      Strike 100: +100 × 0.0001 × 10000 = +100
      Strike 110: -300 × 0.0001 × 10000 = -300
      Net = -200
    """
    chain = [
        _row(100, "CE", 100,  0.0001),
        _row(100, "PE",   0,  0.0),
        _row(110, "CE",   0,  0.0),
        _row(110, "PE", 300,  0.0001),
    ]
    result = net_gex(chain, spot=100.0, lot_size=1.0)
    assert abs(result - (-200.0)) < 1e-6


def test_net_gex_empty_chain_is_zero():
    """Empty chain → net GEX = 0."""
    assert net_gex([], spot=44_500.0, lot_size=30.0) == 0.0


# ──────────────────── gamma-flip detection ──────────────────────────────────


def test_gamma_flip_known_crossing():
    """
    Three-strike chain (spot=100, lot_size=1, spot²=10000):
      Strike 44000: CE_OI=100, CE_γ=0.0001 → GEX = +100
      Strike 44500: CE_OI=50,  CE_γ=0.0001 → GEX = +50
      Strike 45000: PE_OI=200, PE_γ=0.0001 → GEX = -200
    Cumulative (ascending): +100 → +150 → -50
    Crossing between 44500 and 45000:
      flip = 44500 + (0 − 150) / (−50 − 150) × (45000 − 44500)
           = 44500 + 150/200 × 500 = 44500 + 375 = 44875
    """
    chain = [
        _row(44000, "CE", 100, 0.0001),
        _row(44000, "PE",   0, 0.0),
        _row(44500, "CE",  50, 0.0001),
        _row(44500, "PE",   0, 0.0),
        _row(45000, "CE",   0, 0.0),
        _row(45000, "PE", 200, 0.0001),
    ]
    flip = gamma_flip(chain, spot=100.0, lot_size=1.0)
    assert flip is not None
    assert abs(flip - 44875.0) < 1e-6


def test_gamma_flip_none_when_all_positive():
    """No zero-crossing when every strike has positive GEX."""
    chain = [
        _row(100, "CE", 500, 0.001),
        _row(100, "PE",   0, 0.0),
        _row(110, "CE", 300, 0.001),
        _row(110, "PE",   0, 0.0),
    ]
    assert gamma_flip(chain, spot=100.0, lot_size=1.0) is None


# ──────────────────── GexRegimeProvider regime mapping ──────────────────────


def test_gex_provider_default_is_ranging():
    """Before any chain update, default net_gex=0 → conservative 'ranging'."""
    provider = GexRegimeProvider()
    assert provider.get_regime(_dummy_payload()) == "ranging"


def test_gex_provider_positive_net_gex_is_ranging():
    """Positive net GEX → dealers suppress moves → ranging."""
    provider = GexRegimeProvider()
    chain = [
        _row(100, "CE", 1000, 0.001),
        _row(100, "PE",    0, 0.0),
    ]
    provider.update_chain(chain, spot=100.0, lot_size=1.0)
    assert provider.get_regime(_dummy_payload()) == "ranging"


def test_gex_provider_negative_net_gex_is_trending():
    """Negative net GEX → dealers amplify moves → trending."""
    provider = GexRegimeProvider()
    chain = [
        _row(100, "CE",    0, 0.0),
        _row(100, "PE", 1000, 0.001),
    ]
    provider.update_chain(chain, spot=100.0, lot_size=1.0)
    assert provider.get_regime(_dummy_payload()) == "trending"


def test_gex_provider_regime_updates_when_chain_changes():
    """Flipping from call-heavy to put-heavy chain flips the regime."""
    provider = GexRegimeProvider()
    payload = _dummy_payload()

    provider.update_chain(
        [_row(100, "CE", 1000, 0.001), _row(100, "PE", 0, 0.0)],
        spot=100.0
    )
    assert provider.get_regime(payload) == "ranging"

    provider.update_chain(
        [_row(100, "CE", 0, 0.0), _row(100, "PE", 1000, 0.001)],
        spot=100.0
    )
    assert provider.get_regime(payload) == "trending"
