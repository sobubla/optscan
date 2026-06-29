"""
Tests for backend/scanner.py.

Focuses on _oa_chain_to_flat() — the OpenAlgo→Fyers format adapter — and the
OpenAlgo path through scan_index(). All external calls (Fyers, OpenAlgo) are
mocked so no live clients are needed.
"""

from unittest.mock import MagicMock

import pytest

from backend.scanner import OptionScanner, _oa_chain_to_flat


# ──────────────────────────── fixture data ────────────────────────────────────

_OA_STRIKE_A = {
    "strike": 53000,
    "expiry": "27-Jun-2026",
    "call_ltp": 250.0, "call_bid": 249.0, "call_ask": 251.0,
    "call_oi": 150_000, "call_volume": 5_000,
    "call_iv": 0.18,
    "call_delta": 0.45, "call_gamma": 0.0005, "call_theta": -50.0, "call_vega": 15.0,
    "put_ltp": 200.0, "put_bid": 199.0, "put_ask": 201.0,
    "put_oi": 120_000, "put_volume": 4_000,
    "put_iv": 0.17,
    "put_delta": -0.52, "put_gamma": 0.0005, "put_theta": -45.0, "put_vega": 14.0,
    "lotsize": 30,
}

_OA_STRIKE_B = {
    "strike": 53100,
    "expiry": "27-Jun-2026",
    "call_ltp": 180.0, "call_bid": 179.0, "call_ask": 181.0,
    "call_oi": 80_000, "call_volume": 3_000,
    "call_iv": 0.16,
    "call_delta": 0.38, "call_gamma": 0.0004, "call_theta": -40.0, "call_vega": 12.0,
    "put_ltp": 270.0, "put_bid": 269.0, "put_ask": 271.0,
    "put_oi": 90_000, "put_volume": 3_500,
    "put_iv": 0.19,
    "put_delta": -0.60, "put_gamma": 0.0004, "put_theta": -38.0, "put_vega": 11.0,
    "lotsize": 30,
}

_OA_CHAIN_DATA = {
    "underlying_ltp": 53050.0,
    "atm_strike": 53000,
    "expiry_ddmmmyy": "27JUN26",
    "strikes": [_OA_STRIKE_A, _OA_STRIKE_B],
}


# ──────────────── _oa_chain_to_flat ──────────────────────────────────────────

def test_flat_two_rows_per_strike():
    """Each OA row (one strike) explodes into CE + PE flat rows."""
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    assert len(flat) == 2
    assert {r["type"] for r in flat} == {"CE", "PE"}


def test_flat_two_strikes_four_rows():
    flat = _oa_chain_to_flat([_OA_STRIKE_A, _OA_STRIKE_B])
    assert len(flat) == 4


def test_flat_iv_decimal_to_percent():
    """call_iv=0.18 (decimal) → iv=18.0 (percent) in flat row."""
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    ce = next(r for r in flat if r["type"] == "CE")
    assert ce["iv"] == pytest.approx(18.0, rel=1e-3)


def test_flat_put_iv_decimal_to_percent():
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    pe = next(r for r in flat if r["type"] == "PE")
    assert pe["iv"] == pytest.approx(17.0, rel=1e-3)


def test_flat_lot_size_carried():
    """lotsize from the OA row appears as lot_size on each flat leg."""
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    assert all(r["lot_size"] == 30 for r in flat)


def test_flat_oi_change_pct_zero():
    """oi_change_pct is 0.0; detect_oi_shift() will recompute it from snapshots."""
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    assert all(r["oi_change_pct"] == 0.0 for r in flat)


def test_flat_field_names_present():
    """All fields expected by scan_index internals are present."""
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    required = {"strike", "type", "ltp", "bid", "ask", "volume", "oi",
                "oi_change_pct", "iv", "delta", "gamma", "theta", "vega",
                "timestamp", "lot_size"}
    for row in flat:
        assert required <= set(row.keys()), f"Missing fields: {required - set(row.keys())}"


def test_flat_strike_value_correct():
    flat = _oa_chain_to_flat([_OA_STRIKE_A])
    assert all(r["strike"] == 53000 for r in flat)


def test_flat_empty_strikes_returns_empty():
    assert _oa_chain_to_flat([]) == []


def test_flat_missing_lotsize_defaults_to_zero():
    row = {**_OA_STRIKE_A}
    del row["lotsize"]
    flat = _oa_chain_to_flat([row])
    assert all(r["lot_size"] == 0 for r in flat)


# ──────────────── scan_index OpenAlgo path ───────────────────────────────────

def _make_scanner():
    """Return an OptionScanner with a mock Fyers client."""
    fyers_mock = MagicMock()
    fyers_mock.get_option_chain.return_value = []
    fyers_mock.get_spot_price.return_value = 0.0
    fyers_mock.get_atm_strike.return_value = 0
    fyers_mock._iv_history = {}
    fyers_mock.get_iv_percentile.return_value = 50.0
    return OptionScanner(fyers_mock)


def test_scan_index_uses_oa_spot():
    """When oa_chain_data is provided, result['spot'] comes from underlying_ltp."""
    sc = _make_scanner()
    result = sc.scan_index("BANKNIFTY", oa_chain_data=_OA_CHAIN_DATA)
    assert result["spot"] == pytest.approx(53050.0)


def test_scan_index_uses_oa_atm():
    """When oa_chain_data is provided, result['atm'] comes from atm_strike."""
    sc = _make_scanner()
    result = sc.scan_index("BANKNIFTY", oa_chain_data=_OA_CHAIN_DATA)
    assert result["atm"] == 53000


def test_scan_index_fyers_not_called_when_oa_provided():
    """Fyers chain/spot/atm calls are skipped when oa_chain_data is present."""
    sc = _make_scanner()
    sc.scan_index("BANKNIFTY", oa_chain_data=_OA_CHAIN_DATA)
    sc.fyers.get_option_chain.assert_not_called()
    sc.fyers.get_spot_price.assert_not_called()
    sc.fyers.get_atm_strike.assert_not_called()


_MINIMAL_FYERS_CHAIN = [
    {"strike": 52000, "type": "CE", "ltp": 100.0, "bid": 99.0, "ask": 101.0,
     "volume": 1_000, "oi": 50_000, "oi_change_pct": 0.0, "iv": 18.0,
     "delta": 0.45, "gamma": 0.0005, "theta": -50.0, "vega": 15.0, "timestamp": ""},
    {"strike": 52000, "type": "PE", "ltp": 95.0, "bid": 94.0, "ask": 96.0,
     "volume": 900, "oi": 45_000, "oi_change_pct": 0.0, "iv": 17.5,
     "delta": -0.52, "gamma": 0.0005, "theta": -45.0, "vega": 14.0, "timestamp": ""},
]


def test_scan_index_fyers_called_when_no_oa():
    """Fyers path is used (fallback) when oa_chain_data is None."""
    sc = _make_scanner()
    sc.fyers.get_option_chain.return_value = _MINIMAL_FYERS_CHAIN
    sc.fyers.get_spot_price.return_value = 52000.0
    sc.fyers.get_atm_strike.return_value = 52000
    result = sc.scan_index("NIFTY")  # no oa_chain_data
    sc.fyers.get_option_chain.assert_called_once_with("NIFTY")
    sc.fyers.get_spot_price.assert_called_once_with("NIFTY")
    assert result["spot"] == pytest.approx(52000.0)
