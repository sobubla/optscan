# OPT.SCAN — Installation Guide

Step-by-step setup for a fresh machine. Follow every section in order the first time.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11** | 3.12+ untested with fyers-apiv3; stay on 3.11 |
| **Fyers account** | API app created at [myapi.fyers.in](https://myapi.fyers.in) — you need your Client ID and Secret Key |
| **TradingView Pro** (or higher) | Free tier does not support webhook alerts |
| **ngrok** | Exposes `localhost:8000` so TradingView can reach it; free tier works |
| **OpenAlgo** (optional) | Required for the strike-selector and exit-monitor layers; scanner and journal work without it |

---

## 1. Clone the repository

```bash
git clone <your-repo-url> options_scanner
cd options_scanner
```

---

## 2. Create and activate the virtual environment

The project uses `venv/` (not `.venv/`). Keep this name — the test runner commands assume it.

```bash
python3.11 -m venv venv
source venv/bin/activate          # macOS / Linux
# venv\Scripts\activate           # Windows
```

Confirm the right Python is active:

```bash
python --version    # should print Python 3.11.x
```

---

## 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs: FastAPI, Uvicorn, Pydantic, fyers-apiv3, py-vollib (Black-76 IV/Greeks), optionlab, numpy, pandas, requests, python-dotenv, and pytest.

Verify the install:

```bash
pip show fastapi fyers-apiv3 vollib optionlab | grep -E "^Name|^Version"
```

---

## 4. Configure Fyers credentials

Open `config/settings.py` and fill in the three lines under the **FYERS API CREDENTIALS** block:

```python
FYERS_CLIENT_ID   = "ABCD1234-100"       # from myapi.fyers.in → My Apps
FYERS_SECRET_KEY  = "your_secret_key"    # from the same app page
FYERS_REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"
```

`FYERS_REDIRECT_URI` must be registered exactly as shown in your Fyers app's Redirect URI field. The value above is the standard one — add it in your app's settings on myapi.fyers.in if it is not already there.

Leave `FYERS_ACCESS_TOKEN` blank for now — the next step generates it.

---

## 5. Generate your daily Fyers access token

Fyers tokens expire every day at end-of-session. Run this script once each morning before starting the server:

```bash
python generate_token.py
```

The script will:

1. Print a login URL — open it in your browser.
2. Log in with your Fyers credentials, PIN, and TOTP.
3. After login Fyers redirects you to a page. Copy the `auth_code` value from the browser URL bar — it is the long string between `auth_code=` and `&state=`.
4. Paste it when the script prompts.
5. The script exchanges the code for an access token and writes it directly into `config/settings.py` (`FYERS_ACCESS_TOKEN = "..."`).
6. It then verifies the token with a test API call and prints your account name.

If you see `ERROR: Edit config/settings.py first...`, go back to step 4 — the client ID is still at its placeholder value.

---

## 6. Set the webhook secret

In `config/settings.py`, change the `WEBHOOK_SECRET` to a long random string of your choosing:

```python
WEBHOOK_SECRET = "choose_a_long_random_string_here"
```

You will paste the exact same value into TradingView's alert message later (step 10). Keep it private.

---

## 7. Configure OpenAlgo (optional)

OpenAlgo is needed for the options strike selector (entry suggestions) and the exit monitor. The scanner dashboard and trade journal work without it.

If you have OpenAlgo running, create a `.env` file at the project root:

```bash
# .env  — never commit this file
OPENALGO_BASE_URL=http://127.0.0.1:8080
OPENALGO_API_KEY=your_openalgo_api_key
```

Then load it into the shell before starting the server:

```bash
export $(grep -v '^#' .env | xargs)   # macOS / Linux (bash)
```

On **Windows** (PowerShell), set the variables manually instead:
```powershell
$env:OPENALGO_BASE_URL = "http://127.0.0.1:8080"
$env:OPENALGO_API_KEY  = "your_openalgo_api_key"
```

Or add these two lines near the top of `config/settings.py` to load the file automatically (one-time edit):

```python
from dotenv import load_dotenv
load_dotenv()                          # reads .env before the os.environ.get() calls below
```

Without this, the `.env` file is not auto-loaded — the `os.environ.get()` calls in `settings.py` will return empty strings and the strike-selector will be silently skipped.

If OpenAlgo is not configured, leave both values empty and the server will start without the entry-suggestion layer.

---

## 8. Review and tune scanner thresholds (optional, first run)

These live in `config/settings.py`. Defaults are reasonable starting points; tune after paper trading:

| Setting | Default | What it controls |
|---|---|---|
| `SCAN_INTERVAL_SECONDS` | `30` | How often the option chain is re-fetched |
| `IV_PERCENTILE_GOOD` | `30` | Below this → IV cheap → setups generated |
| `IV_PERCENTILE_AVOID` | `70` | Above this → IV expensive → no setups |
| `HARD_STOP_LOSS_PCT` | `30` | Premium drop % that triggers a hard-stop alert |
| `MAX_HOLD_MINUTES` | `45` | Time-decay exit if trade has not moved |
| `GATE_MIN_FILTERS` | `9` | Min confluence filters required to `take` a webhook signal |
| `GATE_COOLDOWN_BARS` | `10` | Bars between consecutive takes on the same sym+direction |
| `GATE_ADX_THRESHOLD` | `35` | ADX ≥ this → trending regime (z-gate relaxed) |
| `ENTRY_RISK_PCT` | `0.02` | Fraction of equity risked per trade (2%) |
| `EQUITY_RUPEES` | `200000` | Trading equity for position sizing |

---

## 9. Create the logs directory

The server writes to `logs/` on startup. Create it once:

```bash
mkdir -p logs
```

---

## 10. Verify the installation with the test suite

```bash
venv/bin/pytest tests/ -v
```

All 99 tests should pass. If any fail, check that your virtual environment is active and all packages installed correctly.

---

## 11. Start the server

```bash
venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port 8000 --reload
```

You should see output like:

```
INFO:     Started server process [...]
INFO:     Application startup complete.
INFO     Scanner started
```

Open the dashboards:

| URL | Page |
|---|---|
| `http://localhost:8000/` | Scanner — live NIFTY & BANKNIFTY panels |
| `http://localhost:8000/journal` | Trade journal — log trades, view P&L, CSV export |

On first run the scanner panels show `--` for spot prices until Fyers authenticates and the first chain is fetched (up to 30 seconds).

---

## 12. Expose the server to TradingView with ngrok

TradingView webhook alerts require a public HTTPS URL. ngrok creates one for your local server:

```bash
ngrok http 8000
```

ngrok prints a forwarding URL like `https://abc123.ngrok-free.app`. Copy it — you need it for the TradingView alert.

> **Note:** ngrok free tier assigns a new random URL every time you restart it. Update the TradingView webhook URL whenever you restart ngrok. A paid ngrok plan with a fixed domain avoids this.

---

## 13. Wire the TradingView alert

1. Open TradingView and load the `OPT.SCAN` Pine Script indicator on your BANKNIFTY or NIFTY 9-minute chart.
2. Click **Alert** → **Create alert** on the indicator.
3. Set **Condition** to **OPT.SCAN** → **Any alert() function call**.
4. Under **Notifications**, enable **Webhook URL** and paste:
   ```
   https://abc123.ngrok-free.app/webhook/optscan
   ```
   (replace with your actual ngrok URL)
5. The alert **Message** box must contain the Pine `{{strategy.order.alert_message}}` placeholder — the Pine script already writes the full JSON payload there. Do not change the message format.
6. The JSON payload includes a `secret` field that must match your `WEBHOOK_SECRET` in `settings.py`. The Pine script reads this from a `input.string` — set it to the same value you chose in step 6.
7. Set the alert to **Open-ended** (no expiry) and save.

Test it: trigger a signal manually on the chart. The server terminal should log either `TAKE` or `SKIP` and the scanner page should update.

---

## 14. Daily startup routine

Fyers tokens expire each night. Every morning before trading:

```bash
# 1. Activate environment
source venv/bin/activate

# 2. Generate a fresh Fyers token (takes ~30 seconds)
python generate_token.py

# 3. Load OpenAlgo credentials (if using OpenAlgo) — bash/macOS/Linux only
# On Windows PowerShell: $env:OPENALGO_BASE_URL="..."; $env:OPENALGO_API_KEY="..."
export $(grep -v '^#' .env | xargs)

# 4. Start the server
venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port 8000

# 5. In a separate terminal, start ngrok
ngrok http 8000
```

If your ngrok URL changed (free tier), update the TradingView alert webhook URL before the session.

---

## 15. Logging a paper trade (journal workflow)

Once a setup appears on the scanner:

1. Open `http://localhost:8000/journal`.
2. Click the **+** button (bottom right) to open the new-trade form.
3. Fill in index, strike, type (CE/PE), quantity, lot size, entry price, entry IV, and entry spot.
4. Set **Entry reason** to `System signal` and optionally link the **Signal ID** from the scanner log.
5. When you close the trade, click **Close** on the trade row and fill in exit price and exit reason.

The journal tracks P&L in rupees (`quantity × lot_size × (exit − entry)`), hold time, and rolling win-rate / expectancy — visible in the top ribbon.

---

## 16. Troubleshooting

**Scanner shows `--` for spot / panels empty after 30s**

- The Fyers token may have expired. Run `python generate_token.py` and restart the server.
- Check `logs/scanner.log` for API errors.

**Webhook returns 401**

- The `secret` field in the TradingView alert message does not match `WEBHOOK_SECRET` in `settings.py`.

**Webhook returns 400 / validation error**

- The Pine Script payload format does not match the expected schema. Check `logs/scanner.log` for the validation detail. The required fields are documented in `CLAUDE.md → Webhook contract`.

**Dashboard shows DEGRADED / "entries paused"**

- The scanner panel header turns amber and shows an "entries paused" reason when a data-quality problem is detected. Common causes:
  - **Expiry unavailable** — OpenAlgo could not return a real expiry date for that index. No entry suggestion will be produced (by design — the system refuses to trade on a guessed expiry). Check that OpenAlgo is running and reachable.
  - **Chain empty** — OpenAlgo returned no strikes for the expiry. May be a pre-market or holiday condition.
  - **OpenAlgo unavailable** — `OPENALGO_BASE_URL` is set but the server is unreachable. The scanner falls back to Fyers for display but entry suggestions are blocked.
  - **IV history cold** — fewer than 20 IV samples recorded; strike selector will reject every entry until history builds up (first ~20 scan cycles).

**Strike selector never fires / no entry suggestions**

- `OPENALGO_BASE_URL` and `OPENALGO_API_KEY` are not set, or not loaded into the environment before the server starts. See step 7.
- Check `logs/scanner.log` for `ENTRY SKIP` or `Strike selector error`.

**ngrok tunnel disconnects**

- Free ngrok sessions time out after a few hours. Keep the ngrok terminal open and restart it when needed.

**`pytest` fails with `ModuleNotFoundError`**

- The virtual environment is not activated. Run `source venv/bin/activate` first.

**`fyers_apiv3` import error**

- The token was generated for a different app. Make sure `FYERS_CLIENT_ID` in `settings.py` matches the app whose redirect URI is `https://trade.fyers.in/api-login/redirect-uri/index.html`.

---

## Project layout (actual)

```
options_scanner/
├── CLAUDE.md               # design specs and hard rules for agentic development
├── INSTALL.md              # this file
├── README.md               # project overview
├── requirements.txt        # dependencies (floor-pinned with >=)
├── generate_token.py       # daily Fyers auth-code → access-token flow
├── config/
│   └── settings.py         # all thresholds and credentials (not committed with real values)
├── backend/
│   ├── server.py           # FastAPI app — uvicorn entry point
│   ├── scanner.py          # option chain scanning engine
│   ├── gate.py             # regime-aware webhook gate
│   ├── exit_manager.py     # OR-triggered exit alerts (scanner positions)
│   ├── exit_monitor.py     # OR-triggered exit alerts (gate-layer positions)
│   ├── strike_selector.py  # delta-band strike selection + optionlab evaluation
│   ├── fyers_client.py     # Fyers API wrapper (chain, spot, IV/Greeks via Black-76)
│   ├── openalgo_client.py  # OpenAlgo HTTP client (chain + Greeks, optional)
│   ├── journal.py          # SQLite CRUD — signals, trades, suggestions
│   ├── journal_routes.py   # FastAPI router for /api/journal/*
│   ├── gex.py              # per-strike GEX, net GEX, gamma flip
│   └── models.py           # Pydantic payload models
├── frontend/
│   ├── index.html          # scanner dashboard (NIFTY + BANKNIFTY)
│   └── journal.html        # trade journal UI
├── ml/
│   └── label.py            # meta-label data preparation (priority #5)
├── tests/
│   ├── test_gate.py
│   ├── test_gex.py
│   ├── test_iv_greeks.py
│   ├── test_openalgo_client.py
│   ├── test_position_guard.py
│   ├── test_scanner.py
│   ├── test_server_expiry.py
│   ├── test_strike.py
│   └── test_exit_monitor.py
├── data/
│   ├── journal.db          # SQLite database (auto-created on first run)
│   └── iv_history.json     # per-index ATM IV history for percentile calc
└── logs/
    └── scanner.log         # rotating log (create the directory before first run)
```

---

## Security checklist before going live

- [ ] `config/settings.py` is in `.gitignore` (it holds live credentials).
- [ ] `.env` is in `.gitignore`.
- [ ] `data/journal.db` is in `.gitignore` (contains trade history).
- [ ] `WEBHOOK_SECRET` is a unique random string, not a dictionary word.
- [ ] No credentials printed to console or written to `logs/`.
- [ ] OpenAlgo API key has read-only scope if your broker supports it.

---

*OPT.SCAN is a personal decision-support tool. It suggests; you decide. No part of this system places, modifies, or cancels orders without explicit human approval.*
