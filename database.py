"""
Thin SQLite wrapper.  All heavy lifting is in plain SQL; no ORM.
Tables:
  posts             — raw filtered Reddit posts
  analysis_results  — per-post Anthropic analysis
  signals           — aggregated BUY/SELL/HOLD per ticker per run
  simulated_trades  — final trade log derived from signals
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

from config import DB_PATH

logger = logging.getLogger(__name__)

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS performance (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    signal              TEXT NOT NULL,
    signal_timestamp    TEXT NOT NULL,
    price_at_signal     REAL,
    price_6h_later      REAL,
    price_24h_later     REAL,
    price_7d_later      REAL,
    outcome_6h          TEXT NOT NULL DEFAULT 'PENDING',
    outcome             TEXT NOT NULL DEFAULT 'PENDING',
    percent_change_6h   REAL,
    percent_change_24h  REAL,
    percent_change_7d   REAL,
    option_type         TEXT,
    option_contract     TEXT,
    option_strike       REAL,
    option_expiry       TEXT,
    option_last         REAL,
    option_bid          REAL,
    option_ask          REAL,
    option_iv           REAL,
    option_delta        REAL,
    option_oi           INTEGER,
    option_cost         REAL,
    option_breakeven    REAL
);

CREATE INDEX IF NOT EXISTS idx_performance_ticker  ON performance(ticker);
CREATE INDEX IF NOT EXISTS idx_performance_outcome ON performance(outcome);

CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT,
    author          TEXT,
    flair           TEXT,
    upvotes         INTEGER,
    upvote_ratio    REAL,
    num_comments    INTEGER,
    url             TEXT,
    created_utc     REAL,
    collected_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL,
    has_thesis      INTEGER,
    is_ironic       INTEGER,
    quality_score   REAL,
    sentiment       TEXT,
    confidence      REAL,
    tickers         TEXT,   -- JSON array of ticker strings
    reasoning       TEXT,
    analyzed_at     TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    signal          TEXT NOT NULL,   -- BUY | SELL | HOLD
    bullish_weight  REAL,
    bearish_weight  REAL,
    total_weight    REAL,
    bullish_count   INTEGER,
    bearish_count   INTEGER,
    post_ids        TEXT,            -- JSON array
    reasoning       TEXT,
    generated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS simulated_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER,
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,   -- BUY | SELL | HOLD
    signal_strength REAL,
    reasoning       TEXT,
    executed_at     TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE INDEX IF NOT EXISTS idx_analysis_post_id   ON analysis_results(post_id);
CREATE INDEX IF NOT EXISTS idx_signals_ticker      ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_ticker       ON simulated_trades(ticker);
"""

# ── Connection helpers ─────────────────────────────────────────────────────────

def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate_performance_6h(conn)
        _migrate_performance_options(conn)
    logger.info("Database initialised at '%s'", DB_PATH)


def _migrate_performance_6h(conn: sqlite3.Connection) -> None:
    """Add 6h columns to an existing performance table that predates them."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    for col, defn in [
        ("price_6h_later",    "REAL"),
        ("percent_change_6h", "REAL"),
        ("outcome_6h",        "TEXT NOT NULL DEFAULT 'PENDING'"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {defn}")


def _migrate_performance_options(conn: sqlite3.Connection) -> None:
    """Add options advisor columns to an existing performance table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    for col, defn in [
        ("option_type",      "TEXT"),
        ("option_contract",  "TEXT"),
        ("option_strike",    "REAL"),
        ("option_expiry",    "TEXT"),
        ("option_last",      "REAL"),
        ("option_bid",       "REAL"),
        ("option_ask",       "REAL"),
        ("option_iv",        "REAL"),
        ("option_delta",     "REAL"),
        ("option_oi",        "INTEGER"),
        ("option_cost",      "REAL"),
        ("option_breakeven", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {defn}")


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ── Posts ──────────────────────────────────────────────────────────────────────

def save_post(post: dict) -> bool:
    """Insert a post; returns True if new, False if already existed."""
    sql = """
        INSERT OR IGNORE INTO posts
            (id, title, body, author, flair, upvotes, upvote_ratio,
             num_comments, url, created_utc)
        VALUES
            (:id, :title, :body, :author, :flair, :upvotes, :upvote_ratio,
             :num_comments, :url, :created_utc)
    """
    try:
        with _connect() as conn:
            cur = conn.execute(sql, post)
            inserted = cur.rowcount > 0
            if inserted:
                logger.debug("Saved new post %s", post["id"])
            else:
                logger.debug("Post %s already in DB — skipped", post["id"])
            return inserted
    except Exception as exc:
        logger.error("save_post failed for %s: %s", post.get("id"), exc)
        return False


def post_exists(post_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        return row is not None


def analysis_exists(post_id: str) -> bool:
    """Return True if this post has already been analysed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM analysis_results WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None

# ── Analysis results ───────────────────────────────────────────────────────────

def save_analysis(analysis: dict) -> int | None:
    """Persist one analysis result; returns the new row id."""
    sql = """
        INSERT INTO analysis_results
            (post_id, has_thesis, is_ironic, quality_score,
             sentiment, confidence, tickers, reasoning)
        VALUES
            (:post_id, :has_thesis, :is_ironic, :quality_score,
             :sentiment, :confidence, :tickers, :reasoning)
    """
    payload = dict(analysis)
    payload["tickers"] = json.dumps(payload.get("tickers", []))
    try:
        with _connect() as conn:
            cur = conn.execute(sql, payload)
            row_id = cur.lastrowid
            logger.debug(
                "Saved analysis id=%s for post %s", row_id, analysis.get("post_id")
            )
            return row_id
    except Exception as exc:
        logger.error(
            "save_analysis failed for post %s: %s", analysis.get("post_id"), exc
        )
        return None

# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(signal: dict) -> int | None:
    """Persist one ticker signal; returns the new row id."""
    sql = """
        INSERT INTO signals
            (ticker, signal, bullish_weight, bearish_weight, total_weight,
             bullish_count, bearish_count, post_ids, reasoning)
        VALUES
            (:ticker, :signal, :bullish_weight, :bearish_weight, :total_weight,
             :bullish_count, :bearish_count, :post_ids, :reasoning)
    """
    payload = dict(signal)
    payload["post_ids"] = json.dumps(payload.get("post_ids", []))
    try:
        with _connect() as conn:
            cur = conn.execute(sql, payload)
            row_id = cur.lastrowid
            logger.debug("Saved signal id=%s for %s", row_id, signal.get("ticker"))
            return row_id
    except Exception as exc:
        logger.error("save_signal failed for %s: %s", signal.get("ticker"), exc)
        return None

# ── Simulated trades ───────────────────────────────────────────────────────────

def save_trade(trade: dict) -> int | None:
    """Persist one simulated trade; returns the new row id."""
    sql = """
        INSERT INTO simulated_trades
            (signal_id, ticker, action, signal_strength, reasoning)
        VALUES
            (:signal_id, :ticker, :action, :signal_strength, :reasoning)
    """
    try:
        with _connect() as conn:
            cur = conn.execute(sql, trade)
            row_id = cur.lastrowid
            logger.debug(
                "Saved trade id=%s: %s %s", row_id, trade.get("action"), trade.get("ticker")
            )
            return row_id
    except Exception as exc:
        logger.error("save_trade failed for %s: %s", trade.get("ticker"), exc)
        return None

# ── Queries ────────────────────────────────────────────────────────────────────

def get_recent_signals(limit: int = 20) -> list[dict]:
    """Return the most recent signals as plain dicts."""
    sql = """
        SELECT ticker, signal, bullish_weight, bearish_weight,
               total_weight, bullish_count, bearish_count, reasoning, generated_at
        FROM signals
        ORDER BY generated_at DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_recent_trades(limit: int = 20) -> list[dict]:
    """Return the most recent simulated trades as plain dicts."""
    sql = """
        SELECT ticker, action, signal_strength, reasoning, executed_at
        FROM simulated_trades
        ORDER BY executed_at DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Performance tracking ───────────────────────────────────────────────────────

def insert_performance_row(
    ticker: str,
    signal: str,
    signal_timestamp: str,
    price_at_signal: float | None,
    options_data: dict | None = None,
) -> int | None:
    """Insert a new performance tracking row; returns the new row id."""
    sql = """
        INSERT INTO performance (
            ticker, signal, signal_timestamp, price_at_signal,
            option_type, option_contract, option_strike, option_expiry,
            option_last, option_bid, option_ask, option_iv, option_delta,
            option_oi, option_cost, option_breakeven
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    opt = options_data or {}
    params = (
        ticker, signal, signal_timestamp, price_at_signal,
        opt.get("option_type"),
        opt.get("contract_symbol"),
        opt.get("strike"),
        opt.get("expiration"),
        opt.get("last_price"),
        opt.get("bid"),
        opt.get("ask"),
        opt.get("implied_volatility"),
        opt.get("delta"),
        opt.get("open_interest"),
        opt.get("cost_per_contract"),
        opt.get("breakeven_price"),
    )
    try:
        with _connect() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid
    except Exception as exc:
        logger.error("insert_performance_row failed for %s: %s", ticker, exc)
        return None


def get_pending_24h_rows() -> list[dict]:
    """Rows where 24h price is still missing and 24h have elapsed."""
    sql = """
        SELECT * FROM performance
        WHERE price_24h_later IS NULL
          AND price_at_signal IS NOT NULL
          AND signal_timestamp <= datetime('now', '-24 hours')
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_pending_7d_rows() -> list[dict]:
    """Rows where 7d price is still missing and 7 days have elapsed."""
    sql = """
        SELECT * FROM performance
        WHERE price_7d_later IS NULL
          AND price_at_signal IS NOT NULL
          AND signal_timestamp <= datetime('now', '-7 days')
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_all_pending_rows() -> list[dict]:
    """All rows where 24h price is still missing, regardless of age."""
    sql = """
        SELECT * FROM performance
        WHERE price_24h_later IS NULL
          AND price_at_signal IS NOT NULL
        ORDER BY signal_timestamp ASC
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_pending_6h_rows() -> list[dict]:
    """Rows where 6h price is still missing and 6h have elapsed."""
    sql = """
        SELECT * FROM performance
        WHERE price_6h_later IS NULL
          AND price_at_signal IS NOT NULL
          AND signal_timestamp <= datetime('now', '-6 hours')
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def update_performance_24h(
    row_id: int, price: float, pct_change: float, outcome: str
) -> None:
    sql = """
        UPDATE performance
        SET price_24h_later    = ?,
            percent_change_24h = ?,
            outcome            = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, row_id))
    except Exception as exc:
        logger.error("update_performance_24h failed for id=%s: %s", row_id, exc)


def update_performance_6h(
    row_id: int, price: float, pct_change: float, outcome: str
) -> None:
    sql = """
        UPDATE performance
        SET price_6h_later    = ?,
            percent_change_6h = ?,
            outcome_6h        = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, row_id))
    except Exception as exc:
        logger.error("update_performance_6h failed for id=%s: %s", row_id, exc)


def update_performance_7d(
    row_id: int, price: float, pct_change: float, outcome: str
) -> None:
    sql = """
        UPDATE performance
        SET price_7d_later    = ?,
            percent_change_7d = ?,
            outcome           = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, row_id))
    except Exception as exc:
        logger.error("update_performance_7d failed for id=%s: %s", row_id, exc)


def get_all_performance_rows() -> list[dict]:
    sql = """
        SELECT * FROM performance ORDER BY signal_timestamp DESC
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_untracked_trades() -> list[dict]:
    """Return simulated_trades rows (BUY/SELL only) that have no matching
    performance row, used for one-time backfill."""
    sql = """
        SELECT t.ticker, t.action, t.executed_at
        FROM simulated_trades t
        WHERE t.action IN ('BUY', 'SELL')
          AND NOT EXISTS (
              SELECT 1 FROM performance p
              WHERE p.ticker          = t.ticker
                AND p.signal          = t.action
                AND p.signal_timestamp = t.executed_at
          )
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_pipeline_stats() -> dict:
    """Quick counts useful for the run summary."""
    with _connect() as conn:
        total_posts    = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_analyses = conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
        total_signals  = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        total_trades   = conn.execute("SELECT COUNT(*) FROM simulated_trades").fetchone()[0]
    return {
        "total_posts":    total_posts,
        "total_analyses": total_analyses,
        "total_signals":  total_signals,
        "total_trades":   total_trades,
    }
