"""
Thin HTTP client for OpenAlgo's option-chain, Greeks, expiry, depth, and quote endpoints.

Consumes the OpenAlgo REST API only — no OpenAlgo source code is included.
OpenAlgo's core is AGPL v3.0; see CLAUDE.md licensing rule.

API reference: https://docs.openalgo.in/api/v1/
Credentials come from environment variables (loaded by the human via .env):
  OPENALGO_BASE_URL  – e.g. "http://127.0.0.1:5000"
  OPENALGO_API_KEY   – OpenAlgo API key

The api_key is transmitted in POST request bodies only.
It is never logged, printed, or included in exception messages.

Response shape notes (verified against SDK v1.0.47 source):
  /api/v1/optionchain  → no 'data' wrapper; fields at top level
  /api/v1/optiongreeks → no 'data' wrapper; single-option dict
  /api/v1/quotes       → has 'data' wrapper
  /api/v1/expiry       → has 'data' wrapper
  /api/v1/depth        → no 'data' wrapper

IV/Greeks enrichment for get_enriched_chain() is computed locally via vollib
Black-76 (same as fyers_client.py). The /api/v1/optiongreeks endpoint is
per-symbol (single call per option), not per-chain, so local computation
is far cheaper for whole-chain enrichment.
"""

import logging
import math
from datetime import datetime, date
from typing import Optional

import requests

from backend.fyers_client import compute_iv, compute_greeks

logger = logging.getLogger(__name__)


class OpenAlgoError(Exception):
    """Raised when OpenAlgo returns an HTTP error or a non-success response body."""


class OpenAlgoClient:
    """
    Wrapper around OpenAlgo REST endpoints for options data.

    Methods:
      get_enriched_chain   — option chain + local Black-76 IV/Greeks
      get_option_greeks    — per-symbol IV/Greeks from OpenAlgo (single option)
      get_expiry           — list of available expiry dates
      get_quote            — spot quote for an instrument
      get_depth            — market depth (order book)

    Args:
        base_url: OpenAlgo server root, e.g. ``"http://127.0.0.1:5000"``.
        api_key:  OpenAlgo API key.  Never logged.
        timeout:  Per-request timeout in seconds (default 10).
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ──────────────────────────────────────────────────────────────────────── #
    # Public methods
    # ──────────────────────────────────────────────────────────────────────── #

    def get_enriched_chain(
        self,
        underlying: str,
        exchange: str,
        expiry_ddmmmyy: str,
        strike_count: Optional[int] = None,
        r: float = 0.065,
    ) -> dict:
        """
        Fetch option chain from OpenAlgo and enrich with local Black-76 IV/Greeks.

        Args:
            underlying:     Underlying symbol, e.g. ``"BANKNIFTY"`` or ``"NIFTY"``.
            exchange:       Exchange code, e.g. ``"NSE_INDEX"``.
            expiry_ddmmmyy: Expiry in OpenAlgo DDMMMYY format, e.g. ``"27JUN26"``.
            strike_count:   Number of strikes around ATM (e.g. 20 → 41 total). All if None.
            r:              Risk-free rate for Black-76 (default 6.5% = RBI repo).

        Returns::

            {
              "underlying_ltp": float,
              "atm_strike": int,
              "expiry_ddmmmyy": str,
              "strikes": [
                {
                  "strike": int,
                  "expiry": str,          # "%d-%b-%Y" — for strike_selector compat
                  "call_symbol": str,
                  "call_ltp": float,
                  "call_bid": float,
                  "call_ask": float,
                  "call_oi": int,
                  "call_volume": int,
                  "call_spread_pct": float,
                  "call_iv": float,       # annualised decimal (0.18 = 18%)
                  "call_delta": float,
                  "call_gamma": float,
                  "call_theta": float,
                  "call_vega": float,
                  "put_symbol": str,
                  "put_ltp": float,
                  "put_bid": float,
                  "put_ask": float,
                  "put_oi": int,
                  "put_volume": int,
                  "put_spread_pct": float,
                  "put_iv": float,
                  "put_delta": float,
                  "put_gamma": float,
                  "put_theta": float,
                  "put_vega": float,
                  "lotsize": int,
                }
              ]
            }
        """
        body: dict = {"underlying": underlying, "exchange": exchange,
                      "expiry_date": expiry_ddmmmyy}
        if strike_count is not None:
            body["strike_count"] = int(strike_count)

        raw = self._post_raw("/api/v1/optionchain", body)

        underlying_ltp = float(raw.get("underlying_ltp", 0.0))
        atm_strike = int(raw.get("atm_strike", 0))
        expiry_str = _ddmmmyy_to_display(expiry_ddmmmyy)
        dte = _days_to_expiry(expiry_ddmmmyy)

        strikes = []
        for row in raw.get("chain", []):
            ce = row.get("ce") or {}
            pe = row.get("pe") or {}
            strike = int(row.get("strike", 0))

            ce_ltp = float(ce.get("ltp", 0.0))
            pe_ltp = float(pe.get("ltp", 0.0))
            ce_iv = compute_iv(ce_ltp, underlying_ltp, strike, dte, is_call=True, risk_free_rate=r)
            pe_iv = compute_iv(pe_ltp, underlying_ltp, strike, dte, is_call=False, risk_free_rate=r)
            ce_g = compute_greeks(underlying_ltp, strike, dte, ce_iv, is_call=True, risk_free_rate=r)
            pe_g = compute_greeks(underlying_ltp, strike, dte, pe_iv, is_call=False, risk_free_rate=r)

            lotsize = int(ce.get("lotsize") or pe.get("lotsize") or 0)

            strikes.append({
                "strike":        strike,
                "expiry":        expiry_str,
                "call_symbol":   ce.get("symbol", ""),
                "call_ltp":      ce_ltp,
                "call_bid":      float(ce.get("bid", 0.0)),
                "call_ask":      float(ce.get("ask", 0.0)),
                "call_oi":       int(ce.get("oi", 0)),
                "call_volume":   int(ce.get("volume", 0)),
                "call_spread_pct": _spread_pct(ce.get("bid", 0.0), ce.get("ask", 0.0)),
                "call_iv":       ce_iv,
                "call_delta":    ce_g["delta"],
                "call_gamma":    ce_g["gamma"],
                "call_theta":    ce_g["theta"],
                "call_vega":     ce_g["vega"],
                "put_symbol":    pe.get("symbol", ""),
                "put_ltp":       pe_ltp,
                "put_bid":       float(pe.get("bid", 0.0)),
                "put_ask":       float(pe.get("ask", 0.0)),
                "put_oi":        int(pe.get("oi", 0)),
                "put_volume":    int(pe.get("volume", 0)),
                "put_spread_pct": _spread_pct(pe.get("bid", 0.0), pe.get("ask", 0.0)),
                "put_iv":        pe_iv,
                "put_delta":     pe_g["delta"],
                "put_gamma":     pe_g["gamma"],
                "put_theta":     pe_g["theta"],
                "put_vega":      pe_g["vega"],
                "lotsize":       lotsize,
            })

        logger.debug("OpenAlgo enriched chain %s %s → %d strikes", underlying, expiry_ddmmmyy, len(strikes))
        return {
            "underlying_ltp": underlying_ltp,
            "atm_strike":     atm_strike,
            "expiry_ddmmmyy": expiry_ddmmmyy,
            "strikes":        strikes,
        }

    def get_option_greeks(
        self,
        option_symbol: str,
        exchange: str = "NFO",
        interest_rate: Optional[float] = None,
    ) -> dict:
        """
        Fetch IV and Black-76 Greeks for a single option symbol from OpenAlgo.

        This is a per-symbol call — not a chain call. Use ``get_enriched_chain``
        when you need Greeks across the whole chain.

        Args:
            option_symbol:  Full option symbol, e.g. ``"BANKNIFTY27JUN2653000CE"``.
            exchange:       Exchange code (default ``"NFO"``).
            interest_rate:  Risk-free rate as annual percentage (e.g. 6.5 for 6.5%).
                            If None, OpenAlgo uses its default (0).

        Returns::

            {
              "symbol": str,
              "implied_volatility": float,   # percentage, e.g. 15.2 means 15.2%
              "delta": float,
              "gamma": float,
              "theta": float,
              "vega": float,
              "days_to_expiry": int,
              "spot_price": float,
              "option_price": float,
            }
        """
        body: dict = {"symbol": option_symbol, "exchange": exchange}
        if interest_rate is not None:
            body["interest_rate"] = interest_rate

        raw = self._post_raw("/api/v1/optiongreeks", body)
        greeks = raw.get("greeks", {})
        return {
            "symbol":             raw.get("symbol", option_symbol),
            "implied_volatility": float(raw.get("implied_volatility", 0.0)),
            "delta":              float(greeks.get("delta", 0.0)),
            "gamma":              float(greeks.get("gamma", 0.0)),
            "theta":              float(greeks.get("theta", 0.0)),
            "vega":               float(greeks.get("vega", 0.0)),
            "days_to_expiry":     int(raw.get("days_to_expiry", 0)),
            "spot_price":         float(raw.get("spot_price", 0.0)),
            "option_price":       float(raw.get("option_price", 0.0)),
        }

    def get_expiry(
        self,
        symbol: str,
        exchange: str,
        instrumenttype: str = "options",
    ) -> list:
        """
        Fetch available expiry dates for a symbol.

        Args:
            symbol:         Underlying symbol, e.g. ``"BANKNIFTY"``.
            exchange:       Exchange code, e.g. ``"NFO"``.
            instrumenttype: Instrument type (default ``"options"``).

        Returns:
            List of expiry strings in OpenAlgo DDMMMYY format,
            e.g. ``["27JUN26", "04JUL26", "31JUL26"]``.
        """
        data = self._post_data(
            "/api/v1/expiry",
            {"symbol": symbol, "exchange": exchange, "instrumenttype": instrumenttype},
        )
        if isinstance(data, list):
            return data
        return data.get("expiry", []) if isinstance(data, dict) else []

    def get_quote(self, symbol: str, exchange: str = "NSE") -> dict:
        """
        Fetch a spot quote for *symbol*.

        Returns::

            {ltp, open, high, low, close, volume}
        """
        data = self._post_data("/api/v1/quotes", {"symbol": symbol, "exchange": exchange})
        return _parse_quote(data)

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """
        Fetch market depth (order book) for *symbol*.

        Returns the raw OpenAlgo response dict (bids, asks, ltp, etc.).
        """
        return self._post_raw("/api/v1/depth", {"symbol": symbol, "exchange": exchange})

    # ──────────────────────────────────────────────────────────────────────── #
    # Internal HTTP helpers
    # ──────────────────────────────────────────────────────────────────────── #

    def _post_data(self, path: str, body: dict):
        """POST and return ``parsed['data']``. For endpoints that wrap results in a 'data' key."""
        url = self._base + path
        payload = {"apikey": self._key, **body}
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise OpenAlgoError(
                f"HTTP {exc.response.status_code} from {path}"
            ) from exc
        except requests.RequestException as exc:
            raise OpenAlgoError(f"Request failed for {path}: {exc}") from exc

        parsed = resp.json()
        if parsed.get("status") != "success":
            raise OpenAlgoError(
                f"OpenAlgo {path} returned status={parsed.get('status')!r}"
                + (f": {parsed['message']}" if "message" in parsed else "")
            )
        return parsed["data"]

    def _post_raw(self, path: str, body: dict):
        """POST and return the full parsed response dict. For endpoints with no 'data' wrapper."""
        url = self._base + path
        payload = {"apikey": self._key, **body}
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise OpenAlgoError(
                f"HTTP {exc.response.status_code} from {path}"
            ) from exc
        except requests.RequestException as exc:
            raise OpenAlgoError(f"Request failed for {path}: {exc}") from exc

        parsed = resp.json()
        if parsed.get("status") != "success":
            raise OpenAlgoError(
                f"OpenAlgo {path} returned status={parsed.get('status')!r}"
                + (f": {parsed['message']}" if "message" in parsed else "")
            )
        logger.debug("OpenAlgo %s OK", path)
        return parsed


# ────────────────────────────── helpers ──────────────────────────────────────


def _spread_pct(bid, ask) -> float:
    """Bid-ask spread as a percentage of mid-price. Returns 0 if mid is 0."""
    bid, ask = float(bid or 0), float(ask or 0)
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 0.0
    return round((ask - bid) / mid * 100, 2)


def _ddmmmyy_to_display(expiry_ddmmmyy: str) -> str:
    """
    Convert OpenAlgo DDMMMYY expiry to display format understood by strike_selector.

    ``"27JUN26"`` → ``"27-Jun-2026"``
    """
    try:
        return datetime.strptime(expiry_ddmmmyy, "%d%b%y").strftime("%d-%b-%Y")
    except ValueError:
        return expiry_ddmmmyy


def _days_to_expiry(expiry_ddmmmyy: str) -> float:
    """
    Calendar days from today to expiry. Returns 0.5 minimum (avoids div-by-zero in Black-76).
    """
    try:
        expiry_date = datetime.strptime(expiry_ddmmmyy, "%d%b%y").date()
        dte = (expiry_date - date.today()).days
        return max(float(dte), 0.5)
    except ValueError:
        return 0.5


def _parse_quote(data: dict) -> dict:
    """
    Normalise a ``/api/v1/quotes`` data block.

    Expected shape::

        {"ltp": 44523.5, "open": 44200.0, "high": 44650.0,
         "low": 44100.0, "close": 44523.5, "volume": 12345678}
    """
    if "ltp" not in data:
        raise OpenAlgoError(
            f"Quote response missing required field 'ltp'; got: {sorted(data.keys())}"
        )
    return {
        "ltp":    float(data["ltp"]),
        "open":   float(data.get("open", 0.0)),
        "high":   float(data.get("high", 0.0)),
        "low":    float(data.get("low", 0.0)),
        "close":  float(data.get("close", 0.0)),
        "volume": int(data.get("volume", 0)),
    }
