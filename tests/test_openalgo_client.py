"""
Tests for backend/openalgo_client.py.

All HTTP calls are mocked — no live OpenAlgo instance is needed.
IV/Greeks computation (compute_iv / compute_greeks from fyers_client) is also
mocked so tests don't depend on vollib being importable here.

Response shapes match real OpenAlgo REST API v1 (verified against SDK v1.0.47):
  - optionchain / optiongreeks / depth → no 'data' wrapper (fields at top level)
  - quotes / expiry                    → wrapped in 'data' key
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.openalgo_client import OpenAlgoClient, OpenAlgoError


# ──────────────────────── helpers ─────────────────────────────────────────────


def _client() -> OpenAlgoClient:
    """Client pointed at a non-existent host with a fake key — no real secrets."""
    return OpenAlgoClient(base_url="http://localhost:5000", api_key="test-key")


def _mock_raw(data: dict, http_status: int = 200):
    """Build a mock response for endpoints with NO 'data' wrapper."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = http_status
    resp.json.return_value = data
    if http_status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _mock_data(inner, status: str = "success", http_status: int = 200):
    """Build a mock response for endpoints that wrap their result in a 'data' key."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = http_status
    resp.json.return_value = {"status": status, "data": inner}
    if http_status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


# ─────────── shared fixtures ──────────────────────────────────────────────────

_CHAIN_RESPONSE = {
    "status": "success",
    "underlying": "BANKNIFTY",
    "underlying_ltp": 53000.0,
    "expiry_date": "27JUN26",
    "atm_strike": 53000,
    "chain": [
        {
            "strike": 53000,
            "ce": {
                "symbol": "BANKNIFTY27JUN2653000CE",
                "ltp": 250.0, "bid": 249.0, "ask": 251.0,
                "oi": 150_000, "volume": 5_000, "lotsize": 30, "label": "ATM",
            },
            "pe": {
                "symbol": "BANKNIFTY27JUN2653000PE",
                "ltp": 200.0, "bid": 199.0, "ask": 201.0,
                "oi": 120_000, "volume": 4_000, "lotsize": 30, "label": "ATM",
            },
        },
        {
            "strike": 53100,
            "ce": {
                "symbol": "BANKNIFTY27JUN2653100CE",
                "ltp": 180.0, "bid": 179.0, "ask": 181.0,
                "oi": 80_000, "volume": 3_000, "lotsize": 30, "label": "OTM1",
            },
            "pe": {
                "symbol": "BANKNIFTY27JUN2653100PE",
                "ltp": 270.0, "bid": 269.0, "ask": 271.0,
                "oi": 90_000, "volume": 3_500, "lotsize": 30, "label": "ITM1",
            },
        },
    ],
}

_GREEKS_RESPONSE = {
    "status": "success",
    "symbol": "BANKNIFTY27JUN2653000CE",
    "exchange": "NFO",
    "underlying": "BANKNIFTY",
    "strike": 53000,
    "option_type": "CE",
    "expiry_date": "27JUN26",
    "days_to_expiry": 3,
    "spot_price": 53000.0,
    "option_price": 250.0,
    "interest_rate": 0,
    "implied_volatility": 15.2,
    "greeks": {
        "delta": 0.45,
        "gamma": 0.0005,
        "theta": -50.0,
        "vega": 15.0,
        "rho": 0.3,
    },
}

_QUOTE_DATA = {
    "ltp": 53000.0,
    "open": 52800.0,
    "high": 53200.0,
    "low": 52700.0,
    "close": 53000.0,
    "volume": 12_345_678,
}

_EXPIRY_DATA = ["27JUN26", "04JUL26", "31JUL26", "28AUG26"]


# ─── enriched chain ───────────────────────────────────────────────────────────

_ZERO_GREEKS = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


def _patch_iv_greeks(monkeypatch, iv=0.18, greeks=None):
    """Patch compute_iv and compute_greeks in the client module."""
    if greeks is None:
        greeks = {"delta": 0.45, "gamma": 0.0005, "theta": -50.0, "vega": 15.0}
    monkeypatch.setattr("backend.openalgo_client.compute_iv", lambda *a, **kw: iv)
    monkeypatch.setattr("backend.openalgo_client.compute_greeks", lambda *a, **kw: greeks)


def test_get_enriched_chain_returns_correct_strike_count(monkeypatch):
    """Chain with 2 rows → 2 enriched strike rows returned."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    assert len(result["strikes"]) == 2


def test_get_enriched_chain_field_names(monkeypatch):
    """All expected field names present on a strike row."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    row = result["strikes"][0]
    for key in ("strike", "expiry", "call_symbol", "call_ltp", "call_bid", "call_ask",
                "call_oi", "call_volume", "call_spread_pct",
                "call_iv", "call_delta", "call_gamma", "call_theta", "call_vega",
                "put_symbol", "put_ltp", "put_bid", "put_ask",
                "put_oi", "put_volume", "put_spread_pct",
                "put_iv", "put_delta", "put_gamma", "put_theta", "put_vega",
                "lotsize"):
        assert key in row, f"Missing key: {key}"


def test_get_enriched_chain_expiry_format_normalized(monkeypatch):
    """DDMMMYY from API → '%d-%b-%Y' string on each strike row."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    assert result["strikes"][0]["expiry"] == "27-Jun-2026"


def test_get_enriched_chain_has_underlying_ltp(monkeypatch):
    """Top-level underlying_ltp and atm_strike present."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    assert result["underlying_ltp"] == pytest.approx(53000.0)
    assert result["atm_strike"] == 53000


def test_get_enriched_chain_spread_pct_computed(monkeypatch):
    """call_spread_pct = (ask-bid)/mid*100; for bid=249, ask=251 → 0.8%."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    row = result["strikes"][0]
    assert row["call_spread_pct"] == pytest.approx((251 - 249) / 250.0 * 100, rel=1e-3)


def test_get_enriched_chain_sends_correct_params(monkeypatch):
    """underlying, exchange, expiry_date sent in POST body; apikey present."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post",
                      return_value=_mock_raw(_CHAIN_RESPONSE)) as mock_post:
        _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26", strike_count=10)
    body = mock_post.call_args.kwargs["json"]
    assert body["apikey"] == "test-key"
    assert body["underlying"] == "BANKNIFTY"
    assert body["exchange"] == "NSE_INDEX"
    assert body["expiry_date"] == "27JUN26"
    assert body["strike_count"] == 10


def test_get_enriched_chain_api_error_raises(monkeypatch):
    """Non-success status → OpenAlgoError."""
    err_resp = {"status": "error", "message": "Symbol not found"}
    with patch.object(requests.Session, "post", return_value=_mock_raw(err_resp)):
        with pytest.raises(OpenAlgoError, match="status="):
            _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")


def test_get_enriched_chain_http_error_raises(monkeypatch):
    """HTTP 500 → OpenAlgoError."""
    err_resp = {"status": "error"}
    with patch.object(requests.Session, "post",
                      return_value=_mock_raw(err_resp, http_status=500)):
        with pytest.raises(OpenAlgoError, match="HTTP 500"):
            _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")


def test_get_enriched_chain_lotsize(monkeypatch):
    """lotsize taken from ce/pe sub-dict."""
    _patch_iv_greeks(monkeypatch)
    with patch.object(requests.Session, "post", return_value=_mock_raw(_CHAIN_RESPONSE)):
        result = _client().get_enriched_chain("BANKNIFTY", "NSE_INDEX", "27JUN26")
    assert result["strikes"][0]["lotsize"] == 30


# ─── option Greeks (per-symbol) ───────────────────────────────────────────────


def test_get_option_greeks_returns_correct_fields():
    """Happy path: all key fields present and correctly typed."""
    with patch.object(requests.Session, "post", return_value=_mock_raw(_GREEKS_RESPONSE)):
        g = _client().get_option_greeks("BANKNIFTY27JUN2653000CE", "NFO")
    assert g["symbol"] == "BANKNIFTY27JUN2653000CE"
    assert g["implied_volatility"] == pytest.approx(15.2)
    assert g["delta"] == pytest.approx(0.45)
    assert g["gamma"] == pytest.approx(0.0005)
    assert g["theta"] == pytest.approx(-50.0)
    assert g["vega"] == pytest.approx(15.0)
    assert g["days_to_expiry"] == 3
    assert g["spot_price"] == pytest.approx(53000.0)
    assert g["option_price"] == pytest.approx(250.0)


def test_get_option_greeks_sends_symbol_and_exchange():
    """symbol and exchange (not underlying + expiry) sent in body."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_raw(_GREEKS_RESPONSE)) as mock_post:
        _client().get_option_greeks("BANKNIFTY27JUN2653000CE", "NFO", interest_rate=6.5)
    body = mock_post.call_args.kwargs["json"]
    assert body["apikey"] == "test-key"
    assert body["symbol"] == "BANKNIFTY27JUN2653000CE"
    assert body["exchange"] == "NFO"
    assert body["interest_rate"] == 6.5


def test_get_option_greeks_http_error_raises():
    """HTTP 401 → OpenAlgoError."""
    err_resp = {"status": "error", "message": "Unauthorized"}
    with patch.object(requests.Session, "post",
                      return_value=_mock_raw(err_resp, http_status=401)):
        with pytest.raises(OpenAlgoError, match="HTTP 401"):
            _client().get_option_greeks("BANKNIFTY27JUN2653000CE")


# ─── expiry ───────────────────────────────────────────────────────────────────


def test_get_expiry_returns_list():
    """Happy path: returns a list of expiry strings."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_data(_EXPIRY_DATA)):
        result = _client().get_expiry("BANKNIFTY", "NFO")
    assert isinstance(result, list)
    assert "27JUN26" in result


def test_get_expiry_sends_correct_params():
    """symbol, exchange, instrumenttype in POST body."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_data(_EXPIRY_DATA)) as mock_post:
        _client().get_expiry("NIFTY", "NFO", instrumenttype="options")
    body = mock_post.call_args.kwargs["json"]
    assert body["symbol"] == "NIFTY"
    assert body["exchange"] == "NFO"
    assert body["instrumenttype"] == "options"


# ─── quotes ───────────────────────────────────────────────────────────────────


def test_get_quote_success():
    """Happy path: all OHLCV fields present and correctly typed."""
    with patch.object(requests.Session, "post", return_value=_mock_data(_QUOTE_DATA)):
        q = _client().get_quote("NSE:NIFTY50-INDEX")
    assert q["ltp"] == pytest.approx(53000.0)
    assert q["high"] == pytest.approx(53200.0)
    assert q["low"] == pytest.approx(52700.0)
    assert q["volume"] == 12_345_678


def test_get_quote_sends_symbol_and_exchange():
    """symbol and exchange must be in the POST body."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_data(_QUOTE_DATA)) as mock_post:
        _client().get_quote("NSE:NIFTY50-INDEX", exchange="NSE")
    body = mock_post.call_args.kwargs["json"]
    assert body["symbol"] == "NSE:NIFTY50-INDEX"
    assert body["exchange"] == "NSE"


def test_get_quote_missing_ltp_raises():
    """Quote response without 'ltp' → OpenAlgoError naming the field."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_data({"open": 52800.0})):
        with pytest.raises(OpenAlgoError, match="ltp"):
            _client().get_quote("NSE:NIFTY50-INDEX")


def test_get_quote_api_error_raises():
    """API-level error → OpenAlgoError before any field access."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_data(None, status="error")):
        with pytest.raises(OpenAlgoError):
            _client().get_quote("NSE:NIFTY50-INDEX")
