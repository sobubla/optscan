"""
Tests for backend/openalgo_client.py.

All HTTP calls are mocked — no live OpenAlgo instance is needed.
The mocked response shapes match the OpenAlgo REST API v1 format
(see _parse_* functions in openalgo_client.py for the expected field names).

Expected to FAIL before backend/openalgo_client.py exists.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.openalgo_client import OpenAlgoClient, OpenAlgoError


# ──────────────────────── helpers ─────────────────────────────────────────────


def _client() -> OpenAlgoClient:
    """Client pointed at a non-existent host with a fake key — no real secrets."""
    return OpenAlgoClient(base_url="http://localhost:5000", api_key="test-key")


def _mock_response(data, status: str = "success", http_status: int = 200):
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = http_status
    resp.json.return_value = {"status": status, "data": data}
    if http_status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


# ──────────────────────── option chain ───────────────────────────────────────

_CHAIN_ROWS = [
    {
        "strikePrice": 44000,
        "expiryDate": "29-FEB-2024",
        "CE": {"lastPrice": 545.0, "openInterest": 12_345},
        "PE": {"lastPrice": 22.5,  "openInterest": 8_000},
    },
    {
        "strikePrice": 44500,
        "expiryDate": "29-FEB-2024",
        "CE": {"lastPrice": 120.0, "openInterest": 55_000},
        "PE": {"lastPrice": 118.0, "openInterest": 48_000},
    },
]


def test_get_option_chain_success():
    """Happy path: parses two strikes, all CE/PE fields correct."""
    with patch.object(requests.Session, "post", return_value=_mock_response(_CHAIN_ROWS)):
        rows = _client().get_option_chain("BANKNIFTY", "29-FEB-2024")

    assert len(rows) == 2
    r = rows[0]
    assert r["strike"] == 44000
    assert r["expiry"] == "29-FEB-2024"
    assert r["call_ltp"] == pytest.approx(545.0)
    assert r["call_oi"] == 12_345
    assert r["put_ltp"] == pytest.approx(22.5)
    assert r["put_oi"] == 8_000


def test_get_option_chain_sends_apikey_in_body():
    """api_key must appear in the POST body; symbol and expiry too."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(_CHAIN_ROWS)) as mock_post:
        _client().get_option_chain("NIFTY", "29-FEB-2024")

    body = mock_post.call_args.kwargs["json"]
    assert body["apikey"] == "test-key"
    assert body["symbol"] == "NIFTY"
    assert body["expiry"] == "29-FEB-2024"


def test_get_option_chain_api_error_raises():
    """API-level error (status != 'success') → OpenAlgoError."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(None, status="error")):
        with pytest.raises(OpenAlgoError, match="status="):
            _client().get_option_chain("BANKNIFTY", "29-FEB-2024")


def test_get_option_chain_http_error_raises():
    """HTTP 500 → OpenAlgoError with the status code in the message."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(None, http_status=500)):
        with pytest.raises(OpenAlgoError, match="HTTP 500"):
            _client().get_option_chain("BANKNIFTY", "29-FEB-2024")


# ──────────────────────── option Greeks ──────────────────────────────────────

_GREEKS_ROWS = [
    {
        "strikePrice": 44500,
        "expiryDate": "29-FEB-2024",
        "CE": {
            "lastPrice": 120.0,
            "openInterest": 55_000,
            "impliedVolatility": 0.185,
            "delta": 0.52,
            "gamma": 0.00055,
            "theta": -45.2,
            "vega": 16.1,
        },
        "PE": {
            "lastPrice": 118.0,
            "openInterest": 48_000,
            "impliedVolatility": 0.188,
            "delta": -0.48,
            "gamma": 0.00055,
            "theta": -44.8,
            "vega": 16.0,
        },
    },
]


def test_get_option_greeks_success():
    """Happy path: IV and all four Greeks parsed for both CE and PE."""
    with patch.object(requests.Session, "post", return_value=_mock_response(_GREEKS_ROWS)):
        rows = _client().get_option_greeks("BANKNIFTY", "29-FEB-2024")

    assert len(rows) == 1
    r = rows[0]
    assert r["strike"] == 44500
    assert r["expiry"] == "29-FEB-2024"
    assert r["call_iv"]    == pytest.approx(0.185)
    assert r["call_delta"] == pytest.approx(0.52)
    assert r["call_gamma"] == pytest.approx(0.00055)
    assert r["call_theta"] == pytest.approx(-45.2)
    assert r["call_vega"]  == pytest.approx(16.1)
    assert r["put_iv"]     == pytest.approx(0.188)
    assert r["put_delta"]  == pytest.approx(-0.48)
    assert r["put_ltp"]    == pytest.approx(118.0)


def test_get_option_greeks_missing_put_leg_defaults_to_zero():
    """A row missing the PE key should default all put fields to 0, not raise."""
    partial = [
        {
            "strikePrice": 44000,
            "expiryDate": "29-FEB-2024",
            "CE": {"lastPrice": 100.0, "openInterest": 1000, "impliedVolatility": 0.20,
                   "delta": 0.55, "gamma": 0.0006, "theta": -50.0, "vega": 17.0},
        }
    ]
    with patch.object(requests.Session, "post", return_value=_mock_response(partial)):
        rows = _client().get_option_greeks("BANKNIFTY", "29-FEB-2024")

    r = rows[0]
    assert r["call_ltp"]  == pytest.approx(100.0)
    assert r["call_iv"]   == pytest.approx(0.20)
    assert r["put_iv"]    == pytest.approx(0.0)
    assert r["put_delta"] == pytest.approx(0.0)
    assert r["put_oi"]    == 0


def test_get_option_greeks_http_error_raises():
    """HTTP 401 (bad API key) → OpenAlgoError."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(None, http_status=401)):
        with pytest.raises(OpenAlgoError, match="HTTP 401"):
            _client().get_option_greeks("BANKNIFTY", "29-FEB-2024")


# ──────────────────────── quotes ─────────────────────────────────────────────

_QUOTE_DATA = {
    "ltp": 44523.5,
    "open": 44200.0,
    "high": 44650.0,
    "low": 44100.0,
    "close": 44523.5,
    "volume": 12_345_678,
}


def test_get_quote_success():
    """Happy path: all OHLCV fields present and correctly typed."""
    with patch.object(requests.Session, "post", return_value=_mock_response(_QUOTE_DATA)):
        q = _client().get_quote("NSE:NIFTY50-INDEX")

    assert q["ltp"]    == pytest.approx(44523.5)
    assert q["high"]   == pytest.approx(44650.0)
    assert q["low"]    == pytest.approx(44100.0)
    assert q["volume"] == 12_345_678


def test_get_quote_sends_symbol_and_exchange():
    """symbol and exchange must be in the POST body."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(_QUOTE_DATA)) as mock_post:
        _client().get_quote("NSE:NIFTY50-INDEX", exchange="NSE")

    body = mock_post.call_args.kwargs["json"]
    assert body["symbol"] == "NSE:NIFTY50-INDEX"
    assert body["exchange"] == "NSE"


def test_get_quote_missing_ltp_raises():
    """Quote response without 'ltp' → OpenAlgoError naming the field."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response({"open": 44200.0})):
        with pytest.raises(OpenAlgoError, match="ltp"):
            _client().get_quote("NSE:NIFTY50-INDEX")


def test_get_quote_api_error_raises():
    """API-level error → OpenAlgoError before any field access."""
    with patch.object(requests.Session, "post",
                      return_value=_mock_response(None, status="error")):
        with pytest.raises(OpenAlgoError):
            _client().get_quote("NSE:NIFTY50-INDEX")
