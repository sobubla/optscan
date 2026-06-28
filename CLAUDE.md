# CLAUDE.md — OPT.SCAN

Project instructions for Claude Code. Read this at the start of every session.

## What this project is

OPT.SCAN is an intraday options scanning and journaling system for NIFTY and BankNifty on Indian markets (NSE). A TradingView Pine Script indicator generates the primary signal and emits it via webhook; a FastAPI backend receives it, applies a regime-aware gate, enriches with option-chain data, and logs everything to a SQLite journal. The system is for the author's own trading and connects to live broker accounts.

> ⚠️ This is a **live-money trading system**. Changes can have financial consequences. Bias toward caution. See **Hard rules** below.

## Architecture

> Adjust this section to match the actual repo layout — it is a starting summary, not ground truth.

- **Signal layer** — Pine Script (v13) on TradingView: 12 confluence filters + hard gates (volatility, range expansion, cooldown), MSS-Trap Reversal Engine with CVD confirmation, z-score mean reversion, FVG logic, ATR trail. **Now emits the raw confluence signal as a JSON webhook** (see the gate-extraction section); its own chart markers/backtest remain as a visual reference only.
- **Backend** — FastAPI service on `localhost:8000`. Receives the webhook, runs the **regime-aware gate** (the decision layer that used to live in Pine), persists results, and surfaces takes to the human-approval path.
- **Data** — Fyers API for the live option chain. Fyers returns **no IV and no Greeks**, so IV is solved locally.
- **IV / Greeks** — currently a pure-Python Black-Scholes + Newton-Raphson IV solver (being migrated to Black-76 via `py_vollib`).
- **Journal** — SQLite. Source of truth for trade history and the training data for the meta-label.
- **Dashboard** — terminal-aesthetic web UI.
- **Brokers** — Fyers (primary), plus Shoonya and Angel One.

## Tech stack & running it

> Fill in the exact commands for your setup.

- Python 3.11, virtualenv.
- FastAPI + Uvicorn (backend on port 8000).
- SQLite (no server).
- Key libs: `fastapi`, `uvicorn`, `pydantic`, an HTTP client (`httpx`/`requests`), `numpy`, `scipy`, and `py_vollib` (incoming).

```bash
# example — replace with your real commands
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
pytest
```

## Hard rules — non-negotiable

These protect real money, real credentials, and this repo's license position. Do not violate them even if a single task asks you to. If a request conflicts with one, **stop and flag it** rather than proceeding.

**Secrets & credentials**
- Never read, modify, print, log, or commit broker API keys, tokens, TOTP secrets, or the `.env` file.
- All credential handling is done by the human, outside of you. If a task appears to need a secret, ask the human to supply or configure it themselves.

**Execution & order placement**
- Never write code that places, modifies, or cancels live orders autonomously.
- The **deterministic core** (signal → gate → meta-label → risk gate) owns execution. Any LLM/agentic component reads, reasons, and writes notes only — it must not decide or place trades.
- The gate's job ends at returning a decision and persisting it. On a "take", surface to the human-approval / semi-auto path — **do not auto-execute**.
- Semi-automatic with explicit human approval is the maximum autonomy. No fully autonomous trading paths.

**Licensing (this repo is the author's own work)**
- OpenAlgo's core is **AGPL v3.0**. Consume it **only via its HTTP API**. **Never copy OpenAlgo source into this repo** — it would impose AGPL copyleft on OPT.SCAN.
- Only **permissively licensed** code (MIT/BSD/Apache) may be adapted in — e.g. `aakash-code/GammaGEX`, `KaranChavan21/GEX_Dashboard` (both MIT). Preserve attribution/license headers.
- Before pulling in any third-party code, check its license. If not permissive, consume via API or do not use it.

## Working style

This project has the `karpathy-guidelines` skill installed; follow it. In short:

- **Think first.** State assumptions explicitly. If multiple interpretations exist, present them rather than picking silently. If a simpler approach exists, say so.
- **Simplicity first.** Write the minimum code that solves the task. No speculative features, abstractions, or configurability that wasn't asked for.
- **Surgical changes.** Touch only what the task requires. Don't refactor or reformat adjacent code. Match existing style. Mention unrelated dead code; don't delete it.
- **Goal-driven.** Turn each task into a verifiable goal and confirm it (run the tests) before declaring done. For multi-step work, state a brief plan with a verify step per item.

Workflow:
- Assume work starts from a clean git tree. Make one logical change per commit so each is a reviewable, revertible diff.
- Before changing anything in an unfamiliar area, read it and summarize the structure back before editing.
- Run the test suite after each change. Never blanket-assume success on this system.

---

## ACTIVE TASK — Webhook gate extraction (the decision layer moves from Pine to Python)

**Why:** the Pine refinement gate (FVG / Pullback / Z-Score combine) is regime-blind. A mean-reversion z-gate vetoes momentum signals in trending markets — confirmed on two BANKNIFTY charts (a long blocked at z=+1.91 in an uptrend, a short blocked at z=-1.22 in a downtrend). The fix needs the trend/range regime, which only the backend can see. So Pine now emits the raw confluence signal and the backend makes the final, regime-aware decision.

### 1. Webhook contract (what Pine sends)

Pine fires `alert()` with this flat JSON on every raw confluence signal (count >= min filters + EMA-state gate + volatility gate + range gate, on bar close, **before** cooldown / refinement / regime). Build a pydantic model matching it exactly:

| field | type | notes |
|---|---|---|
| `v` | str | payload version, `"optscan-v13"` |
| `sym` | str | e.g. `"BANKNIFTY"` |
| `tf` | str | signal timeframe, e.g. `"9"` |
| `dir` | str | `"long"` or `"short"` |
| `bar_time` | int | bar open time, epoch **ms** |
| `price` | float | close at signal |
| `atr` | float | |
| `adx` | float | **used as the initial regime proxy** |
| `filters` | int | confluence count, 0-12 |
| `f_ema`,`f_rsi`,`f_vol`,`f_vwap`,`f_mvwap`,`f_band`,`f_cvd`,`f_st`,`f_macd`,`f_poc`,`f_mss`,`f_adx` | bool | the 12 filter states (direction-appropriate) |
| `z` | float | z-score of price vs anchor |
| `z_long_zone` | bool | z in mean-reversion long zone (approx -3..-1) |
| `z_short_zone` | bool | z in mean-reversion short zone (approx +1..+3) |
| `z_bull_pa`,`z_bear_pa` | bool | reversal price-action bar present |
| `fvg_ok` | bool | FVG refinement passed (direction-appropriate) |
| `pb_ok` | bool | pullback refinement passed |
| `vol_ok` | bool | volatility gate (always true here; Pine pre-filtered) |
| `range_ratio` | float | bar range / avg range |
| `bars_since` | int | bars since last same-dir Pine signal (advisory only) |
| `hh`,`ll` | bool | higher-high / lower-low structure |
| `ext_long`,`ext_short` | bool | price extended beyond the band |
| `mss_state` | int | -1 / 0 / +1 |

### 2. FastAPI endpoint

`POST /webhook/optscan`:
1. Parse + validate the body against the pydantic model. Reject malformed payloads with 400.
2. Call `gate.evaluate(payload) -> Decision`.
3. Persist the signal **and** the decision to SQLite (`signals` table) — **every** signal, taken or skipped, with all features. This is the meta-label training data; never drop skipped signals.
4. If `decision.take`: enqueue to the human-approval / semi-auto path. **Never auto-place an order.**
5. Return 200 with the decision JSON (for logging/debug).

### 3. The gate logic (`gate.evaluate`)

Apply in order; first failure short-circuits with a reason:

1. **Min filters** — reject if `filters < min_filters` (config; may be regime-adaptive later, e.g. require one fewer in a trending regime).
2. **Cooldown** — query SQLite for the last **taken** trade in this `sym`+`dir`; reject if within `cooldown_bars`. Count from **taken trades, not emitted signals** (this is the whole reason cooldown left Pine — Pine reset its counter on signals it never traded). `bars_since` from the payload is advisory only.
3. **Refinement** (config-driven, mirrors Pine's refine modes): if `require_fvg`, need `fvg_ok`; if `require_pullback`, need `pb_ok`.
4. **Regime routing — the core change:**
   - Determine `regime`. **Initial proxy: `trending` if `adx >= adx_threshold` else `ranging`.** Make this pluggable — a `RegimeProvider` interface — so it swaps to net-GEX sign later (trending = negative gamma, ranging = positive gamma) with no change to the gate. `adx_threshold` in config (start at 35 to match the Pine default).
   - **`ranging`** -> apply the mean-reversion z-gate as Pine intended: long requires `z_long_zone`, short requires `z_short_zone`. (Buy dips / sell rallies.)
   - **`trending`** -> **suppress the mean-reversion z-requirement.** Allow the entry aligned with the confluence direction (the 12-filter count already confirmed direction). Optionally reject only the far opposite extreme (e.g. a long with `z >= +z_max`). Defer final quality to the meta-label once priority #5 below exists.
5. If all pass -> `take`; else `skip`.

Return `Decision{ take: bool, direction, regime, reason: str, features: dict }`.

**Reason string** — mirror the Pine tooltip style so the backend stays as readable as the chart:
- `"LONG skip — regime: ranging; z=+1.91 outside long zone [-3,-1]; filters 11/12"`
- `"LONG take — regime: trending; z-gate relaxed; filters 11/12"`
- `"SHORT skip — cooldown: last short 4 bars ago (need 10)"`

### 4. Tests (`tests/test_gate.py`) — encode the two real screenshots

Strong success criteria. These must pass:
- **Screenshot 1** — long payload, `z=+1.91`, `filters>=10`, `adx=40` (trending) -> `take`, regime `trending`.
- Same payload, `adx=20` (ranging) -> `skip`, reason names z outside the long zone.
- **Screenshot 2** — short payload, `z=-1.22`, `filters>=10`, `adx=40` (trending) -> `take`, regime `trending`.
- Same payload, `adx=20` (ranging) -> `skip`.
- **Cooldown** — two takes in the same direction within `cooldown_bars` (seed the first into SQLite) -> second `skip` with a cooldown reason.
- **Min filters** — `filters` below threshold -> `skip` regardless of regime.

### 5. Boundaries for this task (restating the hard rules)

- The gate returns a decision and persists it. It does **not** call any broker. Takes go to the human-approval path.
- No credentials touched. The webhook endpoint needs no broker keys; if you add a shared-secret check on the webhook, the secret is supplied by the human via `.env` (which you never read/print).
- Keep it simple: one endpoint, one `gate.py`, one pydantic model, one SQLite `signals` table, the test file. No framework beyond FastAPI.

---

## Options execution layer (design — build after the gate)

After the gate returns a `take` on the index, this layer turns it into an option trade and manages it to exit. Two parts, both deterministic for now and both logging to the journal so the meta-label can tune them later. Prerequisites: priorities #1 (Black-76 IV) and #2 (GEX regime) below.

### Strike-and-premium entry layer

Input: gate `take` = `{dir, regime, spot, atr, time, sym}`.

1. **Chain + Greeks** — fetch the option chain for `sym` + chosen expiry, enriched with IV and Greeks. *Use OpenAlgo `/optiongreeks` (Black-76) or `py_vollib` locally — do not build a solver.*
2. **Expiry** — current weekly for intraday; roll to next weekly if `DTE < min_dte` (dodge the expiry-day theta/gamma cliff).
3. **Strike** — pick by target delta band (≈0.35–0.50 for a directional buy), map `dir`→CE/PE, filter by liquidity (min OI/volume, max bid-ask spread).
4. **IV-aware entry check** — reject IV-rich strikes (IV percentile) and illiquid strikes; entry premium = ask/mid.
5. **Evaluate** (optional, mainly for spreads) — model the candidate with `optionlab` (P&L, Greeks, PoP). *Library, don't build.*
6. **Size** — by premium-at-risk: for a long option the premium paid is the max loss; size to a fixed % of equity.
7. **Output** — `{sym, expiry, strike, type, action, entry_premium, lots, stop_premium, target_premium, time_stop, rationale}` → exit gate (once filled) + approval path. Log to journal.

What you write: the selection + sizing policy (your edge). What's a library: OpenAlgo (chain/Greeks/GEX), optionlab (evaluation).

### Exit gate — separate, OR-triggered, continuous

**Principle: entry and exit are asymmetric. Entry = strict AND (every condition must agree → one careful decision). Exit = responsive OR (any single trigger fires → exit immediately).** Never reuse the entry gate for exits — waiting for confluence to exit is how winners round-trip and losers deepen.

The exit gate is a **separate monitor watching each open position every bar** (faster cadence than entry signals fire), not part of the new-signal-bar/entry logic. Any one of these triggers exits, all in premium/Greeks terms:

1. **Premium stop** — mapped from the index ATR stop via delta.
2. **Premium target / trailing stop** — target hit, or trail once in profit.
3. **Theta / time stop** — hard clock cutoff; tighter on expiry day. (No entry analog.)
4. **IV collapse** — vega turned against you, IV crushed beyond a threshold.
5. **Regime flip** — the regime that justified the trade flipped (e.g. trending→ranging); thesis gone.
6. **EOD square-off** (intraday) — non-negotiable cutoff (~15:15 IST), close everything.

On any trigger: produce an exit suggestion → approval path (human-in-loop boundary holds; no autonomous order placement). Log the exit with its trigger, premium, and P&L to the journal.

### Journal → meta-label

Every entry suggestion and every exit (trigger + features + outcome) writes to the journal. The gates are deterministic now; later, XGBoost reads the `signals` table and trade outcomes to tune which entries to take and which exit triggers fire best in which regime. Keep the schema clean and complete — this is the training data.

## Horizon as configuration (intraday now, positional later)

Make horizon a config dimension, not a code fork. A `mode` (`intraday` | `positional`) parameterizes:

- **signal timeframe** — 9m intraday / 1h–daily positional
- **expiry policy** — weekly + roll on expiry day / monthly
- **leg structure** — single long option / defined-risk spreads (verticals, calendars) via optionlab
- **regime provider** — intraday GEX/gamma-flip / a longer-horizon regime view (the intraday GEX flag does not transfer)
- **exit triggers** — intraday is dominated by the theta time-stop and EOD square-off; positional has **no** EOD square-off and **cannot** time-stop an overnight gap, so vega and gap/event guards dominate instead

Build and validate `intraday` first. Positional is then a new config plus a few horizon-specific pieces (monthly expiry, spread structuring, longer-horizon regime, weekend/event gap guards) — **not** a rewrite. Do not enable positional until intraday is validated; it doubles the risk surface (vega, overnight gaps, events).

---

## Next priorities (after the gate)

1. **Black-76 IV migration** — replace the Black-Scholes + Newton-Raphson solver with Black-76 via `py_vollib` (correct for European, forward-priced index options; `py_vollib` provides the IV solver too, so the Newton-Raphson loop can go). Verify against the old solver on a sample chain. *(No secrets, no execution.)*
2. **GEX regime signal** — per-strike GEX, net GEX, gamma flip; expose the **net-GEX sign** as a `RegimeProvider` that replaces the ADX proxy in the gate above. Convention: dealers long call gamma, short put gamma. *(No secrets, no execution.)*
3. **OpenAlgo API client** — thin client over OpenAlgo's HTTP endpoints (option chain, Greeks, quotes). **API only** — see licensing rule.
4. **GEX analytics modules** — adapt MIT-licensed GEX/Greeks code (walls, profiles) into the backend.
5. **XGBoost meta-labeling** — gather-label -> train -> deploy over the SQLite journal (time-ordered splits, purged walk-forward, calibration). Becomes the quality arbiter for the trending-regime path of the gate.
6. **Agentic layer (optional)** — briefing / journaling / review / Q&A as a tool-use loop around the deterministic core. Reads and writes notes only; never executes.

## Domain notes (so changes stay correct)

- Indian index options (NIFTY/BankNifty) are **European and effectively priced off the futures/forward**, which is why Black-76 (not Black-Scholes on spot) is correct.
- Fyers' option-chain API returns **no IV and no Greeks** — compute locally.
- For intraday, use the **current weekly expiry** (optionally summing front expiries); that's where hedging flow concentrates.
- **Net GEX sign = regime:** positive -> dealers suppress moves (pinning, mean-reversion); negative -> dealers amplify (trending, vol expansion). This is the principled replacement for the ADX regime proxy.
- The mean-reversion z-gate is **correct in ranging regimes and wrong in trending ones** — that asymmetry is the entire reason for the regime router.
- The SQLite journal/`signals` table is the **training data** for the meta-label — keep its schema clean and every signal (taken or skipped) fully recorded with features.