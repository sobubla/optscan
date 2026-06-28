"""
Position guard — checks active positions before generating an entry suggestion.

Two outcomes:
  "position_already_open"  — same sym+direction already held. Block suggestion silently.
  "opposite_position_open" — opposite direction held. Reversal signal: surface advisory.
  None                     — no conflict, proceed to strike selector.

Kept as a pure function so it can be tested without importing server.py.
"""

from typing import Optional


def check_position_conflicts(
    sym: str,
    direction: str,
    active_positions: dict,
) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (blocked_reason, position_id) or (None, None) if clear.

    sym            — e.g. "BANKNIFTY" (case-insensitive)
    direction      — "long" or "short"
    active_positions — dict of {position_id: OpenPosition} from the exit monitor
    """
    same_type = "CE" if direction == "long" else "PE"
    opp_type  = "PE" if direction == "long" else "CE"

    for pos in active_positions.values():
        if pos.sym.upper() != sym.upper():
            continue
        if pos.option_type == same_type:
            return "position_already_open", pos.position_id
        if pos.option_type == opp_type:
            return "opposite_position_open", pos.position_id

    return None, None
