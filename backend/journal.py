"""
Trade Journal - SQLite-backed journal for every trade taken.

Schema design:
- 'signals' table: every signal the system fires (whether you trade it or not)
- 'trades' table: every trade you actually took (linked to a signal)
- 'analytics' views: pre-computed queries for the dashboard

This separation is intentional. Logging ALL signals (including ones you skipped)
later lets you analyze: "what's the win rate of signals I skipped vs took?"
That data is gold for understanding your discretion edge or lack thereof.
"""

import sqlite3
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "journal.db"


def get_connection():
    """Get a SQLite connection with row factory for dict-like access."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call repeatedly."""
    conn = get_connection()
    try:
        conn.executescript("""
        -- Every signal the system generates, whether you trade it or not
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            index_name TEXT NOT NULL,          -- NIFTY / BANKNIFTY / SENSEX
            direction TEXT NOT NULL,           -- bullish / bearish
            spot_price REAL,
            atm_strike INTEGER,
            confluence_score INTEGER,
            iv_percentile REAL,
            iv_regime TEXT,
            pcr REAL,
            max_pain INTEGER,
            oi_signal TEXT,
            tv_signal TEXT,
            vol_state TEXT,                    -- OK / LOW VOL
            vol_z_score REAL,
            filter_states TEXT,                -- JSON of all 9 filter states
            recommended_strikes TEXT,          -- JSON of suggested setups
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
        CREATE INDEX IF NOT EXISTS idx_signals_index ON signals(index_name);

        -- Trades you actually took
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,                 -- nullable: discretionary trades have no signal
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            index_name TEXT NOT NULL,
            strike INTEGER NOT NULL,
            option_type TEXT NOT NULL,         -- CE / PE
            quantity INTEGER NOT NULL,         -- number of lots
            lot_size INTEGER NOT NULL,

            entry_price REAL NOT NULL,
            exit_price REAL,
            entry_iv REAL,
            exit_iv REAL,
            entry_spot REAL,
            exit_spot REAL,

            -- Outcome
            pnl_rupees REAL,                   -- calculated on close
            pnl_pct REAL,                      -- (exit - entry) / entry * 100
            max_favorable_pct REAL,            -- peak gain during hold
            max_adverse_pct REAL,              -- worst drawdown during hold
            hold_minutes INTEGER,

            -- Decisions
            exit_reason TEXT,                  -- hard_stop / profit_lock / time_decay / iv_crush / manual_exit / target_hit
            entry_reason TEXT,                 -- system_signal / manual_discretion / news_play

            -- Context
            time_of_day TEXT,                  -- morning / midday / afternoon / close
            day_of_week TEXT,                  -- Mon, Tue, ... Fri
            is_expiry_day BOOLEAN DEFAULT 0,
            notes TEXT,                        -- your free-text notes

            status TEXT DEFAULT 'open',        -- open / closed
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);

        -- Trigger to update updated_at on row changes
        CREATE TRIGGER IF NOT EXISTS trades_updated_at
        AFTER UPDATE ON trades
        BEGIN
            UPDATE trades SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
        END;

        -- optscan webhook signals: every Pine payload + gate decision.
        -- Never delete rows — this table is the meta-label training set.
        CREATE TABLE IF NOT EXISTS optscan_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            -- payload fields (names match OptScanPayload exactly)
            v            TEXT    NOT NULL,
            sym          TEXT    NOT NULL,
            tf           TEXT    NOT NULL,
            dir          TEXT    NOT NULL,
            bar_time     INTEGER NOT NULL,
            price        REAL    NOT NULL,
            atr          REAL    NOT NULL,
            adx          REAL    NOT NULL,
            filters      INTEGER NOT NULL,
            f_ema        INTEGER NOT NULL,
            f_rsi        INTEGER NOT NULL,
            f_vol        INTEGER NOT NULL,
            f_vwap       INTEGER NOT NULL,
            f_mvwap      INTEGER NOT NULL,
            f_band       INTEGER NOT NULL,
            f_cvd        INTEGER NOT NULL,
            f_st         INTEGER NOT NULL,
            f_macd       INTEGER NOT NULL,
            f_poc        INTEGER NOT NULL,
            f_mss        INTEGER NOT NULL,
            f_adx        INTEGER NOT NULL,
            z            REAL    NOT NULL,
            z_long_zone  INTEGER NOT NULL,
            z_short_zone INTEGER NOT NULL,
            z_bull_pa    INTEGER NOT NULL,
            z_bear_pa    INTEGER NOT NULL,
            fvg_ok       INTEGER NOT NULL,
            pb_ok        INTEGER NOT NULL,
            vol_ok       INTEGER NOT NULL,
            range_ratio  REAL    NOT NULL,
            bars_since   INTEGER NOT NULL,
            hh           INTEGER NOT NULL,
            ll           INTEGER NOT NULL,
            ext_long     INTEGER NOT NULL,
            ext_short    INTEGER NOT NULL,
            mss_state    INTEGER NOT NULL,
            -- gate decision
            take         INTEGER NOT NULL,
            regime       TEXT    NOT NULL,
            reason       TEXT    NOT NULL,
            created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_optscan_sym_dir_take
            ON optscan_signals(sym, dir, take);
        """)
        conn.commit()
        logger.info(f"Journal DB ready at {DB_PATH}")
    finally:
        conn.close()


# ============================================================================
# SIGNAL LOGGING
# ============================================================================
def log_signal(signal_data: Dict[str, Any]) -> int:
    """
    Log a signal fired by the scanner.

    signal_data should contain:
        timestamp, index_name, direction, spot_price, atm_strike,
        confluence_score, iv_percentile, iv_regime, pcr, max_pain,
        oi_signal, tv_signal, vol_state, vol_z_score,
        filter_states (dict), recommended_strikes (list)
    """
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO signals (
                timestamp, index_name, direction, spot_price, atm_strike,
                confluence_score, iv_percentile, iv_regime, pcr, max_pain,
                oi_signal, tv_signal, vol_state, vol_z_score,
                filter_states, recommended_strikes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_data.get("timestamp", datetime.now().isoformat()),
            signal_data["index_name"],
            signal_data["direction"],
            signal_data.get("spot_price"),
            signal_data.get("atm_strike"),
            signal_data.get("confluence_score"),
            signal_data.get("iv_percentile"),
            signal_data.get("iv_regime"),
            signal_data.get("pcr"),
            signal_data.get("max_pain"),
            signal_data.get("oi_signal"),
            signal_data.get("tv_signal"),
            signal_data.get("vol_state"),
            signal_data.get("vol_z_score"),
            json.dumps(signal_data.get("filter_states", {})),
            json.dumps(signal_data.get("recommended_strikes", [])),
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ============================================================================
# TRADE CRUD
# ============================================================================
def add_trade(trade_data: Dict[str, Any]) -> int:
    """
    Log a new trade entry. Most fields auto-computed if not provided.

    Required: index_name, strike, option_type, quantity, lot_size, entry_price
    Optional: signal_id (link to signal), entry_iv, entry_spot, entry_reason, notes
    """
    entry_time = datetime.now()
    day_of_week = entry_time.strftime("%a")

    hour = entry_time.hour
    if hour < 10 or (hour == 9 and entry_time.minute < 30):
        tod = "open"
    elif hour < 11:
        tod = "morning"
    elif hour < 14:
        tod = "midday"
    elif hour < 15:
        tod = "afternoon"
    else:
        tod = "close"

    # Detect Thursday expiry for index options
    is_expiry = day_of_week == "Thu"

    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO trades (
                signal_id, entry_time, index_name, strike, option_type,
                quantity, lot_size, entry_price, entry_iv, entry_spot,
                entry_reason, time_of_day, day_of_week, is_expiry_day, notes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (
            trade_data.get("signal_id"),
            entry_time.isoformat(),
            trade_data["index_name"],
            trade_data["strike"],
            trade_data["option_type"],
            trade_data["quantity"],
            trade_data["lot_size"],
            trade_data["entry_price"],
            trade_data.get("entry_iv"),
            trade_data.get("entry_spot"),
            trade_data.get("entry_reason", "system_signal"),
            tod,
            day_of_week,
            is_expiry,
            trade_data.get("notes", ""),
        ))
        conn.commit()
        trade_id = cur.lastrowid
        logger.info(f"Trade logged: id={trade_id} {trade_data['index_name']} {trade_data['strike']}{trade_data['option_type']}")
        return trade_id
    finally:
        conn.close()


def close_trade(trade_id: int, exit_data: Dict[str, Any]) -> bool:
    """
    Close a trade. Computes pnl_rupees, pnl_pct, hold_minutes automatically.

    Required in exit_data: exit_price, exit_reason
    Optional: exit_iv, exit_spot, max_favorable_pct, max_adverse_pct, notes
    """
    conn = get_connection()
    try:
        trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False
        if trade["status"] == "closed":
            logger.warning(f"Trade {trade_id} already closed")
            return False

        exit_time = datetime.now()
        entry_time = datetime.fromisoformat(trade["entry_time"])
        hold_minutes = int((exit_time - entry_time).total_seconds() / 60)

        entry_price = trade["entry_price"]
        exit_price = exit_data["exit_price"]
        quantity = trade["quantity"]
        lot_size = trade["lot_size"]

        pnl_rupees = (exit_price - entry_price) * quantity * lot_size
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100

        # Merge notes if both exist
        existing_notes = trade["notes"] or ""
        new_notes = exit_data.get("notes", "")
        merged_notes = (existing_notes + "\n--- EXIT ---\n" + new_notes).strip() if new_notes else existing_notes

        conn.execute("""
            UPDATE trades SET
                exit_time = ?, exit_price = ?, exit_iv = ?, exit_spot = ?,
                pnl_rupees = ?, pnl_pct = ?, hold_minutes = ?,
                max_favorable_pct = ?, max_adverse_pct = ?,
                exit_reason = ?, notes = ?, status = 'closed'
            WHERE id = ?
        """, (
            exit_time.isoformat(),
            exit_price,
            exit_data.get("exit_iv"),
            exit_data.get("exit_spot"),
            pnl_rupees,
            pnl_pct,
            hold_minutes,
            exit_data.get("max_favorable_pct"),
            exit_data.get("max_adverse_pct"),
            exit_data["exit_reason"],
            merged_notes,
            trade_id,
        ))
        conn.commit()
        logger.info(f"Trade {trade_id} closed: pnl={pnl_rupees:+.0f} ({pnl_pct:+.1f}%) reason={exit_data['exit_reason']}")
        return True
    finally:
        conn.close()


def get_trade(trade_id: int) -> Optional[Dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_trades(status: Optional[str] = None, limit: int = 100, days_back: Optional[int] = None) -> List[Dict]:
    """List trades, optionally filtered by status and date range."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM trades WHERE 1=1"
        params = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if days_back is not None:
            cutoff = datetime.now().timestamp() - days_back * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()
            sql += " AND entry_time >= ?"
            params.append(cutoff_iso)
        sql += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_trade(trade_id: int) -> bool:
    """Delete a trade (use sparingly — typically for mistakes)."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_trade_note(trade_id: int, note: str) -> bool:
    """Append a note to an existing trade."""
    conn = get_connection()
    try:
        trade = conn.execute("SELECT notes FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not trade:
            return False
        existing = trade["notes"] or ""
        timestamp = datetime.now().strftime("%H:%M")
        new_notes = f"{existing}\n[{timestamp}] {note}".strip()
        conn.execute("UPDATE trades SET notes = ? WHERE id = ?", (new_notes, trade_id))
        conn.commit()
        return True
    finally:
        conn.close()


# ============================================================================
# ANALYTICS QUERIES
# ============================================================================
def get_summary_stats(days_back: int = 30) -> Dict:
    """Overall performance summary."""
    conn = get_connection()
    try:
        cutoff = (datetime.now().timestamp() - days_back * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()

        row = conn.execute("""
            SELECT
                COUNT(*)                                              AS total_trades,
                SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END)       AS winners,
                SUM(CASE WHEN pnl_rupees < 0 THEN 1 ELSE 0 END)       AS losers,
                COALESCE(SUM(pnl_rupees), 0)                          AS total_pnl,
                COALESCE(AVG(CASE WHEN pnl_rupees > 0 THEN pnl_rupees END), 0) AS avg_winner,
                COALESCE(AVG(CASE WHEN pnl_rupees < 0 THEN pnl_rupees END), 0) AS avg_loser,
                COALESCE(MAX(pnl_rupees), 0)                          AS best_trade,
                COALESCE(MIN(pnl_rupees), 0)                          AS worst_trade,
                COALESCE(AVG(hold_minutes), 0)                        AS avg_hold_min
            FROM trades
            WHERE status = 'closed' AND entry_time >= ?
        """, (cutoff_iso,)).fetchone()

        stats = dict(row)
        total = stats["total_trades"] or 0
        winners = stats["winners"] or 0
        stats["win_rate"] = round((winners / total) * 100, 1) if total > 0 else 0
        # Expectancy = (win_rate × avg_winner) + (loss_rate × avg_loser)
        if total > 0:
            loss_rate = (stats["losers"] or 0) / total
            win_rate = winners / total
            stats["expectancy"] = round(win_rate * stats["avg_winner"] + loss_rate * stats["avg_loser"], 2)
        else:
            stats["expectancy"] = 0
        stats["days_back"] = days_back
        return stats
    finally:
        conn.close()


def get_breakdown_by(dimension: str, days_back: int = 30) -> List[Dict]:
    """
    Performance breakdown by a dimension.
    Valid dimensions: index_name, time_of_day, day_of_week, option_type, exit_reason, entry_reason
    """
    valid_dims = {"index_name", "time_of_day", "day_of_week", "option_type", "exit_reason", "entry_reason"}
    if dimension not in valid_dims:
        raise ValueError(f"Invalid dimension. Use one of: {valid_dims}")

    conn = get_connection()
    try:
        cutoff = (datetime.now().timestamp() - days_back * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()

        rows = conn.execute(f"""
            SELECT
                {dimension}                                          AS bucket,
                COUNT(*)                                              AS trades,
                SUM(CASE WHEN pnl_rupees > 0 THEN 1 ELSE 0 END)       AS winners,
                COALESCE(SUM(pnl_rupees), 0)                          AS total_pnl,
                COALESCE(AVG(pnl_rupees), 0)                          AS avg_pnl,
                COALESCE(AVG(hold_minutes), 0)                        AS avg_hold_min
            FROM trades
            WHERE status = 'closed' AND entry_time >= ?
            GROUP BY {dimension}
            ORDER BY total_pnl DESC
        """, (cutoff_iso,)).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["win_rate"] = round((d["winners"] / d["trades"]) * 100, 1) if d["trades"] > 0 else 0
            result.append(d)
        return result
    finally:
        conn.close()


def get_today_pnl() -> Dict:
    """Quick query: today's P&L and trade count. Used for the daily-loss-limit check."""
    conn = get_connection()
    try:
        today = date.today().isoformat()
        row = conn.execute("""
            SELECT
                COUNT(*)                                              AS trades_today,
                COALESCE(SUM(pnl_rupees), 0)                          AS pnl_today,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END)      AS open_positions
            FROM trades
            WHERE date(entry_time) = ?
        """, (today,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def export_to_csv(filepath: str, days_back: int = 90) -> int:
    """Export trades to CSV for external analysis. Returns row count."""
    import csv
    conn = get_connection()
    try:
        cutoff_iso = datetime.fromtimestamp(datetime.now().timestamp() - days_back * 86400).isoformat()
        rows = conn.execute("""
            SELECT * FROM trades WHERE entry_time >= ? ORDER BY entry_time DESC
        """, (cutoff_iso,)).fetchall()

        if not rows:
            Path(filepath).write_text("")
            return 0

        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        return len(rows)
    finally:
        conn.close()


# ============================================================================
# OPTSCAN SIGNAL LOGGING (webhook gate path)
# ============================================================================

def log_optscan_signal(payload: dict, take: bool, regime: str, reason: str) -> int:
    """
    Persist one optscan webhook signal + gate decision.
    `payload` is the result of OptScanPayload.model_dump().
    Called for every signal, taken or skipped — never filtered.
    """
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO optscan_signals (
                v, sym, tf, dir, bar_time, price, atr, adx, filters,
                f_ema, f_rsi, f_vol, f_vwap, f_mvwap, f_band, f_cvd, f_st,
                f_macd, f_poc, f_mss, f_adx,
                z, z_long_zone, z_short_zone, z_bull_pa, z_bear_pa,
                fvg_ok, pb_ok, vol_ok, range_ratio, bars_since,
                hh, ll, ext_long, ext_short, mss_state,
                take, regime, reason
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
        """, (
            payload["v"], payload["sym"], payload["tf"], payload["dir"],
            payload["bar_time"], payload["price"], payload["atr"], payload["adx"],
            payload["filters"],
            int(payload["f_ema"]), int(payload["f_rsi"]), int(payload["f_vol"]),
            int(payload["f_vwap"]), int(payload["f_mvwap"]), int(payload["f_band"]),
            int(payload["f_cvd"]), int(payload["f_st"]), int(payload["f_macd"]),
            int(payload["f_poc"]), int(payload["f_mss"]), int(payload["f_adx"]),
            payload["z"],
            int(payload["z_long_zone"]), int(payload["z_short_zone"]),
            int(payload["z_bull_pa"]), int(payload["z_bear_pa"]),
            int(payload["fvg_ok"]), int(payload["pb_ok"]), int(payload["vol_ok"]),
            payload["range_ratio"], payload["bars_since"],
            int(payload["hh"]), int(payload["ll"]),
            int(payload["ext_long"]), int(payload["ext_short"]),
            payload["mss_state"],
            int(take), regime, reason,
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_last_taken(sym: str, direction: str) -> Optional[dict]:
    """
    Return the most recent taken optscan signal for sym+dir, or None.
    Used by the gate's cooldown check.
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT bar_time, tf, created_at
            FROM optscan_signals
            WHERE sym = ? AND dir = ? AND take = 1
            ORDER BY bar_time DESC
            LIMIT 1
        """, (sym, direction)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


if __name__ == "__main__":
    # Quick CLI test
    init_db()
    print("Journal initialized at:", DB_PATH)
    print("Today:", get_today_pnl())
    print("30-day summary:", get_summary_stats(30))
