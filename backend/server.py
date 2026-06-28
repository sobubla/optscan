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
from backend.journal import (
    init_db, log_signal, log_optscan_signal,
    log_entry_suggestion, get_pending_suggestions, reject_suggestion,
    log_exit_suggestion, get_pending_exit_suggestions,
    reject_exit_suggestion, approve_entry_suggestion,
)
from backend.journal_routes import router as journal_router
from backend.models import OptScanPayload
from backend.gate import Gate, AdxRegimeProvider, GexRegimeProvider
from backend.openalgo_client import OpenAlgoClient
import backend.strike_selector as strike_selector
import backend.exit_monitor as exit_monitor
from backend.position_guard import check_position_conflicts

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
_regime_provider: AdxRegimeProvider | GexRegimeProvider = (
    GexRegimeProvider()
    if getattr(settings, "GATE_REGIME_PROVIDER", "adx") == "gex"
    else AdxRegimeProvider(adx_threshold=settings.GATE_ADX_THRESHOLD)
)
gate = Gate(
    regime_provider=_regime_provider,
    min_filters=settings.GATE_MIN_FILTERS,
    cooldown_bars=settings.GATE_COOLDOWN_BARS,
    require_fvg=settings.GATE_REQUIRE_FVG,
    require_pullback=settings.GATE_REQUIRE_PULLBACK,
)
_openalgo: Optional[OpenAlgoClient] = (
    OpenAlgoClient(settings.OPENALGO_BASE_URL, settings.OPENALGO_API_KEY)
    if settings.OPENALGO_BASE_URL and settings.OPENALGO_API_KEY
    else None
)
# Gate-layer open positions tracked by the exit monitor (keyed by position_id).
# Populated when a human approves an entry suggestion via POST /api/suggestion/{id}/approve.
_active_positions: Dict[str, exit_monitor.OpenPosition] = {}

# Reversal conflict advisories: keyed by sym.
# Set when an opposite-direction signal fires while a position is open.
# Cleared automatically when the conflicting position exits, or manually via dismiss.
_position_conflicts: Dict[str, dict] = {}
latest_scans: Dict[str, dict] = {}

# Track when we last recorded daily IV per index (one record/day)
_last_iv_record_date: Dict[str, str] = {}

# Track signals we've already logged this session
_logged_signal_keys = set()


def _find_ltp(chain: list, strike: int, option_type: str) -> Optional[float]:
    """Find the LTP for a specific strike/option_type in an OpenAlgo-format chain."""
    key = "call_ltp" if option_type == "CE" else "put_ltp"
    for row in chain:
        if row.get("strike") == strike:
            v = row.get(key)
            return float(v) if v else None
    return None


def _find_iv(chain: list, strike: int, option_type: str) -> Optional[float]:
    """Find the IV for a specific strike/option_type in an OpenAlgo-format chain."""
    key = "call_iv" if option_type == "CE" else "put_iv"
    for row in chain:
        if row.get("strike") == strike:
            v = row.get(key)
            return float(v) if v and float(v) > 0 else None
    return None


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

                    # Push fresh chain to GEX regime provider if active
                    if isinstance(gate.regime_provider, GexRegimeProvider):
                        spot = result.get("spot", 0) if isinstance(result, dict) else 0
                        if spot > 0:
                            lot_size = settings.INDICES[idx].get("lot_size", 1)
                            chain_for_gex = fyers.get_option_chain(idx)
                            gate.regime_provider.update_chain(chain_for_gex, spot, lot_size)

                    # Exit monitor: check gate-layer positions for this index.
                    # Uses OpenAlgo chain (OpenAlgo-format LTP/IV); skipped if not configured.
                    active_for_idx = [
                        p for p in _active_positions.values()
                        if p.sym.upper() == idx.upper()
                    ]
                    if active_for_idx and _openalgo:
                        try:
                            expiry = strike_selector.next_weekly_expiry(
                                date.today(), min_dte=settings.ENTRY_MIN_DTE
                            )
                            ol_chain = _openalgo.get_option_greeks(idx, expiry)
                            for pos in active_for_idx:
                                ltp = _find_ltp(ol_chain, pos.strike, pos.option_type)
                                if ltp is None:
                                    continue
                                iv = _find_iv(ol_chain, pos.strike, pos.option_type)
                                state = exit_monitor.MarketState(
                                    current_premium=ltp,
                                    current_iv=iv if iv is not None else pos.entry_iv,
                                    current_regime=gate.regime_provider.current_regime,
                                    now=datetime.now(),
                                )
                                signal = exit_monitor.check_exit(pos, state, settings)
                                if signal:
                                    from dataclasses import asdict
                                    log_exit_suggestion(asdict(signal))
                                    del _active_positions[pos.position_id]
                                    # Clear any reversal advisory for this sym now that
                                    # the position is gone — the conflict is resolved.
                                    _position_conflicts.pop(pos.sym, None)
                                    logger.warning(
                                        "EXIT SIGNAL — %s trigger=%s pnl=%.1f%% [awaiting approval]",
                                        pos.position_id, signal.trigger, signal.pnl_pct,
                                    )
                        except Exception:
                            logger.exception("Exit monitor error for %s — positions retained", idx)

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
    optscan_id = log_optscan_signal(
        payload.model_dump(), decision.take, decision.regime, decision.reason
    )
    if decision.take:
        logger.info("TAKE — %s [awaiting human approval]", decision.reason)
    else:
        logger.info("SKIP — %s", decision.reason)

    suggestion_data = None
    blocked_reason = None
    conflict_data = None

    if decision.take:
        # Position guard: check before hitting the strike selector.
        # Signal is already logged above (training data preserved regardless).
        guard_reason, guard_pos_id = check_position_conflicts(
            payload.sym, payload.dir, _active_positions
        )

        if guard_reason == "position_already_open":
            blocked_reason = "position_already_open"
            logger.info(
                "SUGGESTION BLOCKED — %s %s already open (pos: %s)",
                payload.sym,
                "CE" if payload.dir == "long" else "PE",
                guard_pos_id,
            )

        elif guard_reason == "opposite_position_open":
            blocked_reason = "opposite_position_open"
            conflict_data = {
                "sym": payload.sym,
                "new_dir": payload.dir,
                "new_option_type": "CE" if payload.dir == "long" else "PE",
                "existing_option_type": "PE" if payload.dir == "long" else "CE",
                "existing_position_id": guard_pos_id,
                "signal_time": datetime.now().isoformat(),
                "regime": decision.regime,
                "filters": payload.filters,
            }
            _position_conflicts[payload.sym] = conflict_data
            logger.warning(
                "REVERSAL SIGNAL — %s %s fired while %s open (pos: %s)",
                payload.sym,
                "CE" if payload.dir == "long" else "PE",
                "PE" if payload.dir == "long" else "CE",
                guard_pos_id,
            )

        elif _openalgo:
            # No conflict and OpenAlgo is configured → generate entry suggestion.
            try:
                from dataclasses import asdict
                expiry = strike_selector.next_weekly_expiry(
                    date.today(), min_dte=settings.ENTRY_MIN_DTE
                )
                chain = _openalgo.get_option_greeks(payload.sym, expiry)
                iv_hist = fyers._iv_history.get(payload.sym.upper(), [])
                suggestion = strike_selector.evaluate(
                    sym=payload.sym,
                    direction=payload.dir,
                    regime=decision.regime,
                    spot=payload.price,
                    atr=payload.atr,
                    chain=chain,
                    iv_history=iv_hist,
                    mode=settings.ENTRY_MODE,
                    config=settings,
                )
                if suggestion:
                    suggestion_data = asdict(suggestion)
                    log_entry_suggestion(suggestion_data, optscan_id=optscan_id)
                    logger.info(
                        "SUGGESTION — %s %s%s @%.1f lots=%d [%s]",
                        suggestion.sym, suggestion.strike, suggestion.option_type,
                        suggestion.entry_premium, suggestion.lots, suggestion.rationale,
                    )
            except Exception:
                logger.exception(
                    "Strike selector error — gate take recorded, selector skipped"
                )

    return {
        "take": decision.take,
        "direction": decision.direction,
        "regime": decision.regime,
        "reason": decision.reason,
        "features": decision.features,
        "suggestion": suggestion_data,
        "blocked_reason": blocked_reason,
        "conflict": conflict_data,
    }


@app.get("/api/pending-suggestions")
async def pending_suggestions_endpoint():
    return get_pending_suggestions()


@app.post("/api/suggestion/{suggestion_id}/reject")
async def reject_suggestion_endpoint(suggestion_id: int):
    ok = reject_suggestion(suggestion_id)
    if not ok:
        raise HTTPException(404, "Suggestion not found")
    return {"status": "rejected"}


@app.post("/api/suggestion/{suggestion_id}/approve")
async def approve_suggestion_endpoint(suggestion_id: int):
    """
    Approve an entry suggestion: marks it approved in the journal and opens
    a gate-layer position in the exit monitor. Human is responsible for
    actually placing the trade — this endpoint only registers it for exit monitoring.
    """
    row = approve_entry_suggestion(suggestion_id)
    if not row:
        raise HTTPException(404, "Suggestion not found or already actioned")
    pos_id = f"{row['sym']}_{row['strike']}{row['option_type']}_{suggestion_id}"
    direction = "long" if row["option_type"] == "CE" else "short"
    pos = exit_monitor.OpenPosition(
        position_id=pos_id,
        sym=row["sym"],
        expiry=row["expiry"],
        strike=row["strike"],
        option_type=row["option_type"],
        direction=direction,
        entry_premium=row["entry_premium"],
        stop_premium=row["stop_premium"],
        target_premium=row["target_premium"],
        entry_iv=row["iv"],
        entry_regime=row["regime"],
        entry_time=datetime.now(),
        time_stop=row["time_stop"],
        mode=row.get("mode", "intraday"),
        peak_premium=row["entry_premium"],
    )
    _active_positions[pos_id] = pos
    logger.info("POSITION OPENED — %s via approved suggestion #%d", pos_id, suggestion_id)
    return {"position_id": pos_id, "status": "approved"}


@app.get("/api/active-positions")
async def active_positions_endpoint():
    """Return all active gate-layer positions currently monitored by the exit gate."""
    from dataclasses import asdict
    return [asdict(p) for p in _active_positions.values()]


@app.get("/api/pending-exit-suggestions")
async def pending_exit_suggestions_endpoint():
    return get_pending_exit_suggestions()


@app.post("/api/exit-suggestion/{suggestion_id}/reject")
async def reject_exit_suggestion_endpoint(suggestion_id: int):
    ok = reject_exit_suggestion(suggestion_id)
    if not ok:
        raise HTTPException(404, "Exit suggestion not found")
    return {"status": "rejected"}


@app.get("/api/position-conflicts")
async def position_conflicts_endpoint():
    """Return active reversal conflict advisories (opposite-direction signal while position open)."""
    return list(_position_conflicts.values())


@app.post("/api/position-conflict/{sym}/dismiss")
async def dismiss_position_conflict_endpoint(sym: str):
    """Dismiss a reversal conflict advisory for a given symbol."""
    _position_conflicts.pop(sym.upper(), None)
    _position_conflicts.pop(sym, None)
    return {"status": "dismissed"}


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
