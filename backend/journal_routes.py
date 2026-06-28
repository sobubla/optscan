"""
Journal API routes. Mount these into your existing FastAPI app.

Add to backend/server.py:
    from backend.journal_routes import router as journal_router
    app.include_router(journal_router)

And add to the startup lifespan:
    from backend.journal import init_db
    init_db()
"""

import logging
from datetime import datetime
from typing import Optional, List
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import journal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journal", tags=["journal"])


# =================== Pydantic Models ===================
class SignalLog(BaseModel):
    index_name: str
    direction: str  # bullish/bearish
    spot_price: Optional[float] = None
    atm_strike: Optional[int] = None
    confluence_score: Optional[int] = None
    iv_percentile: Optional[float] = None
    iv_regime: Optional[str] = None
    pcr: Optional[float] = None
    max_pain: Optional[int] = None
    oi_signal: Optional[str] = None
    tv_signal: Optional[str] = None
    vol_state: Optional[str] = None
    vol_z_score: Optional[float] = None
    filter_states: Optional[dict] = None
    recommended_strikes: Optional[list] = None


class TradeEntry(BaseModel):
    signal_id: Optional[int] = None
    index_name: str
    strike: int
    option_type: str = Field(..., pattern="^(CE|PE)$")
    quantity: int = Field(..., gt=0)
    lot_size: int = Field(..., gt=0)
    entry_price: float = Field(..., gt=0)
    entry_iv: Optional[float] = None
    entry_spot: Optional[float] = None
    entry_reason: str = "system_signal"  # system_signal | manual_discretion | news_play
    notes: str = ""


class TradeExit(BaseModel):
    exit_price: float = Field(..., gt=0)
    exit_reason: str  # hard_stop / profit_lock / time_decay / iv_crush / manual_exit / target_hit
    exit_iv: Optional[float] = None
    exit_spot: Optional[float] = None
    max_favorable_pct: Optional[float] = None
    max_adverse_pct: Optional[float] = None
    notes: str = ""


class TradeNote(BaseModel):
    note: str


# =================== Routes ===================
@router.post("/signal")
async def log_signal(payload: SignalLog):
    """Log a signal fired by the scanner."""
    signal_id = journal.log_signal(payload.dict())
    return {"signal_id": signal_id}


@router.post("/trade")
async def open_trade(payload: TradeEntry):
    """Log a new trade entry."""
    trade_id = journal.add_trade(payload.dict())
    return {"trade_id": trade_id}


@router.post("/trade/{trade_id}/close")
async def close_trade(trade_id: int, payload: TradeExit):
    """Close an open trade."""
    success = journal.close_trade(trade_id, payload.dict())
    if not success:
        raise HTTPException(404, "Trade not found or already closed")
    return {"status": "closed", "trade_id": trade_id}


@router.post("/trade/{trade_id}/note")
async def add_note(trade_id: int, payload: TradeNote):
    """Add a note to an existing trade."""
    success = journal.add_trade_note(trade_id, payload.note)
    if not success:
        raise HTTPException(404, "Trade not found")
    return {"status": "ok"}


@router.delete("/trade/{trade_id}")
async def delete_trade(trade_id: int):
    """Delete a trade (mistakes only)."""
    success = journal.delete_trade(trade_id)
    if not success:
        raise HTTPException(404, "Trade not found")
    return {"status": "deleted"}


@router.get("/trades")
async def list_trades(
    status: Optional[str] = Query(None, pattern="^(open|closed)$"),
    days_back: Optional[int] = Query(None, ge=1, le=365),
    limit: int = Query(100, ge=1, le=1000),
):
    """List trades with optional filters."""
    return journal.list_trades(status=status, limit=limit, days_back=days_back)


@router.get("/trade/{trade_id}")
async def get_trade(trade_id: int):
    """Get one trade by ID."""
    trade = journal.get_trade(trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    return trade


@router.get("/today")
async def today_summary():
    """Today's P&L summary - used by dashboard ribbon."""
    return journal.get_today_pnl()


@router.get("/summary")
async def summary_stats(days_back: int = Query(30, ge=1, le=365)):
    """Overall performance over N days."""
    return journal.get_summary_stats(days_back)


@router.get("/breakdown/{dimension}")
async def breakdown(dimension: str, days_back: int = Query(30, ge=1, le=365)):
    """
    Performance breakdown by dimension.
    Dimensions: index_name, time_of_day, day_of_week, option_type, exit_reason, entry_reason
    """
    try:
        return journal.get_breakdown_by(dimension, days_back)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/export")
async def export_csv(days_back: int = Query(90, ge=1, le=365)):
    """Export trades to CSV. Returns the file."""
    export_path = Path("/tmp") / f"trades_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    count = journal.export_to_csv(str(export_path), days_back)
    return FileResponse(export_path, media_type="text/csv", filename=export_path.name)
