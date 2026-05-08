"""
Performance tracker — measures how well the bot's BUY/SELL signals did.

Two entry points for the scheduler:
  update_performance()         — fill in 24h / 7d prices on matured rows
  print_performance_summary()  — console report of all tracked signals
  backfill_performance_from_trades() — one-time import of historical trades
"""

import logging
from datetime import datetime, timedelta

import yfinance as yf

from database import (
    get_all_pending_rows,
    get_pending_6h_rows,
    get_pending_24h_rows,
    get_pending_7d_rows,
    update_performance_6h,
    update_performance_24h,
    update_performance_7d,
    get_all_performance_rows,
    get_untracked_trades,
    insert_performance_row,
)

logger = logging.getLogger(__name__)

_SEP = "-" * 70


# ── Price helpers ─────────────────────────────────────────────────────────────

def _fetch_price(ticker: str) -> float | None:
    try:
        t = yf.Ticker(ticker)
        price = t.fast_info.last_price
        if price is not None and float(price) > 0:
            return float(price)
        # fast_info.last_price is None outside market hours — fall back to last close
        hist = t.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        return None
    except Exception as exc:
        logger.warning("Price fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_historical_price(ticker: str, timestamp_str: str) -> float | None:
    """Return the closing price on the trading day of timestamp_str."""
    try:
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        start = dt.date()
        end = start + timedelta(days=4)   # buffer for weekends / holidays
        hist = yf.Ticker(ticker).history(start=str(start), end=str(end))
        if not hist.empty:
            return float(hist["Close"].iloc[0])
        # Fall back to current price if history unavailable (e.g. same-day)
        return _fetch_price(ticker)
    except Exception as exc:
        logger.warning(
            "Historical price fetch failed for %s at %s: %s", ticker, timestamp_str, exc
        )
        return _fetch_price(ticker)


# ── Outcome logic ─────────────────────────────────────────────────────────────

def _outcome(signal: str, pct_change: float) -> str:
    if signal == "BUY":
        return "WIN" if pct_change > 0 else "LOSS"
    return "WIN" if pct_change < 0 else "LOSS"   # SELL wins when price drops


def _option_perf(row: dict, current_price: float) -> tuple[float | None, float | None, float | None, str | None]:
    """Return (option_value, option_return_pct, option_profit, option_outcome).
    Returns (None, None, None, None) when the row has no options data."""
    strike   = row.get("option_strike")
    cost     = row.get("option_cost")
    opt_type = row.get("option_type")
    if not (strike and cost and opt_type and cost > 0):
        return None, None, None, None

    if opt_type == "CALL":
        option_value = max(0.0, current_price - strike) * 100
    else:
        option_value = max(0.0, strike - current_price) * 100

    option_return = ((option_value - cost) / cost) * 100
    option_profit = option_value - cost
    outcome = "WIN" if option_profit > 0 else "LOSS"
    return round(option_value, 2), round(option_return, 4), round(option_profit, 2), outcome


# ── Public: update ────────────────────────────────────────────────────────────

def update_performance() -> None:
    """Fill in 6h, 24h, and 7d outcome prices for rows that have matured."""
    now = datetime.utcnow()

    # ── Print age / countdown for every still-pending trade ──────────────────
    pending_all = get_all_pending_rows()
    if pending_all:
        print(f"\n  [perf] {len(pending_all)} pending trade(s) as of {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        for row in pending_all:
            sig_dt = datetime.strptime(row["signal_timestamp"], "%Y-%m-%d %H:%M:%S")
            age_h  = (now - sig_dt).total_seconds() / 3600
            to_6h  = 6  - age_h
            to_24h = 24 - age_h
            s6  = f"OVERDUE by {-to_6h:.1f}h"  if to_6h  <= 0 else f"in {to_6h:.1f}h"
            s24 = f"OVERDUE by {-to_24h:.1f}h" if to_24h <= 0 else f"in {to_24h:.1f}h"
            print(
                f"    {row['signal']:<4} {row['ticker']:<6}  "
                f"logged {row['signal_timestamp']}  age={age_h:.1f}h  "
                f"| 6h: {s6}  | 24h: {s24}"
            )
        print()

    already_resolved_6h = sum(
        1 for row in pending_all
        if (now - datetime.strptime(row["signal_timestamp"], "%Y-%m-%d %H:%M:%S")).total_seconds() >= 6 * 3600
        and row.get("price_6h_later") is not None
    )

    updated_6h  = 0
    updated_24h = 0
    updated_7d  = 0

    for row in get_pending_6h_rows():
        price = _fetch_price(row["ticker"])
        if price is None:
            continue
        pct = (price - row["price_at_signal"]) / row["price_at_signal"] * 100
        opt_val, opt_ret, opt_profit, opt_out = _option_perf(row, price)
        update_performance_6h(row["id"], price, pct, _outcome(row["signal"], pct),
                               opt_val, opt_ret, opt_profit, opt_out)
        updated_6h += 1
        logger.info(
            "6h update: %s %s  entry=%.2f  now=%.2f  pct=%.2f%%",
            row["signal"], row["ticker"], row["price_at_signal"], price, pct,
        )

    for row in get_pending_24h_rows():
        price = _fetch_price(row["ticker"])
        if price is None:
            continue
        pct = (price - row["price_at_signal"]) / row["price_at_signal"] * 100
        opt_val, opt_ret, opt_profit, opt_out = _option_perf(row, price)
        update_performance_24h(row["id"], price, pct, _outcome(row["signal"], pct),
                                opt_val, opt_ret, opt_profit, opt_out)
        updated_24h += 1
        logger.info(
            "24h update: %s %s  entry=%.2f  now=%.2f  pct=%.2f%%",
            row["signal"], row["ticker"], row["price_at_signal"], price, pct,
        )

    for row in get_pending_7d_rows():
        price = _fetch_price(row["ticker"])
        if price is None:
            continue
        pct = (price - row["price_at_signal"]) / row["price_at_signal"] * 100
        opt_val, opt_ret, opt_profit, opt_out = _option_perf(row, price)
        update_performance_7d(row["id"], price, pct, _outcome(row["signal"], pct),
                               opt_val, opt_ret, opt_profit, opt_out)
        updated_7d += 1
        logger.info(
            "7d update: %s %s  entry=%.2f  now=%.2f  pct=%.2f%%",
            row["signal"], row["ticker"], row["price_at_signal"], price, pct,
        )

    logger.info(
        "update_performance complete: %d 6h already resolved, %d 6h newly updated, "
        "%d 24h updates, %d 7d updates",
        already_resolved_6h, updated_6h, updated_24h, updated_7d,
    )


# ── Public: summary ───────────────────────────────────────────────────────────

def print_performance_summary() -> None:
    rows = get_all_performance_rows()
    if not rows:
        print(f"\n{_SEP}")
        print("  PERFORMANCE TRACKER  —  no signals tracked yet")
        print(f"{_SEP}\n")
        return

    total       = len(rows)
    pending     = sum(1 for r in rows if r["percent_change_24h"] is None)
    resolved_24 = [r for r in rows if r["percent_change_24h"] is not None]
    wins_24     = sum(1 for r in resolved_24 if r["outcome"] == "WIN")
    win_rate    = (wins_24 / len(resolved_24) * 100) if resolved_24 else 0.0

    # Best / worst by whichever change column is available (prefer 7d)
    def _best_pct(r: dict) -> float:
        if r["percent_change_7d"] is not None:
            return r["percent_change_7d"]
        if r["percent_change_24h"] is not None:
            return r["percent_change_24h"]
        return 0.0

    resolved_any = [r for r in rows if r["percent_change_24h"] is not None]
    best  = max(resolved_any, key=_best_pct) if resolved_any else None
    worst = min(resolved_any, key=_best_pct) if resolved_any else None

    print(f"\n{_SEP}")
    print("  PERFORMANCE TRACKER  —  Running Score")
    print(_SEP)
    print(f"  Total signals tracked : {total}")
    print(f"  Pending (under 24h)   : {pending}")
    if resolved_24:
        print(f"  Win rate (24h)        : {win_rate:.1f}%  ({wins_24}/{len(resolved_24)})")
    else:
        print("  Win rate (24h)        : N/A  (no resolved signals yet)")

    if best:
        pct = _best_pct(best)
        print(f"  Best call             : {best['signal']} {best['ticker']}  {pct:+.2f}%")
    if worst:
        pct = _best_pct(worst)
        print(f"  Worst call            : {worst['signal']} {worst['ticker']}  {pct:+.2f}%")

    # ── Pending countdown ─────────────────────────────────────────────────────
    now = datetime.utcnow()
    pending_rows = [r for r in rows if r["percent_change_24h"] is None]
    if pending_rows:
        print(f"\n  PENDING COUNTDOWNS  (now {now.strftime('%Y-%m-%d %H:%M')} UTC)")
        for r in pending_rows:
            sig_dt = datetime.strptime(r["signal_timestamp"], "%Y-%m-%d %H:%M:%S")
            age_h  = (now - sig_dt).total_seconds() / 3600
            to_6h  = 6  - age_h
            to_24h = 24 - age_h
            s6  = f"OVERDUE" if to_6h  <= 0 else f"{to_6h:.1f}h"
            s24 = f"OVERDUE" if to_24h <= 0 else f"{to_24h:.1f}h"
            resolve_at = sig_dt + timedelta(hours=24)
            print(
                f"    {r['signal']:<4} {r['ticker']:<6}  "
                f"@ {r['signal_timestamp']}  age={age_h:.1f}h  "
                f"6h→{s6}  24h→{s24} (~{resolve_at.strftime('%m-%d %H:%M')} UTC)"
            )

    # ── Per-ticker breakdown ──────────────────────────────────────────────────
    tickers: dict[str, list[dict]] = {}
    for r in rows:
        tickers.setdefault(r["ticker"], []).append(r)

    print(f"\n  {'Ticker':<8}  {'Signal':<5}  {'Entry':>8}  {'6h':>8}  {'24h':>8}  {'7d':>8}  {'Outcome'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*7}")
    for ticker in sorted(tickers):
        for r in tickers[ticker]:
            entry = f"${r['price_at_signal']:.2f}" if r["price_at_signal"] else "N/A"
            ch6h  = f"{r['percent_change_6h']:+.2f}%"  if r["percent_change_6h"]  is not None else "pending"
            ch24  = f"{r['percent_change_24h']:+.2f}%" if r["percent_change_24h"] is not None else "pending"
            ch7d  = f"{r['percent_change_7d']:+.2f}%"  if r["percent_change_7d"]  is not None else "pending"
            outcome_6h = r.get("outcome_6h", "PENDING")
            outcome_disp = r["outcome"] if r["outcome"] != "PENDING" else outcome_6h
            print(f"  {ticker:<8}  {r['signal']:<5}  {entry:>8}  {ch6h:>8}  {ch24:>8}  {ch7d:>8}  {outcome_disp}")

    print(_SEP + "\n")


# ── Public: backfill ──────────────────────────────────────────────────────────

def backfill_performance_from_trades() -> None:
    """One-time import: insert performance rows for existing simulated_trades."""
    trades = get_untracked_trades()
    if not trades:
        logger.info("Performance backfill: nothing to backfill")
        return

    logger.info("Performance backfill: %d trade(s) to import", len(trades))
    inserted = 0
    for trade in trades:
        ticker = trade["ticker"]
        price = _fetch_historical_price(ticker, trade["executed_at"])
        row_id = insert_performance_row(
            ticker, trade["action"], trade["executed_at"], price
        )
        if row_id is not None:
            inserted += 1
            logger.info(
                "Backfilled: %s %s at %s (price=%.2f)",
                trade["action"], ticker, trade["executed_at"],
                price if price else 0,
            )

    logger.info("Performance backfill complete: %d/%d rows inserted", inserted, len(trades))
    print(f"\n  [perf] Backfilled {inserted} historical trade(s) into performance table.\n")
