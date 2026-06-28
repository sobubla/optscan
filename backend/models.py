from typing import Literal
from pydantic import BaseModel, Field, field_validator


class OptScanPayload(BaseModel):
    secret: str = Field(exclude=True)  # webhook auth token; excluded from model_dump()
    v: Literal["optscan-v13"]
    sym: str
    tf: str
    dir: Literal["long", "short"]
    bar_time: int = Field(gt=0)        # bar open time, epoch ms
    price: float
    atr: float
    adx: float
    filters: int = Field(ge=0, le=12)  # confluence count 0-12

    @field_validator("tf")
    @classmethod
    def tf_must_be_numeric(cls, v: str) -> str:
        if not v.isdigit() or int(v) < 1:
            raise ValueError("tf must be a positive integer string of minutes, e.g. '9'")
        return v
    # 12 filter states (direction-appropriate)
    f_ema: bool
    f_rsi: bool
    f_vol: bool
    f_vwap: bool
    f_mvwap: bool
    f_band: bool
    f_cvd: bool
    f_st: bool
    f_macd: bool
    f_poc: bool
    f_mss: bool
    f_adx: bool
    # z-score fields
    z: float
    z_long_zone: bool      # z in mean-reversion long zone (approx -3..-1)
    z_short_zone: bool     # z in mean-reversion short zone (approx +1..+3)
    z_bull_pa: bool
    z_bear_pa: bool
    # refinement flags
    fvg_ok: bool
    pb_ok: bool
    vol_ok: bool           # always True here; Pine pre-filters
    range_ratio: float
    bars_since: int        # advisory only — gate uses DB query for cooldown
    # structure
    hh: bool
    ll: bool
    ext_long: bool
    ext_short: bool
    mss_state: Literal[-1, 0, 1]
