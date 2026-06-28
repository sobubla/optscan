# OPT.SCAN

Intraday options scanning and journaling for **NIFTY & BankNifty** — a personal, decision-support trading system. A regime-aware signal engine on the index feeds a logged, human-approved options workflow.

> **It suggests, you decide.** OPT.SCAN is not financial advice and not an automated trading system. Every trade goes through human approval.

---

## Status

**Paper-trading / data-collection phase.** The goal of this phase is **30–50 fully logged trades** before any machine-learning layer is trained. The deterministic core — signal → gate → journal — is the priority; the options strike/exit layer and the XGBoost meta-label come after.

## What it is

OPT.SCAN turns a directional index signal into a managed options-trade suggestion, with every decision recorded for later analysis.

- A **Pine Script v5** strategy on 9-minute BANKNIFTY/NIFTY **index futures** emits a raw confluence signal — a 12-filter stack plus an MSS-Trap reversal engine — as a webhook alert.
- A **FastAPI** backend receives the webhook and runs a **regime-aware gate** that decides take or skip: mean-reversion logic in ranging regimes, momentum-aligned logic in trending ones.
- Every signal, **taken or skipped**, is written to a **SQLite journal** with its full feature set — this is the training data for the meta-label.
- *(In progress)* a **strike-and-premium layer** translates a taken signal into a specific option (strike, expiry, premium entry/exit), and a separate **exit gate** monitors the open position. Both produce suggestions for human approval and place no orders.

The signal engine runs on the index; the options layer is what makes it an options system. The full build roadmap, hard rules, and design specs live in [`CLAUDE.md`](./CLAUDE.md).

## Architecture at a glance

```
TradingView (Pine v5, 9m index futures)
        │  webhook alert (JSON)
        ▼
FastAPI backend ── regime-aware gate ──► SQLite journal (taken + skipped)
        │
        ▼  (in progress)
strike-and-premium layer  ──►  exit gate (continuous OR-monitor)
        │                              │
        └──────────► human approval ◄──┘   (no autonomous orders)
```

**Build order** (dependency chain): regime-aware gate → Black-76 IV → GEX regime provider → OpenAlgo chain client → strike-and-premium layer → exit gate → XGBoost meta-label.

## Tech stack

- **Signal** — Pine Script v5 (TradingView Pro)
- **Backend** — Python, FastAPI, Uvicorn
- **Frontend** — static HTML/JS (dashboard + journal viewer, reads the journal via the backend)
- **Storage** — SQLite (the trade journal)
- **Market data / execution** — Fyers API (primary); OpenAlgo (broker-agnostic chain, Black-76 Greeks, GEX — planned)
- **Options math** — py_vollib (Black-76 IV & Greeks), optionlab (candidate evaluation)
- **ML (later)** — XGBoost meta-labeling over the journal
- **Tunnel** — ngrok (TradingView cannot reach `localhost`)

## Hard rules

Non-negotiable, and enforced in `CLAUDE.md` for agentic development:

1. **No autonomous orders.** The system decides and suggests; a human approves every trade. The "take" path is a logged suggestion, never an order.
2. **Never touch secrets.** Broker keys and tokens live in `.env`, which is never read, printed, or committed.
3. **Licensing.** OpenAlgo (AGPL) is consumed via its API only — no source copied in. MIT-licensed components may be adapted with attribution.
4. **Surgical changes.** One logical change per commit, tests first, minimal diffs.

## Repository layout

```
optscan/
├── CLAUDE.md              # roadmap, hard rules, design specs (Claude Code reads this)
├── README.md
├── .env.example
├── .gitignore             # .env and data/ must be ignored
├── requirements.txt
├── pine/
│   └── optscan_strategy.pine
├── app/
│   ├── main.py            # FastAPI app + POST /webhook/optscan
│   ├── models.py          # pydantic payload + Decision
│   ├── gate.py            # regime-aware entry gate
│   ├── regime.py          # RegimeProvider (ADX now, GEX later)
│   ├── greeks.py          # Black-76 IV / Greeks (py_vollib)
│   ├── gex.py             # per-strike + net GEX, gamma flip
│   ├── openalgo_client.py # thin HTTP client (API only)
│   ├── strike.py          # strike-and-premium entry layer
│   ├── exit_gate.py       # continuous OR-monitor
│   └── journal.py         # SQLite signals + trades
├── frontend/
│   ├── index.html         # Scanner: live 3-index dashboard (NIFTY/BankNifty/SENSEX)
│   └── journal.html       # Journal: manual trade logging, stats, breakdowns, CSV export
├── tests/
│   ├── test_gate.py
│   ├── test_strike.py
│   └── test_exit.py
└── data/
    └── optscan.db         # gitignored
```

## Setup

### Prerequisites
- Python 3.11+
- TradingView Pro (for open-ended alerts)
- Fyers API credentials
- ngrok, or another tunnel, to expose the local webhook to TradingView

### Install
```bash
git clone <your-repo-url>
cd optscan
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your credentials
```

### Configure
Set your credentials in `.env` (never commit this file):
```
FYERS_APP_ID=
FYERS_ACCESS_TOKEN=
OPENALGO_BASE_URL=
OPENALGO_API_KEY=
WEBHOOK_SHARED_SECRET=
```

### Run the backend
```bash
uvicorn app.main:app --reload --port 8000
```
Then open the **Scanner** at `http://localhost:8000/` and the **Journal** at `http://localhost:8000/journal`.

### Wire the TradingView alert
1. Paste the webhook-emit block into the Pine strategy (see `pine/`) and save.
2. Expose the backend: `ngrok http 8000`.
3. In TradingView, create an alert on the strategy with condition **"Any alert() function call"**, and set the **Webhook URL** to your ngrok HTTPS URL + `/webhook/optscan`.
4. Test the payload against `webhook.site` first to confirm the JSON, then point it at the backend.

## Frontend

Two static pages (plain HTML/CSS/JS, no build step), served by the backend at its own origin — open them once the server is running.

**`/` — Scanner.** A live three-panel dashboard for **NIFTY, BankNifty, and SENSEX**. Each panel shows spot, market context (IV regime + percentile, PCR, max pain, OI signal, latest TradingView signal), and qualifying option **setups** — strike, CE/PE, LTP, Δ/Γ/Θ/IV, suggested SL and two targets, and the reasons behind each. A positions ribbon and an exit-alerts bar sit across the top and bottom. Polls every few seconds.

**`/journal` — Trade Journal.** Manual trade logging and review for the paper-trading phase: log an entry, close a trade with an exit reason, and track today's P&L, a rolling summary (win rate, average winner/loser, expectancy), the full trade table, and P&L breakdowns by index, time of day, day of week, and exit reason — with CSV export. Refreshes every 30s.

The close-trade form's exit reasons — target hit, hard stop, profit lock, time decay, IV crush, manual — line up with the exit-gate triggers in `CLAUDE.md`, so the journal is already capturing the very labels the meta-label will train on.

### API endpoints (the contract the frontend consumes)

**Scanner** — `GET /api/scan` (per-index spot, context, setups), `GET /api/positions`, `POST /api/position/add`, `GET /api/exit-alerts`.

**Journal** (under `/api/journal`) — `GET /today`, `GET /summary`, `GET /trades`, `GET /breakdown/{index_name|time_of_day|day_of_week|exit_reason}`, `POST /trade`, `GET /trade/{id}`, `POST /trade/{id}/close`, `DELETE /trade/{id}`, `GET /export`.

**Webhook** — `POST /webhook/optscan` (the TradingView alert).

## Working with Claude Code

Agentic development is governed by [`CLAUDE.md`](./CLAUDE.md) at the repo root — launch Claude Code from the root so it loads automatically. It defines the active task, the build priorities, the hard rules, and the options-layer design. Use Plan mode, write tests first, review the diff, and commit each green step.

## Disclaimer

OPT.SCAN is a personal research and decision-support tool, **not financial advice** and **not an automated trading system**. Options trading carries a substantial risk of loss. Nothing in this repository is a recommendation to buy or sell any instrument. Use at your own risk.
