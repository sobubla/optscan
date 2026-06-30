# CLAUDE.md — OPT.SCAN

Project instructions for Claude Code. Read this at the start of every session.

## What this project is

OPT.SCAN is an intraday options scanning and journaling system for NIFTY and BankNifty on Indian markets (NSE). A TradingView Pine Script indicator generates the primary signal and emits it via webhook; a FastAPI backend receives it, applies a regime-aware gate, enriches with option-chain data, and logs everything to a SQLite journal. The system is for the author's own trading and connects to live broker accounts.

> ⚠️ This is a **live-money trading system**. Changes can have financial consequences. Bias toward caution. See **Hard rules** below.

## Architecture

> Adjust this section to match the actual repo layout — it is a starting summary, not ground truth.

- **Signal layer** — Pine Script (v13) on TradingView: 12 confluence filters + hard gates (volatility, range expansion, cooldown), MSS-Trap Reversal Engine with CVD confirmation, z-score mean reversion, FVG logic, ATR trail. **Now emits the raw confluence signal as a JSON webhook** (see the gate-extraction section); its own chart markers/backtest remain as a visual reference only.
- **Backend** — FastAPI service on `localhost:8000`. Receives the webhook, runs the **regime-aware gate** (the decision layer that used to live in Pine), persists results, and surfaces takes to the human-approval path.
- **Data** — **OpenAlgo is the primary market-data layer** (broker-agnostic, consumed via its REST + WebSocket API only): option chain, Greeks, expiry, quotes, depth, history. Fyers (`fyers_client.py`) is the legacy/fallback source. See the OpenAlgo data-layer task below.
- **IV / Greeks** — Black-76 (correct for forward-priced European index options). Server-side from OpenAlgo (`MultiOptionGreeks`, priced off the real future); `vollib` Black-76 in `fyers_client.py` is the local fallback. The old Black-Scholes + Newton-Raphson solver is retired.
- **Journal** — SQLite. Source of truth for trade history and the training data for the meta-label.
- **Dashboard** — terminal-aesthetic web UI.
- **Brokers** — Fyers (current data/exec), plus Shoonya and Angel One. **OpenAlgo abstracts the broker**, so adding Shoonya/Angel becomes config rather than a new client.

## Tech stack & running it

> Fill in the exact commands for your setup.

- Python 3.11, virtualenv.
- FastAPI + Uvicorn (backend on port 8000).
- SQLite (no server).
- Key libs: `fastapi`, `uvicorn`, `pydantic`, `requests` (HTTP), the **`openalgo`** Python client (primary data layer), `vollib`/`py_vollib` (Black-76 fallback), `optionlab` (candidate evaluation), `numpy`.

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

## Current state (what's already built)

Much of the original roadmap is **done** — trust the code, not this file's history:
- **Regime-aware gate** — `gate.py` (`Gate`, `AdxRegimeProvider`, `GexRegimeProvider`), `models.py` (`OptScanPayload`).
- **Black-76 IV + Greeks** — `fyers_client.py` via `vollib` (old Newton-Raphson solver retired).
- **GEX** — `gex.py` (per-strike / net GEX, gamma flip) feeding `GexRegimeProvider`.
- **Strike-and-premium entry layer** — `strike_selector.py` (delta band, IV percentile, premium-at-risk sizing, optionlab eval).
- **Exit gate** — `exit_monitor.py` (7 OR-triggers, premium terms, mode-aware).
- **Journal + API + dashboard** — `journal.py`, `journal_routes.py`, Scanner + Journal frontend.

Two known cleanups, both resolved by the OpenAlgo data layer below: (a) **two parallel exit systems** — `exit_monitor.py` (premium terms, matches this doc) vs `exit_manager.py` (%-terms, feeds the dashboard) — reconcile onto one; (b) **two setup finders** — `scanner._find_setups` (dashboard, Fyers chain) vs `strike_selector` (gate pipeline, OpenAlgo-shaped chain) — unify onto one chain source.

## ACTIVE TASK — Consolidate the data layer onto OpenAlgo

**Goal: make OpenAlgo the primary market-data source and shrink `fyers_client.py` to a fallback.** OpenAlgo already provides, server-side and broker-agnostic, most of what `fyers_client.py` hand-rolls. Consume the **REST + WebSocket API only** — never vendor OpenAlgo source (AGPL; see hard rules). `openalgo_client.py` exists but was written against an *assumed* response shape and is **not yet correct or wired in** — fixing and wiring it is this task.

### Use these OpenAlgo endpoints (replace the hand-rolled equivalents)

| Need | OpenAlgo endpoint | Replaces (in our code) |
|---|---|---|
| Expiry dates | `expiry(symbol, exchange, instrumenttype)` → real dated list | `fyers_client.days_to_nearest_expiry()` heuristic — **delete it** (its BankNifty "monthly ≈ Thu+14d" guess is wrong) |
| Chain prices + spot + ATM + lot size | `optionchain(underlying, exchange="NSE_INDEX", expiry_date[, strike_count])` → `underlying_ltp`, `atm_strike`, per-strike `ce`/`pe` with `ltp/bid/ask/oi/volume/lotsize/tick_size` | chain fetch, the spot-extraction hack in `_parse_chain`, `get_spot_price`, and hardcoded `lot_size`/`step` in `settings.INDICES` |
| IV + Greeks (Black-76) | `multioptiongreeks(...)` over the ATM±N symbols (pass `underlying_symbol=<FUT>` for a true forward, or `forward_price`) | local `vollib` solve in `fyers_client` (keep `vollib` as **offline fallback only**) |
| Live price / IV per bar | WebSocket `subscribe_quote` / `subscribe_ltp` (+ cached reads) | `get_spot_price` polling + the 429 retry loop |
| Backtest / feature history | `history(...)` + Historify (DuckDB) | nothing today (needed for the meta-label phase) |

### Correct the endpoint shapes (this is where `openalgo_client.py` is wrong)

- `optionchain` returns `{underlying_ltp, atm_strike, chain:[{strike, ce:{ltp,bid,ask,oi,volume,lotsize,tick_size,...}, pe:{...}}]}`. It does **not** include Greeks/IV.
- `optiongreeks` is **per-option** (one symbol → one `{implied_volatility, greeks:{delta,gamma,theta,vega,rho}, days_to_expiry, spot_price}`). The current `get_option_greeks` assuming a CE/PE *chain row* is wrong — for a chain use `multioptiongreeks`, not a single call.
- **An enriched chain = `optionchain` (prices/OI/lotsize) merged with `multioptiongreeks` (IV/Greeks) on the ATM±N strikes.** That merged per-strike shape (`call_delta`/`put_delta`/`call_oi`/`call_iv`/…) is exactly what `strike_selector.select_strike` already expects, so wiring it **completes the strike layer's intended data source**. `gex.py` / `GexRegimeProvider` consume the same enriched chain (adapt the per-strike field names).

### Sequencing (lowest-risk first; keep Fyers as fallback throughout)

1. **Fix `openalgo_client.py`** to the documented shapes (expiry, optionchain, quotes, multioptiongreeks, depth, history). Tests with mocked responses; no live calls.
2. **Wire the no-math correctness wins first** into `scanner.py`: real `expiry`, live `lotsize`, and `underlying_ltp`/`atm_strike` from the chain. **No downside** — these fix real bugs (BankNifty expiry, stale hardcoded lot sizes).
3. **Move Greeks to `multioptiongreeks`** (priced off the real future); keep `vollib` Black-76 as the offline fallback.
4. **Add the WebSocket feed for `exit_monitor.py` first** — it needs `current_premium`/`current_iv` every bar; stream the open position's option symbol instead of polling.

### Boundaries

- **API only.** No OpenAlgo source copied in (AGPL). `openalgo-mcp` is MIT and may be lifted for the future agentic layer.
- **Never log or print the OpenAlgo `apikey`** (already handled in `openalgo_client._post` — keep it that way).
- **No execution.** This task is data only. Order placement (`optionsorder`/`placesmartorder`) and OpenAlgo's **Analyzer/sandbox** mode are future, human-approved work.
- **Fallback, don't rip out.** Keep `fyers_client.py` and the local `vollib` solver as a fallback source, gated behind config, until OpenAlgo is proven live.

## Webhook gate — BUILT (reference contract)

**Status: implemented** in `gate.py` (regime router, `AdxRegimeProvider` + `GexRegimeProvider`) and `models.py` (`OptScanPayload`). Kept here as the reference for the webhook JSON contract and the gate's decision logic — **not active work**; the current active task is the OpenAlgo consolidation above.

**Why it exists:** the Pine refinement gate (FVG / Pullback / Z-Score combine) is regime-blind. A mean-reversion z-gate vetoes momentum signals in trending markets — confirmed on two BANKNIFTY charts (a long blocked at z=+1.91 in an uptrend, a short blocked at z=-1.22 in a downtrend). The fix needs the trend/range regime, which only the backend can see. So Pine now emits the raw confluence signal and the backend makes the final, regime-aware decision.

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

## Options execution layer — BUILT (`strike_selector.py` + `exit_monitor.py`)

**Status: implemented.** Reference design for the strike-and-premium entry layer (`strike_selector.py`) and the exit gate (`exit_monitor.py`) — both deterministic and logging to the journal for the meta-label. They consume an enriched option chain; the OpenAlgo consolidation (active task) feeds them the merged `optionchain` + `multioptiongreeks` chain.

### Strike-and-premium entry layer

Input: gate `take` = `{dir, regime, spot, atr, time, sym}`.

1. **Chain + Greeks** — enriched chain from the OpenAlgo data layer (`optionchain` merged with `multioptiongreeks`); `vollib` Black-76 is the local fallback. *Do not build a solver.*
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

## Next priorities

1. **Consolidate the data layer onto OpenAlgo** — the active task above. Highest priority: it fixes real correctness bugs (expiry, lot size) and unifies the two chain sources.
2. **Reconcile the duplicate exit systems and setup finders** — converge `exit_manager.py` / `exit_monitor.py`, and `scanner._find_setups` / `strike_selector`, onto one path fed by the OpenAlgo chain.
3. **XGBoost meta-labeling** — gather-label → train → deploy over the SQLite journal (time-ordered splits, purged walk-forward, calibration). The quality arbiter for the trending-regime path of the gate. Needs ~30–50 logged trades first.
4. **Agentic layer (optional)** — briefing / journaling / review / Q&A as a tool-use loop around the deterministic core; reads/writes notes only, never executes. Can use the MIT-licensed `openalgo-mcp` for live data and account reads.
5. **(Later) Human-approved execution via OpenAlgo** — route approved suggestions through `optionsorder` / `placesmartorder`, validated first in OpenAlgo's **Analyzer / sandbox** mode. Still human-in-the-loop; never autonomous.

## Domain notes (so changes stay correct)

- Indian index options (NIFTY/BankNifty) are **European and effectively priced off the futures/forward**, which is why Black-76 (not Black-Scholes on spot) is correct.
- Fyers' option-chain API returns **no IV and no Greeks** — compute locally.
- For intraday, use the **current weekly expiry** (optionally summing front expiries); that's where hedging flow concentrates.
- **Net GEX sign = regime:** positive -> dealers suppress moves (pinning, mean-reversion); negative -> dealers amplify (trending, vol expansion). This is the principled replacement for the ADX regime proxy.
- The mean-reversion z-gate is **correct in ranging regimes and wrong in trending ones** — that asymmetry is the entire reason for the regime router.
- The SQLite journal/`signals` table is the **training data** for the meta-label — keep its schema clean and every signal (taken or skipped) fully recorded with features.
- **SENSEX is intentionally excluded — do not re-add it.** The system trades **NIFTY and BankNifty only**. SENSEX was dropped on purpose (TradingView's 15-minute data delay on it, plus high correlation to NIFTY), so `settings.INDICES` is deliberately NSE-only and the scanner is 2-panel. A stale `SENSEX` panel was already removed from `frontend/index.html` once. If SENSEX is ever wanted back, the **only** correct change is adding it to `settings.INDICES` (which drives both the scan loop and the UI panels) — never by hand-adding a panel to the frontend.