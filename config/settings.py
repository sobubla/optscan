"""
Central configuration. EDIT THIS FILE to tune the system to your style.
All thresholds here are starting points based on Indian index option behavior;
expect to tune them over 2-3 weeks of paper trading.
"""

import os

# =========================================================================
# FYERS API CREDENTIALS - fill these in from https://myapi.fyers.in
# =========================================================================
FYERS_CLIENT_ID = "VCEE9JFJI9-100"      # e.g. "ABCD1234-100"
FYERS_SECRET_KEY = "GZSXIXB93B"
FYERS_REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"
FYERS_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiZDoxIiwiZDoyIiwieDowIiwieDoxIiwieDoyIl0sImF0X2hhc2giOiJnQUFBQUFCcUdUTGkwcEpjR04zMVMtR2R6a3dYRUdYbGFwUkpON0ExSWsxT1dDdmdsbWVQak50N1RobHNGTXlQU1lobkhPOWxhTDk2b0stUzJ4RWlmMjBKSHFBOWR3d3JxMnlUMnZiWmV1WGZLbFRvc09lenNoST0iLCJkaXNwbGF5X25hbWUiOiIiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiI3N2YwN2RjODlmN2ZkNzRlOTc5YjFmMmI1ZmE4MTlhMjIwNTVjYmFiNDg2NWI4MzNjNGRjOGVmMyIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImZ5X2lkIjoiWVMxNDIwMSIsImFwcFR5cGUiOjEwMCwiZXhwIjoxNzgwMTAxMDAwLCJpYXQiOjE3ODAwMzYzMjIsImlzcyI6ImFwaS5meWVycy5pbiIsIm5iZiI6MTc4MDAzNjMyMiwic3ViIjoiYWNjZXNzX3Rva2VuIn0.8lYLJ-AyxA0zt7HoXYWAU_C_AG7is0i3KNFBhNixGSo"                      # auto-generated, leave blank initially

# =========================================================================
# INSTRUMENTS TO SCAN
# =========================================================================
INDICES = {
    "NIFTY":     {"symbol": "NSE:NIFTY50-INDEX",      "lot_size": 75,  "step": 50},
    "BANKNIFTY": {"symbol": "NSE:NIFTYBANK-INDEX",    "lot_size": 30,  "step": 100},
}

# How many strikes around ATM to scan (each side)
STRIKES_AROUND_ATM = 10

# =========================================================================
# SCANNER THRESHOLDS - tune these to your taste
# =========================================================================
SCAN_INTERVAL_SECONDS = 30          # how often to refresh option chain

# IV percentile thresholds (option buying favors LOW IV)
IV_PERCENTILE_GOOD = 30              # below this = cheap options, good for buying
IV_PERCENTILE_AVOID = 70             # above this = expensive, avoid buying

# OI shift detection (5-min window)
OI_UNWIND_THRESHOLD_PCT = 5.0        # >5% drop in OI on opposite side = bullish/bearish signal
OI_BUILDUP_THRESHOLD_PCT = 5.0

# Strike efficiency: gamma-to-theta ratio (higher = better scalp candidate)
MIN_GAMMA_THETA_RATIO = 0.05

# =========================================================================
# EXIT MANAGER RULES - the most important section for you
# =========================================================================
# Trailing profit lock (premium-based, not underlying)
PROFIT_LOCK_TIERS = [
    {"profit_pct": 20, "lock_pct": 10},   # at +20%, lock +10%
    {"profit_pct": 40, "lock_pct": 25},   # at +40%, lock +25%
    {"profit_pct": 60, "lock_pct": 40},   # at +60%, lock +40%
    {"profit_pct": 100, "lock_pct": 70},  # at +100%, lock +70%
]

# Hard stop loss (% of premium paid)
HARD_STOP_LOSS_PCT = 30

# Time-based exit (theta is killing you if trade hasn't worked)
MAX_HOLD_MINUTES = 45                # exit if no progress after this
NO_PROGRESS_THRESHOLD_PCT = 5        # "progress" means at least +5% at some point

# Volatility crush exit
IV_DROP_EXIT_PCT = 5.0               # if IV drops this much while holding, exit

# =========================================================================
# OPENALGO API CLIENT
# Set OPENALGO_BASE_URL and OPENALGO_API_KEY in your .env file.
# Load the .env file before starting the server (e.g. `export $(cat .env)` or
# call `python-dotenv` from your startup script).  These values are never
# printed or logged by the client.
# =========================================================================
OPENALGO_BASE_URL = os.environ.get("OPENALGO_BASE_URL", "")
OPENALGO_API_KEY  = os.environ.get("OPENALGO_API_KEY", "")

# =========================================================================
# WEBHOOK / SERVER
# =========================================================================
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
WEBHOOK_SECRET = "soumya_bnf_2026_xyz"  # TradingView will send this

# =========================================================================
# RISK LIMITS (hard caps - system will refuse to flag setups beyond these)
# =========================================================================
MAX_DAILY_LOSS_RUPEES = 5000         # stop scanning after this much loss
MAX_TRADES_PER_DAY = 5               # quality over quantity
MAX_CAPITAL_PER_TRADE_RUPEES = 15000

# =========================================================================
# GATE CONFIG — regime-aware webhook gate (backend/gate.py)
# =========================================================================
GATE_MIN_FILTERS = 9                 # minimum confluence filters to consider a signal
GATE_COOLDOWN_BARS = 10              # bars between taken trades in same sym+dir
GATE_REGIME_PROVIDER = "adx"         # "adx" | "gex" — which RegimeProvider to use
GATE_ADX_THRESHOLD = 35              # adx >= this → trending; below → ranging (ADX mode)
GATE_REQUIRE_FVG = False             # require fvg_ok from Pine refinement
GATE_REQUIRE_PULLBACK = False        # require pb_ok from Pine refinement

# =========================================================================
# ENTRY LAYER — strike and premium selection
# =========================================================================
ENTRY_MODE                 = "intraday"  # "intraday" | "positional" (positional not yet enabled)
ENTRY_DELTA_BAND_MIN       = 0.35        # abs(delta) lower bound for directional buy
ENTRY_DELTA_BAND_MAX       = 0.50        # abs(delta) upper bound
ENTRY_MIN_DTE              = 2           # roll to next expiry if DTE < this
ENTRY_MIN_OI               = 50_000      # minimum open interest per strike-side
ENTRY_MAX_SPREAD_PCT       = 5.0         # max spread as % of mid (field optional in chain)
ENTRY_IV_PERCENTILE_REJECT = 70          # skip strike if IV percentile > this
ENTRY_RISK_PCT             = 0.02        # fraction of equity at risk per trade
ENTRY_STOP_PCT             = 0.30        # advisory stop: exit if premium drops this %
ENTRY_TARGET_PCT           = 0.50        # advisory target: exit at this % premium gain
ENTRY_EOD_SQUAREOFF        = "15:15"     # advisory intraday time stop (IST, HH:MM)
EQUITY_RUPEES              = 200_000     # total trading equity for position sizing

# =========================================================================
# EXIT MONITOR — OR-triggered position monitor
# =========================================================================
EXIT_TRAIL_PCT           = 0.20   # trail fires if premium drops this % from peak (once peak >= target)
EXIT_IV_CRUSH_DELTA      = 0.05   # IV drop in absolute decimal (0.05 = 5pp, e.g. 0.20→0.15)
EXIT_HOLD_MINUTES_EXPIRY = 30     # tighter theta/time-stop on expiry day (normal: MAX_HOLD_MINUTES=45)
