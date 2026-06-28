"""
OPT.SCAN meta-label preparation — data + labeling step (CLAUDE.md priority #5).

Join path:
    optscan_signals (every raw signal + gate decision + 12-filter feature vector)
    → LEFT JOIN entry_suggestions  (entry metadata for gate takes)
    → LEFT JOIN exit_suggestions   (outcome: pnl_pct, trigger)

Label definition (binary):
    label = 1  →  pnl_pct > threshold   (profitable trade; default threshold = 0.0%)
    label = 0  →  pnl_pct ≤ threshold   (loss or breakeven)

Only take=1 signals that have a completed exit get a label.
Skipped signals (take=0) and takes without exits yet are not labeled — they
appear in the "pending" count and can be labeled later as positions close.

The labeled dataset becomes training data for an XGBoost meta-label model that
acts as a quality arbiter on the gate's trending-regime path (see CLAUDE.md §5).

Usage
-----
    python -m ml.label                     # real DB (data/journal.db)
    python -m ml.label --threshold 5.0    # require >5% pnl for label=1
    python -m ml.label --demo             # synthetic in-memory DB to show output format
    python -m ml.label --db /path/to.db   # explicit DB path

Public API
----------
    raw, labeled = load_labeled(db_path, threshold)
    report(raw, labeled, threshold)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

DB_PATH = Path(__file__).parent.parent / "data" / "journal.db"

# ── columns that become model features ───────────────────────────────────────
# Ordered: signal-level first (always present), then entry-layer (null when
# OpenAlgo was unconfigured or no strike found).
FEATURE_COLS = [
    # gate / signal features
    "adx", "filters",
    "f_ema", "f_rsi", "f_vol", "f_vwap", "f_mvwap",
    "f_band", "f_cvd", "f_st", "f_macd", "f_poc", "f_mss", "f_adx",
    "z", "z_long_zone", "z_short_zone", "z_bull_pa", "z_bear_pa",
    "fvg_ok", "pb_ok", "vol_ok",
    "range_ratio", "bars_since",
    "hh", "ll", "ext_long", "ext_short", "mss_state",
    # entry-layer features (may be null)
    "delta", "iv", "entry_premium",
]

# ── join query ────────────────────────────────────────────────────────────────
_JOIN_SQL = """
SELECT
    os.id           AS signal_id,
    os.bar_time,
    os.sym,
    os.dir,
    os.tf,
    os.regime,
    os.take,
    os.adx,         os.filters,
    os.f_ema,       os.f_rsi,       os.f_vol,   os.f_vwap,  os.f_mvwap,
    os.f_band,      os.f_cvd,       os.f_st,    os.f_macd,  os.f_poc,
    os.f_mss,       os.f_adx,
    os.z,
    os.z_long_zone, os.z_short_zone, os.z_bull_pa, os.z_bear_pa,
    os.fvg_ok,      os.pb_ok,       os.vol_ok,
    os.range_ratio, os.bars_since,
    os.hh,          os.ll,          os.ext_long, os.ext_short,
    os.mss_state,
    es.id           AS entry_id,
    es.delta,       es.iv,          es.entry_premium,
    ex.pnl_pct,
    ex.trigger      AS exit_trigger
FROM optscan_signals os
LEFT JOIN entry_suggestions es
    ON es.optscan_id = os.id
LEFT JOIN (
    -- one exit per entry (take the first chronological one)
    SELECT entry_suggestion_id,
           pnl_pct,
           trigger,
           MIN(created_at) AS first_exit
    FROM exit_suggestions
    GROUP BY entry_suggestion_id
) ex ON ex.entry_suggestion_id = es.id
ORDER BY os.bar_time
"""


# ── public API ────────────────────────────────────────────────────────────────


def load_labeled(
    db_path: Path = DB_PATH,
    threshold: float = 0.0,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load the joined dataset and apply the meta-label.

    Returns
    -------
    raw : pd.DataFrame
        All optscan_signals rows with left-joined entry/exit columns.
        Rows without exits have NaN in pnl_pct / exit_trigger.
    labeled : pd.DataFrame
        Subset of take=1 rows with non-null pnl_pct.
        Adds column ``label`` (int 0/1) and derived ``pnl_bin`` bucket.
    """
    _own = conn is None
    if _own:
        conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "optscan_signals" not in tables:
            return pd.DataFrame(), pd.DataFrame()

        raw = pd.read_sql(_JOIN_SQL, conn)
    finally:
        if _own:
            conn.close()

    # Labeled rows: gate approved + closed exit present
    labeled = raw[(raw["take"] == 1) & raw["pnl_pct"].notna()].copy()

    if not labeled.empty:
        labeled["label"] = (labeled["pnl_pct"] > threshold).astype(int)
        labeled["pnl_bin"] = pd.cut(
            labeled["pnl_pct"],
            bins=[-np.inf, -30, -10, 0, 10, 30, 50, np.inf],
            labels=["<-30", "-30:-10", "-10:0", "0:10", "10:30", "30:50", ">50"],
        )

    return raw, labeled


def report(
    raw: pd.DataFrame,
    labeled: pd.DataFrame,
    threshold: float = 0.0,
) -> None:
    """Print the label distribution report to stdout."""
    W = 60

    def hr(c="-"):
        print(c * W)

    print()
    print("OPT.SCAN Meta-Label Report")
    hr("=")
    print(f"  Label  : pnl_pct > {threshold:+.1f}%  →  label=1 (win)")
    print(f"           pnl_pct ≤ {threshold:+.1f}%  →  label=0 (loss / breakeven)")
    hr()

    # ── Dataset overview ──────────────────────────────────────────────────────
    n_total = len(raw)
    if n_total == 0:
        print()
        print("  optscan_signals table is empty.")
        print("  Paper/live trading will populate it via the webhook gate.")
        print()
        return

    n_take = int((raw["take"] == 1).sum())
    n_skip = n_total - n_take
    print(f"  Total signals             : {n_total:>6,}")
    print(f"  take=1 (gate approved)    : {n_take:>6,}  ({n_take/n_total*100:.1f}%)")
    print(f"  take=0 (gate skipped)     : {n_skip:>6,}  ({n_skip/n_total*100:.1f}%)")

    takes = raw[raw["take"] == 1]
    n_has_entry = int(takes["entry_id"].notna().sum())
    n_labeled   = len(labeled)
    n_pending   = n_has_entry - n_labeled
    no_entry    = n_take - n_has_entry

    print()
    print(f"  Gate takes → entry selector fired  : {n_has_entry:>5,}  ({n_has_entry/max(n_take,1)*100:.1f}% of takes)")
    if no_entry:
        print(f"  Gate takes → no entry (OpenAlgo off): {no_entry:>5,}")
    print(f"  With exit outcome (labeled)         : {n_labeled:>5,}  ({n_labeled/max(n_has_entry,1)*100:.1f}%)")
    print(f"  Pending (position not yet closed)   : {n_pending:>5,}  ({n_pending/max(n_has_entry,1)*100:.1f}%)")

    if n_labeled == 0:
        print()
        print("  No labeled rows yet — exits populate as positions close.")
        print()
        return

    # ── Label distribution ────────────────────────────────────────────────────
    n1    = int((labeled["label"] == 1).sum())
    n0    = int((labeled["label"] == 0).sum())
    ratio = n1 / max(n0, 1)

    print()
    hr()
    print(f"Label distribution  (n={n_labeled:,})")
    hr()
    bar_w = 30
    bar1  = "█" * round(n1 / n_labeled * bar_w)
    bar0  = "█" * round(n0 / n_labeled * bar_w)
    print(f"  label=1 (win)   {n1:>5,}  {n1/n_labeled*100:5.1f}%  {bar1}")
    print(f"  label=0 (loss)  {n0:>5,}  {n0/n_labeled*100:5.1f}%  {bar0}")
    print(f"  imbalance ratio : {ratio:.2f} : 1  (1.0 = perfectly balanced)")
    print()
    print(f"  pnl_pct  mean / std     : {labeled.pnl_pct.mean():+.1f}% / {labeled.pnl_pct.std():.1f}%")
    print(f"  pnl_pct  p10 / p50 / p90: "
          f"{labeled.pnl_pct.quantile(0.10):+.1f}% / "
          f"{labeled.pnl_pct.quantile(0.50):+.1f}% / "
          f"{labeled.pnl_pct.quantile(0.90):+.1f}%")

    # pnl bucket histogram
    print()
    print("  pnl_pct distribution")
    bucket_counts = labeled["pnl_bin"].value_counts().sort_index()
    for bucket, cnt in bucket_counts.items():
        bar = "█" * round(cnt / n_labeled * bar_w)
        print(f"  {str(bucket):>10}  {cnt:>4}  {bar}")

    # ── By regime ─────────────────────────────────────────────────────────────
    print()
    hr()
    print("By regime")
    hr()
    _pivot(labeled, "regime")

    # ── By direction ──────────────────────────────────────────────────────────
    print()
    hr()
    print("By direction")
    hr()
    _pivot(labeled, "dir")

    # ── By symbol ─────────────────────────────────────────────────────────────
    print()
    hr()
    print("By symbol")
    hr()
    _pivot(labeled, "sym")

    # ── Exit trigger breakdown ─────────────────────────────────────────────────
    if labeled["exit_trigger"].notna().any():
        print()
        hr()
        print("Exit trigger breakdown")
        hr()
        g    = labeled.groupby("exit_trigger")["label"]
        trig = pd.DataFrame({"n": g.count(), "win": g.sum()})
        trig["loss"] = trig["n"] - trig["win"]
        trig["win%"] = (trig["win"] / trig["n"] * 100).round(1)
        trig = trig.sort_values("n", ascending=False)
        print(f"  {'trigger':<22} {'n':>5}  {'win':>5}  {'loss':>5}  {'win%':>6}")
        hr()
        for name, row in trig.iterrows():
            print(f"  {name:<22} {int(row.n):>5}  {int(row.win):>5}  {int(row.loss):>5}  {row['win%']:>5.1f}%")

    # ── Feature means by label ────────────────────────────────────────────────
    present = [c for c in FEATURE_COLS if c in labeled.columns and labeled[c].notna().any()]
    if present:
        print()
        hr()
        print("Feature means by label (only numeric, non-null columns)")
        hr()
        means = labeled.groupby("label")[present].mean()
        print(f"  {'feature':<22} {'label=0':>10}  {'label=1':>10}  {'Δ (1−0)':>10}")
        hr()
        for col in present:
            if col not in means.columns:
                continue
            v0 = means.loc[0, col] if 0 in means.index else np.nan
            v1 = means.loc[1, col] if 1 in means.index else np.nan
            delta = v1 - v0
            print(f"  {col:<22} {v0:>10.3f}  {v1:>10.3f}  {delta:>+10.3f}")

    print()
    hr()
    print(f"  {n_labeled} labeled rows ready.  Next: purged walk-forward XGBoost training.")
    print()


def _pivot(df: pd.DataFrame, col: str) -> None:
    g   = df.groupby(col)["label"]
    tbl = pd.DataFrame({"n": g.count(), "win": g.sum()})
    tbl["loss"] = tbl["n"] - tbl["win"]
    tbl["win%"] = (tbl["win"] / tbl["n"] * 100).round(1)
    print(f"  {col:<18}  {'n':>6}  {'win%':>6}  {'win':>6}  {'loss':>6}")
    for val, row in tbl.iterrows():
        print(f"  {str(val):<18}  {int(row.n):>6}  {row['win%']:>5.1f}%  {int(row.win):>6}  {int(row.loss):>6}")


# ── demo helpers ──────────────────────────────────────────────────────────────


def _build_demo_conn(n_signals: int = 280, seed: int = 42) -> sqlite3.Connection:
    """
    Build an in-memory SQLite DB with synthetic data that mirrors the real schema.
    Realistic distributions based on BANKNIFTY/NIFTY intraday option behavior.
    """
    rng = np.random.default_rng(seed)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE optscan_signals (
            id INTEGER PRIMARY KEY,
            bar_time INTEGER, sym TEXT, dir TEXT, tf TEXT,
            regime TEXT, take INTEGER,
            adx REAL, filters INTEGER,
            f_ema INTEGER, f_rsi INTEGER, f_vol INTEGER, f_vwap INTEGER, f_mvwap INTEGER,
            f_band INTEGER, f_cvd INTEGER, f_st INTEGER, f_macd INTEGER, f_poc INTEGER,
            f_mss INTEGER, f_adx INTEGER,
            z REAL,
            z_long_zone INTEGER, z_short_zone INTEGER, z_bull_pa INTEGER, z_bear_pa INTEGER,
            fvg_ok INTEGER, pb_ok INTEGER, vol_ok INTEGER,
            range_ratio REAL, bars_since INTEGER,
            hh INTEGER, ll INTEGER, ext_long INTEGER, ext_short INTEGER,
            mss_state INTEGER,
            reason TEXT, created_at TEXT
        );
        CREATE TABLE entry_suggestions (
            id INTEGER PRIMARY KEY,
            optscan_id INTEGER,
            delta REAL, iv REAL, entry_premium REAL,
            stop_premium REAL, target_premium REAL, time_stop TEXT,
            sym TEXT, expiry TEXT, strike INTEGER, option_type TEXT,
            lots INTEGER, regime TEXT, mode TEXT, rationale TEXT, status TEXT,
            created_at TEXT
        );
        CREATE TABLE exit_suggestions (
            id INTEGER PRIMARY KEY,
            entry_suggestion_id INTEGER,
            pnl_pct REAL, trigger TEXT, position_id TEXT,
            sym TEXT, strike INTEGER, option_type TEXT,
            entry_premium REAL, current_premium REAL,
            mode TEXT, status TEXT, created_at TEXT
        );
    """)

    syms      = ["BANKNIFTY", "NIFTY"]
    dirs      = ["long", "short"]
    regimes   = ["trending", "ranging"]

    bar_t = 1_706_000_000_000   # ~Jan 2024 start in ms

    sig_rows    = []
    entry_rows  = []
    exit_rows   = []
    entry_id    = 0
    exit_id     = 0

    for i in range(1, n_signals + 1):
        bar_t   += int(rng.uniform(9, 18)) * 60_000   # 9-18 min between signals
        sym     = rng.choice(syms, p=[0.6, 0.4])
        dir_    = rng.choice(dirs)
        adx     = float(rng.uniform(18, 58))
        regime  = "trending" if adx >= 35 else "ranging"
        filt    = int(rng.integers(7, 13))
        take    = int(filt >= 9 and rng.random() > 0.35)
        z       = float(rng.normal(0.3 if dir_ == "long" else -0.3, 1.2))

        sig_rows.append((
            i, bar_t, sym, dir_, "9", regime, take,
            adx, filt,
            *rng.integers(0, 2, 12).tolist(),   # 12 filter booleans
            z,
            int(z < -1), int(z > 1), int(z < -1.5), int(z > 1.5),
            int(rng.random() > 0.45), int(rng.random() > 0.55), 1,
            float(rng.uniform(0.85, 2.4)), int(rng.integers(3, 30)),
            int(rng.random() > 0.4), int(rng.random() > 0.6),
            int(rng.random() > 0.7), int(rng.random() > 0.7),
            int(rng.choice([-1, 0, 1])),
            "demo", "2024-01-01 09:30:00",
        ))

        if not take:
            continue

        # ~80% of takes produce an entry suggestion (OpenAlgo configured)
        if rng.random() > 0.20:
            entry_id += 1
            delta     = float(rng.uniform(0.35, 0.50))
            iv        = float(rng.uniform(0.14, 0.25))
            premium   = float(rng.uniform(60, 350))
            entry_rows.append((
                entry_id, i,
                delta, iv, premium,
                round(premium * 0.70, 2), round(premium * 1.50, 2), "15:15",
                sym, "01-Feb-2024", 45000, "CE" if dir_ == "long" else "PE",
                1, regime, "intraday", "demo", "pending",
                "2024-01-01 09:30:01",
            ))

            # ~75% of entries are closed (have an exit)
            if rng.random() > 0.25:
                exit_id   += 1
                # Trending: ~58% win; ranging: ~48% win
                win_p      = 0.58 if regime == "trending" else 0.48
                is_win     = rng.random() < win_p
                if is_win:
                    pnl = float(rng.choice([
                        rng.uniform(5, 55),   # premium_target/trail
                        rng.uniform(2, 15),   # small positive
                    ], p=[0.55, 0.45]))
                    trigger = rng.choice(
                        ["premium_target", "premium_trail", "eod_squareoff"],
                        p=[0.35, 0.30, 0.35],
                    )
                else:
                    pnl = float(-rng.choice([
                        rng.uniform(20, 32),  # stop hit
                        rng.uniform(5, 20),   # small loss
                    ], p=[0.50, 0.50]))
                    trigger = rng.choice(
                        ["premium_stop", "time_stop", "iv_crush", "regime_flip"],
                        p=[0.40, 0.30, 0.20, 0.10],
                    )

                exit_rows.append((
                    exit_id, entry_id,
                    round(pnl, 2), trigger, f"DEMO_{entry_id}",
                    sym, 45000, "CE" if dir_ == "long" else "PE",
                    premium, round(premium * (1 + pnl / 100), 2),
                    "intraday", "pending", "2024-01-01 10:00:00",
                ))

    def _placeholders(n: int) -> str:
        return ",".join("?" * n)

    conn.executemany(
        f"INSERT INTO optscan_signals VALUES ({_placeholders(38)})",
        sig_rows,
    )
    conn.executemany(
        f"INSERT INTO entry_suggestions VALUES ({_placeholders(18)})",
        entry_rows,
    )
    conn.executemany(
        f"INSERT INTO exit_suggestions VALUES ({_placeholders(13)})",
        exit_rows,
    )
    conn.commit()
    return conn


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OPT.SCAN meta-label report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db",        default=str(DB_PATH), help="path to journal.db")
    p.add_argument("--threshold", type=float, default=0.0,
                   help="pnl_pct threshold for label=1 (default: 0.0)")
    p.add_argument("--demo",      action="store_true",
                   help="use synthetic in-memory data to show output format")
    return p.parse_args()


if __name__ == "__main__":
    args  = _parse_args()

    if args.demo:
        print("\n[demo mode — synthetic data, seed=42, ~280 signals]")
        demo_conn = _build_demo_conn()
        raw, labeled = load_labeled(threshold=args.threshold, conn=demo_conn)
        demo_conn.close()
    else:
        raw, labeled = load_labeled(
            db_path=Path(args.db), threshold=args.threshold
        )

    report(raw, labeled, threshold=args.threshold)
