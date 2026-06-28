"""
Fyers data layer. Handles authentication, option chain fetching, IV computation.

Fyers API docs: https://myapi.fyers.in/docsv3
Requires: pip install fyers-apiv3

IMPORTANT NOTES on Fyers limitations (as of 2026):
1. Fyers `optionchain` API does NOT return IV or Greeks. We compute them locally
   via Black-Scholes + Newton-Raphson solver below.
2. The `oich` field in Fyers response is RAW OI change (not percentage). We compute
   the actual percent ourselves.
3. Greeks (delta/gamma/theta/vega) are computed only for ATM ±2 strikes to save
   compute. Other strikes get zeros — they're not used for confluence anyway.
"""

import json
import logging
import math
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

# Persistent IV history file — survives restarts
IV_HISTORY_PATH = Path(__file__).parent.parent / "data" / "iv_history.json"


# ============================================================
# BLACK-SCHOLES IV SOLVER
# ============================================================
def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes theoretical price."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes vega (∂price/∂sigma). Same for calls and puts."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return S * _norm_pdf(d1) * math.sqrt(T)


def compute_iv(price: float, spot: float, strike: float, days_to_expiry: float,
               is_call: bool, risk_free_rate: float = 0.065) -> float:
    """
    Implied volatility via Newton-Raphson.
    Returns annualized IV as decimal (0.18 = 18%). Returns 0.0 if not solvable.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or days_to_expiry <= 0:
        return 0.0

    T = days_to_expiry / 365.0
    intrinsic = max(spot - strike, 0) if is_call else max(strike - spot, 0)
    if price < intrinsic:
        return 0.0  # below intrinsic = arbitrage / stale price

    sigma = 0.30  # initial guess: 30% IV
    for _ in range(50):
        bs_price = _bs_price(spot, strike, T, risk_free_rate, sigma, is_call)
        vega = _bs_vega(spot, strike, T, risk_free_rate, sigma)
        if vega < 1e-8:
            return 0.0
        diff = bs_price - price
        if abs(diff) < 0.01:  # tolerance: 1 paisa
            return max(min(sigma, 5.0), 0.0)  # clamp to reasonable range
        sigma = sigma - diff / vega
        if sigma <= 0 or sigma > 5.0:
            return 0.0  # diverged
    return 0.0  # didn't converge in 50 iters


def compute_greeks(spot: float, strike: float, days_to_expiry: float, sigma: float,
                   is_call: bool, risk_free_rate: float = 0.065) -> Dict:
    """Compute delta, gamma, theta, vega from Black-Scholes."""
    if sigma <= 0 or days_to_expiry <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    T = days_to_expiry / 365.0
    r = risk_free_rate
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)

    if is_call:
        delta = _norm_cdf(d1)
        theta = (-spot * pdf_d1 * sigma / (2 * math.sqrt(T))
                 - r * strike * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-spot * pdf_d1 * sigma / (2 * math.sqrt(T))
                 + r * strike * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0

    gamma = pdf_d1 / (spot * sigma * math.sqrt(T))
    vega = spot * pdf_d1 * math.sqrt(T) / 100.0  # per 1% IV change

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def days_to_nearest_expiry(index_key: str) -> float:
    """
    Best-effort estimate of days to nearest expiry.
    NIFTY/SENSEX = weekly (Thu/Tue); BANKNIFTY now monthly only.
    """
    today = date.today()
    weekday = today.weekday()  # Mon=0, Tue=1, ..., Sun=6

    if index_key == "NIFTY":
        # Weekly expiry on Thursday
        days_ahead = (3 - weekday) % 7
        days_ahead = days_ahead if days_ahead > 0 else 7
    elif index_key == "SENSEX":
        # Weekly expiry on Tuesday
        days_ahead = (1 - weekday) % 7
        days_ahead = days_ahead if days_ahead > 0 else 7
    else:
        # BANKNIFTY and others: assume monthly, last Thursday of month
        # For simplicity, pick next Thursday + 28 days as approximation
        days_ahead = (3 - weekday) % 7
        days_ahead = days_ahead if days_ahead > 0 else 7
        days_ahead += 14  # rough monthly estimate

    return max(float(days_ahead), 0.5)  # never zero (avoids div by zero)


# ============================================================
# IV HISTORY PERSISTENCE
# ============================================================
def _load_iv_history() -> Dict[str, List[float]]:
    """Load IV history from disk. Returns empty dict if missing."""
    if not IV_HISTORY_PATH.exists():
        return {}
    try:
        with open(IV_HISTORY_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load IV history: {e}")
        return {}


def _save_iv_history(history: Dict[str, List[float]]):
    """Persist IV history to disk."""
    IV_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(IV_HISTORY_PATH, "w") as f:
            json.dump(history, f)
    except Exception as e:
        logger.warning(f"Failed to save IV history: {e}")


# ============================================================
# FYERS CLIENT
# ============================================================
class FyersDataClient:
    """Wrapper around Fyers API for option chain data."""

    def __init__(self):
        self.client_id = settings.FYERS_CLIENT_ID
        self.access_token = settings.FYERS_ACCESS_TOKEN
        self.fyers = None
        self._iv_history: Dict[str, List[float]] = _load_iv_history()
        self._prev_oi: Dict[str, Dict[str, int]] = {}  # index_key -> {strike_type: oi} from last fetch

    def connect(self):
        """Initialize Fyers connection. Call this once at startup."""
        from fyers_apiv3 import fyersModel
        self.fyers = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.access_token,
            log_path="logs/"
        )
        profile = self.fyers.get_profile()
        if profile.get("s") != "ok":
            raise ConnectionError(f"Fyers auth failed: {profile}")
        logger.info(f"Connected to Fyers as {profile['data']['name']}")

    def get_spot_price(self, index_key: str) -> float:
        """Get current spot price of an index (NIFTY/BANKNIFTY/SENSEX)."""
        import time
        symbol = settings.INDICES[index_key]["symbol"]
        # Retry once with delay on rate-limit error
        for attempt in range(2):
            response = self.fyers.quotes({"symbols": symbol})
            if response.get("s") == "ok":
                return float(response["d"][0]["v"]["lp"])
            if response.get("code") == 429:
                logger.warning(f"Rate limit on {symbol}, retrying in 1.5s...")
                time.sleep(1.5)
                continue
            logger.error(f"Quote fetch failed for {symbol}: {response}")
            return 0
        return 0

    def get_atm_strike(self, index_key: str) -> int:
        """Calculate ATM strike based on current spot."""
        spot = self.get_spot_price(index_key)
        step = settings.INDICES[index_key]["step"]
        return round(spot / step) * step

    def get_option_chain(self, index_key: str, expiry: Optional[str] = None) -> List[Dict]:
        """Fetch live option chain from Fyers, augmented with computed IV and Greeks."""
        n = settings.STRIKES_AROUND_ATM
        payload = {
            "symbol": settings.INDICES[index_key]["symbol"],
            "strikecount": n,
            "timestamp": ""
        }
        response = self.fyers.optionchain(payload)
        if response.get("s") != "ok":
            logger.error(f"Option chain fetch failed for {index_key}: {response}")
            return []

        return self._parse_chain(response, index_key)

    def _parse_chain(self, response: Dict, index_key: str) -> List[Dict]:
        """Normalize Fyers option chain + compute IV/Greeks/OI%."""
        chain = []
        data = response.get("data", {})
        options_data = data.get("optionsChain", [])

        # Extract spot from the index row in the response itself (avoids extra API call).
        # Fyers includes the underlying index as a row in the optionsChain array,
        # typically without a strike_price OR with option_type that isn't CE/PE.
        spot = 0.0
        for row in options_data:
            if not row.get("strike_price") or row.get("option_type") not in ("CE", "PE"):
                # This is likely the index row — try common LTP field names
                candidate = row.get("ltp") or row.get("lp") or row.get("last_price")
                if candidate and float(candidate) > 100:  # sanity: indices are >100
                    spot = float(candidate)
                    break

        # Fallback: also check data.indiceData or data.underlying if present
        if spot == 0:
            spot = float(data.get("indexLtp") or data.get("underlyingLtp") or 0)

        # Last resort: if still 0, log a warning but continue with IV=0
        if spot == 0:
            logger.warning(f"Could not extract spot price from option chain for {index_key}; IV will be 0")

        atm_step = settings.INDICES[index_key]["step"]
        atm_strike = round(spot / atm_step) * atm_step if spot > 0 else 0
        days_to_exp = days_to_nearest_expiry(index_key)

        # Track OI for next call's % comparison
        current_oi: Dict[str, int] = {}
        prev_oi = self._prev_oi.get(index_key, {})

        for opt in options_data:
            if not opt.get("strike_price") or opt.get("option_type") not in ("CE", "PE"):
                continue

            strike = int(opt.get("strike_price", 0))
            opt_type = opt.get("option_type")
            ltp = float(opt.get("ltp", 0))
            oi = int(opt.get("oi", 0))

            # Compute proper OI percentage change
            oi_key = f"{strike}_{opt_type}"
            current_oi[oi_key] = oi
            previous = prev_oi.get(oi_key, 0)
            if previous > 0:
                oi_change_pct = round(((oi - previous) / previous) * 100, 2)
            else:
                oi_change_pct = 0.0

            # Only compute IV for strikes near ATM (saves CPU; far OTM IVs are noise)
            iv = 0.0
            greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
            if spot > 0 and atm_strike > 0 and abs(strike - atm_strike) <= atm_step * 5:
                iv = compute_iv(
                    price=ltp,
                    spot=spot,
                    strike=strike,
                    days_to_expiry=days_to_exp,
                    is_call=(opt_type == "CE"),
                )
                if iv > 0:
                    greeks = compute_greeks(
                        spot=spot, strike=strike, days_to_expiry=days_to_exp,
                        sigma=iv, is_call=(opt_type == "CE"),
                    )

            chain.append({
                "strike": strike,
                "type": opt_type,
                "ltp": ltp,
                "bid": float(opt.get("bid", 0)),
                "ask": float(opt.get("ask", 0)),
                "volume": int(opt.get("volume", 0)),
                "oi": oi,
                "oi_change_pct": oi_change_pct,
                "iv": round(iv * 100, 2),  # store as percent (18.42 not 0.1842)
                "delta": greeks["delta"],
                "gamma": greeks["gamma"],
                "theta": greeks["theta"],
                "vega": greeks["vega"],
                "timestamp": datetime.now().isoformat(),
            })

        # Save current OI for next comparison
        self._prev_oi[index_key] = current_oi
        return chain

    def get_iv_percentile(self, index_key: str, current_iv: float) -> float:
        """IV percentile based on rolling history. Returns 0-100."""
        history = self._iv_history.get(index_key, [])
        if len(history) < 5:
            return 50.0
        below = sum(1 for iv in history if iv < current_iv)
        return round((below / len(history)) * 100, 1)

    def update_iv_history(self, index_key: str, current_iv: float):
        """Append today's ATM IV to history. Call once per day (e.g., end-of-day)."""
        if current_iv <= 0:
            return  # skip junk values
        if index_key not in self._iv_history:
            self._iv_history[index_key] = []
        self._iv_history[index_key].append(current_iv)
        self._iv_history[index_key] = self._iv_history[index_key][-60:]  # keep last 60 days
        _save_iv_history(self._iv_history)
