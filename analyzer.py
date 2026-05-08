"""
Calls the Anthropic API (claude-haiku-4-5) to analyse each filtered WSB post.

Key behaviours:
  - System prompt is cached with cache_control="ephemeral" to save tokens.
  - Any post scoring below MIN_QUALITY_SCORE is discarded (returns None).
  - If is_ironic is True the sentiment is flipped before returning.
  - Every error is caught so one bad post never kills the pipeline.
"""

import json
import logging
import math
import re

import anthropic
import yfinance as yf

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, MIN_QUALITY_SCORE, MIN_COMMENT_QUALITY_SCORE

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a financial analyst specialising in Reddit's r/wallstreetbets (WSB) community.
You have deep knowledge of WSB culture, humour, irony, and investing slang.

## WSB slang glossary
- retard / regard        → term of endearment for members; NOT offensive in WSB context
- tendies                → profits / gains
- to the moon / 🚀       → expecting a huge price increase
- loss porn              → ironically sharing catastrophic trading losses (pride, not sadness)
- gain porn              → sharing massive gains
- apes / ape together    → community members holding/buying together
- diamond hands / 💎🙌   → holding a position no matter what
- paper hands            → selling too early out of fear
- YOLO                   → betting everything on one trade
- DD                     → due diligence; a research post with real thesis
- theta gang             → options premium sellers
- IV crush               → implied-volatility collapse after earnings
- bags / bagholder       → holding a losing position indefinitely
- short squeeze          → forced short-cover rally
- gamma squeeze          → options-driven price spike
- calls / puts           → options contracts (calls = bullish, puts = bearish)
- FD / 0DTE              → very short-dated options (often lottery tickets)
- autist                 → someone who does obsessively deep research
- chad                   → someone making big gains with conviction
- NFA / DYOR             → "not financial advice / do your own research"

## Critical irony detection rules
1. "Loss porn" posts CELEBRATE losses — treat as neutral/ironic, NOT bearish signals.
2. Exaggerated rocket emojis with no substance are likely jokes.
3. A post saying "trust me bro, 100% going to the moon" with no data is ironic hype.
4. Extreme all-caps enthusiasm with no thesis is likely meme content.
5. A well-reasoned post that happens to use WSB slang CAN be a genuine thesis.

## Your task
Analyse the provided WSB post and return ONLY a valid JSON object — no markdown, no prose.

### Required JSON structure
{
  "has_thesis":     <boolean — true if the post makes a real investment argument>,
  "is_ironic":      <boolean — true if the post is sarcastic, a joke, or loss porn>,
  "quality_score":  <integer 0–10>,
  "sentiment":      <"bullish" | "bearish" | "neutral">,
  "confidence":     <float 0.0–1.0 — your confidence in the sentiment call>,
  "tickers":        <array of official exchange-listed ticker symbols only (e.g. AVGO not BROADCOM, INTC not INTEL). If you only know the company name, convert it to its ticker symbol. Never return a company name as a ticker.>,
  "reasoning":      <string — 1–3 sentence explanation>
}

### Quality score guide — regular posts
0–3   No real thesis; pure meme, joke, loss porn, or ironic hype
4–6   Some thesis present but weak evidence or low conviction
7–10  Strong DD with clear thesis, supporting evidence, and high conviction

### Quality score guide — discussion comments
Discussion comments are short by nature. A comment like '$NVDA calls, earnings look strong' is quality_score=3. Do not penalize brevity for comments.
3–10  Mentions a specific ticker with any reason (even just "NVDA calls, earnings tomorrow")
2     Mentions a ticker with sentiment but no supporting reason
1     Pure meme, no ticker, or zero investment content

When the post type is "Discussion comment", use the discussion comment rubric above."""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _flip_sentiment(sentiment: str) -> str:
    return {"bullish": "bearish", "bearish": "bullish"}.get(sentiment, sentiment)


def _parse_json(text: str) -> dict:
    """Try multiple strategies to extract a JSON object from the model response."""
    # 1. Direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 2. JSON inside a markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First JSON-shaped blob anywhere in the text
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No parseable JSON found in model response: {text[:300]!r}")


def _validate_analysis(result: dict, post_id: str) -> bool:
    """Check all required fields are present with sane types."""
    required = {
        "has_thesis":    bool,
        "is_ironic":     bool,
        "quality_score": (int, float),
        "sentiment":     str,
        "confidence":    (int, float),
        "tickers":       list,
        "reasoning":     str,
    }
    for field, expected_type in required.items():
        if field not in result:
            logger.warning("Analysis for %s missing field '%s'", post_id, field)
            return False
        if not isinstance(result[field], expected_type):
            logger.warning(
                "Analysis for %s: field '%s' has wrong type %s (expected %s)",
                post_id, field, type(result[field]).__name__, expected_type,
            )
            return False

    if result["sentiment"] not in ("bullish", "bearish", "neutral"):
        logger.warning("Analysis for %s: invalid sentiment '%s'", post_id, result["sentiment"])
        return False

    return True


# ── Ticker normalization & validation ────────────────────────────────────────

_TICKER_MAP: dict[str, str] = {
    "BROADCOM":   "AVGO",
    "INTEL":      "INTC",
    "GOOGLE":     "GOOG",
    "ALPHABET":   "GOOG",
    "FACEBOOK":   "META",
    "AMAZON":     "AMZN",
    "NVIDIA":     "NVDA",
    "MICROSOFT":  "MSFT",
    "APPLE":      "AAPL",
    "TESLA":      "TSLA",
    "ROCKET LAB": "RKLB",
    "ROCKETLAB":  "RKLB",
}

# Module-level cache so each symbol is only validated once per process
_ticker_validity: dict[str, bool] = {}

# Index / macro symbols that yfinance can't price — skip silently
_TICKER_BLACKLIST = {"SPX", "VIX", "DXY", "NDX", "RUT", "DJIA", "TNX", "TYX"}


def _normalize_tickers(tickers: list[str]) -> list[str]:
    """Map company-name variants to their canonical exchange symbol."""
    result = []
    for t in tickers:
        normalized = _TICKER_MAP.get(t.upper(), t.upper())
        result.append(normalized)
    return result


def _is_valid_ticker(symbol: str) -> bool:
    """Return True if yfinance can resolve the symbol to a real security."""
    if symbol in _TICKER_BLACKLIST:
        return False
    if symbol in _ticker_validity:
        return _ticker_validity[symbol]
    try:
        price = yf.Ticker(symbol).fast_info.last_price
        valid = price is not None and not math.isnan(price)
    except Exception:
        valid = False
    _ticker_validity[symbol] = valid
    if not valid:
        logger.debug("Ticker %s failed yfinance validation — discarding", symbol)
    return valid


def _validate_tickers(tickers: list[str]) -> list[str]:
    """Remove any symbols that yfinance cannot resolve."""
    return [t for t in tickers if _is_valid_ticker(t)]


# ── Public API ────────────────────────────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def analyze_post(post: dict) -> dict | None:
    """
    Analyse a single WSB post with Claude.

    Returns an analysis dict (with 'post_id' injected) or None if:
      - the API call failed
      - the response couldn't be parsed
      - quality_score < MIN_QUALITY_SCORE
    """
    post_id    = post.get("id", "unknown")
    title      = post.get("title", "")
    flair      = post.get("flair", "")
    upvotes    = post.get("upvotes", 0)
    body       = post.get("body", "")
    is_comment = title == "comment in daily discussion"

    post_type = "Discussion comment" if is_comment else "Regular post"
    quality_threshold = MIN_COMMENT_QUALITY_SCORE if is_comment else MIN_QUALITY_SCORE

    user_content = (
        f"Post type: {post_type}\n"
        f"Title: {title}\n"
        f"Flair: {flair}\n"
        f"Upvotes: {upvotes}\n\n"
        f"Body:\n{body[:5000]}"   # cap at 5 000 chars to stay within token budget
    )

    logger.info("Analysing %s %s ('%s'…)", post_type.lower(), post_id, title[:60])

    try:
        response = _get_client().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # cache the static prompt
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic API error for post %s: status=%s %s", post_id, exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        logger.error("Anthropic connection error for post %s: %s", post_id, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error calling Anthropic for post %s: %s", post_id, exc)
        return None

    raw_text = response.content[0].text if response.content else ""

    try:
        result = _parse_json(raw_text)
    except ValueError as exc:
        logger.error("JSON parse failed for post %s: %s", post_id, exc)
        return None

    if not _validate_analysis(result, post_id):
        return None

    # Normalise types
    result["quality_score"] = float(result["quality_score"])
    result["confidence"]    = float(result["confidence"])
    result["tickers"]       = [t.upper().strip() for t in result["tickers"] if t.strip()]

    # Map company names to canonical symbols, then drop unresolvable ones
    result["tickers"] = _normalize_tickers(result["tickers"])
    result["tickers"] = _validate_tickers(result["tickers"])

    # Quality gate
    if result["quality_score"] < quality_threshold:
        logger.info(
            "Post %s discarded: quality_score=%.1f < %d",
            post_id, result["quality_score"], quality_threshold,
        )
        return None

    # Irony flip
    if result["is_ironic"]:
        orig = result["sentiment"]
        result["sentiment"] = _flip_sentiment(orig)
        logger.info(
            "Post %s is ironic — flipped sentiment %s → %s",
            post_id, orig, result["sentiment"],
        )

    result["post_id"] = post_id

    logger.info(
        "Post %s → sentiment=%s  quality=%.1f  confidence=%.2f  tickers=%s",
        post_id, result["sentiment"], result["quality_score"],
        result["confidence"], result["tickers"],
    )
    return result


def analyze_posts(posts: list[dict]) -> list[dict]:
    """
    Analyse a list of posts; skip any that already have an analysis in the DB.
    Returns list of successful analysis dicts.
    """
    from database import analysis_exists  # local import avoids circular deps at module load

    results: list[dict] = []
    total = len(posts)

    for i, post in enumerate(posts, 1):
        post_id = post.get("id", "?")
        logger.info("Analysing post %d/%d  id=%s", i, total, post_id)

        if analysis_exists(post_id):
            logger.info("Post %s already analysed — skipping", post_id)
            continue

        analysis = analyze_post(post)
        if analysis:
            results.append(analysis)

    logger.info(
        "analyze_posts complete: %d/%d posts yielded usable analyses",
        len(results), total,
    )
    return results
