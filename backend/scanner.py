"""
Scanner engine. Takes raw option chain data and filters down to high-quality setups
based on IV regime, OI shifts, and strike efficiency.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from config import settings


def _oa_chain_to_flat(oa_strikes: list) -> list:
    """
    Convert an OpenAlgo enriched chain (one row per strike, call+put together) to the
    Fyers-compatible flat format used by scan_index internals (one row per leg).

    IV conversion: OpenAlgo stores IV as annualized decimal (0.18 = 18%);
    flat format uses percent (18.0) to match Fyers _parse_chain output.

    lot_size is carried per-row as an extra field so _find_setups() can read it
    without touching settings.INDICES.
    """
    flat = []
    ts = datetime.now().isoformat()
    for row in oa_strikes:
        strike = row["strike"]
        lotsize = row.get("lotsize", 0)
        for prefix, opt_type in (("call", "CE"), ("put", "PE")):
            flat.append({
                "strike":        strike,
                "type":          opt_type,
                "ltp":           row.get(f"{prefix}_ltp", 0.0),
                "bid":           row.get(f"{prefix}_bid", 0.0),
                "ask":           row.get(f"{prefix}_ask", 0.0),
                "volume":        row.get(f"{prefix}_volume", 0),
                "oi":            row.get(f"{prefix}_oi", 0),
                "oi_change_pct": 0.0,  # detect_oi_shift() recomputes from last_chains snapshot
                "iv":            round(row.get(f"{prefix}_iv", 0.0) * 100, 2),
                "delta":         row.get(f"{prefix}_delta", 0.0),
                "gamma":         row.get(f"{prefix}_gamma", 0.0),
                "theta":         row.get(f"{prefix}_theta", 0.0),
                "vega":          row.get(f"{prefix}_vega", 0.0),
                "timestamp":     ts,
                "lot_size":      lotsize,
            })
    return flat

logger = logging.getLogger(__name__)


class OptionScanner:
    """Filters option chain data to surface high-probability scalp setups."""

    def __init__(self, fyers_client):
        self.fyers = fyers_client
        self.last_chains: Dict[str, List[Dict]] = {}
        self.tv_signals: Dict[str, Dict] = {}  # latest TV webhook per index

    def register_tv_signal(self, index_key: str, signal: Dict):
        """Store latest TradingView webhook signal for an index."""
        signal["received_at"] = datetime.now()
        self.tv_signals[index_key] = signal
        logger.info(f"TV signal registered for {index_key}: {signal.get('action')}")

    def _is_tv_signal_fresh(self, index_key: str, max_age_seconds: int = 120) -> Optional[Dict]:
        """Return TV signal if it's recent enough to use."""
        sig = self.tv_signals.get(index_key)
        if not sig:
            return None
        age = (datetime.now() - sig["received_at"]).total_seconds()
        if age > max_age_seconds:
            return None
        return sig

    def calculate_strike_efficiency(self, option: Dict) -> float:
        """
        Gamma-to-theta ratio. Higher = better scalp candidate.
        You want maximum gamma (price sensitivity) per unit of theta paid.
        """
        theta = abs(option.get("theta", 0))
        gamma = option.get("gamma", 0)
        if theta == 0:
            return 0
        return round(gamma / theta * 1000, 4)  # scaled for readability

    def detect_oi_shift(self, index_key: str, current_chain: List[Dict]) -> Dict:
        """
        Compare current OI to previous snapshot. Detects unwinding/buildup.

        Bullish signal: PE OI unwinding + CE OI buildup
        Bearish signal: CE OI unwinding + PE OI buildup
        """
        prev = self.last_chains.get(index_key, [])
        if not prev:
            self.last_chains[index_key] = current_chain
            return {"signal": "neutral", "reason": "no_prior_data"}

        # Build lookup: (strike, type) -> oi
        prev_oi = {(o["strike"], o["type"]): o["oi"] for o in prev}

        ce_unwind = pe_unwind = ce_buildup = pe_buildup = 0
        for opt in current_chain:
            key = (opt["strike"], opt["type"])
            old_oi = prev_oi.get(key, opt["oi"])
            if old_oi == 0:
                continue
            change_pct = ((opt["oi"] - old_oi) / old_oi) * 100

            if opt["type"] == "CE":
                if change_pct < -settings.OI_UNWIND_THRESHOLD_PCT:
                    ce_unwind += 1
                elif change_pct > settings.OI_BUILDUP_THRESHOLD_PCT:
                    ce_buildup += 1
            else:
                if change_pct < -settings.OI_UNWIND_THRESHOLD_PCT:
                    pe_unwind += 1
                elif change_pct > settings.OI_BUILDUP_THRESHOLD_PCT:
                    pe_buildup += 1

        self.last_chains[index_key] = current_chain

        # Bullish: puts unwinding (sellers covering = expectation of up move)
        if pe_unwind >= 3 and ce_buildup <= 1:
            return {"signal": "bullish", "reason": f"PE unwind on {pe_unwind} strikes"}
        if ce_unwind >= 3 and pe_buildup <= 1:
            return {"signal": "bearish", "reason": f"CE unwind on {ce_unwind} strikes"}
        return {"signal": "neutral", "reason": "no clear OI shift"}

    def calculate_pcr(self, chain: List[Dict]) -> float:
        """Put-Call Ratio based on OI. >1 = bearish sentiment, <0.7 = bullish."""
        ce_oi = sum(o["oi"] for o in chain if o["type"] == "CE")
        pe_oi = sum(o["oi"] for o in chain if o["type"] == "PE")
        if ce_oi == 0:
            return 0
        return round(pe_oi / ce_oi, 2)

    def calculate_max_pain(self, chain: List[Dict]) -> int:
        """Strike where option writers lose the least (price tends to gravitate here on expiry)."""
        strikes = sorted(set(o["strike"] for o in chain))
        pain_by_strike = {}
        for test_strike in strikes:
            total_pain = 0
            for opt in chain:
                if opt["type"] == "CE" and test_strike > opt["strike"]:
                    total_pain += (test_strike - opt["strike"]) * opt["oi"]
                elif opt["type"] == "PE" and test_strike < opt["strike"]:
                    total_pain += (opt["strike"] - test_strike) * opt["oi"]
            pain_by_strike[test_strike] = total_pain
        return min(pain_by_strike, key=pain_by_strike.get)

    def scan_index(self, index_key: str, oa_chain_data: Optional[dict] = None) -> Dict:
        """
        Run full scan on one index. Returns dict with:
        - setups: list of qualifying option contracts to consider
        - market_context: PCR, max pain, IV regime, OI signal
        - tv_confirmation: whether TradingView signal aligns

        If oa_chain_data is provided (from OpenAlgo get_enriched_chain()), spot and atm
        come from its underlying_ltp / atm_strike fields and the chain is converted via
        _oa_chain_to_flat(). Otherwise the Fyers client is used (fallback).
        """
        if oa_chain_data is not None:
            spot = oa_chain_data["underlying_ltp"]
            atm  = oa_chain_data["atm_strike"]
            chain = _oa_chain_to_flat(oa_chain_data["strikes"])
        else:
            chain = self.fyers.get_option_chain(index_key)
            spot = self.fyers.get_spot_price(index_key)
            atm = self.fyers.get_atm_strike(index_key)

        # Empty chain is a data-availability problem: return degraded cleanly instead
        # of crashing in calculate_max_pain (min() on empty sequence) or poisoning
        # detect_oi_shift's last_chains snapshot.
        if not chain:
            return {
                "index": index_key,
                "spot": spot,
                "atm": atm,
                "timestamp": datetime.now().isoformat(),
                "market_context": None,
                "setups": [],
                "health": {
                    "state": "degraded",
                    "reason": "option chain empty — no data from OpenAlgo or Fyers",
                    "ts": datetime.now().isoformat(),
                },
            }

        # Market-wide context
        atm_options = [o for o in chain if o["strike"] == atm]
        avg_iv = sum(o["iv"] for o in atm_options) / len(atm_options) if atm_options else 0
        iv_pct = self.fyers.get_iv_percentile(index_key, avg_iv)
        oi_signal = self.detect_oi_shift(index_key, chain)
        pcr = self.calculate_pcr(chain)
        max_pain = self.calculate_max_pain(chain)
        tv_sig = self._is_tv_signal_fresh(index_key)

        # Don't generate setups if IV is too rich
        iv_regime = "cheap" if iv_pct < settings.IV_PERCENTILE_GOOD else \
                    "expensive" if iv_pct > settings.IV_PERCENTILE_AVOID else "neutral"

        setups = []
        if iv_regime != "expensive":
            setups = self._find_setups(chain, atm, spot, oi_signal, tv_sig, index_key)

        return {
            "index": index_key,
            "spot": spot,
            "atm": atm,
            "timestamp": datetime.now().isoformat(),
            "market_context": {
                "iv_percentile": iv_pct,
                "iv_regime": iv_regime,
                "avg_atm_iv": round(avg_iv, 2),
                "pcr": pcr,
                "max_pain": max_pain,
                "oi_signal": oi_signal,
                "tv_signal": tv_sig.get("action") if tv_sig else None,
            },
            "setups": setups,
            "health": {"state": "ok", "reason": None, "ts": datetime.now().isoformat()},
        }

    def _find_setups(self, chain, atm, spot, oi_signal, tv_sig, index_key) -> List[Dict]:
        """Filter chain to qualifying scalp candidates."""
        setups = []
        step = settings.INDICES[index_key]["step"]

        # Decide direction bias: TV signal takes priority, then OI
        direction = None
        if tv_sig:
            action = tv_sig.get("action", "").lower()
            if "buy" in action or "long" in action or "bullish" in action:
                direction = "bullish"
            elif "sell" in action or "short" in action or "bearish" in action:
                direction = "bearish"

        if not direction and oi_signal["signal"] in ("bullish", "bearish"):
            direction = oi_signal["signal"]

        if not direction:
            return []  # no clear bias, no setups

        target_type = "CE" if direction == "bullish" else "PE"

        # Look at ATM and slightly OTM strikes (best gamma for scalping)
        candidates = [
            o for o in chain
            if o["type"] == target_type
            and atm - 2*step <= o["strike"] <= atm + 2*step
        ]

        for opt in candidates:
            efficiency = self.calculate_strike_efficiency(opt)
            if efficiency < settings.MIN_GAMMA_THETA_RATIO:
                continue

            # Capital check — prefer live lotsize from chain row (OpenAlgo path),
            # fall back to settings for Fyers path which doesn't carry lotsize.
            lot_size = opt.get("lot_size") or settings.INDICES[index_key]["lot_size"]
            premium_cost = opt["ltp"] * lot_size
            if premium_cost > settings.MAX_CAPITAL_PER_TRADE_RUPEES:
                continue

            confluence_score = 0
            reasons = []

            if tv_sig:
                confluence_score += 2
                reasons.append(f"TV signal: {tv_sig.get('action')}")
            if oi_signal["signal"] == direction:
                confluence_score += 2
                reasons.append(f"OI: {oi_signal['reason']}")
            if abs(opt["delta"]) > 0.4:
                confluence_score += 1
                reasons.append(f"Delta: {opt['delta']}")
            if efficiency > settings.MIN_GAMMA_THETA_RATIO * 2:
                confluence_score += 1
                reasons.append("High gamma efficiency")

            if confluence_score >= 3:  # need at least decent confluence
                setups.append({
                    "strike": opt["strike"],
                    "type": opt["type"],
                    "ltp": opt["ltp"],
                    "premium_cost": premium_cost,
                    "delta": opt["delta"],
                    "gamma": opt["gamma"],
                    "theta": opt["theta"],
                    "iv": opt["iv"],
                    "efficiency_score": efficiency,
                    "confluence_score": confluence_score,
                    "reasons": reasons,
                    "direction": direction,
                    "suggested_sl": round(opt["ltp"] * (1 - settings.HARD_STOP_LOSS_PCT/100), 2),
                    "suggested_target_1": round(opt["ltp"] * 1.20, 2),
                    "suggested_target_2": round(opt["ltp"] * 1.40, 2),
                })

        # Rank by confluence score
        setups.sort(key=lambda s: s["confluence_score"], reverse=True)
        return setups[:3]  # top 3 only - quality over quantity
