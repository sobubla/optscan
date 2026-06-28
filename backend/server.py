"""
FastAPI server (v3 - with IV history persistence + chain reuse fix).

Endpoints:
- /                         : Scanner dashboard
- /journal                  : Trade journal dashboard
- /webhook/tradingview      : receives Pine Script alerts
- /health                   : health check (for uptime monitors)
- /api/scan                 : scanner results
- /api/positions            : open positions (from exit manager)
- /api/exit-alerts          : pending exit alerts
- /api/position/add         : register a position
- /api/position/.../close   : mark closed
- /api/journal/*            : trade journal endpoints (see journal_routes.py)

Run: uvicorn backend.server:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import settings
from backend.fyers_client import FyersDataClient
from backend.scanner import OptionScanner
from backend.exit_manager import ExitManager
from backend.journal import init_db, log_signal, log_optscan_signal
from backend.journal_routes import router as journal_router
from backend.models import OptScanPayload
from backend.gate import Gate, AdxRegimeProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/scanner.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---- Global state ----
fyers = FyersDataClient()
scanner = OptionScanner(fyers)
exits = ExitManager()
gate = Gate(
    regime_provider=AdxRegimeProvider(adx_threshold=settings.GATE_ADX_THRESHOLD),
    min_filters=settings.GATE_MIN_FILTERS,
    cooldown_bars=settings.GATE_COOLDOWN_BARS,
    require_fvg=settings.GATE_REQUIRE_FVG,
    require_pullback=settings.GATE_REQUIRE_PULLBACK,
)
latest_scans: Dict[str, dict] = {}

# Track when we last recorded daily IV per index (one record/day)
_last_iv_record_date: Dict[str, str] = {}

# Track signals we've already logged this session
_logged_signal_keys = set()


def _signal_key(index: str, scan: dict) -> Optional[str]:
    """Build a key that uniquely identifies a 'signal moment'.
    Returns None if there's nothing worth logging (no setups)."""
    setups = scan.get("setups") or []
    if not setups:
        return None
    top = setups[0]
    return f"{index}:{top['strike']}{top['type']}:{top['direction']}:{top['confluence_score']}"


def _maybe_log_signal(index: str, scan: dict):
    """If the scan has setups and we haven't logged this exact signal yet, log it."""
    key = _signal_key(index, scan)
    if not key or key in _logged_signal_keys:
        return
    _logged_signal_keys.add(key)

    top = scan["setups"][0]
    ctx = scan.get("market_context", {})

    try:
        log_signal({
            "timestamp": scan.get("timestamp"),
            "index_name": index,
            "direction": top["direction"],
            "spot_price": scan.get("spot"),
            "atm_strike": scan.get("atm"),
            "confluence_score": top.get("confluence_score"),
            "iv_percentile": ctx.get("iv_percentile"),
            "iv_regime": ctx.get("iv_regime"),
            "pcr": ctx.get("pcr"),
            "max_pain": ctx.get("max_pain"),
            "oi_signal": str(ctx.get("oi_signal")),
            "tv_signal": ctx.get("tv_signal"),
            "vol_state": ctx.get("vol_state"),
            "vol_z_score": ctx.get("vol_z_score"),
            "filter_states": ctx.get("filter_states", {}),
            "recommended_strikes": scan["setups"],
        })
        logger.info(f"Signal logged: {index} {top['strike']}{top['type']} (conf={top['confluence_score']})")
    except Exception as e:
        logger.exception(f"Failed to log signal: {e}")


def _maybe_record_iv_history(index: str, scan: dict):
    """Record ATM IV once per calendar day so iv_percentile has data to work with.
    Idempotent: first scan after midnight records, rest of day skipped."""
    today_str = date.today().isoformat()
    if _last_iv_record_date.get(index) == today_str:
        return

    avg_iv = scan.get("market_context", {}).get("avg_atm_iv", 0)
    if avg_iv > 0:
        try:
            fyers.update_iv_history(index, avg_iv)
            _last_iv_record_date[index] = today_str
            logger.info(f"Recorded daily IV history: {index} = {avg_iv}%")
        except Exception as e:
            logger.exception(f"Failed to record IV history for {index}: {e}")


async def scan_loop():
    """Background task: scans all indices every N seconds."""
    while True:
        try:
            for idx in settings.INDICES.keys():
                try:
                    result = scanner.scan_index(idx)
                    latest_scans[idx] = result

                    # Record today's ATM IV once per day
                    _maybe_record_iv_history(idx, result)

                    # Log signal to journal (deduped by key)
                    _maybe_log_signal(idx, result)

                    # Update live prices for open positions.
                    # Fetch the chain only IF there's an open position for this index
                    # (avoids unnecessary API calls when no positions are open).
                    open_for_this_idx = [p for p in exits.get_open_positions() if p["index"] == idx]
                    if open_for_this_idx:
                        chain = fyers.get_option_chain(idx)
                        for pos in open_for_this_idx:
                            match = next(
                                (o for o in chain
                                 if o["strike"] == pos["strike"] and o["type"] == pos["option_type"]),
                                None
                            )
                            if match:
                                exits.update_position(
                                    pos["position_id"],
                                    match["ltp"],
                                    match["iv"]
                                )
                except Exception as e:
                    # Don't let one index failure crash the whole loop
                    logger.exception(f"Scan failed for {idx}: {e}")

            exits.check_exits()
        except Exception as e:
            logger.exception(f"Scan loop error: {e}")
        await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    fyers.connect()
    init_db()
    task = asyncio.create_task(scan_loop())
    logger.info("Scanner started")
    yield
    task.cancel()
    logger.info("Scanner stopped")


app = FastAPI(title="Options Scanner", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(journal_router)


# ============= Pydantic Models =============
class TVWebhookPayload(BaseModel):
    secret: str
    index: str
    action: str
    price: Optional[float] = None
    note: Optional[str] = None


class PositionEntry(BaseModel):
    index: str
    strike: int
    option_type: str
    entry_price: float
    quantity: int
    entry_iv: float


# ============= Routes =============
@app.post("/webhook/optscan")
async def optscan_webhook(payload: OptScanPayload):
    if payload.secret != settings.WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid webhook secret")
    decision = gate.evaluate(payload)
    log_optscan_signal(payload.model_dump(), decision.take, decision.regime, decision.reason)
    if decision.take:
        logger.info("TAKE — %s [awaiting human approval]", decision.reason)
    else:
        logger.info("SKIP — %s", decision.reason)
    return {
        "take": decision.take,
        "direction": decision.direction,
        "regime": decision.regime,
        "reason": decision.reason,
        "features": decision.features,
    }


@app.post("/webhook/tradingview")
async def tradingview_webhook(payload: TVWebhookPayload):
    if payload.secret != settings.WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid webhook secret")
    if payload.index not in settings.INDICES:
        raise HTTPException(400, f"Unknown index: {payload.index}")

    scanner.register_tv_signal(payload.index, payload.dict())
    return {"status": "ok", "received": payload.dict()}


@app.get("/health")
async def health():
    """Lightweight health check for Render and uptime monitors. Always 200."""
    return {
        "status": "ok",
        "service": "opt-scan",
        "timestamp": datetime.now().isoformat(),
        "indices_scanned": list(latest_scans.keys()),
        "open_positions": len(exits.get_open_positions()),
        "iv_history_size": {
            idx: len(fyers._iv_history.get(idx, []))
            for idx in settings.INDICES.keys()
        },
    }


@app.get("/api/scan")
async def get_scans():
    return latest_scans


@app.get("/api/scan/{index}")
async def get_scan_for(index: str):
    if index not in latest_scans:
        raise HTTPException(404, f"No scan data for {index}")
    return latest_scans[index]


@app.get("/api/positions")
async def get_positions():
    return exits.get_open_positions()


@app.get("/api/exit-alerts")
async def get_exit_alerts():
    return exits.get_recent_alerts()


@app.post("/api/position/add")
async def add_position(entry: PositionEntry):
    if entry.index not in settings.INDICES:
        raise HTTPException(400, "Unknown index")
    pos_id = exits.add_position(
        index=entry.index,
        strike=entry.strike,
        option_type=entry.option_type,
        entry_price=entry.entry_price,
        quantity=entry.quantity,
        lot_size=settings.INDICES[entry.index]["lot_size"],
        entry_iv=entry.entry_iv,
    )
    return {"position_id": pos_id}


@app.post("/api/position/{pos_id}/close")
async def close_position(pos_id: str):
    exits.close_position(pos_id)
    return {"status": "closed"}


# ============= Dashboard Pages =============
@app.get("/")
async def dashboard():
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    return FileResponse(html_path)


@app.get("/journal")
async def journal_page():
    """Trade journal dashboard."""
    html_path = Path(__file__).parent.parent / "frontend" / "journal.html"
    return FileResponse(html_path)
