# WSB Sentiment Trading Bot

## Project Overview

A Reddit sentiment trading bot that monitors r/wallstreetbets and uses Claude AI to generate BUY/SELL signals with live options recommendations.

- Scrapes the daily discussion thread and hot posts from r/wallstreetbets
- Fetches 3000+ comments from the daily discussion thread for deep sentiment coverage
- Claude AI analyzes each post and comment with full WSB cultural context, including irony detection
- Generates BUY/SELL signals requiring 2+ agreeing posts and 70%+ weighted consensus
- Recommends options plays (calls/puts) with live data from yfinance
- Tracks signal performance at 6h, 24h, and 7d checkpoints with full P&L
- Apple-style web dashboard served at `http://localhost:5000`

---

## Live Test Results

I ran the bot live in May 2026. The test was kept short because every analysis cycle costs Anthropic API credits.

**Activity:** 1,023 posts collected, 756 analyzed by Claude, 80 signals generated (42 BUY, 22 SELL, 16 HOLD), and 66 simulated trades logged.

**Stock signals (24-hour outcome)**

After removing duplicate entries and trades whose checks fell while the market was closed, 42 trades had a real result:

| Signal type | Trades | Win rate | Avg 24h return |
|---|---|---|---|
| All signals | 42 | 57% | +0.85% |
| BUY | 30 | 60% | +1.05% |
| SELL | 12 | 50% | +0.35% |

- Best calls: RKLB BUY (+34%), INTC BUY (+14%), AMD BUY (+11%), CRWV SELL (+11%)
- Worst calls: NET BUY (-24%), ASTS BUY (-12%), SNDK BUY (-7%)
- Median return was only +0.05%, so a few large winners carried the average

**Options recommendations (24-hour outcome)**

- 26 contracts tracked, 38% profitable
- Buying one of each would have cost about $52,000 and lost about $3,400 (-6.6%)
- A single SNDK call accounted for most of the loss (-$4,945)

**Conclusion**

The full pipeline works end to end, but this test does not show that WSB sentiment predicts stock moves. A 57% win rate on 42 trades is close to a coin flip, and results swung with the overall market (77% correct in the strongest session, 25% in the weakest). On the options side, time decay and the high implied volatility of WSB favorites turned small stock moves into losses on most contracts. The target for even considering real money is 200+ resolved trades at a 65%+ win rate, so the bot remains a paper-trading research project.

---

## How It Works

Each pipeline cycle runs these stages in order:

1. **Collect**: scrapes r/wallstreetbets via the public `.json` endpoint. Fetches top hot/new posts plus up to 3000+ comments from the Daily Discussion thread. Posts not meeting flair, upvote ratio, or quality criteria are dropped.
2. **Persist**: new posts and comments are written to a local SQLite database; already-seen items are skipped.
3. **Analyse**: each new item is sent to Claude, which extracts ticker symbols, assigns sentiment (bullish/bearish/neutral), rates quality (1–5), returns a confidence score, and applies irony detection to flip sarcastic sentiment.
4. **Generate signals**: analyses are grouped by ticker. Each item's weight is `upvotes × quality_score × confidence`. A BUY or SELL signal is emitted when 2+ posts agree and the dominant side holds 70%+ of total weighted sentiment.
5. **Options recommendations**: for every signal, live options chains are fetched from yfinance targeting strikes ~5% OTM and expiry 14–30 days out, filtered for liquidity and annotated with IV warnings.
6. **Paper trade**: signals are logged as simulated trades with the entry price recorded via yfinance. A 4-hour cooldown per ticker per direction prevents duplicate signals.
7. **Track performance**: a background job runs hourly, fills in price snapshots at 6h, 24h, and 7d, and computes P&L. Trades move through a lifecycle: `PENDING → ACTIVE → CLOSED` (profit / loss / expiry / reversal).

---

## File Structure

| File | Purpose |
|---|---|
| `main.py` | Orchestrator and APScheduler entry point |
| `config.py` | Environment variables and constants |
| `database.py` | SQLite schema setup and migrations |
| `collector.py` | Reddit scraping with deep comment fetching |
| `analyzer.py` | Claude sentiment analysis with WSB cultural context |
| `signals.py` | Weighted consensus signal generation |
| `options.py` | Live options chain recommendations via yfinance |
| `trader.py` | Simulated trade logging with cooldown system |
| `performance.py` | Automated performance tracking and P&L |
| `dashboard.py` | Flask web dashboard with live prices |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for required environment variables |

---

## Dashboard Features

Served at `http://localhost:5000`.

- **Today's Signals tab**: actionable signals with options recommendation cards and a stock view toggle
- **Performance History tab**: results grouped by date with filter controls
- **Live price ticker bar**: updates every 30 seconds for all tracked tickers
- **Overall Avg Return** (stock) and **Options Avg Return** metrics in the header
- **Total Hypothetical P&L** tracking across all closed trades
- **Trade lifecycle status** badges: `ACTIVE` / `PENDING` / `REVERSED` / `CLOSED`
- **Market status banner** indicating open, closed, after-hours, or weekend

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

**3. Add your Anthropic API key**

```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

**4. Run the bot**

```bash
python main.py
```

**5. Open the dashboard**

```
http://localhost:5000
```

The bot runs one pipeline cycle immediately on startup, then repeats on the configured schedule. Press `Ctrl+C` to stop.

---

## Configuration

Key thresholds are set in `config.py` (and can be overridden via `.env`):

| Setting | Value | Description |
|---|---|---|
| `MIN_QUALITY_SCORE` | `3` | Minimum quality score for regular posts (1–5) |
| `MIN_COMMENT_QUALITY_SCORE` | `2` | Lower quality threshold for Daily Discussion comments |
| `MIN_AGREEING_POSTS` | `2` | Posts that must agree on direction to emit a signal |
| `MIN_TOTAL_WEIGHT` | `20` | Minimum total weighted score to emit a signal (40 on weekends) |
| `BULLISH_THRESHOLD` | `0.70` | Weighted share required to emit a BUY signal |
| `BEARISH_THRESHOLD` | `0.70` | Weighted share required to emit a SELL signal |
| Signal cooldown | `4 hours` | Per ticker per direction: prevents duplicate signals |
| Options expiry target | `14–30 days` | Target DTE window for options recommendations |
| IV warning threshold | `60%` | Orange IV warning; red above 80% |

---

## Performance Tracking

Every simulated trade gets price snapshots at three checkpoints:

| Window | Meaning |
|---|---|
| **6h** | Early directional signal: short-term market reaction |
| **24h** | Primary performance measure: next-day outcome |
| **7d** | Longer-term outcome: medium-term trend confirmation |

- Options P&L is calculated using live market prices from yfinance
- SELL signal returns are sign-flipped so profit = positive in all cases
- Overall Avg Return is audited and verified via the browser console on each dashboard load
- Trades logged before the performance table existed are automatically backfilled on startup using historical closing prices

---

## Signal Quality Filters

Signals are filtered at multiple stages to reduce noise:

- **Flair filter**: only DD, Discussion, News, and Earnings Thread flairs are processed
- **Upvote ratio**: minimum 0.70 to filter divisive or brigaded posts
- **Upvote floor**: posts require 100+ upvotes; comments require 2+
- **Quality score**: Claude assigns 1–5; items below threshold are excluded
- **Irony detection**: Claude identifies sarcasm and flips the sentiment accordingly
- **Ticker normalization**: company names and common WSB shorthand are mapped to canonical symbols
- **Ticker validation**: symbols are confirmed against yfinance before a signal is recorded

---

## Options Recommendations

For each BUY/SELL signal, the bot fetches a live options recommendation:

- **Strike**: ~5% OTM (calls for BUY, puts for SELL)
- **Expiry**: nearest expiry in the 14–30 day window
- **Liquidity filter**: open interest 100+, bid $0.10+
- **IV warnings**: color-coded: orange at 60–80% IV, red above 80%
- **Sizing**: conservative suggestion based on confidence score; max 1–2 contracts recommended

---

## Known Issues and Next Steps

Issues found during the live test, to fix before collecting more data:

- **Duplicate trades:** some signals were logged twice within seconds of each other
- **Weekend checks:** 6h and 24h checks that land while the market is closed record 0% change and get scored as losses
- **Contradictory signals:** the bot occasionally issued BUY and SELL on the same ticker minutes apart
- **Options price lookups:** a failed lookup is stored as a $0 value (-100%) instead of being retried
- **Reddit API:** swap the public `.json` endpoint for PRAW once official API access is approved

---

## Disclaimer

**This project is for educational and research purposes only. It is not financial advice. All trades are simulated (paper trading only): no real money is ever placed.**

- Paper trade only until you have sufficient performance data to evaluate signal quality
- Options trading carries a significant risk of total loss of capital
- Past simulated performance does not guarantee future results
- Always do your own research before trading real money
