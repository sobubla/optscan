"""
Gate tests — encode the two real-screenshot scenarios from CLAUDE.md
and the cooldown / min-filters cases.

Expected to FAIL right now (ModuleNotFoundError: backend.gate, backend.models)
until those modules are written.

All tests redirect journal.DB_PATH to a temp file via monkeypatch so they
never touch data/journal.db.
"""

import pytest

import backend.journal as journal
from backend.models import OptScanPayload
from backend.gate import AdxRegimeProvider, Gate


# ─────────────────────────── fixtures / factories ───────────────────────────


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp DB for every test; reverts DB_PATH automatically on teardown."""
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "test_journal.db")
    journal.init_db()


@pytest.fixture
def gate() -> Gate:
    return Gate(
        regime_provider=AdxRegimeProvider(adx_threshold=35),
        min_filters=9,
        cooldown_bars=10,
    )


def _long_payload(**overrides) -> OptScanPayload:
    """
    Screenshot 1 base: BANKNIFTY long, z=+1.91 (price extended above anchor),
    11/12 filters, adx defaulting to 40 (trending).
    z_long_zone=False because +1.91 is NOT in the mean-reversion long zone [-3, -1].
    """
    base = dict(
        secret="test",
        v="optscan-v13",
        sym="BANKNIFTY",
        tf="9",
        dir="long",
        bar_time=1_700_000_000_000,
        price=45_000.0,
        atr=120.0,
        adx=40.0,
        filters=11,
        f_ema=True, f_rsi=True, f_vol=True, f_vwap=True, f_mvwap=True,
        f_band=True, f_cvd=True, f_st=True, f_macd=True, f_poc=True,
        f_mss=True, f_adx=False,   # 11 of 12
        z=1.91,
        z_long_zone=False,
        z_short_zone=True,
        z_bull_pa=True,
        z_bear_pa=False,
        fvg_ok=True,
        pb_ok=True,
        vol_ok=True,
        range_ratio=1.4,
        bars_since=15,
        hh=True,
        ll=False,
        ext_long=False,
        ext_short=False,
        mss_state=1,
    )
    base.update(overrides)
    return OptScanPayload(**base)


def _short_payload(**overrides) -> OptScanPayload:
    """
    Screenshot 2 base: BANKNIFTY short, z=-1.22 (price slightly below anchor),
    11/12 filters, adx defaulting to 40 (trending).
    z_short_zone=False because -1.22 is NOT in the mean-reversion short zone [+1, +3].
    """
    base = dict(
        secret="test",
        v="optscan-v13",
        sym="BANKNIFTY",
        tf="9",
        dir="short",
        bar_time=1_700_000_000_000,
        price=44_500.0,
        atr=110.0,
        adx=40.0,
        filters=11,
        f_ema=True, f_rsi=True, f_vol=True, f_vwap=True, f_mvwap=True,
        f_band=True, f_cvd=True, f_st=True, f_macd=True, f_poc=True,
        f_mss=True, f_adx=False,   # 11 of 12
        z=-1.22,
        z_long_zone=True,
        z_short_zone=False,
        z_bull_pa=False,
        z_bear_pa=True,
        fvg_ok=True,
        pb_ok=True,
        vol_ok=True,
        range_ratio=1.3,
        bars_since=20,
        hh=False,
        ll=True,
        ext_long=False,
        ext_short=False,
        mss_state=-1,
    )
    base.update(overrides)
    return OptScanPayload(**base)


# ──────────────────────── screenshot 1: long, z=+1.91 ───────────────────────


def test_long_trending_takes(gate):
    """adx=40 (trending) → z-gate suppressed → take."""
    d = gate.evaluate(_long_payload(adx=40.0))
    assert d.take is True
    assert d.regime == "trending"


def test_long_ranging_skips_z(gate):
    """adx=20 (ranging) → z=+1.91 outside long zone [-3,-1] → skip."""
    d = gate.evaluate(_long_payload(adx=20.0))
    assert d.take is False
    assert d.regime == "ranging"
    assert "z" in d.reason.lower()


# ──────────────────────── screenshot 2: short, z=-1.22 ──────────────────────


def test_short_trending_takes(gate):
    """adx=40 (trending) → z-gate suppressed → take."""
    d = gate.evaluate(_short_payload(adx=40.0))
    assert d.take is True
    assert d.regime == "trending"


def test_short_ranging_skips_z(gate):
    """adx=20 (ranging) → z=-1.22 outside short zone [+1,+3] → skip."""
    d = gate.evaluate(_short_payload(adx=20.0))
    assert d.take is False
    assert d.regime == "ranging"
    assert "z" in d.reason.lower()


# ──────────────────────────── cooldown ──────────────────────────────────────


def test_cooldown_blocks_second_take(gate):
    """
    Seed a prior taken long 5 bars ago (< cooldown_bars=10).
    The next long signal for the same sym+dir must be rejected.
    """
    incoming = _long_payload(adx=40.0)

    # Place the prior taken signal 5 bars before the incoming one.
    tf_ms = int(incoming.tf) * 60_000   # 9 min × 60 000 ms
    prior = _long_payload(adx=40.0, bar_time=incoming.bar_time - 5 * tf_ms)
    journal.log_optscan_signal(prior.model_dump(), take=True, regime="trending", reason="seeded")

    d = gate.evaluate(incoming)
    assert d.take is False
    assert "cooldown" in d.reason.lower()


# ──────────────────────────── min-filters ───────────────────────────────────


def test_min_filters_skips(gate):
    """filters=7 < min_filters=9 → skip regardless of regime or z-score."""
    d = gate.evaluate(_long_payload(adx=40.0, filters=7))
    assert d.take is False
    assert "filter" in d.reason.lower()
