"""
Position guard tests.

Covers: no conflict, same-direction block (long/short), opposite-direction
conflict (long/short), case-insensitive sym matching, and NIFTY/BANKNIFTY
independence.
"""

from datetime import datetime
import pytest

from backend.exit_monitor import OpenPosition
from backend.position_guard import check_position_conflicts


# ─────────────────────── factory ─────────────────────────────────────────────

def _pos(sym: str, option_type: str, position_id: str = None) -> OpenPosition:
    return OpenPosition(
        position_id=position_id or f"{sym}_{option_type}_1",
        sym=sym,
        expiry="03-Jul-2026",
        strike=52500,
        option_type=option_type,
        direction="long" if option_type == "CE" else "short",
        entry_premium=100.0,
        stop_premium=70.0,
        target_premium=150.0,
        entry_iv=0.18,
        entry_regime="trending",
        entry_time=datetime(2026, 6, 29, 9, 45),
        time_stop="15:15",
        mode="intraday",
        peak_premium=100.0,
    )


# ─────────────────────── no conflict ─────────────────────────────────────────

def test_empty_positions_is_clear():
    reason, pid = check_position_conflicts("BANKNIFTY", "long", {})
    assert reason is None
    assert pid is None


def test_different_sym_is_clear():
    pos = _pos("NIFTY", "CE")
    reason, pid = check_position_conflicts("BANKNIFTY", "long", {pos.position_id: pos})
    assert reason is None
    assert pid is None


def test_nifty_and_banknifty_are_independent():
    """NIFTY PE open should not block a BANKNIFTY long signal."""
    pos = _pos("NIFTY", "PE")
    reason, _ = check_position_conflicts("BANKNIFTY", "short", {pos.position_id: pos})
    assert reason is None


# ─────────────────────── same-direction block ─────────────────────────────────

def test_same_dir_long_blocks():
    pos = _pos("BANKNIFTY", "CE")
    reason, pid = check_position_conflicts("BANKNIFTY", "long", {pos.position_id: pos})
    assert reason == "position_already_open"
    assert pid == pos.position_id


def test_same_dir_short_blocks():
    pos = _pos("BANKNIFTY", "PE")
    reason, pid = check_position_conflicts("BANKNIFTY", "short", {pos.position_id: pos})
    assert reason == "position_already_open"
    assert pid == pos.position_id


# ─────────────────────── opposite-direction conflict ─────────────────────────

def test_opposite_long_signal_with_pe_open():
    """Long CE signal fires while PE (short) position is open → reversal advisory."""
    pos = _pos("BANKNIFTY", "PE")
    reason, pid = check_position_conflicts("BANKNIFTY", "long", {pos.position_id: pos})
    assert reason == "opposite_position_open"
    assert pid == pos.position_id


def test_opposite_short_signal_with_ce_open():
    """Short PE signal fires while CE (long) position is open → reversal advisory."""
    pos = _pos("BANKNIFTY", "CE")
    reason, pid = check_position_conflicts("BANKNIFTY", "short", {pos.position_id: pos})
    assert reason == "opposite_position_open"
    assert pid == pos.position_id


# ─────────────────────── edge cases ──────────────────────────────────────────

def test_sym_match_is_case_insensitive():
    pos = _pos("BANKNIFTY", "CE")
    reason, _ = check_position_conflicts("banknifty", "long", {pos.position_id: pos})
    assert reason == "position_already_open"
