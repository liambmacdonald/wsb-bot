"""
Simulated trader — no real orders, no Alpaca.

For each incoming signal:
  1. Persist the signal to the DB.
  2. Derive a simulated trade action from the signal.
  3. Persist the trade to the DB.
  4. Print a human-readable summary to the console.
"""

import logging
from datetime import datetime, timezone

import yfinance as yf

from database import (
    save_signal, save_trade, insert_performance_row,
    get_recent_trade_for_ticker, save_hold,
    get_latest_active_performance_row, update_trade_status,
)
from options import format_recommendation, get_options_recommendation

logger = logging.getLogger(__name__)

# ── Formatting helpers ────────────────────────────────────────────────────────

_ACTION_ICON = {"BUY": "[BUY ]", "SELL": "[SELL]", "HOLD": "[HOLD]"}


def _fetch_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        price = float(info.last_price)
        return price if price > 0 else None
    except Exception as exc:
        logger.warning("Could not fetch price for %s: %s", ticker, exc)
        return None

_SEPARATOR = "-" * 70


def _format_signal_block(signal: dict, trade_id: int | None) -> str:
    ticker    = signal["ticker"]
    sig       = signal["signal"]
    icon      = _ACTION_ICON.get(sig, "⚪")
    b_weight  = signal["bullish_weight"]
    be_weight = signal["bearish_weight"]
    total_w   = signal["total_weight"]
    b_count   = signal["bullish_count"]
    be_count  = signal["bearish_count"]
    reasoning = signal["reasoning"]
    ts        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    bull_pct = (b_weight / total_w * 100) if total_w else 0.0
    bear_pct = (be_weight / total_w * 100) if total_w else 0.0

    lines = [
        _SEPARATOR,
        f"  {icon}  SIMULATED {sig}  —  {ticker}  [{ts}]",
        _SEPARATOR,
        f"  Ticker          : {ticker}",
        f"  Action          : {sig}",
        f"  Trade ID        : {trade_id if trade_id else 'N/A'}",
        f"  Posts (bullish) : {b_count}   weighted {b_weight:,.1f}  ({bull_pct:.1f}%)",
        f"  Posts (bearish) : {be_count}   weighted {be_weight:,.1f}  ({bear_pct:.1f}%)",
        f"  Total weight    : {total_w:,.1f}",
        f"  Reasoning       : {reasoning}",
        _SEPARATOR,
    ]
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def execute_signals(signals: list[dict]) -> list[dict]:
    """
    Persist and log every signal as a simulated trade.

    Args:
        signals: Signal dicts produced by signals.generate_signals().

    Returns:
        List of trade dicts that were successfully saved.
    """
    if not signals:
        logger.info("No signals to execute this run.")
        print("\n  [trader] No signals generated this cycle.\n")
        return []

    trades: list[dict] = []

    print(f"\n{'-' * 70}")
    print(f"  WSB SIMULATED TRADES  --  {len(signals)} signal(s) this cycle")
    print(f"{'-' * 70}")

    for signal in signals:
        ticker = signal.get("ticker", "??")
        action = signal.get("signal", "HOLD")
        total_weight = signal.get("total_weight", 0.0)

        # ── HOLD signals: console only, log to holds_log, skip trades/performance ──
        if action == "HOLD":
            print(_format_signal_block(signal, None))
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            save_hold(
                ticker=ticker,
                signal_timestamp=ts,
                total_weight=signal.get("total_weight"),
                bullish_weight=signal.get("bullish_weight"),
                bearish_weight=signal.get("bearish_weight"),
                reasoning=signal.get("reasoning"),
            )
            logger.info("HOLD %s — logged to holds_log, skipping trade/performance", ticker)
            continue

        # ── Cooldown check: skip duplicate direction within 4h ─────────────────
        recent = get_recent_trade_for_ticker(ticker, hours=4.0)
        if recent is not None:
            recent_action = recent["action"]
            recent_ts     = recent["executed_at"]
            if recent_action == action:
                try:
                    dt    = datetime.strptime(recent_ts, "%Y-%m-%d %H:%M:%S")
                    age_h = (datetime.utcnow() - dt).total_seconds() / 3600
                except Exception:
                    age_h = 0.0
                print(f"  [cooldown] Skipping {action} {ticker} — already logged {age_h:.1f}h ago")
                logger.info("[cooldown] Skipping %s %s — already logged %.1fh ago", action, ticker, age_h)
                save_signal(signal)  # record signal for reference, skip trade/perf
                continue
            else:
                # Direction reversed — close the previous open trade
                old_row = get_latest_active_performance_row(ticker)
                if old_row is not None:
                    update_trade_status(
                        old_row["id"],
                        "CLOSED_REVERSAL",
                        close_reason=f"Bot issued opposite {action} signal",
                    )
                    logger.info(
                        "CLOSED_REVERSAL: %s id=%d (was %s, now %s)",
                        ticker, old_row["id"], recent_action, action,
                    )

        # Persist the signal
        signal_id = save_signal(signal)
        if signal_id is None:
            logger.error("Failed to save signal for %s — trade will not be logged", ticker)
            continue

        # Build the simulated trade
        b_weight  = signal.get("bullish_weight", 0.0)
        be_weight = signal.get("bearish_weight", 0.0)
        if total_weight > 0:
            signal_strength = max(b_weight, be_weight) / total_weight
        else:
            signal_strength = 0.0

        trade = {
            "signal_id":       signal_id,
            "ticker":          ticker,
            "action":          action,
            "signal_strength": round(signal_strength, 4),
            "reasoning":       signal.get("reasoning", ""),
        }

        options_rec = None
        trade_id = save_trade(trade)
        if trade_id is None:
            logger.error("Failed to save trade for %s", ticker)
        else:
            trade["id"] = trade_id
            trades.append(trade)
            logger.info(
                "Simulated trade logged: id=%d  %s %s  strength=%.2f",
                trade_id, action, ticker, signal_strength,
            )
            price = _fetch_current_price(ticker)
            ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if price is not None:
                options_rec = get_options_recommendation(ticker, action, price, signal_strength)
            insert_performance_row(ticker, action, ts, price, options_rec)
            logger.debug("Performance row inserted for %s @ %s", ticker, price)

        # Console output
        print(_format_signal_block(signal, trade_id))
        if options_rec is not None:
            print(format_recommendation(options_rec))

    print(f"\n  Summary: {len(trades)} trade(s) logged to database.\n")
    return trades


def print_run_summary(
    posts_collected: int,
    posts_new: int,
    analyses_completed: int,
    signals_generated: int,
    trades_executed: int,
    run_duration_seconds: float,
) -> None:
    """Print a high-level summary of the pipeline run."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'-' * 70}")
    print(f"  PIPELINE RUN COMPLETE  [{ts}]")
    print(f"{'-' * 70}")
    print(f"  Posts collected    : {posts_collected} (new: {posts_new})")
    print(f"  Posts analysed     : {analyses_completed}")
    print(f"  Signals generated  : {signals_generated}")
    print(f"  Trades logged      : {trades_executed}")
    print(f"  Run duration       : {run_duration_seconds:.1f}s")
    print(f"{'-' * 70}\n")
