"""
WSB Sentiment Trading Bot — main orchestrator.

Pipeline (runs every SCHEDULE_INTERVAL_MINUTES minutes):
  1. Collect filtered posts from r/wallstreetbets
  2. Save new posts to the database
  3. Analyse new posts with Claude
  4. Save analyses to the database
  5. Generate BUY / SELL / HOLD signals
  6. Execute (simulate) trades and log them
  7. Print a run summary

Run:
    python main.py
"""

import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

import config
from config import setup_logging, validate_config, SCHEDULE_INTERVAL_MINUTES
from database import init_db, save_post, analysis_exists, save_analysis, get_pipeline_stats, write_heartbeat
from collector import collect_posts, collect_discussion_comments
from analyzer import analyze_posts
from signals import generate_signals
from trader import execute_signals, print_run_summary
from performance import update_performance, print_performance_summary, backfill_performance_from_trades
import dashboard

logger = logging.getLogger(__name__)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline() -> None:
    """Execute one full pipeline cycle."""
    run_start = time.monotonic()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    logger.info("=" * 60)
    logger.info("Pipeline run starting  [%s]", ts)
    logger.info("=" * 60)

    # ── Step 1: Collect ──────────────────────────────────────────────────────
    logger.info("Step 1/5  — Collecting posts from r/%s", config.SUBREDDIT)
    try:
        regular_posts = collect_posts(sorts=["hot", "top"])
    except Exception as exc:
        logger.error("Regular post collection failed: %s", exc, exc_info=True)
        regular_posts = []

    try:
        discussion_comments = collect_discussion_comments()
    except Exception as exc:
        logger.error("Discussion comment collection failed: %s", exc, exc_info=True)
        discussion_comments = []

    posts = regular_posts + discussion_comments
    posts_collected = len(posts)
    logger.info(
        "Collected %d item(s) total (%d regular posts, %d discussion comments)",
        posts_collected, len(regular_posts), len(discussion_comments),
    )

    # ── Step 2: Persist new posts ────────────────────────────────────────────
    logger.info("Step 2/5  — Persisting new posts to database")
    posts_new = 0
    for post in posts:
        if save_post(post):
            posts_new += 1

    logger.info("%d new post(s) saved (%d already known)", posts_new, posts_collected - posts_new)

    # ── Step 3: Analyse new posts ────────────────────────────────────────────
    logger.info("Step 3/5  — Analysing posts with Claude")
    new_posts = [
        p for p in posts
        if p.get("flair") == "Daily Discussion" or not analysis_exists(p["id"])
    ]
    logger.info("%d post(s) need analysis", len(new_posts))

    analyses: list[dict] = []
    if new_posts:
        try:
            analyses = analyze_posts(new_posts)
        except Exception as exc:
            logger.error("Analysis phase error: %s", exc, exc_info=True)

    logger.info("%d analysis result(s) produced", len(analyses))

    # ── Step 4: Persist analyses ─────────────────────────────────────────────
    logger.info("Step 4/5  — Saving analyses to database")
    saved_count = 0
    for analysis in analyses:
        if save_analysis(analysis) is not None:
            saved_count += 1

    logger.info("Saved %d/%d analysis result(s)", saved_count, len(analyses))

    # ── Step 5: Generate signals & simulate trades ───────────────────────────
    logger.info("Step 5/5  — Generating signals and executing simulated trades")
    signals: list[dict] = []
    trades:  list[dict] = []

    if analyses:
        try:
            signals = generate_signals(posts, analyses)
        except Exception as exc:
            logger.error("Signal generation error: %s", exc, exc_info=True)

        if signals:
            try:
                trades = execute_signals(signals)
            except Exception as exc:
                logger.error("Trade execution error: %s", exc, exc_info=True)
        else:
            logger.info("No signals generated this run")
            print("\n  [trader] No actionable signals this cycle.\n")
    else:
        logger.info("Skipping signal generation — no new analyses")

    # ── Summary ───────────────────────────────────────────────────────────────
    run_duration = time.monotonic() - run_start
    print_run_summary(
        posts_collected    = posts_collected,
        posts_new          = posts_new,
        analyses_completed = len(analyses),
        signals_generated  = len(signals),
        trades_executed    = len(trades),
        run_duration_seconds = run_duration,
    )

    stats = get_pipeline_stats()
    logger.info(
        "DB totals — posts=%d  analyses=%d  signals=%d  trades=%d",
        stats["total_posts"], stats["total_analyses"],
        stats["total_signals"], stats["total_trades"],
    )
    logger.info("Pipeline run complete in %.1fs", run_duration)

    write_heartbeat(run_duration, posts_collected, len(signals))
    dashboard.set_last_run()
    print_performance_summary()


# ── APScheduler callbacks ─────────────────────────────────────────────────────

def _on_job_executed(event) -> None:
    logger.debug("Scheduled job executed successfully (id=%s)", event.job_id)


def _on_job_error(event) -> None:
    logger.error(
        "Scheduled job raised an exception (id=%s): %s",
        event.job_id, event.exception,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger.info("WSB Sentiment Bot starting up …")

    # Fail fast on missing required config
    errors = validate_config()
    if errors:
        for err in errors:
            logger.critical("Configuration error: %s", err)
        raise SystemExit("Fix .env configuration errors above before running.")

    # Initialise database schema
    init_db()

    # Start the web dashboard on port 5000
    dashboard.start()

    # One-time backfill of any pre-existing trades into the performance table
    backfill_performance_from_trades()

    # Run once immediately so we get output right away
    logger.info("Running initial pipeline pass …")
    run_pipeline()

    # Fill in any overdue performance prices immediately — don't wait 60 min
    logger.info("Running initial performance update …")
    update_performance()

    # Schedule recurring runs
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        func=run_pipeline,
        trigger="interval",
        minutes=SCHEDULE_INTERVAL_MINUTES,
        id="wsb_pipeline",
        name="WSB Sentiment Pipeline",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        func=update_performance,
        trigger="interval",
        minutes=60,
        id="perf_update",
        name="Performance Tracker Update",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_on_job_error,    EVENT_JOB_ERROR)

    logger.info(
        "Scheduler started — pipeline will run every %d minute(s). Press Ctrl+C to stop.",
        SCHEDULE_INTERVAL_MINUTES,
    )
    print(
        f"\n  WSB Bot running.  Next scheduled run in {SCHEDULE_INTERVAL_MINUTES} min(s).\n"
        f"  Press Ctrl+C to stop.\n"
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received — shutting down scheduler.")
        scheduler.shutdown(wait=False)
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
