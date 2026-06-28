"""
GEX (Gamma Exposure) analytics.

Convention (CLAUDE.md domain note):
  dealers long call gamma, short put gamma.
  Call contribution: positive (dealers stabilise price moves — ranging)
  Put  contribution: negative (dealers amplify  price moves — trending)

per-strike GEX = (call_OI × call_γ − put_OI × put_γ) × spot² × lot_size
net GEX        = Σ per-strike GEX across all strikes
regime signal  : net_gex ≥ 0 → ranging; net_gex < 0 → trending

gamma_flip: the interpolated strike where the cumulative per-strike GEX
profile (sorted ascending by strike) crosses zero.  Useful as a diagnostic
level — when spot is above the flip, net GEX tends to be negative (trending);
below it, positive (ranging).  The actual regime decision uses net_gex sign
on the live chain, not distance from the flip.
"""

from collections import defaultdict
from typing import Optional


def strike_gex(
    call_oi: int,
    put_oi: int,
    call_gamma: float,
    put_gamma: float,
    spot: float,
    lot_size: float = 1.0,
) -> float:
    """Dealer GEX at one strike.  Calls positive, puts negative."""
    return (call_oi * call_gamma - put_oi * put_gamma) * spot ** 2 * lot_size


def _group_by_strike(chain: list) -> dict:
    """Collect flat CE/PE entries into per-strike dicts."""
    by_strike: dict = defaultdict(
        lambda: {"call_oi": 0, "call_gamma": 0.0, "put_oi": 0, "put_gamma": 0.0}
    )
    for row in chain:
        k = row["strike"]
        if row["type"] == "CE":
            by_strike[k]["call_oi"] = row["oi"]
            by_strike[k]["call_gamma"] = row["gamma"]
        elif row["type"] == "PE":
            by_strike[k]["put_oi"] = row["oi"]
            by_strike[k]["put_gamma"] = row["gamma"]
    return dict(by_strike)


def net_gex(chain: list, spot: float, lot_size: float = 1.0) -> float:
    """Net dealer GEX summed across all strikes in the chain."""
    by_strike = _group_by_strike(chain)
    total = 0.0
    for v in by_strike.values():
        total += strike_gex(
            v["call_oi"], v["put_oi"],
            v["call_gamma"], v["put_gamma"],
            spot, lot_size,
        )
    return total


def gamma_flip(chain: list, spot: float, lot_size: float = 1.0) -> Optional[float]:
    """
    Return the linearly-interpolated strike where cumulative per-strike GEX
    (sorted ascending) crosses zero.  Returns None if no crossing exists.
    """
    by_strike = _group_by_strike(chain)
    if not by_strike:
        return None

    gex_profile = sorted(
        (
            (k, strike_gex(v["call_oi"], v["put_oi"],
                           v["call_gamma"], v["put_gamma"],
                           spot, lot_size))
            for k, v in by_strike.items()
        ),
        key=lambda t: t[0],
    )

    cum = 0.0
    prev_cum: Optional[float] = None
    prev_k: Optional[float] = None
    for k, g in gex_profile:
        cum += g
        if prev_cum is not None and (prev_cum >= 0) != (cum >= 0):
            # linear interpolation of the zero crossing
            return prev_k + (0.0 - prev_cum) / (cum - prev_cum) * (k - prev_k)
        prev_cum = cum
        prev_k = float(k)
    return None
