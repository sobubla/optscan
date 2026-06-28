"""
Thin HTTP client for OpenAlgo's option-chain, Greeks, and quote endpoints.

Consumes the OpenAlgo REST API only — no OpenAlgo source code is included.
OpenAlgo's core is AGPL v3.0; see CLAUDE.md licensing rule.

API reference: https://docs.openalgo.in/api/v1/
Credentials come from environment variables (loaded by the human via .env):
  OPENALGO_BASE_URL  – e.g. "http://127.0.0.1:5000"
  OPENALGO_API_KEY   – OpenAlgo API key

The api_key is transmitted in POST request bodies only.
It is never logged, printed, or included in exception messages.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class OpenAlgoError(Exception):
    """Raised when OpenAlgo returns an HTTP error or a non-success response body."""


class OpenAlgoClient:
    """
    Thin wrapper around three OpenAlgo endpoints:

      POST /api/v1/optionchain   – chain rows (strike, OI, LTP)
      POST /api/v1/optiongreeks  – chain rows + IV and Black-76 Greeks
      POST /api/v1/quotes        – single-instrument spot quote

    Normalises all responses into plain dicts so callers have no dependency
    on OpenAlgo types or field names.

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

    def get_option_chain(self, symbol: str, expiry: str) -> list:
        """
        Fetch the option chain for *symbol* at *expiry*.

        Returns a list of per-strike dicts::

            {strike, expiry, call_ltp, call_oi, put_ltp, put_oi}

        Args:
            symbol: instrument name, e.g. ``"NIFTY"``, ``"BANKNIFTY"``.
            expiry: OpenAlgo-formatted expiry, e.g. ``"29-FEB-2024"``.
        """
        raw = self._post("/api/v1/optionchain", {"symbol": symbol, "expiry": expiry})
        return [_parse_chain_row(row) for row in raw]

    def get_option_greeks(self, symbol: str, expiry: str) -> list:
        """
        Fetch the option chain with Black-76 Greeks for *symbol* at *expiry*.

        Returns a list of per-strike dicts::

            {strike, expiry,
             call_iv, call_delta, call_gamma, call_theta, call_vega, call_ltp, call_oi,
             put_iv,  put_delta,  put_gamma,  put_theta,  put_vega,  put_ltp,  put_oi}

        IV is an annualised decimal (0.18 = 18%).
        theta is per calendar day; vega is per 1% IV change.

        Args:
            symbol: instrument name, e.g. ``"BANKNIFTY"``.
            expiry: OpenAlgo-formatted expiry, e.g. ``"29-FEB-2024"``.
        """
        raw = self._post("/api/v1/optiongreeks", {"symbol": symbol, "expiry": expiry})
        return [_parse_greeks_row(row) for row in raw]

    def get_quote(self, symbol: str, exchange: str = "NSE") -> dict:
        """
        Fetch a spot quote for *symbol*.

        Returns::

            {ltp, open, high, low, close, volume}

        Args:
            symbol:   Full instrument symbol understood by OpenAlgo,
                      e.g. ``"NSE:NIFTY50-INDEX"``.
            exchange: Exchange identifier (default ``"NSE"``).
        """
        data = self._post("/api/v1/quotes", {"symbol": symbol, "exchange": exchange})
        return _parse_quote(data)

    # ──────────────────────────────────────────────────────────────────────── #

    def _post(self, path: str, body: dict):
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
        data = parsed["data"]
        logger.debug(
            "OpenAlgo %s → %s",
            path,
            f"{len(data)} rows" if isinstance(data, list) else "1 record",
        )
        return data


# ────────────────────────────── row parsers ───────────────────────────────────
# Expected OpenAlgo response shapes are documented inline.
# Adjust field names here if your OpenAlgo version differs.


def _leg(row: dict, key: str) -> dict:
    """Return CE or PE sub-dict from a chain row, falling back to empty dict."""
    return row.get(key) or {}


def _parse_chain_row(row: dict) -> dict:
    """
    Normalise one ``/api/v1/optionchain`` row.

    OpenAlgo shape::

        {"strikePrice": 44000, "expiryDate": "29-FEB-2024",
         "CE": {"lastPrice": 150.5, "openInterest": 12345},
         "PE": {"lastPrice": 80.0,  "openInterest": 8000}}
    """
    ce = _leg(row, "CE")
    pe = _leg(row, "PE")
    return {
        "strike":   int(row["strikePrice"]),
        "expiry":   str(row.get("expiryDate", "")),
        "call_ltp": float(ce.get("lastPrice", 0.0)),
        "call_oi":  int(ce.get("openInterest", 0)),
        "put_ltp":  float(pe.get("lastPrice", 0.0)),
        "put_oi":   int(pe.get("openInterest", 0)),
    }


def _parse_greeks_row(row: dict) -> dict:
    """
    Normalise one ``/api/v1/optiongreeks`` row.

    OpenAlgo shape extends the chain row with::

        "CE": {..., "impliedVolatility": 0.185,
                    "delta": 0.52, "gamma": 0.00055, "theta": -45.2, "vega": 16.1}
    """
    ce = _leg(row, "CE")
    pe = _leg(row, "PE")
    return {
        "strike":     int(row["strikePrice"]),
        "expiry":     str(row.get("expiryDate", "")),
        # call leg
        "call_ltp":   float(ce.get("lastPrice", 0.0)),
        "call_oi":    int(ce.get("openInterest", 0)),
        "call_iv":    float(ce.get("impliedVolatility", 0.0)),
        "call_delta": float(ce.get("delta", 0.0)),
        "call_gamma": float(ce.get("gamma", 0.0)),
        "call_theta": float(ce.get("theta", 0.0)),
        "call_vega":  float(ce.get("vega", 0.0)),
        # put leg
        "put_ltp":    float(pe.get("lastPrice", 0.0)),
        "put_oi":     int(pe.get("openInterest", 0)),
        "put_iv":     float(pe.get("impliedVolatility", 0.0)),
        "put_delta":  float(pe.get("delta", 0.0)),
        "put_gamma":  float(pe.get("gamma", 0.0)),
        "put_theta":  float(pe.get("theta", 0.0)),
        "put_vega":   float(pe.get("vega", 0.0)),
    }


def _parse_quote(data: dict) -> dict:
    """
    Normalise a ``/api/v1/quotes`` data block.

    OpenAlgo shape::

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
