"""
Aggregates per-post analysis results into ticker-level BUY / SELL / HOLD signals.

Weighting formula:
    weight = upvotes * quality_score * confidence

A signal is only generated when at least MIN_AGREEING_POSTS posts share the
dominant direction (bullish OR bearish).  Within those posts the weighted share
of the dominant direction determines the final call:
    >= BULLISH_THRESHOLD  → BUY
    >= BEARISH_THRESHOLD  → SELL
    otherwise             → HOLD
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from config import BULLISH_THRESHOLD, BEARISH_THRESHOLD, MIN_AGREEING_POSTS

logger = logging.getLogger(__name__)

_MIN_WEIGHT_NORMAL  = 20.0
_MIN_WEIGHT_WEEKEND = 40.0


def _is_weekend() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5  # Sat=5, Sun=6


def _is_after_hours() -> bool:
    now = datetime.now(timezone.utc)
    et_offset = -4 if 3 <= now.month <= 11 else -5
    et_minutes = (now.hour * 60 + now.minute) + et_offset * 60
    if et_minutes < 0:
        et_minutes += 24 * 60
    et_hour = et_minutes // 60
    et_min  = et_minutes % 60
    before_open = et_hour < 9 or (et_hour == 9 and et_min < 30)
    after_close = et_hour >= 16
    return before_open or after_close


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_weight(post: dict, analysis: dict) -> float:
    """
    Weight = upvotes × quality_score × confidence.
    All three factors must be positive for the post to count.
    """
    upvotes       = max(int(post.get("upvotes", 0)), 1)   # floor at 1 to avoid zero weight
    quality_score = float(analysis.get("quality_score", 0))
    confidence    = float(analysis.get("confidence", 0))

    if quality_score <= 0 or confidence <= 0:
        return 0.0

    return upvotes * quality_score * confidence


def _build_ticker_buckets(
    posts: list[dict],
    analyses: list[dict],
) -> dict[str, list[dict]]:
    """
    Map each ticker symbol → list of {weight, sentiment, post_id} entries.
    Only sentiments 'bullish' and 'bearish' contribute directional weight;
    'neutral' posts are recorded but do not count toward agreeing posts.
    """
    post_map = {p["id"]: p for p in posts}
    buckets: dict[str, list[dict]] = defaultdict(list)

    for analysis in analyses:
        post_id   = analysis.get("post_id", "")
        tickers   = analysis.get("tickers", [])
        sentiment = analysis.get("sentiment", "neutral")

        post = post_map.get(post_id)
        if post is None:
            logger.warning("No matching post for analysis post_id=%s", post_id)
            continue

        weight = _compute_weight(post, analysis)
        if weight <= 0:
            logger.debug("Zero weight for post %s — skipping", post_id)
            continue

        entry = {
            "post_id":   post_id,
            "sentiment": sentiment,
            "weight":    weight,
            "upvotes":   post.get("upvotes", 0),
        }

        for ticker in tickers:
            ticker = ticker.upper().strip()
            if ticker:
                buckets[ticker].append(entry)
                logger.debug(
                    "Ticker %s ← post %s  sentiment=%s  weight=%.1f",
                    ticker, post_id, sentiment, weight,
                )

    return dict(buckets)


def _generate_signal_for_ticker(
    ticker: str, entries: list[dict], min_weight: float = _MIN_WEIGHT_NORMAL
) -> dict | None:
    """
    Given all weighted entries for one ticker, compute a signal.
    Returns a signal dict or None if the minimum-post or weight threshold isn't met.
    """
    bullish_entries = [e for e in entries if e["sentiment"] == "bullish"]
    bearish_entries = [e for e in entries if e["sentiment"] == "bearish"]

    bullish_count = len(bullish_entries)
    bearish_count = len(bearish_entries)

    dominant_count = max(bullish_count, bearish_count)

    if dominant_count < MIN_AGREEING_POSTS:
        logger.info(
            "Ticker %s: dominant side has only %d agreeing post(s) (need %d) — no signal",
            ticker, dominant_count, MIN_AGREEING_POSTS,
        )
        return None

    bullish_weight = sum(e["weight"] for e in bullish_entries)
    bearish_weight = sum(e["weight"] for e in bearish_entries)
    total_weight   = bullish_weight + bearish_weight

    if total_weight == 0:
        logger.warning("Ticker %s: total weight is zero — skipping", ticker)
        return None

    if total_weight < min_weight:
        logger.info(
            "Ticker %s: total weight %.1f below minimum %.1f — no signal",
            ticker, total_weight, min_weight,
        )
        return None

    bullish_share = bullish_weight / total_weight
    bearish_share = bearish_weight / total_weight

    logger.info(
        "Ticker %s: bullish=%.2f (n=%d)  bearish=%.2f (n=%d)  total_weight=%.1f",
        ticker, bullish_share, bullish_count, bearish_share, bearish_count, total_weight,
    )

    if bullish_share >= BULLISH_THRESHOLD:
        signal = "BUY"
        reasoning = (
            f"{bullish_count} bullish post(s) represent {bullish_share:.0%} of weighted "
            f"sentiment (threshold {BULLISH_THRESHOLD:.0%}). "
            f"Total weight: {total_weight:.1f}."
        )
    elif bearish_share >= BEARISH_THRESHOLD:
        signal = "SELL"
        reasoning = (
            f"{bearish_count} bearish post(s) represent {bearish_share:.0%} of weighted "
            f"sentiment (threshold {BEARISH_THRESHOLD:.0%}). "
            f"Total weight: {total_weight:.1f}."
        )
    else:
        signal = "HOLD"
        reasoning = (
            f"Mixed sentiment: {bullish_share:.0%} bullish / {bearish_share:.0%} bearish. "
            f"Neither direction clears its threshold. Total weight: {total_weight:.1f}."
        )

    return {
        "ticker":         ticker,
        "signal":         signal,
        "bullish_weight": round(bullish_weight, 2),
        "bearish_weight": round(bearish_weight, 2),
        "total_weight":   round(total_weight, 2),
        "bullish_count":  bullish_count,
        "bearish_count":  bearish_count,
        "post_ids":       [e["post_id"] for e in entries],
        "reasoning":      reasoning,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def generate_signals(posts: list[dict], analyses: list[dict]) -> list[dict]:
    """
    Build signals from this pipeline run's posts and analyses.

    Args:
        posts:    Raw post dicts (from collector or DB).
        analyses: Analysis dicts returned by analyzer.analyze_posts.

    Returns:
        List of signal dicts ready for database.save_signal() and trader.execute_signals().
    """
    if not analyses:
        logger.info("No analyses to aggregate — returning empty signal list")
        return []

    logger.info(
        "Generating signals from %d analyses across %d posts …",
        len(analyses), len(posts),
    )

    buckets    = _build_ticker_buckets(posts, analyses)
    signals: list[dict] = []
    weekend    = _is_weekend()
    after_hrs  = _is_after_hours()
    min_weight = _MIN_WEIGHT_WEEKEND if weekend else _MIN_WEIGHT_NORMAL

    if weekend:
        logger.info("Weekend mode: minimum weight raised to %.1f", _MIN_WEIGHT_WEEKEND)
    if after_hrs:
        logger.info("After-hours mode: signals will carry lower-confidence note")

    for ticker, entries in sorted(buckets.items()):
        logger.info(
            "Processing ticker %s: %d weighted entr%s",
            ticker, len(entries), "y" if len(entries) == 1 else "ies",
        )
        sig = _generate_signal_for_ticker(ticker, entries, min_weight=min_weight)
        if sig:
            if after_hrs:
                sig["after_hours"] = True
                sig["reasoning"] = sig["reasoning"] + " After Hours Signal — lower confidence."
            else:
                sig["after_hours"] = False
            signals.append(sig)
            logger.info("Signal generated: %s → %s", ticker, sig["signal"])

    buy_count  = sum(1 for s in signals if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals if s["signal"] == "SELL")
    hold_count = sum(1 for s in signals if s["signal"] == "HOLD")

    logger.info(
        "generate_signals complete: %d signal(s) — BUY=%d  SELL=%d  HOLD=%d",
        len(signals), buy_count, sell_count, hold_count,
    )
    return signals
