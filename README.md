# WSB Sentiment Trading Bot

## Project Overview

This bot scrapes r/wallstreetbets, uses Claude (Haiku) to analyse the sentiment and quality of each post, and aggregates those analyses into weighted BUY / SELL / HOLD signals for individual tickers. It runs on a recurring schedule, simulates paper trades, and tracks how each signal performs at 6h, 24h, and 7d intervals using live prices from Yahoo Finance.

---

## How It Works

Each pipeline cycle runs these stages in order:

1. **Collect** — fetches the top 100 hot/top posts from r/wallstreetbets via Reddit's public JSON endpoint, plus comments from the Daily Discussion thread. Posts that don't meet minimum upvote, comment-count, body-length, and flair criteria are dropped.
2. **Persist** — new posts are written to a local SQLite database; already-known posts are skipped.
3. **Analyse** — each new post is sent to Claude, which extracts ticker symbols, assigns a sentiment (bullish / bearish / neutral), rates quality (1–5), and returns a confidence score.
4. **Save analyses** — results are stored in the database.
5. **Generate signals** — analyses are grouped by ticker. Each post's weight is `upvotes × quality_score × confidence`. If the dominant side reaches `MIN_AGREEING_POSTS` posts and its weighted share clears `BULLISH_THRESHOLD` or `BEARISH_THRESHOLD`, a BUY or SELL signal is emitted; otherwise HOLD.
6. **Execute (paper) trades** — signals are logged as simulated trades with the current market price recorded via Yahoo Finance.
7. **Track performance** — a background job runs hourly to fill in price snapshots at 6h, 24h, and 7d after each signal, then marks each call WIN or LOSS.

---

## File Structure

| File | Purpose |
|---|---|
| `main.py` | Entry point and scheduler; orchestrates the full pipeline |
| `config.py` | All configurable constants; loads `.env` and validates required keys |
| `collector.py` | Scrapes r/wallstreetbets via the public JSON API and applies post filters |
| `analyzer.py` | Sends posts to Claude and parses structured analysis results |
| `signals.py` | Aggregates analyses into weighted ticker-level BUY/SELL/HOLD signals |
| `trader.py` | Records simulated trades and prints the per-run summary |
| `performance.py` | Tracks signal outcomes at 6h / 24h / 7d using Yahoo Finance prices |
| `database.py` | SQLite schema, all read/write helpers |
| `options.py` | Options chain utilities (supporting module) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for the required environment variables |

---

## Setup Instructions

**1. Clone and install dependencies**

```bash
git clone <repo-url>
cd wsb-bot
pip install -r requirements.txt
```

**2. Create your `.env` file**

```bash
cp .env.example .env
```

Open `.env` and fill in your Anthropic API key. All other values have working defaults:

```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Reddit scraping (no API key needed — public .json endpoint)
REDDIT_USER_AGENT=wsb-sentiment-bot/1.0 (educational)

# Post filters
MIN_UPVOTES=100
MIN_UPVOTE_RATIO=0.70
MIN_COMMENTS=20
MIN_BODY_LENGTH=200

# Analysis
MIN_QUALITY_SCORE=4

# Signal generation
MIN_AGREEING_POSTS=3
BULLISH_THRESHOLD=0.70
BEARISH_THRESHOLD=0.70

# Scheduler (minutes between pipeline runs)
SCHEDULE_INTERVAL_MINUTES=15

# Database
DB_PATH=wsb_bot.db

# Logging: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=INFO
```

---

## How to Run

```bash
python main.py
```

The bot runs one pipeline cycle immediately on startup, then repeats every `SCHEDULE_INTERVAL_MINUTES` minutes. Press `Ctrl+C` to stop.

---

## Configuration

All thresholds can be tuned via `.env` without touching any code.

| Variable | Default | What it does |
|---|---|---|
| `MIN_UPVOTES` | `100` | Minimum upvotes a post must have to enter the pipeline |
| `MIN_UPVOTE_RATIO` | `0.70` | Minimum ratio of upvotes to total votes (filters divisive posts) |
| `MIN_COMMENTS` | `20` | Minimum comment count; low-engagement posts are skipped |
| `MIN_BODY_LENGTH` | `200` | Minimum character count of the post body; filters thin posts |
| `MIN_QUALITY_SCORE` | `4` | Minimum quality score (1–5) Claude must assign before a post influences signals |
| `MIN_COMMENT_QUALITY_SCORE` | `2` | Same threshold applied to Daily Discussion comments (lower bar) |
| `MIN_AGREEING_POSTS` | `3` | Number of posts that must agree on direction before a signal is generated |
| `BULLISH_THRESHOLD` | `0.70` | Weighted-sentiment share (0–1) required to emit a BUY signal |
| `BEARISH_THRESHOLD` | `0.70` | Weighted-sentiment share (0–1) required to emit a SELL signal |
| `SCHEDULE_INTERVAL_MINUTES` | `15` | How often the pipeline runs |

**Tuning tips:**
- Raise `MIN_AGREEING_POSTS` or the threshold values for fewer but higher-conviction signals.
- Lower `MIN_QUALITY_SCORE` to cast a wider net; raise it to filter out noise.
- Increase `SCHEDULE_INTERVAL_MINUTES` to reduce API costs during quiet market periods.

---

## Performance Tracking

Every simulated trade is inserted into the `performance` table at execution time, with the market price recorded as the entry price. A background job runs once per hour and checks three time-based snapshots:

| Window | Meaning |
|---|---|
| **6h** | Price 6 hours after the signal — short-term reaction |
| **24h** | Price 24 hours after the signal — primary outcome window |
| **7d** | Price 7 days after the signal — medium-term trend |

For each window, the bot fetches the current price via Yahoo Finance, computes the percentage change from entry, and marks the call **WIN** or **LOSS**:
- A **BUY** signal wins if the price is higher at the measurement time.
- A **SELL** signal wins if the price is lower.

The performance summary printed after every pipeline run shows total signals tracked, win rate (24h), best and worst calls, and a per-ticker breakdown with all three time-window results.

Trades logged before the performance table existed are automatically backfilled on startup using historical closing prices.

---

## Disclaimer

**This project is for educational and research purposes only. It is not financial advice. All trades are simulated (paper trading only) — no real money is ever placed. Options trading carries a significant risk of total loss of capital. Past signal performance does not guarantee future results. Do not use this bot to make real investment decisions.**
