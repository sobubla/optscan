"""
Exit gate — OR-triggered, continuous position monitor.

Checks six exit triggers every bar against a single open option position.
Any one trigger firing returns an ExitSignal; the caller (server.py scan_loop)
logs it to the journal (exit_suggestions) and surfaces it for human approval.

Principle: entry and exit are asymmetric.
  Entry = strict AND (every condition must agree → one careful decision).
  Exit  = responsive OR (any single trigger fires → exit immediately).

This module is fully separate from gate.py, strike_selector.py, and exit_manager.py.
It never calls any broker and never touches the DB.

Six OR-triggers (checked in priority order):
  1. premium_stop   — premium dropped to or below the advisory stop level
  2. premium_target — premium reached or exceeded the advisory target
  3. premium_trail  — premium dropped > EXIT_TRAIL_PCT from peak (once peak >= target)
  4. time_stop      — held too long (MAX_HOLD_MINUTES; tighter on expiry day)
  5. iv_crush       — IV dropped by >= EXIT_IV_CRUSH_DELTA from entry
  6. regime_flip    — current regime differs from entry regime; thesis gone
  7. eod_squareoff  — past EOD time (intraday mode only; non-negotiable)

Mode awareness:
  "intraday"   — all 7 trigger slots active, including EOD square-off
  "positional" — EOD square-off suppressed; other 6 triggers still apply
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── data types ──────────────────────────────────────


@dataclass
class OpenPosition:
    """
    Mutable state the exit monitor tracks for one open option position.

    Created when a human approves an entry suggestion; updated each bar
    (peak_premium is the only field mutated by check_exit).
    """
    position_id: str
    sym: str
    expiry: str           # OpenAlgo format: "DD-Mon-YYYY"
    strike: int
    option_type: str      # CE | PE
    direction: str        # long | short
    entry_premium: float
    stop_premium: float   # from EntrySuggestion.stop_premium
    target_premium: float # from EntrySuggestion.target_premium
    entry_iv: float       # annualised decimal at entry (e.g. 0.185)
    entry_regime: str     # trending | ranging at entry
    entry_time: datetime
    time_stop: str        # "HH:MM" (e.g. "15:15") or "" if no EOD stop
    mode: str             # intraday | positional
    peak_premium: float = field(default=0.0)


@dataclass
class MarketState:
    """Current market snapshot injected by the scan loop each bar."""
    current_premium: float
    current_iv: float        # annualised decimal
    current_regime: str      # trending | ranging
    now: datetime


@dataclass
class ExitSignal:
    """
    Emitted when any exit trigger fires. Goes to the journal (exit_suggestions)
    and the approval path — never directly to any broker.
    """
    position_id: str
    trigger: str          # one of: premium_stop | premium_target | premium_trail |
                          #         time_stop | iv_crush | regime_flip | eod_squareoff
    current_premium: float
    entry_premium: float
    pnl_pct: float        # (current - entry) / entry * 100, rounded 2dp
    reason: str           # tooltip-style human-readable message
    mode: str
    sym: str = ""
    strike: int = 0
    option_type: str = ""


# ─────────────────────────── helpers ─────────────────────────────────────────


def _parse_expiry(expiry_str: str) -> date:
    """Parse OpenAlgo expiry string "DD-Mon-YYYY" → date."""
    return datetime.strptime(expiry_str, "%d-%b-%Y").date()


def _signal(
    position: OpenPosition,
    trigger: str,
    state: MarketState,
    pnl_pct: float,
    detail: str,
) -> ExitSignal:
    leg = f"{position.direction.upper()} {position.option_type} {position.strike}"
    reason = f"{leg} EXIT — {trigger}: {detail} (pnl={pnl_pct:+.1f}%)"
    return ExitSignal(
        position_id=position.position_id,
        trigger=trigger,
        current_premium=state.current_premium,
        entry_premium=position.entry_premium,
        pnl_pct=pnl_pct,
        reason=reason,
        mode=position.mode,
        sym=position.sym,
        strike=position.strike,
        option_type=position.option_type,
    )


# ─────────────────────────── main function ───────────────────────────────────


def check_exit(
    position: OpenPosition,
    state: MarketState,
    config,
) -> Optional[ExitSignal]:
    """
    Check all OR-triggers in priority order.

    Side effect: updates position.peak_premium in-place (caller's mutable object).
    Returns an ExitSignal on the first trigger that fires, or None (hold).

    config attributes used:
      EXIT_TRAIL_PCT, EXIT_IV_CRUSH_DELTA, EXIT_HOLD_MINUTES_EXPIRY,
      MAX_HOLD_MINUTES, ENTRY_EOD_SQUAREOFF
    """
    # Keep running peak
    position.peak_premium = max(position.peak_premium, state.current_premium)

    pnl_pct = round(
        (state.current_premium - position.entry_premium) / position.entry_premium * 100,
        2,
    )

    # ── 1. Premium stop ───────────────────────────────────────────────────────
    if state.current_premium <= position.stop_premium:
        return _signal(
            position, "premium_stop", state, pnl_pct,
            f"current={state.current_premium:.1f} <= stop={position.stop_premium:.1f}",
        )

    # ── 2a. Premium target ────────────────────────────────────────────────────
    if state.current_premium >= position.target_premium:
        return _signal(
            position, "premium_target", state, pnl_pct,
            f"current={state.current_premium:.1f} >= target={position.target_premium:.1f}",
        )

    # ── 2b. Premium trail (activates once peak >= target) ────────────────────
    if position.peak_premium >= position.target_premium:
        trail_level = position.peak_premium * (1.0 - config.EXIT_TRAIL_PCT)
        if state.current_premium <= trail_level:
            return _signal(
                position, "premium_trail", state, pnl_pct,
                f"current={state.current_premium:.1f} <= trail={trail_level:.1f} "
                f"(peak={position.peak_premium:.1f}, retracement={config.EXIT_TRAIL_PCT:.0%})",
            )

    # ── 3. Theta / time stop ──────────────────────────────────────────────────
    held_minutes = (state.now - position.entry_time).total_seconds() / 60
    is_expiry_day = (state.now.date() == _parse_expiry(position.expiry))
    hold_limit = config.EXIT_HOLD_MINUTES_EXPIRY if is_expiry_day else config.MAX_HOLD_MINUTES
    if held_minutes >= hold_limit:
        return _signal(
            position, "time_stop", state, pnl_pct,
            f"held={held_minutes:.0f}m >= limit={hold_limit}m"
            + (" (expiry day)" if is_expiry_day else ""),
        )

    # ── 4. IV collapse ────────────────────────────────────────────────────────
    if position.entry_iv > 0:
        iv_drop = position.entry_iv - state.current_iv
        if iv_drop >= config.EXIT_IV_CRUSH_DELTA:
            return _signal(
                position, "iv_crush", state, pnl_pct,
                f"IV {position.entry_iv:.3f}→{state.current_iv:.3f} "
                f"(drop={iv_drop:.3f} >= {config.EXIT_IV_CRUSH_DELTA:.3f})",
            )

    # ── 5. Regime flip ────────────────────────────────────────────────────────
    if state.current_regime != position.entry_regime:
        return _signal(
            position, "regime_flip", state, pnl_pct,
            f"{position.entry_regime}→{state.current_regime}; thesis gone",
        )

    # ── 6. EOD square-off (intraday only) ────────────────────────────────────
    if position.mode == "intraday" and position.time_stop:
        eod = datetime.strptime(position.time_stop, "%H:%M").time()
        if state.now.time() >= eod:
            return _signal(
                position, "eod_squareoff", state, pnl_pct,
                f"{state.now.strftime('%H:%M')} >= {position.time_stop} (EOD)",
            )

    return None
