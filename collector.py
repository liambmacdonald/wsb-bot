"""
Scrapes r/wallstreetbets via the public Reddit JSON endpoint.
No API key required; only a descriptive User-Agent is needed.

Applies all quality filters before returning posts:
  - upvotes   >= MIN_UPVOTES
  - upvote_ratio >= MIN_UPVOTE_RATIO
  - num_comments >= MIN_COMMENTS
  - flair in VALID_FLAIRS
  - body length >= MIN_BODY_LENGTH

Also scrapes top-level comments from the daily discussion post and filters:
  - upvotes >= 5
  - body length >= 50 characters
  - contains a stock ticker ($AAPL style or known ticker symbol)
"""

import logging
import re
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.reddit.com/r/{sub}/{sort}.json"
_HEADERS = {"User-Agent": config.REDDIT_USER_AGENT}

_DAILY_DISCUSSION_KEYWORDS = (
    "Daily Discussion",
    "What Are Your Moves",
    "Weekend Discussion",
    "Weekend Thread",
    "Monday Thread",
    "Pre-Market",
)

_TICKER_RE = re.compile(r'\$[A-Z]{1,5}\b')

_KNOWN_TICKERS = {
    "SPY", "QQQ", "TSLA", "AAPL", "AMZN", "NVDA", "META", "GOOG", "GOOGL",
    "MSFT", "AMD", "PLTR", "GME", "AMC", "BB", "MARA", "RIOT", "COIN",
    "HOOD", "SOFI", "RIVN", "LCID", "F", "GM", "UBER", "LYFT", "NFLX",
    "DIS", "WMT", "TGT", "COST", "HD", "LOW", "BA", "LMT", "RTX", "XOM",
    "CVX", "OXY", "MRO", "SLB", "GS", "JPM", "BAC", "WFC", "C", "MS",
    "V", "MA", "PYPL", "SQ", "SNAP", "PINS", "RBLX", "DKNG", "PENN",
    "MGM", "CZR", "NET", "CRWD", "ZS", "OKTA", "SNOW", "DDOG", "MDB",
    "SHOP", "SE", "BABA", "JD", "NIO", "XPEV", "LI", "IWM", "TQQQ",
    "SQQQ", "UVXY", "ARKK", "GLD", "SLV", "VIX", "SPX", "SPXL", "TLT",
    "HYG", "XLF", "XLE", "XLK", "INTC", "MU", "QCOM", "AVGO", "TSM",
    "SMCI", "ARM", "MSTR", "IBIT", "FBTC",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_listing(sort: str = "hot", after: str | None = None) -> list[dict]:
    """Fetch one page of the subreddit listing; return raw post dicts."""
    url = _BASE_URL.format(sub=config.SUBREDDIT, sort=sort)
    params: dict[str, Any] = {"limit": config.REDDIT_FETCH_LIMIT, "raw_json": 1}
    if after:
        params["after"] = after

    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Reddit fetch error (%s %s): %s", sort, after, exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error("Failed to parse Reddit JSON: %s", exc)
        return []

    children = data.get("data", {}).get("children", [])
    logger.debug("Fetched %d raw posts from r/%s (%s)", len(children), config.SUBREDDIT, sort)
    return children


def _extract_post(child: dict) -> dict | None:
    """Normalise a Reddit 'child' object into our internal post format."""
    try:
        d = child.get("data", {})

        post_id     = d.get("id", "")
        title       = d.get("title", "").strip()
        selftext    = (d.get("selftext") or "").strip()
        author      = d.get("author", "[deleted]")
        flair       = (d.get("link_flair_text") or "").strip()
        upvotes     = int(d.get("ups", 0))
        upvote_ratio = float(d.get("upvote_ratio", 0.0))
        num_comments = int(d.get("num_comments", 0))
        url         = d.get("url", "")
        created_utc = float(d.get("created_utc", 0))
        permalink   = d.get("permalink", "")

        if not post_id:
            return None

        return {
            "id":           post_id,
            "title":        title,
            "body":         selftext,
            "author":       author,
            "flair":        flair,
            "upvotes":      upvotes,
            "upvote_ratio": upvote_ratio,
            "num_comments": num_comments,
            "url":          f"https://www.reddit.com{permalink}",
            "created_utc":  created_utc,
        }
    except Exception as exc:
        logger.warning("Could not extract post from child: %s", exc)
        return None


def _passes_filters(post: dict) -> bool:
    """Return True only if the post clears every quality gate."""
    pid = post.get("id", "?")

    if post["upvotes"] < config.MIN_UPVOTES:
        logger.debug("Post %s filtered: upvotes %d < %d", pid, post["upvotes"], config.MIN_UPVOTES)
        return False

    if post["upvote_ratio"] < config.MIN_UPVOTE_RATIO:
        logger.debug(
            "Post %s filtered: upvote_ratio %.2f < %.2f",
            pid, post["upvote_ratio"], config.MIN_UPVOTE_RATIO,
        )
        return False

    if post["num_comments"] < config.MIN_COMMENTS:
        logger.debug(
            "Post %s filtered: num_comments %d < %d",
            pid, post["num_comments"], config.MIN_COMMENTS,
        )
        return False

    # Normalise flair for a case-insensitive partial match
    post_flair = post["flair"].lower()
    matched_flair = any(v.lower() in post_flair for v in config.VALID_FLAIRS)
    if not matched_flair:
        logger.debug("Post %s filtered: flair '%s' not in valid set", pid, post["flair"])
        return False

    body_len = len(post.get("body", ""))
    if body_len < config.MIN_BODY_LENGTH:
        logger.debug(
            "Post %s filtered: body length %d < %d", pid, body_len, config.MIN_BODY_LENGTH
        )
        return False

    logger.debug(
        "Post %s passed all filters (upvotes=%d, ratio=%.2f, comments=%d, flair='%s', body=%d)",
        pid, post["upvotes"], post["upvote_ratio"],
        post["num_comments"], post["flair"], body_len,
    )
    return True


# ── Daily discussion helpers ──────────────────────────────────────────────────

def _is_daily_discussion(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in _DAILY_DISCUSSION_KEYWORDS)


def _find_daily_discussion() -> dict | None:
    """Search hot then new for the daily discussion post; return its id and title."""
    for sort in ("hot", "new"):
        children = _fetch_listing(sort=sort)
        for child in children:
            d = child.get("data", {})
            title = d.get("title", "")
            if _is_daily_discussion(title):
                post_id = d.get("id", "")
                logger.info(
                    "Found daily discussion post '%s' (id=%s) in %s", title, post_id, sort
                )
                print(f"\n[INFO] Discussion thread found ({sort}): {title!r}")
                return {"id": post_id, "title": title}
        time.sleep(config.REDDIT_REQUEST_DELAY)
    logger.warning("No daily discussion post found in hot or new")
    return None


def _fetch_discussion_comments(post_id: str) -> list[dict]:
    """Fetch top-level comments from a post via the .json endpoint."""
    url = f"https://www.reddit.com/r/{config.SUBREDDIT}/comments/{post_id}.json"
    params: dict[str, Any] = {"limit": 500, "sort": "top", "raw_json": 1}
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to fetch comments for post %s: %s", post_id, exc)
        return []

    # Response is [post_listing, comments_listing]
    if not isinstance(data, list) or len(data) < 2:
        logger.error("Unexpected comments JSON structure for post %s", post_id)
        return []

    children = data[1].get("data", {}).get("children", [])
    logger.debug("Fetched %d raw comment children for post %s", len(children), post_id)
    return children


def _comment_has_ticker(text: str) -> bool:
    if _TICKER_RE.search(text):
        return True
    words = set(re.findall(r'\b[A-Z]{1,5}\b', text))
    return bool(words & _KNOWN_TICKERS)


def _collect_more_ids(children: list[dict]) -> list[str]:
    """Extract all child IDs from 'more' placeholder objects."""
    ids: list[str] = []
    for child in children:
        if child.get("kind") == "more":
            ids.extend(child.get("data", {}).get("children", []))
    return ids


def _fetch_more_children_batch(post_id: str, ids: list[str]) -> list[dict]:
    """Fetch up to 100 comment IDs via the morechildren API."""
    url = "https://www.reddit.com/api/morechildren.json"
    params = {
        "link_id": f"t3_{post_id}",
        "children": ",".join(ids),
        "api_type": "json",
        "raw_json": 1,
    }
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to fetch more children for post %s: %s", post_id, exc)
        return []

    things = data.get("json", {}).get("data", {}).get("things", [])
    logger.debug("morechildren batch: %d things returned", len(things))
    return things


def _extract_comment(child: dict, post_id: str) -> dict | None:
    try:
        if child.get("kind") != "t1":
            return None
        d = child.get("data", {})
        body = (d.get("body") or "").strip()
        if not body or body in ("[deleted]", "[removed]"):
            return None
        comment_id = d.get("id", "")
        return {
            "id":           comment_id,
            "title":        "comment in daily discussion",
            "body":         body,
            "author":       d.get("author", "[deleted]"),
            "flair":        "Daily Discussion",
            "upvotes":      int(d.get("ups", 0)),
            "upvote_ratio": 1.0,
            "num_comments": 0,
            "url":          (
                f"https://www.reddit.com/r/{config.SUBREDDIT}"
                f"/comments/{post_id}/_/{comment_id}"
            ),
            "created_utc":  float(d.get("created_utc", 0)),
        }
    except Exception as exc:
        logger.warning("Could not extract comment: %s", exc)
        return None


def _passes_comment_filters(comment: dict) -> bool:
    if comment["upvotes"] < 5:
        return False
    if len(comment["body"]) < 50:
        return False
    if not _comment_has_ticker(comment["body"]):
        return False
    return True


# ── Public API ────────────────────────────────────────────────────────────────

def collect_posts(sorts: list[str] | None = None) -> list[dict]:
    """
    Fetch and filter posts from the subreddit.

    Args:
        sorts: Reddit listing sorts to try, e.g. ["hot", "new"].
               Defaults to ["hot", "top"] if None.

    Returns:
        Deduplicated list of posts that passed all quality filters.
    """
    if sorts is None:
        sorts = ["hot", "top"]

    seen_ids: set[str] = set()
    filtered: list[dict] = []
    total_raw = 0

    for sort in sorts:
        logger.info("Fetching r/%s/%s …", config.SUBREDDIT, sort)
        children = _fetch_listing(sort=sort)

        raw_count = len(children)
        total_raw += raw_count
        pass_count = 0

        # Collect flair names before any filtering so we can see what WSB uses
        flairs_seen: list[str] = []
        for child in children:
            d = child.get("data", {})
            flair = (d.get("link_flair_text") or "").strip()
            flairs_seen.append(flair if flair else "(no flair)")

        print(f"\n[DEBUG] Flairs seen in r/wallstreetbets/{sort} ({raw_count} posts):")
        for flair in sorted(set(flairs_seen)):
            count = flairs_seen.count(flair)
            print(f"  {flair!r:35s} x{count}")

        for child in children:
            post = _extract_post(child)
            if post is None:
                continue
            if _is_daily_discussion(post["title"]):
                continue  # handled separately by collect_discussion_comments
            if post["id"] in seen_ids:
                continue
            seen_ids.add(post["id"])

            if _passes_filters(post):
                filtered.append(post)
                pass_count += 1

        logger.info(
            "  %s: %d raw → %d passed filters", sort, raw_count, pass_count
        )

        # Be polite to Reddit's servers
        time.sleep(config.REDDIT_REQUEST_DELAY)

    print(f"\n[DEBUG] Regular post summary:")
    print(f"  Total raw posts fetched : {total_raw}")
    print(f"  Unique posts passed filters: {len(filtered)}")

    logger.info(
        "collect_posts complete: %d unique posts passed all filters", len(filtered)
    )
    return filtered


def collect_discussion_comments() -> list[dict]:
    """
    Find the daily discussion post and return its top-level comments
    that pass the comment quality filters.

    Returns:
        List of comment dicts in the same format as collect_posts items.
    """
    _MAX_PASSED = 2000
    _MAX_BATCHES = 50

    post = _find_daily_discussion()
    if post is None:
        print("\n[DEBUG] Daily discussion: post not found")
        return []

    time.sleep(config.REDDIT_REQUEST_DELAY)
    children = _fetch_discussion_comments(post["id"])

    initial_count = len(children)
    more_ids = _collect_more_ids(children)
    passed: list[dict] = []
    total_raw = 0

    for child in children:
        if child.get("kind") == "t1":
            total_raw += 1
        comment = _extract_comment(child, post["id"])
        if comment is None:
            continue
        if _passes_comment_filters(comment):
            passed.append(comment)
            if len(passed) >= _MAX_PASSED:
                break

    batches_fetched = 0
    while more_ids and batches_fetched < _MAX_BATCHES and len(passed) < _MAX_PASSED:
        time.sleep(1)
        batch_ids, more_ids = more_ids[:100], more_ids[100:]
        batch_children = _fetch_more_children_batch(post["id"], batch_ids)
        batches_fetched += 1

        for child in batch_children:
            if child.get("kind") == "t1":
                total_raw += 1
            comment = _extract_comment(child, post["id"])
            if comment is None:
                continue
            if _passes_comment_filters(comment):
                passed.append(comment)
                if len(passed) >= _MAX_PASSED:
                    break

    print(f"\n[DEBUG] Daily discussion '{post['title']}':")
    print(f"  Comments fetched (initial)  : {initial_count}")
    print(f"  Additional batches fetched  : {batches_fetched} / {_MAX_BATCHES}")
    print(f"  Total raw comments seen     : {total_raw:,}")
    print(f"  Comments passed filters     : {len(passed)} / {_MAX_PASSED}")

    logger.info(
        "collect_discussion_comments: %d initial + %d batches → %d raw → %d passed filters",
        initial_count, batches_fetched, total_raw, len(passed),
    )
    return passed
