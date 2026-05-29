#!/usr/bin/env python3
"""
sentiment.py — Weekly Reddit sentiment analysis for mcp-catalog.

Uses Reddit's public JSON API (no credentials required) to search
relevant subreddits for mentions of each catalog server, then calls
Claude to produce sentiment summaries and trending scores.

Patches only the sentiment fields in stats.json, preserving GitHub
stats and usage counts written by refresh.py.

Environment variables:
  ANTHROPIC_API_KEY   Required.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import anthropic
import requests

SERVERS_FILE = "servers.json"
STATS_FILE = "stats.json"
CONFIG_FILE = "config.json"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
USER_AGENT = "mcp-catalog-bot/1.0 (github.com/rayjohnson/mcp-catalog)"
LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Reddit public JSON API
# ---------------------------------------------------------------------------

def search_reddit(subreddit: str, query: str) -> list[dict]:
    """Search a subreddit for posts matching query. No auth required."""
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {
        "q": query,
        "sort": "new",
        "limit": 100,
        "t": "month",
        "restrict_sr": 1,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("data", {}).get("children", [])
        return []
    except Exception as e:
        print(f"  Reddit request failed ({subreddit}/{query}): {e}")
        return []


def collect_mentions(server_name: str, subreddits: list[str]) -> list[dict]:
    """Collect posts mentioning server_name across all subreddits."""
    all_posts = []
    for sub in subreddits:
        posts = search_reddit(sub, server_name)
        for post in posts:
            d = post.get("data", {})
            all_posts.append({
                "title": d.get("title", ""),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "subreddit": d.get("subreddit", sub),
                "url": f"https://reddit.com{d.get('permalink', '')}",
            })
        time.sleep(2)  # Rate limit: 2s between subreddit requests
    return all_posts


# ---------------------------------------------------------------------------
# Claude sentiment analysis
# ---------------------------------------------------------------------------

SENTIMENT_PROMPT = """\
Analyze the following Reddit posts mentioning the MCP server "{server_name}".

Posts (title, score, comments):
{posts_summary}

Produce a JSON object with:
{{
  "sentimentSummary": "<1-2 sentences describing community sentiment — factual, no hype>",
  "trendingScore": <integer 0-100 based on mention velocity, engagement, and sentiment>,
  "mentionCount": <total number of posts above>
}}

Rules:
- sentimentSummary: be honest about both praise and criticism
- trendingScore: 0=no buzz, 50=moderate interest, 80+=high activity this week
- If posts are mostly low-score noise with no real discussion, score ≤ 20
- Output ONLY the JSON object
"""

SUBREDDIT_REVIEW_PROMPT = """\
You manage a list of subreddits monitored for MCP (Model Context Protocol) server discussions.

Current subreddits: {current_list}

Today's date: {today}

Review task:
1. Are these subreddits still active and relevant for MCP server discussions?
2. Are there new subreddits worth adding (check for new MCP communities, AI tool communities)?
3. Are any subreddits too noisy or off-topic to be useful?

Produce a JSON object:
{{
  "updated_list": ["SubredditName1", "SubredditName2", ...],
  "changes_made": "<brief description of changes, or 'no changes'>"
}}

Rules:
- Include only subreddit names (no r/ prefix)
- Keep the list concise (3-8 subreddits)
- Only suggest subreddits that genuinely discuss MCP servers or AI coding tools
- Output ONLY the JSON object
"""


def analyze_sentiment(server_name: str, posts: list[dict], client: anthropic.Anthropic) -> dict:
    posts_summary = "\n".join(
        f"- [{p['score']}↑, {p['num_comments']} comments] {p['title']}"
        for p in posts[:30]
    )
    prompt = SENTIMENT_PROMPT.format(server_name=server_name, posts_summary=posts_summary)
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  Sentiment analysis failed for {server_name}: {e}")
        return {}


def review_subreddits(current: list[str], client: anthropic.Anthropic) -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = SUBREDDIT_REVIEW_PROMPT.format(
        current_list=", ".join(current),
        today=today
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        updated = result.get("updated_list", current)
        changes = result.get("changes_made", "no changes")
        print(f"  Subreddit review: {changes}")
        return updated
    except Exception as e:
        print(f"  Subreddit review failed: {e}")
        return current


# ---------------------------------------------------------------------------
# isTrending computation
# ---------------------------------------------------------------------------

def compute_trending_flags(scores: dict[str, int]) -> dict[str, bool]:
    """Mark top quartile of scored servers as trending."""
    if not scores:
        return {}
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) < 4:
        cutoff = 50  # Require at least score 50 if very few entries
    else:
        top_quartile_idx = max(0, len(sorted_scores) // 4 - 1)
        cutoff = sorted_scores[top_quartile_idx]
        # Never mark trending if score < 30 (minimum meaningful activity)
        cutoff = max(cutoff, 30)
    return {key: score >= cutoff for key, score in scores.items()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=anthropic_key)

    # Load config (subreddit list)
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    subreddits = config.get("subreddits", ["ClaudeAI", "ClaudeCode", "MCPservers"])
    last_review = config.get("metadata", {}).get("lastSubredditReviewDate")

    # Check if monthly subreddit review is due
    review_due = False
    if not last_review:
        review_due = True
    else:
        try:
            last_dt = datetime.fromisoformat(last_review)
            days_since = (datetime.now(timezone.utc) - last_dt).days
            review_due = days_since >= 30
        except Exception:
            review_due = True

    if review_due:
        print("Monthly subreddit review due — asking Claude...")
        subreddits = review_subreddits(subreddits, client)
        config["subreddits"] = subreddits
        config.setdefault("metadata", {})["lastSubredditReviewDate"] = datetime.now(timezone.utc).isoformat()
        config["metadata"]["nextSubredditReviewDate"] = None
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    # Load servers
    with open(SERVERS_FILE) as f:
        servers_data = json.load(f)
    entries = servers_data.get("entries", [])

    # Load existing stats
    stats_data = {"metadata": {}, "servers": {}}
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            stats_data = json.load(f)
    existing_metrics = stats_data.get("servers", {})

    now_iso = datetime.now(timezone.utc).isoformat()
    trending_scores: dict[str, int] = {}

    print(f"Searching {len(entries)} servers across subreddits: {subreddits}")

    for entry in entries:
        server_key = entry["serverKey"]
        server_name = entry["displayName"]
        print(f"  Searching for: {server_name}")

        posts = collect_mentions(server_name, subreddits)

        if not posts:
            # Clear sentiment fields; set isTrending=false
            existing = existing_metrics.get(server_key, {})
            existing.pop("sentimentSummary", None)
            existing.pop("trendingScore", None)
            existing.pop("mentionCount", None)
            existing.pop("periodDays", None)
            existing.pop("sentimentComputedAt", None)
            existing["isTrending"] = False
            existing["serverKey"] = server_key
            existing_metrics[server_key] = existing
            continue

        print(f"    Found {len(posts)} posts")
        sentiment = analyze_sentiment(server_name, posts, client)
        if not sentiment:
            continue

        score = sentiment.get("trendingScore", 0)
        trending_scores[server_key] = score

        # Patch sentiment fields into existing metrics (preserves GitHub stats/usage)
        existing = existing_metrics.get(server_key, {"serverKey": server_key})
        existing["sentimentSummary"] = sentiment.get("sentimentSummary")
        existing["trendingScore"] = score
        existing["mentionCount"] = sentiment.get("mentionCount", len(posts))
        existing["periodDays"] = LOOKBACK_DAYS
        existing["sentimentComputedAt"] = now_iso
        existing["serverKey"] = server_key
        existing_metrics[server_key] = existing

    # Compute isTrending flags based on score distribution
    trending_flags = compute_trending_flags(trending_scores)
    for server_key, is_trending in trending_flags.items():
        if server_key in existing_metrics:
            existing_metrics[server_key]["isTrending"] = is_trending

    # Write updated stats.json
    stats_data["metadata"]["computedAt"] = now_iso
    stats_data["servers"] = existing_metrics

    with open(STATS_FILE, "w") as f:
        json.dump(stats_data, f, indent=2)

    trending_count = sum(1 for v in trending_flags.values() if v)
    print(f"Sentiment complete. {len(trending_scores)} servers with mentions, {trending_count} trending.")


if __name__ == "__main__":
    main()
