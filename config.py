import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Anthropic ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"

# ── Reddit public JSON scraping ────────────────────────────────────────────────
REDDIT_USER_AGENT: str = os.getenv(
    "REDDIT_USER_AGENT", "wsb-sentiment-bot/1.0 (educational)"
)
SUBREDDIT: str = "wallstreetbets"
REDDIT_FETCH_LIMIT: int = 100          # posts per request (Reddit max = 100)
REDDIT_REQUEST_DELAY: float = 2.0      # seconds between requests to stay polite

# ── Post-quality filters ───────────────────────────────────────────────────────
MIN_UPVOTES: int = int(os.getenv("MIN_UPVOTES", "100"))
MIN_UPVOTE_RATIO: float = float(os.getenv("MIN_UPVOTE_RATIO", "0.70"))
MIN_COMMENTS: int = int(os.getenv("MIN_COMMENTS", "20"))
MIN_BODY_LENGTH: int = int(os.getenv("MIN_BODY_LENGTH", "200"))
VALID_FLAIRS: set[str] = {"DD", "Discussion", "News", "Catalyst", "Earnings Thread", "Daily Discussion"}

# ── Analysis thresholds ────────────────────────────────────────────────────────
MIN_QUALITY_SCORE: int = int(os.getenv("MIN_QUALITY_SCORE", "3"))
MIN_COMMENT_QUALITY_SCORE: int = int(os.getenv("MIN_COMMENT_QUALITY_SCORE", "2"))

# ── Signal-generation thresholds ──────────────────────────────────────────────
MIN_AGREEING_POSTS: int = int(os.getenv("MIN_AGREEING_POSTS", "2"))
BULLISH_THRESHOLD: float = float(os.getenv("BULLISH_THRESHOLD", "0.70"))
BEARISH_THRESHOLD: float = float(os.getenv("BEARISH_THRESHOLD", "0.70"))

# ── Scheduler ─────────────────────────────────────────────────────────────────
SCHEDULE_INTERVAL_MINUTES: int = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "15"))

# ── Persistence ───────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "wsb_bot.db")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT: str = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("wsb_bot.log", encoding="utf-8"),
        ],
    )


def validate_config() -> list[str]:
    """Return a list of configuration errors (empty = OK)."""
    errors: list[str] = []
    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is not set in .env")
    if not (0 < BULLISH_THRESHOLD <= 1):
        errors.append("BULLISH_THRESHOLD must be in (0, 1]")
    if not (0 < BEARISH_THRESHOLD <= 1):
        errors.append("BEARISH_THRESHOLD must be in (0, 1]")
    if MIN_AGREEING_POSTS < 1:
        errors.append("MIN_AGREEING_POSTS must be >= 1")
    return errors
