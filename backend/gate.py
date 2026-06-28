"""
Regime-aware webhook gate.

Evaluate pipeline (first failure short-circuits):
  1. min_filters   — reject if payload.filters < min_filters
  2. cooldown      — reject if last taken trade in sym+dir was < cooldown_bars ago
  3. refinement    — reject if require_fvg/require_pullback flags not met
  4. regime route  — ranging: enforce z-gate; trending: suppress z-gate
  5. take
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import backend.journal as journal
from backend.models import OptScanPayload

logger = logging.getLogger(__name__)


# ─────────────────────────── data types ─────────────────────────────────────


@dataclass
class Decision:
    take: bool
    direction: str
    regime: str
    reason: str
    features: dict = field(default_factory=dict)


# ─────────────────────────── regime providers ────────────────────────────────


class RegimeProvider(ABC):
    """Pluggable regime signal. Swap ADX proxy for net-GEX sign with no gate changes."""

    @abstractmethod
    def get_regime(self, payload: OptScanPayload) -> str:
        """Return 'trending' or 'ranging'."""

    def update_chain(self, chain: list, spot: float, lot_size: float = 1.0) -> None:
        """Push fresh chain data to providers that need it. No-op by default."""


class AdxRegimeProvider(RegimeProvider):
    def __init__(self, adx_threshold: float = 35.0):
        self.adx_threshold = adx_threshold
        self._last_regime: str = "ranging"

    def get_regime(self, payload: OptScanPayload) -> str:
        r = "trending" if payload.adx >= self.adx_threshold else "ranging"
        self._last_regime = r
        return r

    @property
    def current_regime(self) -> str:
        """Last computed regime. 'ranging' until first get_regime() call."""
        return self._last_regime


class GexRegimeProvider(RegimeProvider):
    """
    Net dealer GEX sign as regime proxy.
    Chain data is pushed by the scan loop via update_chain().
    Default (no data yet): ranging — conservative, z-gate stays on.

    Convention (CLAUDE.md): dealers long call gamma, short put gamma.
    positive net GEX → dealers suppress moves → ranging
    negative net GEX → dealers amplify  moves → trending
    """

    def __init__(self) -> None:
        self._net_gex: float = 0.0

    def update_chain(self, chain: list, spot: float, lot_size: float = 1.0) -> None:
        from backend.gex import net_gex as _net_gex
        self._net_gex = _net_gex(chain, spot, lot_size)

    def get_regime(self, payload: OptScanPayload) -> str:
        return "ranging" if self._net_gex >= 0.0 else "trending"

    @property
    def net_gex(self) -> float:
        return self._net_gex

    @property
    def current_regime(self) -> str:
        """Current regime from last chain update. 'ranging' until first update_chain()."""
        return "ranging" if self._net_gex >= 0.0 else "trending"


# ─────────────────────────── gate ────────────────────────────────────────────


class Gate:
    def __init__(
        self,
        regime_provider: RegimeProvider,
        min_filters: int = 9,
        cooldown_bars: int = 10,
        require_fvg: bool = False,
        require_pullback: bool = False,
    ):
        self.regime_provider = regime_provider
        self.min_filters = min_filters
        self.cooldown_bars = cooldown_bars
        self.require_fvg = require_fvg
        self.require_pullback = require_pullback

    # ------------------------------------------------------------------ #

    def evaluate(self, payload: OptScanPayload) -> Decision:
        DIR = payload.dir.upper()

        # 1. Min filters
        if payload.filters < self.min_filters:
            return Decision(
                take=False,
                direction=payload.dir,
                regime="unknown",
                reason=f"{DIR} skip — filters: {payload.filters} < min {self.min_filters}",
                features=self._features(payload),
            )

        # 2. Cooldown
        last = journal.get_last_taken(payload.sym, payload.dir)
        if last is not None:
            tf_ms = int(payload.tf) * 60_000
            elapsed = (payload.bar_time - last["bar_time"]) / tf_ms
            if elapsed < self.cooldown_bars:
                return Decision(
                    take=False,
                    direction=payload.dir,
                    regime="unknown",
                    reason=(
                        f"{DIR} skip — cooldown: last {payload.dir} "
                        f"{int(elapsed)} bars ago (need {self.cooldown_bars})"
                    ),
                    features=self._features(payload),
                )

        # 3. Refinement
        if self.require_fvg and not payload.fvg_ok:
            return Decision(
                take=False,
                direction=payload.dir,
                regime="unknown",
                reason=f"{DIR} skip — FVG refinement not met",
                features=self._features(payload),
            )
        if self.require_pullback and not payload.pb_ok:
            return Decision(
                take=False,
                direction=payload.dir,
                regime="unknown",
                reason=f"{DIR} skip — pullback refinement not met",
                features=self._features(payload),
            )

        # 4. Regime routing
        regime = self.regime_provider.get_regime(payload)

        if regime == "ranging":
            if payload.dir == "long" and not payload.z_long_zone:
                return Decision(
                    take=False,
                    direction=payload.dir,
                    regime=regime,
                    reason=(
                        f"{DIR} skip — regime: ranging; "
                        f"z={payload.z:+.2f} outside long zone [-3,-1]; "
                        f"filters {payload.filters}/12"
                    ),
                    features=self._features(payload),
                )
            if payload.dir == "short" and not payload.z_short_zone:
                return Decision(
                    take=False,
                    direction=payload.dir,
                    regime=regime,
                    reason=(
                        f"{DIR} skip — regime: ranging; "
                        f"z={payload.z:+.2f} outside short zone [+1,+3]; "
                        f"filters {payload.filters}/12"
                    ),
                    features=self._features(payload),
                )

        # 5. Take
        if regime == "trending":
            reason = (
                f"{DIR} take — regime: trending; "
                f"z-gate relaxed; filters {payload.filters}/12"
            )
        else:
            reason = (
                f"{DIR} take — regime: ranging; "
                f"z-gate passed; filters {payload.filters}/12"
            )

        return Decision(
            take=True,
            direction=payload.dir,
            regime=regime,
            reason=reason,
            features=self._features(payload),
        )

    # ------------------------------------------------------------------ #

    def _features(self, payload: OptScanPayload) -> dict:
        return {
            "filters": payload.filters,
            "adx": payload.adx,
            "z": payload.z,
            "z_long_zone": payload.z_long_zone,
            "z_short_zone": payload.z_short_zone,
        }
