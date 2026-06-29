"""
Strike-and-premium entry layer.

Input: gate take = {dir, regime, spot, atr, sym} + enriched option chain.
Output: EntrySuggestion → journal (entry_suggestions table) + approval path.

Pipeline (all filters are AND; first failure returns None):
  1. select_strike  — delta band + OI + optional spread filter
  2. iv_percentile  — skip if IV-rich vs recent ATM IV history
  3. compute_lots   — premium-at-risk sizing (premium paid = max loss for long options)
  4. optionlab eval — soft: P&L profile + PoP; None if not installed or any error
  5. EntrySuggestion returned to caller

Mode:
  "intraday"   — the only enabled mode; time_stop = ENTRY_EOD_SQUAREOFF
  "positional" — config vars present; raises NotImplementedError until validated

Hard rule: this module never calls any broker API. It computes and returns.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── output type ─────────────────────────────────────


@dataclass
class EntrySuggestion:
    sym: str
    expiry: str             # OpenAlgo format: "DD-Mon-YYYY", e.g. "01-Feb-2024"
    strike: int
    option_type: str        # CE | PE
    action: str             # BUY
    entry_premium: float    # LTP used for sizing (= mid when bid/ask available)
    lots: int
    delta: float            # abs value
    iv: float               # annualised decimal, e.g. 0.185 = 18.5%
    stop_premium: float     # advisory: entry * (1 - ENTRY_STOP_PCT)
    target_premium: float   # advisory: entry * (1 + ENTRY_TARGET_PCT)
    time_stop: str          # advisory: "15:15" (intraday) or "" (positional)
    rationale: str
    regime: str
    mode: str               # intraday | positional
    optionlab: Optional[dict] = None   # {pop, max_profit, max_loss, ...} or None


# ─────────────────────────── helpers ─────────────────────────────────────────


def _dir_to_option_type(direction: str) -> str:
    """'long' → 'CE', 'short' → 'PE'."""
    return "CE" if direction == "long" else "PE"


def _dte(expiry_str: str, today: date) -> int:
    """
    Parse OpenAlgo expiry string "DD-Mon-YYYY" → integer DTE (calendar days).
    Returns 0 if expiry is today or already past.
    """
    expiry_date = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    return max(0, (expiry_date - today).days)


def next_weekly_expiry(today: date, min_dte: int = 2) -> str:
    """
    Return the nearest weekly expiry (Thursday) with DTE >= min_dte.

    Indian NSE weekly options expire on Thursday. If today is Thursday and
    DTE=0 < min_dte, rolls to next Thursday.

    Returns the expiry in OpenAlgo format: "%d-%b-%Y", e.g. "01-Feb-2024".
    """
    # weekday(): Mon=0 … Thu=3 … Sun=6
    days_until_thursday = (3 - today.weekday()) % 7
    candidate = today + timedelta(days=days_until_thursday)
    dte = (candidate - today).days
    if dte < min_dte:
        candidate += timedelta(days=7)
    return candidate.strftime("%d-%b-%Y")


# ─────────────────────── strike selection ────────────────────────────────────


def select_strike(
    chain: list,
    expiry: str,
    direction: str,
    delta_band: tuple[float, float] = (0.35, 0.50),
    min_oi: int = 50_000,
    max_spread_pct: float = 5.0,
) -> Optional[dict]:
    """
    Pick the chain row with abs(delta) nearest the band midpoint that passes:
      - option type matches direction (CE for long, PE for short)
      - expiry matches
      - abs(delta) in [delta_band[0], delta_band[1]]
      - OI >= min_oi
      - spread_pct <= max_spread_pct (only if the spread_pct field is present on the row)

    Returns the winning row dict, or None if nothing passes.
    """
    opt_type = _dir_to_option_type(direction)
    if opt_type == "CE":
        delta_key  = "call_delta"
        oi_key     = "call_oi"
        spread_key = "call_spread_pct"
    else:
        delta_key  = "put_delta"
        oi_key     = "put_oi"
        spread_key = "put_spread_pct"

    lo, hi = delta_band
    midpoint = (lo + hi) / 2.0

    candidates = []
    for row in chain:
        if row.get("expiry") != expiry:
            continue
        abs_delta = abs(row.get(delta_key, 0.0))
        if not (lo <= abs_delta <= hi):
            continue
        if row.get(oi_key, 0) < min_oi:
            continue
        spread = row.get(spread_key)
        if spread is not None and spread > max_spread_pct:
            continue
        candidates.append(row)

    if not candidates:
        return None

    return min(candidates, key=lambda r: abs(abs(r[delta_key]) - midpoint))


# ─────────────────────── IV-rich check ───────────────────────────────────────


def iv_percentile(strike_iv: float, history: list[float]) -> int:
    """
    Percentile rank of strike_iv within history (0 = cheapest, 100 = richest).
    Returns 100 if history is empty (conservative: unknown IV treated as rich).
    """
    if not history:
        return 100
    below = sum(1 for h in history if h < strike_iv)
    return int(below / len(history) * 100)


# ─────────────────────── sizing ──────────────────────────────────────────────


def compute_lots(
    premium: float,
    lot_size: int,
    equity: float,
    risk_pct: float,
) -> int:
    """
    For a long option, premium paid is the maximum loss.
    Size so that max_loss = equity * risk_pct.

    lots = floor(equity * risk_pct / (premium * lot_size)), minimum 1.
    """
    risk_capital = equity * risk_pct
    cost_per_lot = premium * lot_size
    if cost_per_lot <= 0:
        return 1
    return max(1, int(risk_capital / cost_per_lot))


# ─────────────────────── optionlab evaluation ────────────────────────────────


def _evaluate_optionlab(
    row: dict,
    direction: str,
    spot: float,
    dte: int,
    risk_free_rate: float = 0.065,
) -> Optional[dict]:
    """
    Soft wrapper: imports optionlab at call time; returns None if not installed or errors.

    Uses Black-Scholes (optionlab limitation) which is acceptable for display-only evaluation
    of European index options. For IV computation, vollib Black-76 is used separately.
    discard_nonbusiness_days=False avoids US holiday calendar dependency.
    """
    try:
        from optionlab import Inputs, run_strategy  # noqa: PLC0415
    except ImportError:
        return None

    opt_type = _dir_to_option_type(direction)
    iv_key  = "call_iv"  if opt_type == "CE" else "put_iv"
    ltp_key = "call_ltp" if opt_type == "CE" else "put_ltp"

    iv      = row.get(iv_key, 0.0)
    premium = row.get(ltp_key, 0.0)

    if iv <= 0 or premium <= 0 or dte <= 0 or spot <= 0:
        return None

    try:
        inputs = Inputs(
            stock_price=spot,
            volatility=iv,
            interest_rate=risk_free_rate,
            dividend_yield=0.0,
            min_stock=spot * 0.75,
            max_stock=spot * 1.25,
            days_to_target_date=dte,
            discard_nonbusiness_days=False,
            strategy=[
                {
                    "type": "call" if opt_type == "CE" else "put",
                    "strike": float(row["strike"]),
                    "premium": premium,
                    "action": "buy",
                    "n": 1,
                }
            ],
        )
        out = run_strategy(inputs)
        return {
            "pop":           round(out.probability_of_profit, 4),
            "max_profit":    round(out.maximum_return_in_the_domain, 2),
            "max_loss":      round(out.minimum_return_in_the_domain, 2),
            "strategy_cost": round(out.strategy_cost, 2),
            "delta":         round(out.delta[0], 4)  if out.delta else None,
            "theta":         round(out.theta[0], 4)  if out.theta else None,
            "vega":          round(out.vega[0], 4)   if out.vega  else None,
        }
    except Exception as exc:
        logger.debug("optionlab evaluation failed for %s strike=%s: %s",
                     direction, row.get("strike"), exc)
        return None


# ─────────────────────── main entry point ────────────────────────────────────


def evaluate(
    *,
    sym: str,
    direction: str,
    regime: str,
    spot: float,
    atr: float,
    chain: list,
    iv_history: list[float],
    today: Optional[date] = None,
    mode: str = "intraday",
    config,
) -> Optional[EntrySuggestion]:
    """
    Turn a gate take into an EntrySuggestion, or return None if nothing passes filters.

    chain must already be for the correct expiry (server.py fetches it via
    next_weekly_expiry() before calling evaluate). The expiry is read from
    chain row fields, not recomputed here.

    mode="positional" raises NotImplementedError — not yet enabled.
    """
    if mode == "positional":
        raise NotImplementedError("positional mode not yet enabled")

    if today is None:
        today = date.today()

    # -- Derive expiry from chain rows (all rows share the same expiry) -------
    expiry: Optional[str] = None
    for row in chain:
        if row.get("expiry"):
            expiry = row["expiry"]
            break
    if not expiry:
        logger.warning("ENTRY SKIP — chain has no expiry field for %s %s", sym, direction)
        return None

    # -- Lot size: prefer live value from chain rows (OpenAlgo carries it),
    # fall back to config for the Fyers path where chain rows have no lotsize.
    lot_size = next((r["lotsize"] for r in chain if r.get("lotsize", 0) > 0), None)
    if not lot_size and hasattr(config, "INDICES"):
        lot_size = config.INDICES.get(sym.upper(), {}).get("lot_size", 1) or 1
    lot_size = lot_size or 1

    # 1. Strike selection -------------------------------------------------------
    row = select_strike(
        chain=chain,
        expiry=expiry,
        direction=direction,
        delta_band=(config.ENTRY_DELTA_BAND_MIN, config.ENTRY_DELTA_BAND_MAX),
        min_oi=config.ENTRY_MIN_OI,
        max_spread_pct=config.ENTRY_MAX_SPREAD_PCT,
    )
    if row is None:
        logger.info("ENTRY SKIP — no strike in delta band for %s %s", sym, direction)
        return None

    opt_type  = _dir_to_option_type(direction)
    iv_key    = "call_iv"    if opt_type == "CE" else "put_iv"
    delta_key = "call_delta" if opt_type == "CE" else "put_delta"
    ltp_key   = "call_ltp"  if opt_type == "CE" else "put_ltp"

    # 2. IV-rich check ---------------------------------------------------------
    strike_iv = row.get(iv_key, 0.0)
    iv_pct    = iv_percentile(strike_iv, iv_history)
    if iv_pct > config.ENTRY_IV_PERCENTILE_REJECT:
        logger.info(
            "ENTRY SKIP — IV rich: %s %s strike=%d iv=%.1f%% pct=%d > threshold=%d",
            sym, direction, row["strike"],
            strike_iv * 100, iv_pct, config.ENTRY_IV_PERCENTILE_REJECT,
        )
        return None

    # 3. Premium + sizing -------------------------------------------------------
    premium = row.get(ltp_key, 0.0)
    if premium <= 0:
        logger.info("ENTRY SKIP — zero or negative LTP for %s %s strike=%d",
                    sym, direction, row["strike"])
        return None

    lots = compute_lots(premium, lot_size, config.EQUITY_RUPEES, config.ENTRY_RISK_PCT)

    # 4. optionlab evaluation (soft) -------------------------------------------
    dte       = _dte(expiry, today)
    ol_result = _evaluate_optionlab(row, direction, spot, dte)

    # 5. Build suggestion -------------------------------------------------------
    stop_premium   = round(premium * (1.0 - config.ENTRY_STOP_PCT), 2)
    target_premium = round(premium * (1.0 + config.ENTRY_TARGET_PCT), 2)
    time_stop      = config.ENTRY_EOD_SQUAREOFF if mode == "intraday" else ""
    abs_delta      = abs(row.get(delta_key, 0.0))

    rationale = (
        f"{direction.upper()} {opt_type} {row['strike']} "
        f"delta={abs_delta:.2f} iv={strike_iv*100:.1f}% "
        f"iv_pct={iv_pct} regime={regime}"
    )

    return EntrySuggestion(
        sym=sym,
        expiry=expiry,
        strike=row["strike"],
        option_type=opt_type,
        action="BUY",
        entry_premium=premium,
        lots=lots,
        delta=abs_delta,
        iv=strike_iv,
        stop_premium=stop_premium,
        target_premium=target_premium,
        time_stop=time_stop,
        rationale=rationale,
        regime=regime,
        mode=mode,
        optionlab=ol_result,
    )
