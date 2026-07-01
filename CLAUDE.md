# CLAUDE.md — OPT.SCAN

Project instructions for Claude Code. Read this at the start of every session.

## What this project is

OPT.SCAN is an intraday options scanning and journaling system for NIFTY and BankNifty on Indian markets (NSE). A TradingView Pine Script indicator generates the primary signal and emits it via webhook; a FastAPI backend receives it, applies a regime-aware gate, enriches with option-chain data, and logs everything to a SQLite journal. The system is for the author's own trading and connects to live broker accounts.

> ⚠️ This is a **live-money trading system**. Changes can have financial consequences. Bias toward caution. See **Hard rules** below.

## Architecture

> Adjust this section to match the actual repo layout — it is a starting summary, not ground truth.

- **Signal layer** — Pine Script (v13) on TradingView: 12 confluence filters + hard gates (volatility, range expansion, cooldown), MSS-Trap Reversal Engine with CVD confirmation, z-score mean reversion, FVG logic, ATR trail. **Emits the raw confluence signal as a JSON webhook** (see the gate section); its own chart markers/backtest remain a visual reference only.
- **Backend** — FastAPI service on `localhost:8000`. Receives the webhook, runs the **regime-aware gate** (the decision layer that used to live in Pine), persists results, and surfaces takes to the human-approval path.
- **Data** — **OpenAlgo is the primary market-data layer** (broker-agnostic, consumed via its REST + WebSocket API only): option chain, expiry, quotes, depth, history. `fyers_client.py` is the legacy/fallback source, gated behind config. See the OpenAlgo data-layer section.
- **IV / Greeks** — Black-76 (correct for forward-priced European index options), computed **locally via `vollib`** in `fyers_client.py` and reused by the OpenAlgo enrichment path. This uses `F = spot·e^(rT)` as the forward (a carry approximation), because the installed OpenAlgo SDK has **no `multioptiongreeks`** endpoint for whole-chain Greeks. For forward-accurate Greeks on the few strikes actually traded, the option is per-symbol `optiongreeks` with `underlying_symbol=<FUT>` — not yet done; the carry approximation is small for short-dated index options. The old Black-Scholes + Newton-Raphson solver is retired.
- **Journal** — SQLite. Source of truth for trade history and the training data for the meta-label.
- **Dashboard** — terminal-aesthetic web UI.
- **Brokers** — Fyers (current data/exec), plus Shoonya and Angel One. **OpenAlgo abstracts the broker**, so adding Shoonya/Angel becomes config rather than a new client.

## Tech stack & running it

> Fill in the exact commands for your setup.

- Python 3.11, virtualenv.
- FastAPI + Uvicorn (backend on port 8000).
- SQLite (no server).
- Key libs: `fastapi`, `uvicorn`, `pydantic`, `requests` (HTTP), the **`openalgo`** Python client (primary data layer), `vollib`/`py_vollib` (Black-76), `optionlab` (candidate evaluation), `numpy`.

```bash
# example — replace with your real commands
source venv/bin/activate
uvicorn app.main:app --reload --port 8000
pytest
```

## Hard rules — non-negotiable

These protect real money, real credentials, and this repo's license position. Do not violate them even if a single task asks you to. If a request conflicts with one, **stop and flag it** rather than proceeding.

**Secrets & credentials**
- Never read, modify, print, log, or commit broker API keys, tokens, TOTP secrets, the OpenAlgo `apikey`, or the `.env` file.
- All credential handling is done by the human, outside of you. If a task appears to need a secret, ask the human to supply or configure it themselves.

**Execution & order placement**
- Never write code that places, modifies, or cancels live orders autonomously.
- The **deterministic core** (signal → gate → meta-label → risk gate) owns execution. Any LLM/agentic component reads, reasons, and writes notes only — it must not decide or place trades.
- The gate's job ends at returning a decision and persisting it. On a "take", surface to the human-approval / semi-auto path — **do not auto-execute**.
- Semi-automatic with explicit human approval is the maximum autonomy. No fully autonomous trading paths.

**Licensing (this repo is the author's own work)**
- OpenAlgo's core is **AGPL v3.0**. Consume it **only via its HTTP/WebSocket API**. **Never copy OpenAlgo source into this repo** — it would impose AGPL copyleft on OPT.SCAN.
- Only **permissively licensed** code (MIT/BSD/Apache) may be adapted in — e.g. `aakash-code/GammaGEX`, `KaranChavan21/GEX_Dashboard`, `openalgo-mcp` (all MIT). Preserve attribution/license headers.
- Before pulling in any third-party code, check its license. If not permissive, consume via API or do not use it.

## Working style

This project has the `karpathy-guidelines` skill installed; follow it. In short:

- **Think first.** State assumptions explicitly. If multiple interpretations exist, present them rather than picking silently. If a simpler approach exists, say so.
- **Simplicity first.** Write the minimum code that solves the task. No speculative features, abstractions, or configurability that wasn't asked for.
- **Surgical changes.** Touch only what the task requires. Don't refactor or reformat adjacent code. Match existing style. Mention unrelated dead code; don't delete it. **Stay within the task's stated scope — if you find yourself expanding it, surface the expansion and let the human decide.**
- **Goal-driven.** Turn each task into a verifiable goal and confirm it (run the tests) before declaring done. For multi-step work, state a brief plan with a verify step per item.

Workflow:
- Assume work starts from a clean git tree. Make one logical change per commit so each is a reviewable, revertible diff.
- Before changing anything in an unfamiliar area, read it and summarize the structure back before editing.
- Run the test suite after each change. Never blanket-assume success on this system.
- When a test fails and you change the test to make it pass, **say why it failed** — a failing test may be exposing a real bug, not a wrong test.

---

## Current state (what's already built)

Most of the original roadmap is **done** — trust the code, not this file's history:
- **Regime-aware gate** — `gate.py` (`Gate`, `AdxRegimeProvider`, `GexRegimeProvider`), `models.py` (`OptScanPayload`).
- **Black-76 IV + Greeks** — `fyers_client.py` via `vollib` (old Newton-Raphson solver retired).
- **GEX** — `gex.py` (per-strike / net GEX, gamma flip) feeding `GexRegimeProvider`.
- **Strike-and-premium entry layer** — `strike_selector.py` (delta band, IV percentile, premium-at-risk sizing, optionlab eval).
- **Exit gate** — `exit_monitor.py` (7 OR-triggers, premium terms, mode-aware).
- **Journal + API + dashboard** — `journal.py`, `journal_routes.py`, Scanner + Journal frontend.
- **OpenAlgo data layer** — `openalgo_client.py` fixed to the real API shapes and wired in: real `expiry`, live `lotsize`, and `underlying_ltp`/`atm_strike` now come from OpenAlgo, with the Fyers path as a config-gated fallback.
- **Data-health surface** — `/api/scan` carries a per-index `health` field (`state`/`reason`/`ts`); the dashboard flips the header to **DEGRADED** and shows "entries paused: &lt;reason&gt;" on affected panels, visually distinct from "no qualifying setups." Degraded triggers: OpenAlgo chain unavailable (Fyers fallback active), expiry from heuristic, empty chain, cold IV history; an uncaught scan exception shows **ERROR**. **An empty chain now degrades cleanly** (previously a `calculate_max_pain` crash), and health uses **cause-over-symptom precedence** — a root cause like "OpenAlgo unavailable" wins over the downstream symptom "chain empty."

**Known issues — fix deliberately; don't let them get patched over silently:**
- **The expiry fallback still GUESSES a weekday — the one open trade-path risk.** `strike_selector.next_weekly_expiry()` assumes **Thursday**; `fyers_client.days_to_nearest_expiry()` assumes Thursday/Tuesday/Thursday+14. **NIFTY weekly expiry is now Tuesday**; BankNifty weeklies were discontinued. `_pick_expiry()` now returns `(expiry, from_heuristic)` and the dashboard shows DEGRADED when the heuristic fires — so the fallback is now *visible* — **but the entry path still proceeds on the wrong guess.** Making it **fail loud and SKIP** (refuse to trade) is the ACTIVE TASK below. *Visibility ≠ refusing to trade.*
- **Cold-start entry gate:** with no `iv_history.json`, `iv_percentile` returns its conservative `100` ("unknown = rich"), so `strike_selector` rejects **every** entry until IV history accumulates. Surfaced on the dashboard as DEGRADED ("IV history cold") below `_IV_HISTORY_MIN_SAMPLES` (20). By design, but know it — and keep that threshold consistent with whatever governs the rejection.
- **Two parallel exit systems** (`exit_monitor.py` premium-terms vs `exit_manager.py` %-terms feeding the dashboard) and **two setup finders** (`scanner._find_setups` vs `strike_selector`) still want reconciling onto one path.

## ACTIVE TASK — Expiry hardening: fail loud, never guess a weekday

> **Status — what's left.** The data-health *surface* is built (the dashboard already shows DEGRADED when the heuristic fires), and `_pick_expiry()` already signals `from_heuristic`. The remaining work is the **core guarantee**: make trade-affecting paths **fail loud and SKIP** rather than proceed on the guessed weekday. Surfacing the fallback is done; refusing to trade on it is not.

**The problem.** NSE moved **NIFTY weekly expiry to Tuesday**, discontinued BankNifty weeklies, and keeps changing the expiry calendar. The OpenAlgo path is correct because it reads the **real published expiry list** (`get_expiry`) and never assumes a weekday. But the fallbacks (`next_weekly_expiry` = Thursday; `days_to_nearest_expiry` = Thursday/Tuesday/Thursday+14) are weekday-hardcoded and now wrong. They stay invisible until OpenAlgo is unavailable — the worst time to discover a wrong-expiry bug, because it would build a position against a contract series that doesn't exist.

**Principle: no hardcoded expiry weekday anywhere on a trade-affecting path.** Real expiry comes from `get_expiry()`. A wrong-expiry guess is one of the few data bugs that can silently put you in the **wrong instrument entirely** — so guessing is not an acceptable fallback.

**Fail loud, don't guess.** On any **trade-affecting** path (entry suggestion, the scan-loop chain fetch, the exit monitor resolving a position's expiry), if OpenAlgo cannot supply a real expiry — not configured, API error, empty list, or a date that won't parse — the system must:
1. **Log a clear error** and **skip** (no entry suggestion), matching the existing entry-skip pattern (logged reason, no suggestion) — **never** fall back to a guessed weekday.
2. **Record a structured, machine-readable status** (per-index: `state`, `reason`, `timestamp`) so a later frontend surface can read it. Not only a log line.

**Disposition of the heuristics:** rip the weekday guess out of every trade-affecting path. `next_weekly_expiry` / `days_to_nearest_expiry` may survive **only** for the display-only dashboard DTE (where a wrong DTE skews a shown IV number but never picks a contract), and only behind a loud warning — or be deleted if the dashboard DTE is also moved to OpenAlgo. Decide this explicitly; do not leave silently-wrong heuristics on a live path.

**Verify reality:** confirm what the installed `openalgo` `expiry()` actually returns (that Tuesday dates appear, in the `DDMMMYY` format the parser expects) — a `strptime` format mismatch would silently trigger the fallback anyway.

**The surface is already built (done).** `/api/scan` carries the per-index `health` field and the dashboard renders DEGRADED + "entries paused" — the *visibility* half is complete. What this task adds is the **refuse-to-trade** half: on the trade-affecting paths, **skip** rather than guess, and emit the skip status the surface already knows how to render. The whole point: a blank panel must never be ambiguous between "nothing qualified" and "blind, not trading" — and a *visible* wrong-expiry guess is still a wrong-expiry trade.

### Boundaries
- No credentials touched; no order placement. This is expiry-correctness + status only.
- Introduce **no** new hardcoded expiry weekday.
- Surgical changes; one logical change per commit; full suite green before done.

## OpenAlgo data layer — largely complete (reference)

OpenAlgo is the primary market-data source; `openalgo_client.py` is fixed to the real API and wired in. Consume the **REST + WebSocket API only** — never vendor OpenAlgo source (AGPL).

### Endpoints — status
| Need | OpenAlgo endpoint | Status |
|---|---|---|
| Expiry dates | `expiry(symbol, exchange, instrumenttype)` → real dated list | **wired** (replaces the weekday heuristics — see ACTIVE TASK) |
| Chain prices + spot + ATM + lot size | `optionchain(underlying, exchange="NSE_INDEX", expiry_date[, strike_count])` → `underlying_ltp`, `atm_strike`, per-strike `ce`/`pe` with `ltp/bid/ask/oi/volume/lotsize/tick_size` | **wired** (replaced the spot-extraction hack + hardcoded lot sizes) |
| IV + Greeks | computed **locally via `vollib` Black-76** from the `optionchain` (the SDK has **no `multioptiongreeks`**) | **done (local)** — forward = `spot·e^(rT)`; for forward accuracy on traded strikes, per-symbol `optiongreeks(symbol, underlying_symbol=<FUT>)` is the option, not yet done |
| Live price / IV per bar | WebSocket `subscribe_quote` / `subscribe_ltp` (+ cached reads) | **pending** — feed `exit_monitor` first (needs `current_premium`/`current_iv` every bar) instead of polling |
| Backtest / feature history | `history(...)` + Historify | pending (meta-label phase) |

### Real endpoint shapes (verified against the installed SDK)
- `optionchain` → `{underlying_ltp, atm_strike, chain:[{strike, ce:{ltp,bid,ask,oi,volume,lotsize,...}, pe:{...}}]}`. The chain has **no** Greeks/IV.
- `optiongreeks` is **per-option** (one symbol → one `{implied_volatility, greeks:{...}, days_to_expiry, spot_price}`). There is **no `multioptiongreeks`** in the installed SDK — do not call it. Whole-chain Greeks are computed locally from the chain.
- The enriched chain (`optionchain` + local `vollib` Greeks) is normalised to the per-strike `call_*`/`put_*` shape that `strike_selector.select_strike` and `gex.py` expect.

### Remaining
- WebSocket feed for `exit_monitor` (replace polling).
- (Optional, accuracy) per-symbol `optiongreeks` off the future for the handful of traded strikes.

### Boundaries
- API only (AGPL); `openalgo-mcp` (MIT) may be lifted for the future agentic layer.
- Never log/print the OpenAlgo `apikey` (handled in `openalgo_client` — keep it).
- No execution; Analyzer/sandbox + `optionsorder` are future, human-approved work.
- Fyers + local `vollib` stay as config-gated fallbacks.

## Webhook gate — BUILT (reference contract)

**Status: implemented** in `gate.py` (regime router, `AdxRegimeProvider` + `GexRegimeProvider`) and `models.py` (`OptScanPayload`). Kept here as the reference for the webhook JSON contract and the gate's decision logic — **not active work**.

**Why it exists:** the Pine refinement gate (FVG / Pullback / Z-Score combine) is regime-blind. A mean-reversion z-gate vetoes momentum signals in trending markets — confirmed on two BANKNIFTY charts (a long blocked at z=+1.91 in an uptrend, a short blocked at z=-1.22 in a downtrend). The fix needs the trend/range regime, which only the backend can see. So Pine emits the raw confluence signal and the backend makes the final, regime-aware decision.

### 1. Webhook contract (what Pine sends)

Pine fires `alert()` with this flat JSON on every raw confluence signal (count >= min filters + EMA-state gate + volatility gate + range gate, on bar close, **before** cooldown / refinement / regime). The pydantic model matches it exactly:

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
   - Determine `regime`. **Initial proxy: `trending` if `adx >= adx_threshold` else `ranging`.** Pluggable via a `RegimeProvider` interface so it swaps to net-GEX sign (trending = negative gamma, ranging = positive gamma) with no change to the gate. `adx_threshold` in config (35 to match the Pine default).
   - **`ranging`** -> apply the mean-reversion z-gate as Pine intended: long requires `z_long_zone`, short requires `z_short_zone`. (Buy dips / sell rallies.)
   - **`trending`** -> **suppress the mean-reversion z-requirement.** Allow the entry aligned with the confluence direction (the 12-filter count already confirmed direction). Optionally reject only the far opposite extreme (e.g. a long with `z >= +z_max`). Defer final quality to the meta-label.
5. If all pass -> `take`; else `skip`.

Return `Decision{ take: bool, direction, regime, reason: str, features: dict }`.

**Reason string** — mirror the Pine tooltip style so the backend stays as readable as the chart:
- `"LONG skip — regime: ranging; z=+1.91 outside long zone [-3,-1]; filters 11/12"`
- `"LONG take — regime: trending; z-gate relaxed; filters 11/12"`
- `"SHORT skip — cooldown: last short 4 bars ago (need 10)"`

### 4. Tests (`tests/test_gate.py`) — encode the two real screenshots

These pass:
- **Screenshot 1** — long payload, `z=+1.91`, `filters>=10`, `adx=40` (trending) -> `take`, regime `trending`.
- Same payload, `adx=20` (ranging) -> `skip`, reason names z outside the long zone.
- **Screenshot 2** — short payload, `z=-1.22`, `filters>=10`, `adx=40` (trending) -> `take`, regime `trending`.
- Same payload, `adx=20` (ranging) -> `skip`.
- **Cooldown** — two takes in the same direction within `cooldown_bars` -> second `skip` with a cooldown reason.
- **Min filters** — `filters` below threshold -> `skip` regardless of regime.

---

## Options execution layer — BUILT (`strike_selector.py` + `exit_monitor.py`)

**Status: implemented.** Reference design for the strike-and-premium entry layer (`strike_selector.py`) and the exit gate (`exit_monitor.py`) — both deterministic and logging to the journal for the meta-label. They consume the enriched option chain (`optionchain` + local `vollib` Greeks).

### Strike-and-premium entry layer

Input: gate `take` = `{dir, regime, spot, atr, time, sym}`.

1. **Chain + Greeks** — enriched chain from the OpenAlgo data layer (`optionchain` + local `vollib` Black-76 Greeks). *Do not build a solver.*
2. **Expiry** — the real nearest expiry from OpenAlgo `get_expiry()`, rolled forward if `DTE < min_dte` (dodge the expiry-day theta/gamma cliff). **No hardcoded weekday** — see the ACTIVE TASK.
3. **Strike** — pick by target delta band (≈0.35–0.50 for a directional buy), map `dir`→CE/PE, filter by liquidity (min OI/volume, max bid-ask spread).
4. **IV-aware entry check** — reject IV-rich strikes (IV percentile) and illiquid strikes; entry premium = ask/mid.
5. **Evaluate** (optional, mainly for spreads) — model the candidate with `optionlab` (P&L, Greeks, PoP). *Library, don't build.*
6. **Size** — by premium-at-risk: for a long option the premium paid is the max loss; size to a fixed % of equity.
7. **Output** — `{sym, expiry, strike, type, action, entry_premium, lots, stop_premium, target_premium, time_stop, rationale}` → exit gate (once filled) + approval path. Log to journal.

What you write: the selection + sizing policy (your edge). What's a library: OpenAlgo (chain), `vollib` (Greeks), optionlab (evaluation).

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

1. **Expiry hardening (ACTIVE)** — the task above: no hardcoded weekday on any trade path; **fail loud and skip** (not just show DEGRADED) when real expiry is unavailable. The open trade-path guarantee.
2. **Reconcile the duplicates** — converge the two exit systems (`exit_monitor.py` / `exit_manager.py`) and the two setup finders (`scanner._find_setups` / `strike_selector`) onto one OpenAlgo-fed path.
3. **WebSocket feed** for `exit_monitor` (replace polling); optionally per-symbol `optiongreeks` off the future for forward-accurate Greeks on traded strikes.
4. **XGBoost meta-labeling** — gather-label → train → deploy over the SQLite journal (time-ordered splits, purged walk-forward, calibration). The quality arbiter for the trending-regime path of the gate. Needs ~30–50 logged trades first.
5. **Agentic layer (optional)** — briefing / journaling / review / Q&A as a tool-use loop around the deterministic core; reads/writes notes only, never executes. Can use the MIT-licensed `openalgo-mcp` for live data and account reads.
6. **(Later) Human-approved execution via OpenAlgo** — route approved suggestions through `optionsorder` / `placesmartorder`, validated first in OpenAlgo's **Analyzer / sandbox** mode. Still human-in-the-loop; never autonomous.

## Domain notes (so changes stay correct)

- Indian index options (NIFTY/BankNifty) are **European and effectively priced off the futures/forward**, which is why Black-76 (not Black-Scholes on spot) is correct.
- **No hardcoded expiry weekday.** **NIFTY weekly expiry is now Tuesday** (NSE moved it); BankNifty weeklies were discontinued; the calendar keeps changing. Real expiry comes from OpenAlgo `get_expiry()` **only**. Any weekday-hardcoded heuristic is a latent wrong-instrument bug — see the ACTIVE TASK.
- **Greeks use a carry-approximated forward.** Local `vollib` Black-76 uses `F = spot·e^(rT)` because the installed SDK has no whole-chain Greeks endpoint (`multioptiongreeks` does not exist). Fine for short-dated index options; for forward accuracy on traded strikes, use per-symbol `optiongreeks` off the future.
- Fyers' option-chain API returns **no IV and no Greeks** — computed locally; OpenAlgo's `optionchain` also carries no Greeks (enrich locally).
- For intraday, use the **current weekly expiry** (optionally summing front expiries); that's where hedging flow concentrates.
- **Net GEX sign = regime:** positive -> dealers suppress moves (pinning, mean-reversion); negative -> dealers amplify (trending, vol expansion). The principled replacement for the ADX regime proxy.
- The mean-reversion z-gate is **correct in ranging regimes and wrong in trending ones** — that asymmetry is the entire reason for the regime router.
- The SQLite journal/`signals` table is the **training data** for the meta-label — keep its schema clean and every signal (taken or skipped) fully recorded with features.
- **SENSEX is intentionally excluded — do not re-add it.** The system trades **NIFTY and BankNifty only**. SENSEX was dropped on purpose (TradingView's 15-minute data delay on it, plus high correlation to NIFTY), so `settings.INDICES` is deliberately NSE-only and the scanner is 2-panel. A stale `SENSEX` panel was already removed from `frontend/index.html` once. If SENSEX is ever wanted back, the **only** correct change is adding it to `settings.INDICES` (which drives both the scan loop and the UI panels) — never by hand-adding a panel to the frontend.