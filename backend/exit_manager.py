"""
Exit Manager - the module that solves your "letting winners run / cutting losers" problem.

This is NOT an order execution module. It tracks positions you've taken and tells you
WHEN to exit based on rules you cannot override in the moment of the trade.

Workflow:
1. When you enter a trade, register it via add_position()
2. Scanner ticks call check_exits() every 30 seconds
3. When an exit triggers, an alert is pushed to the dashboard
4. YOU manually exit (the system never places orders without explicit user action)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class Position:
    position_id: str
    index: str
    strike: int
    option_type: str          # CE or PE
    entry_price: float
    quantity: int             # number of lots
    lot_size: int
    entry_time: datetime
    entry_iv: float

    # Tracking
    current_price: float = 0
    current_iv: float = 0
    peak_price: float = 0     # highest price seen since entry
    peak_pct: float = 0       # highest profit % seen
    locked_profit_pct: float = 0   # currently locked-in profit floor
    status: str = "open"      # open / closed / exit_triggered
    exit_reason: Optional[str] = None
    exit_time: Optional[datetime] = None

    def to_dict(self):
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        if self.exit_time:
            d["exit_time"] = self.exit_time.isoformat()
        return d


class ExitManager:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.exit_alerts: List[Dict] = []  # queue for dashboard

    def add_position(self, **kwargs) -> str:
        """Register a new position. Returns position_id."""
        pos_id = f"{kwargs['index']}_{kwargs['strike']}{kwargs['option_type']}_{int(datetime.now().timestamp())}"
        position = Position(
            position_id=pos_id,
            entry_time=datetime.now(),
            peak_price=kwargs["entry_price"],
            **kwargs
        )
        self.positions[pos_id] = position
        logger.info(f"Position opened: {pos_id} @ {position.entry_price}")
        return pos_id

    def update_position(self, pos_id: str, current_price: float, current_iv: float):
        """Update live price/IV. Call this on every scan tick."""
        if pos_id not in self.positions:
            return
        p = self.positions[pos_id]
        if p.status != "open":
            return

        p.current_price = current_price
        p.current_iv = current_iv

        pct_change = ((current_price - p.entry_price) / p.entry_price) * 100
        if pct_change > p.peak_pct:
            p.peak_pct = pct_change
            p.peak_price = current_price

        # Update locked profit tier
        for tier in settings.PROFIT_LOCK_TIERS:
            if p.peak_pct >= tier["profit_pct"] and p.locked_profit_pct < tier["lock_pct"]:
                p.locked_profit_pct = tier["lock_pct"]
                logger.info(f"{pos_id}: profit locked at +{tier['lock_pct']}%")

    def check_exits(self) -> List[Dict]:
        """
        Run all exit rules on every open position.
        Returns list of exit alerts to surface on the dashboard.
        """
        new_alerts = []
        for pos_id, p in list(self.positions.items()):
            if p.status != "open":
                continue

            exit_signal = self._evaluate_exit_rules(p)
            if exit_signal:
                p.status = "exit_triggered"
                p.exit_reason = exit_signal["reason"]
                p.exit_time = datetime.now()

                alert = {
                    "position_id": pos_id,
                    "index": p.index,
                    "contract": f"{p.strike} {p.option_type}",
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "pnl_pct": round(((p.current_price - p.entry_price) / p.entry_price) * 100, 2),
                    "reason": exit_signal["reason"],
                    "urgency": exit_signal["urgency"],
                    "message": exit_signal["message"],
                    "timestamp": datetime.now().isoformat(),
                }
                new_alerts.append(alert)
                self.exit_alerts.append(alert)
                logger.warning(f"EXIT TRIGGERED {pos_id}: {exit_signal['reason']}")

        return new_alerts

    def _evaluate_exit_rules(self, p: Position) -> Optional[Dict]:
        """Apply all 4 exit rules. First trigger wins."""
        pct_change = ((p.current_price - p.entry_price) / p.entry_price) * 100
        held_minutes = (datetime.now() - p.entry_time).total_seconds() / 60

        # Rule 1: Hard stop loss
        if pct_change <= -settings.HARD_STOP_LOSS_PCT:
            return {
                "reason": "hard_stop_loss",
                "urgency": "critical",
                "message": f"SL HIT: down {pct_change:.1f}%. Exit immediately.",
            }

        # Rule 2: Locked profit floor breached
        if p.locked_profit_pct > 0 and pct_change < p.locked_profit_pct:
            return {
                "reason": "profit_lock_breached",
                "urgency": "high",
                "message": f"Locked profit broken. Peak was +{p.peak_pct:.1f}%, now +{pct_change:.1f}%. Exit and protect +{p.locked_profit_pct}%.",
            }

        # Rule 3: Time-based - no progress
        if held_minutes >= settings.MAX_HOLD_MINUTES:
            return {
                "reason": "time_decay",
                "urgency": "medium",
                "message": f"Held {held_minutes:.0f} min, peak only +{p.peak_pct:.1f}%. Theta is winning. Exit.",
            }
        if held_minutes >= 15 and p.peak_pct < settings.NO_PROGRESS_THRESHOLD_PCT:
            return {
                "reason": "no_progress",
                "urgency": "medium",
                "message": f"15 min in, never crossed +{settings.NO_PROGRESS_THRESHOLD_PCT}%. Trade isn't working.",
            }

        # Rule 4: Volatility crush
        if p.entry_iv > 0:
            iv_drop = p.entry_iv - p.current_iv
            if iv_drop >= settings.IV_DROP_EXIT_PCT:
                return {
                    "reason": "iv_crush",
                    "urgency": "high",
                    "message": f"IV crashed from {p.entry_iv:.1f} to {p.current_iv:.1f}. Volatility crush in progress.",
                }

        return None

    def get_open_positions(self) -> List[Dict]:
        return [p.to_dict() for p in self.positions.values() if p.status == "open"]

    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        return self.exit_alerts[-limit:]

    def close_position(self, pos_id: str):
        """Manually mark a position as closed (after you actually exit)."""
        if pos_id in self.positions:
            self.positions[pos_id].status = "closed"
            if not self.positions[pos_id].exit_time:
                self.positions[pos_id].exit_time = datetime.now()
