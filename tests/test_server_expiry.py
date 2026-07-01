"""
Tests for expiry hardening — refuse-to-trade when real expiry is unavailable.

All 10 tests pass after the expiry hardening fix (server.py commit that:
  - makes _pick_expiry() return (None, True) instead of a Thursday heuristic
  - gates the OA chain fetch and entry suggestion on expiry_heuristic=False
  - sets blocked_reason="expiry_unavailable" unconditionally before strptime)

Tests 1-2, 10: happy-path tests that passed before and after the fix.
Tests 3-7: _pick_expiry() failure cases — confirm (None, True) on every failure
  mode (OA not configured, API error, empty list, bad format, all-past dates).
Tests 8-9: entry-path guarantee — blocked_reason=="expiry_unavailable",
  suggestion==None, get_enriched_chain and evaluate never called.
  Test 9 specifically verifies the skip is unconditional even before the first
  scan cycle (latest_scans empty for the index).
"""

import asyncio
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import backend.server as server
from backend.server import _pick_expiry, app


# ── date helpers ──────────────────────────────────────────────────────────────

def _ddmmmyy(offset: int) -> str:
    """OpenAlgo DDMMMYY string for today + offset days."""
    return (date.today() + timedelta(days=offset)).strftime("%d%b%y").upper()


def _display(offset: int) -> str:
    """Display-format expiry string ('%d-%b-%Y') for today + offset days."""
    return (date.today() + timedelta(days=offset)).strftime("%d-%b-%Y")


def _next_tuesday_ddmmmyy() -> str:
    """DDMMMYY string for the Tuesday at least 8 days from today.

    Skips the current-week Tuesday so DTE is always comfortably positive
    regardless of the weekday on which the test runs.
    """
    today = date.today()
    days_until_tue = (1 - today.weekday()) % 7 or 7   # next Tuesday (could be 7 days)
    d = today + timedelta(days=days_until_tue + 7)     # +7 more = safely future
    assert d.weekday() == 1, f"helper bug: {d} is not a Tuesday"
    return d.strftime("%d%b%y").upper()


def _next_tuesday_display() -> str:
    today = date.today()
    days_until_tue = (1 - today.weekday()) % 7 or 7
    d = today + timedelta(days=days_until_tue + 7)
    return d.strftime("%d-%b-%Y")


# ══════════════════════════════════════════════════════════════════════════════
# 1–7  _pick_expiry() unit tests
#
# Patch server._openalgo directly; no FastAPI, no DB.
# ══════════════════════════════════════════════════════════════════════════════

def _oa(expiry_list: list) -> MagicMock:
    oa = MagicMock()
    oa.get_expiry.return_value = expiry_list
    return oa


def test_pick_expiry_real_date_returned():
    """OA returns a valid future DDMMMYY date → (display-format str, False).

    PASSES on current code — this is the happy path that already works.
    """
    future_ddmmmyy = _ddmmmyy(7)
    expected_display = _display(7)
    with patch.object(server, "_openalgo", _oa([future_ddmmmyy])):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert heuristic is False
    assert expiry == expected_display


def test_pick_expiry_tuesday_date_parses():
    """OA returns a Tuesday DDMMMYY date → (non-None, False), no exception.

    Proves strptime("%d%b%y") is weekday-agnostic: it parses a calendar date,
    not a weekday. Tuesday dates (NIFTY's current expiry day) parse identically
    to any other day of the week.
    PASSES on current code — parser already handles any weekday.
    """
    tuesday_ddmmmyy = _next_tuesday_ddmmmyy()
    with patch.object(server, "_openalgo", _oa([tuesday_ddmmmyy])):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert heuristic is False
    assert expiry is not None
    # Verify the round-trip: the returned display string parses back to a Tuesday
    parsed = datetime.strptime(expiry, "%d-%b-%Y").date()
    assert parsed.weekday() == 1, (
        f"round-trip mismatch: {expiry} parsed to {parsed.strftime('%A')}, expected Tuesday"
    )


def test_pick_expiry_no_oa_returns_none():
    """_openalgo = None → returns (None, True), not a Thursday fallback."""
    with patch.object(server, "_openalgo", None):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert expiry is None
    assert heuristic is True


def test_pick_expiry_get_expiry_raises_returns_none():
    """get_expiry() raises a network/API exception → returns (None, True)."""
    oa = MagicMock()
    oa.get_expiry.side_effect = Exception("connection refused")
    with patch.object(server, "_openalgo", oa):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert expiry is None
    assert heuristic is True


def test_pick_expiry_empty_list_returns_none():
    """get_expiry() returns [] — no expiries published → returns (None, True)."""
    with patch.object(server, "_openalgo", _oa([])):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert expiry is None
    assert heuristic is True


def test_pick_expiry_bad_format_returns_none():
    """get_expiry() returns an unparseable string → strptime raises, returns (None, True)."""
    with patch.object(server, "_openalgo", _oa(["BADFORMAT"])):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=0)
    assert expiry is None
    assert heuristic is True


def test_pick_expiry_all_dates_below_min_dte_returns_none():
    """All OA dates have DTE < min_dte (yesterday → DTE=-1 < min_dte=2) → (None, True)."""
    past = _ddmmmyy(-1)   # yesterday — always DTE < 0 < min_dte=2
    with patch.object(server, "_openalgo", _oa([past])):
        expiry, heuristic = _pick_expiry("NIFTY", min_dte=2)
    assert expiry is None
    assert heuristic is True


# ══════════════════════════════════════════════════════════════════════════════
# Entry-path (FastAPI TestClient) — fixture
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def entry_client():
    """TestClient with all server-level side-effecting dependencies stubbed.

    Stubs applied:
      fyers.connect()        → no-op (no live broker session on startup)
      init_db()              → no-op (no SQLite file needed)
      scan_loop              → suspended (no background scan interference)
      gate.evaluate()        → always returns a "take" Decision (no cooldown DB)
      log_optscan_signal()   → returns 1 (no DB write)
      log_entry_suggestion() → no-op (no DB write; only called when suggestion exists)
      settings.WEBHOOK_SECRET → "test-secret"
      _active_positions      → {} (no position guard blocks entry path)
    """
    fake_decision = MagicMock()
    fake_decision.take = True
    fake_decision.direction = "long"
    fake_decision.regime = "trending"
    fake_decision.reason = "LONG take — regime: trending; filters 11/12"
    fake_decision.features = {"adx": 40.0, "filters": 11}

    async def _noop_scan_loop():
        await asyncio.sleep(999_999)

    with (
        patch.object(server.fyers, "connect", return_value=None),
        patch("backend.server.init_db", return_value=None),
        patch("backend.server.scan_loop", _noop_scan_loop),
        patch.object(server.gate, "evaluate", return_value=fake_decision),
        patch("backend.server.log_optscan_signal", return_value=1),
        patch("backend.server.log_entry_suggestion", return_value=None),
        patch.object(server.settings, "WEBHOOK_SECRET", "test-secret"),
        patch.object(server, "_active_positions", {}),
    ):
        with TestClient(app) as c:
            yield c


# _TAKE_PAYLOAD — valid OptScanPayload with adx=40 (trending → take) and
# bars_since=30 (well beyond any cooldown window the mock gate would check).
_TAKE_PAYLOAD = {
    "secret": "test-secret",
    "v": "optscan-v13", "sym": "NIFTY", "tf": "9",
    "dir": "long", "bar_time": 1_700_000_000_000,
    "price": 25000.0, "atr": 100.0, "adx": 40.0, "filters": 11,
    "f_ema": True, "f_rsi": True, "f_vol": True, "f_vwap": True,
    "f_mvwap": True, "f_band": True, "f_cvd": True, "f_st": True,
    "f_macd": True, "f_poc": True, "f_mss": True, "f_adx": True,
    "z": 0.5, "z_long_zone": False, "z_short_zone": False,
    "z_bull_pa": False, "z_bear_pa": False,
    "fvg_ok": True, "pb_ok": True, "vol_ok": True,
    "range_ratio": 1.2, "bars_since": 30,
    "hh": True, "ll": False, "ext_long": False, "ext_short": False,
    "mss_state": 1,
}


# ══════════════════════════════════════════════════════════════════════════════
# 8–10  Entry-path refuse-to-trade tests
# ══════════════════════════════════════════════════════════════════════════════

def test_entry_skips_when_expiry_unavailable(entry_client):
    """When _pick_expiry returns (None, True), the entry path skips completely.

    Asserts ALL of:
      - response.suggestion is null (no suggestion produced)
      - response.blocked_reason == "expiry_unavailable" (cause clearly identified)
      - _openalgo.get_enriched_chain was NOT called (no fabricated expiry reaches OA API)
      - strike_selector.evaluate was NOT called (no strike picked off a fabricated expiry)
    """
    oa = MagicMock()    # non-None so elif _openalgo: branch is entered
    ss = MagicMock()

    with (
        patch.object(server, "_openalgo", oa),
        patch.object(server, "strike_selector", ss),
        patch("backend.server._pick_expiry", return_value=(None, True)),
    ):
        resp = entry_client.post("/webhook/optscan", json=_TAKE_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()

    assert data["suggestion"] is None, (
        "no suggestion must be produced when expiry is unavailable"
    )
    assert data["blocked_reason"] == "expiry_unavailable", (
        f"expected blocked_reason='expiry_unavailable', got {data['blocked_reason']!r}"
    )
    oa.get_enriched_chain.assert_not_called()
    ss.evaluate.assert_not_called()


def test_entry_skips_unconditionally_without_prior_scan(entry_client):
    """Refuse-to-trade is unconditional — does not depend on latest_scans being populated.

    Simulates a webhook arriving before the first scan cycle has run:
    latest_scans is empty (no entry for 'NIFTY'). The skip must still happen —
    the dashboard health annotation (updating latest_scans) is best-effort and may
    lag; the skip itself must not lag.
    """
    # Simulate cold start: no prior scan cycle has populated latest_scans
    original_scans = dict(server.latest_scans)
    server.latest_scans.clear()
    try:
        oa = MagicMock()
        ss = MagicMock()

        with (
            patch.object(server, "_openalgo", oa),
            patch.object(server, "strike_selector", ss),
            patch("backend.server._pick_expiry", return_value=(None, True)),
        ):
            resp = entry_client.post("/webhook/optscan", json=_TAKE_PAYLOAD)

        assert resp.status_code == 200
        data = resp.json()

        # Skip is unconditional — not gated on latest_scans having an entry
        assert data["suggestion"] is None, (
            "skip must be unconditional even before first scan cycle"
        )
        assert data["blocked_reason"] == "expiry_unavailable"
        oa.get_enriched_chain.assert_not_called()
        ss.evaluate.assert_not_called()
    finally:
        server.latest_scans.update(original_scans)


def test_entry_proceeds_when_real_expiry_available(entry_client):
    """When _pick_expiry returns a real expiry, the entry path produces a suggestion.

    Confirms we skip ONLY on the failure cases — not always.
    PASSES on current code (happy path already works).
    """
    from backend.strike_selector import EntrySuggestion

    expiry_display = _display(14)
    expiry_ddmmmyy = _ddmmmyy(14)

    fake_suggestion = EntrySuggestion(
        sym="NIFTY", expiry=expiry_display, strike=25000,
        option_type="CE", action="BUY",
        entry_premium=120.0, lots=1, delta=0.45, iv=0.20,
        stop_premium=84.0, target_premium=180.0,
        time_stop="15:15", rationale="test", regime="trending", mode="intraday",
    )

    oa = MagicMock()
    oa.get_enriched_chain.return_value = {
        "underlying_ltp": 25000.0,
        "atm_strike": 25000,
        "expiry_ddmmmyy": expiry_ddmmmyy,
        "strikes": [{
            "strike": 25000, "expiry": expiry_display,
            "call_ltp": 120.0, "call_bid": 119.0, "call_ask": 121.0,
            "call_oi": 100_000, "call_iv": 0.20,
            "call_delta": 0.45, "call_gamma": 0.0005,
            "call_theta": -60.0, "call_vega": 25.0,
            "put_ltp": 110.0, "put_bid": 109.0, "put_ask": 111.0,
            "put_oi": 90_000, "put_iv": 0.19,
            "put_delta": -0.52, "put_gamma": 0.0005,
            "put_theta": -58.0, "put_vega": 24.0,
            "lotsize": 25,
        }],
    }
    ss = MagicMock()
    ss.evaluate.return_value = fake_suggestion

    with (
        patch.object(server, "_openalgo", oa),
        patch.object(server, "strike_selector", ss),
        patch("backend.server._pick_expiry", return_value=(expiry_display, False)),
    ):
        resp = entry_client.post("/webhook/optscan", json=_TAKE_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()
    assert data["suggestion"] is not None, (
        "a suggestion must be produced when a real expiry is available"
    )
    assert data["blocked_reason"] is None
    ss.evaluate.assert_called_once()
