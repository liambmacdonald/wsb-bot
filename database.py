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
CREATE TABLE IF NOT EXISTS bot_status (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    last_heartbeat          TEXT,
    last_run_duration       REAL,
    last_posts_collected    INTEGER,
    last_signals_generated  INTEGER,
    status                  TEXT DEFAULT 'offline'
);

CREATE TABLE IF NOT EXISTS performance (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker               TEXT NOT NULL,
    signal               TEXT NOT NULL,
    signal_timestamp     TEXT NOT NULL,
    price_at_signal      REAL,
    price_6h_later       REAL,
    price_24h_later      REAL,
    price_7d_later       REAL,
    outcome_6h           TEXT NOT NULL DEFAULT 'PENDING',
    outcome              TEXT NOT NULL DEFAULT 'PENDING',
    percent_change_6h    REAL,
    percent_change_24h   REAL,
    percent_change_7d    REAL,
    option_type          TEXT,
    option_contract      TEXT,
    option_strike        REAL,
    option_expiry        TEXT,
    option_last          REAL,
    option_bid           REAL,
    option_ask           REAL,
    option_iv            REAL,
    option_delta         REAL,
    option_oi            INTEGER,
    option_cost          REAL,
    option_breakeven     REAL,
    option_value_6h      REAL,
    option_return_6h     REAL,
    option_profit_6h     REAL,
    option_outcome_6h    TEXT,
    option_value_24h     REAL,
    option_return_24h    REAL,
    option_profit_24h    REAL,
    option_outcome_24h   TEXT,
    option_value_7d      REAL,
    option_return_7d     REAL,
    option_profit_7d     REAL,
    option_outcome_7d    TEXT,
    trade_status         TEXT NOT NULL DEFAULT 'PENDING',
    close_reason         TEXT,
    close_price          REAL,
    close_option_value   REAL,
    close_return_pct     REAL,
    close_profit_dollars REAL,
    closed_at            TEXT
);

CREATE TABLE IF NOT EXISTS holds_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT NOT NULL,
    signal_timestamp TEXT NOT NULL,
    total_weight     REAL,
    bullish_weight   REAL,
    bearish_weight   REAL,
    reasoning        TEXT,
    logged_at        TEXT DEFAULT (datetime('now'))
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
        _migrate_performance_options_tracking(conn)
        _migrate_performance_options_profit(conn)
        _migrate_performance_option_source(conn)
        _migrate_bot_status(conn)
        _migrate_holds_log(conn)
        _migrate_performance_trade_status(conn)
        _migrate_remove_holds_from_performance(conn)
    logger.info("Database initialised at '%s'", DB_PATH)


def _migrate_bot_status(conn: sqlite3.Connection) -> None:
    """Create bot_status table if it doesn't exist (for pre-existing DBs)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot_status (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            last_heartbeat          TEXT,
            last_run_duration       REAL,
            last_posts_collected    INTEGER,
            last_signals_generated  INTEGER,
            status                  TEXT DEFAULT 'offline'
        );
    """)


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


def _migrate_performance_options_tracking(conn: sqlite3.Connection) -> None:
    """Add options performance tracking columns to an existing performance table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    for col, defn in [
        ("option_value_6h",    "REAL"),
        ("option_return_6h",   "REAL"),
        ("option_outcome_6h",  "TEXT"),
        ("option_value_24h",   "REAL"),
        ("option_return_24h",  "REAL"),
        ("option_outcome_24h", "TEXT"),
        ("option_value_7d",    "REAL"),
        ("option_return_7d",   "REAL"),
        ("option_outcome_7d",  "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {defn}")


def _migrate_performance_options_profit(conn: sqlite3.Connection) -> None:
    """Add options profit tracking columns to an existing performance table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    for col, defn in [
        ("option_profit_6h",  "REAL"),
        ("option_profit_24h", "REAL"),
        ("option_profit_7d",  "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {defn}")


def _migrate_performance_option_source(conn: sqlite3.Connection) -> None:
    """Add option_price_source column to track whether P&L is live/intrinsic/estimate."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    if "option_price_source" not in existing:
        conn.execute("ALTER TABLE performance ADD COLUMN option_price_source TEXT")


def _migrate_holds_log(conn: sqlite3.Connection) -> None:
    """Create holds_log table if it doesn't exist (for pre-existing DBs)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS holds_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            signal_timestamp TEXT NOT NULL,
            total_weight     REAL,
            bullish_weight   REAL,
            bearish_weight   REAL,
            reasoning        TEXT,
            logged_at        TEXT DEFAULT (datetime('now'))
        );
    """)


def _migrate_performance_trade_status(conn: sqlite3.Connection) -> None:
    """Add trade lifecycle columns to an existing performance table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(performance)").fetchall()}
    for col, defn in [
        ("trade_status",         "TEXT NOT NULL DEFAULT 'PENDING'"),
        ("close_reason",         "TEXT"),
        ("close_price",          "REAL"),
        ("close_option_value",   "REAL"),
        ("close_return_pct",     "REAL"),
        ("close_profit_dollars", "REAL"),
        ("closed_at",            "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE performance ADD COLUMN {col} {defn}")


def _migrate_remove_holds_from_performance(conn: sqlite3.Connection) -> None:
    """Remove HOLD signal rows from performance — HOLDs now go to holds_log."""
    conn.execute("DELETE FROM performance WHERE signal = 'HOLD'")


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
    row_id: int, price: float, pct_change: float, outcome: str,
    option_value: float | None = None,
    option_return: float | None = None,
    option_profit: float | None = None,
    option_outcome: str | None = None,
    option_price_source: str | None = None,
) -> None:
    sql = """
        UPDATE performance
        SET price_24h_later     = ?,
            percent_change_24h  = ?,
            outcome             = ?,
            option_value_24h    = ?,
            option_return_24h   = ?,
            option_profit_24h   = ?,
            option_outcome_24h  = ?,
            option_price_source = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, option_value, option_return, option_profit, option_outcome, option_price_source, row_id))
    except Exception as exc:
        logger.error("update_performance_24h failed for id=%s: %s", row_id, exc)


def update_performance_6h(
    row_id: int, price: float, pct_change: float, outcome: str,
    option_value: float | None = None,
    option_return: float | None = None,
    option_profit: float | None = None,
    option_outcome: str | None = None,
    option_price_source: str | None = None,
) -> None:
    sql = """
        UPDATE performance
        SET price_6h_later      = ?,
            percent_change_6h   = ?,
            outcome_6h          = ?,
            option_value_6h     = ?,
            option_return_6h    = ?,
            option_profit_6h    = ?,
            option_outcome_6h   = ?,
            option_price_source = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, option_value, option_return, option_profit, option_outcome, option_price_source, row_id))
    except Exception as exc:
        logger.error("update_performance_6h failed for id=%s: %s", row_id, exc)


def update_performance_7d(
    row_id: int, price: float, pct_change: float, outcome: str,
    option_value: float | None = None,
    option_return: float | None = None,
    option_profit: float | None = None,
    option_outcome: str | None = None,
    option_price_source: str | None = None,
) -> None:
    sql = """
        UPDATE performance
        SET price_7d_later      = ?,
            percent_change_7d   = ?,
            outcome             = ?,
            option_value_7d     = ?,
            option_return_7d    = ?,
            option_profit_7d    = ?,
            option_outcome_7d   = ?,
            option_price_source = ?
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (price, pct_change, outcome, option_value, option_return, option_profit, option_outcome, option_price_source, row_id))
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


def save_hold(
    ticker: str,
    signal_timestamp: str,
    total_weight: float | None = None,
    bullish_weight: float | None = None,
    bearish_weight: float | None = None,
    reasoning: str | None = None,
) -> int | None:
    """Persist a HOLD signal to holds_log (not to trades/performance)."""
    sql = """
        INSERT INTO holds_log (ticker, signal_timestamp, total_weight, bullish_weight, bearish_weight, reasoning)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    try:
        with _connect() as conn:
            cur = conn.execute(sql, (ticker, signal_timestamp, total_weight, bullish_weight, bearish_weight, reasoning))
            return cur.lastrowid
    except Exception as exc:
        logger.error("save_hold failed for %s: %s", ticker, exc)
        return None


def get_recent_trade_for_ticker(ticker: str, hours: float = 4.0) -> dict | None:
    """Return the most recent BUY/SELL trade for a ticker in the last `hours` hours."""
    sql = """
        SELECT action, executed_at FROM simulated_trades
        WHERE ticker = ?
          AND action IN ('BUY', 'SELL')
          AND executed_at >= datetime('now', ?)
        ORDER BY executed_at DESC
        LIMIT 1
    """
    with _connect() as conn:
        row = conn.execute(sql, (ticker, f"-{hours} hours")).fetchone()
    return dict(row) if row else None


def get_latest_active_performance_row(ticker: str) -> dict | None:
    """Return the most recent non-closed performance row for a ticker."""
    sql = """
        SELECT * FROM performance
        WHERE ticker = ?
          AND signal IN ('BUY', 'SELL')
          AND (trade_status IS NULL OR trade_status NOT LIKE 'CLOSED_%')
        ORDER BY signal_timestamp DESC
        LIMIT 1
    """
    with _connect() as conn:
        row = conn.execute(sql, (ticker,)).fetchone()
    return dict(row) if row else None


def get_active_options_rows() -> list[dict]:
    """Return performance rows with options data that are not yet closed."""
    sql = """
        SELECT * FROM performance
        WHERE option_contract IS NOT NULL
          AND option_cost IS NOT NULL
          AND option_cost > 0
          AND (trade_status IS NULL OR trade_status IN ('PENDING', 'ACTIVE'))
        ORDER BY signal_timestamp DESC
    """
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def update_trade_status(
    row_id: int,
    trade_status: str,
    close_reason: str | None = None,
    close_price: float | None = None,
    close_option_value: float | None = None,
    close_return_pct: float | None = None,
    close_profit_dollars: float | None = None,
) -> None:
    """Update the trade lifecycle status and optional close details."""
    sql = """
        UPDATE performance
        SET trade_status         = ?,
            close_reason         = ?,
            close_price          = ?,
            close_option_value   = ?,
            close_return_pct     = ?,
            close_profit_dollars = ?,
            closed_at            = CASE WHEN ? LIKE 'CLOSED_%' THEN datetime('now') ELSE closed_at END
        WHERE id = ?
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (trade_status, close_reason, close_price, close_option_value,
                               close_return_pct, close_profit_dollars, trade_status, row_id))
    except Exception as exc:
        logger.error("update_trade_status failed for id=%s: %s", row_id, exc)


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


# ── Bot heartbeat ──────────────────────────────────────────────────────────────

def write_heartbeat(
    duration_seconds: float,
    posts_collected: int,
    signals_generated: int,
    status: str = "running",
) -> None:
    sql = """
        INSERT INTO bot_status
            (id, last_heartbeat, last_run_duration, last_posts_collected,
             last_signals_generated, status)
        VALUES (1, datetime('now'), ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_heartbeat         = excluded.last_heartbeat,
            last_run_duration      = excluded.last_run_duration,
            last_posts_collected   = excluded.last_posts_collected,
            last_signals_generated = excluded.last_signals_generated,
            status                 = excluded.status
    """
    try:
        with _connect() as conn:
            conn.execute(sql, (duration_seconds, posts_collected, signals_generated, status))
        logger.debug("Heartbeat written (duration=%.1fs)", duration_seconds)
    except Exception as exc:
        logger.error("write_heartbeat failed: %s", exc)


def get_bot_status() -> dict | None:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bot_status WHERE id = 1").fetchone()
            return dict(row) if row else None
    except Exception as exc:
        logger.error("get_bot_status failed: %s", exc)
        return None
