"""
IV and Greeks migration tests: Black-Scholes NR → Black-76 via vollib.

Compares compute_iv / compute_greeks (backend/fyers_client.py) against
vollib Black-76 evaluated directly, on a handful of realistic near-ATM
BANKNIFTY weekly-option fixtures.

Expected to FAIL before the migration (ModuleNotFoundError or wrong values).
After migration all 12 cases should be green.
"""

import math

import pytest

# vollib.black = Black-76 (forward-based). Correct import path; py_vollib is deprecated.
# Argument order note:
#   implied_volatility(price, F, K, r, t, flag)  -- r before t
#   delta/gamma/theta/vega(flag, F, K, t, r, sigma)  -- t before r
# vollib theta already divides by 365 (per calendar day).
# vollib vega already multiplies by 0.01 (per 1% IV change).
from vollib.black.implied_volatility import implied_volatility as bv_iv
from vollib.black.greeks.analytical import delta as bv_delta
from vollib.black.greeks.analytical import gamma as bv_gamma
from vollib.black.greeks.analytical import theta as bv_theta
from vollib.black.greeks.analytical import vega  as bv_vega

from backend.fyers_client import compute_greeks, compute_iv

# ──────────────────────── constants ──────────────────────────────────────────

SPOT = 44_500.0        # BANKNIFTY spot
DTE  = 3.0             # days to expiry (Wednesday for Thursday weekly)
RATE = 0.065           # RBI repo rate ≈ 6.5%
T    = DTE / 365.0
F    = SPOT * math.exp(RATE * T)   # forward price; B76 input

# Near-ATM BANKNIFTY option prices. Prices correspond to roughly 6-7% IV at
# 3 DTE — lower than typical live IV, but internally consistent for round-trip
# testing purposes. The tests verify solver agreement, not realism.
FIXTURES = [
    # (strike,  is_call, market_price)
    (44_300, True,  235.0),   # 200pt ITM call
    (44_500, True,  120.0),   # ATM call
    (44_700, True,   48.0),   # 200pt OTM call
    (44_300, False,  35.0),   # 200pt OTM put
    (44_500, False, 118.0),   # ATM put
    (44_700, False, 244.0),   # 200pt ITM put
]

# ──────────────────────── tolerances ─────────────────────────────────────────
# After migration both sides call the same vollib function, so agreement is
# floating-point exact. Tolerances are generous to survive any future changes
# that keep the same public convention.
IV_TOL    = 0.005   # 0.5 IV points
DELTA_TOL = 0.005
GAMMA_TOL = 1e-4
THETA_TOL = 2.0     # rupees per calendar day
VEGA_TOL  = 0.5     # rupees per 1% IV move

# ──────────────────────── helpers ────────────────────────────────────────────

def _b76_greeks(strike: float, is_call: bool, sigma: float) -> dict:
    """Reference Black-76 Greeks, matching the output convention of compute_greeks."""
    flag = 'c' if is_call else 'p'
    return {
        # vollib greeks use (flag, F, K, t, r, sigma) — t before r
        "delta": bv_delta(flag, F, strike, T, RATE, sigma),
        "gamma": bv_gamma(flag, F, strike, T, RATE, sigma),
        # vollib theta already per calendar day (divides by 365 internally)
        "theta": bv_theta(flag, F, strike, T, RATE, sigma),
        # vollib vega already per 1% IV change (multiplies by 0.01 internally)
        "vega":  bv_vega (flag, F, strike, T, RATE, sigma),
    }


# ──────────────────────── tests ──────────────────────────────────────────────

@pytest.mark.parametrize("strike,is_call,price", FIXTURES)
def test_iv_agrees_with_black76(strike, is_call, price):
    """compute_iv must agree with vollib Black-76 within IV_TOL."""
    flag = 'c' if is_call else 'p'

    # vollib IV: implied_volatility(price, F, K, r, t, flag) — r before t
    iv_ref = bv_iv(price, F, strike, RATE, T, flag)
    iv_cur = compute_iv(price, SPOT, strike, DTE, is_call, RATE)

    assert iv_cur > 0, f"compute_iv returned 0 for strike={strike} {'C' if is_call else 'P'}"
    assert abs(iv_cur - iv_ref) < IV_TOL, (
        f"IV mismatch strike={strike} {'C' if is_call else 'P'}: "
        f"current={iv_cur:.4f}  b76={iv_ref:.4f}  diff={abs(iv_cur-iv_ref):.4f}"
    )


@pytest.mark.parametrize("strike,is_call,price", FIXTURES)
def test_greeks_agree_with_black76(strike, is_call, price):
    """compute_greeks must agree with vollib Black-76 Greeks within per-Greek tolerances."""
    flag = 'c' if is_call else 'p'
    # derive sigma from the fixture price so both sides are evaluated at the same vol
    sigma = bv_iv(price, F, strike, RATE, T, flag)

    ref = _b76_greeks(strike, is_call, sigma)
    cur = compute_greeks(SPOT, strike, DTE, sigma, is_call, RATE)

    assert abs(cur["delta"] - ref["delta"]) < DELTA_TOL, (
        f"delta mismatch strike={strike}: current={cur['delta']}  b76={ref['delta']:.4f}"
    )
    assert abs(cur["gamma"] - ref["gamma"]) < GAMMA_TOL, (
        f"gamma mismatch strike={strike}: current={cur['gamma']}  b76={ref['gamma']:.6f}"
    )
    assert abs(cur["theta"] - ref["theta"]) < THETA_TOL, (
        f"theta mismatch strike={strike}: current={cur['theta']}  b76={ref['theta']:.4f}"
    )
    assert abs(cur["vega"] - ref["vega"]) < VEGA_TOL, (
        f"vega mismatch strike={strike}: current={cur['vega']}  b76={ref['vega']:.4f}"
    )
