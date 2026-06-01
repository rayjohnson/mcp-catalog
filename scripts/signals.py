#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""
signals.py — Fetch popularity/quality signals for catalog servers and write
them back into stats.json.

Usage:
    uv run scripts/signals.py
    uv run scripts/signals.py --dry-run    # print what would change, no writes
    uv run scripts/signals.py --server github  # single server only

Environment variables:
    GITHUB_TOKEN    Optional. Without it, commits-by-path is skipped and the
                    GitHub API rate limit is 60 req/hr instead of 5000.

This script is intentionally non-destructive: it only writes signal fields
(see SIGNAL_KEYS below) and never touches editorial or GitHub-fetched fields
managed by refresh.py.
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVERS_FILE = REPO_ROOT / "servers.json"
STATS_FILE = REPO_ROOT / "stats.json"

# Fields written exclusively by this script.
SIGNAL_KEYS = {
    "githubStarsIsShared",
    "githubCommits90d",
    "npmWeeklyDownloads",
    "pypiMonthlyDownloads",
    "dockerTotalPulls",
    "smitheryUseCount",
    "baseScore",
    "signalsRefreshedAt",
}

# ---------------------------------------------------------------------------
# Per-server signal config (not stored in servers.json — maintained here)
# ---------------------------------------------------------------------------
# Keys:
#   npm        npm package name, or None
#   pypi       PyPI package name, or None
#   docker     (namespace, image_name) tuple for Docker Hub, or None
#   gh_path    path within the repo for commits-by-path (mono-repos), or None

SIGNAL_CONFIG: dict[str, dict] = {
    "github": {
        "npm": "@modelcontextprotocol/server-github",
        "pypi": None,
        "docker": ("mcp", "github"),
        "gh_path": "src/github",
    },
    "filesystem": {
        "npm": "@modelcontextprotocol/server-filesystem",
        "pypi": None,
        "docker": ("mcp", "filesystem"),
        "gh_path": "src/filesystem",
    },
    "git": {
        "npm": None,
        "pypi": "mcp-server-git",
        "docker": ("mcp", "git"),
        "gh_path": "src/git",
    },
    "memory": {
        "npm": "@modelcontextprotocol/server-memory",
        "pypi": None,
        "docker": ("mcp", "memory"),
        "gh_path": "src/memory",
    },
    "fetch": {
        "npm": None,
        "pypi": "mcp-server-fetch",
        "docker": ("mcp", "fetch"),
        "gh_path": "src/fetch",
    },
    "puppeteer": {
        "npm": "@modelcontextprotocol/server-puppeteer",
        "pypi": None,
        "docker": ("mcp", "puppeteer"),
        "gh_path": "src/puppeteer",
    },
    "brave-search": {
        "npm": "@modelcontextprotocol/server-brave-search",
        "pypi": None,
        "docker": ("mcp", "brave-search"),
        "gh_path": "src/brave-search",
    },
    "postgresql": {
        "npm": "@modelcontextprotocol/server-postgres",
        "pypi": None,
        "docker": ("mcp", "postgres"),
        "gh_path": "src/postgres",
    },
    "sqlite": {
        "npm": None,
        "pypi": "mcp-server-sqlite",
        "docker": ("mcp", "sqlite"),
        "gh_path": "src/sqlite",
    },
    "slack": {
        "npm": "@modelcontextprotocol/server-slack",
        "pypi": None,
        "docker": ("mcp", "slack"),
        "gh_path": "src/slack",
    },
    "google-drive": {
        "npm": "@modelcontextprotocol/server-gdrive",
        "pypi": None,
        "docker": ("mcp", "gdrive"),
        "gh_path": "src/gdrive",
    },
    "linear": {
        "npm": "mcp-linear",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "notion": {
        "npm": "@notionhq/notion-mcp-server",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "home-assistant": {
        "npm": None,
        "pypi": "mcp-server-home-assistant",
        "docker": None,
        "gh_path": None,
    },
    "docker": {
        "npm": None,
        "pypi": "mcp-server-docker",
        "docker": None,
        "gh_path": None,
    },
    "aws-kb-retrieval": {
        "npm": "@modelcontextprotocol/server-aws-kb-retrieval-server",
        "pypi": None,
        "docker": ("mcp", "aws-kb-retrieval"),
        "gh_path": "src/aws-kb-retrieval-server",
    },
    "claude-code": {
        "npm": None,
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "exa-search": {
        "npm": "exa-mcp-server",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    # moov-docs has no distribution signals — editorial_rank handles sort position
    "moov-docs": {
        "npm": None,
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "obsidian": {
        "npm": None,
        "pypi": "mcp-obsidian",
        "docker": None,
        "gh_path": None,
    },
    "semble": {
        "npm": None,
        "pypi": "semble",
        "docker": None,
        "gh_path": None,
    },
    "context7": {
        "npm": "@upstash/context7-mcp",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "playwright": {
        "npm": "@playwright/mcp",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "sequential-thinking": {
        "npm": "@modelcontextprotocol/server-sequential-thinking",
        "pypi": None,
        "docker": None,
        "gh_path": "src/sequentialthinking",
    },
    "zapier": {
        "npm": None,
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "figma": {
        "npm": None,
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
    "firecrawl": {
        "npm": "firecrawl-mcp",
        "pypi": None,
        "docker": None,
        "gh_path": None,
    },
}

# Shared-repo server keys: their GitHub star count spans the whole mono-repo,
# not just this server.
SHARED_REPO_KEYS = {
    "github", "filesystem", "git", "memory", "fetch", "puppeteer",
    "brave-search", "postgresql", "sqlite", "slack", "google-drive",
    "aws-kb-retrieval", "sequential-thinking",
}

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_HEADERS: dict[str, str] = (
    {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
)
RATE_SLEEP = 0.4

SMITHERY_CAP = 100_000
OFFICIAL_BONUS = 5.0
INSTALLED_APP_BOOST = 3.0


# ---------------------------------------------------------------------------
# Signal fetchers
# ---------------------------------------------------------------------------

def fetch_npm_weekly(package: str) -> int | None:
    url = f"https://api.npmjs.org/downloads/point/last-week/{requests.utils.quote(package, safe='')}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get("downloads")
    except Exception as exc:
        print(f"  npm error ({package}): {exc}", file=sys.stderr)
    return None


def fetch_pypi_monthly(package: str) -> int | None:
    url = f"https://pypistats.org/api/packages/{package}/recent"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get("data", {}).get("last_month")
    except Exception as exc:
        print(f"  pypi error ({package}): {exc}", file=sys.stderr)
    return None


def fetch_docker_pulls(namespace: str, name: str) -> int | None:
    url = f"https://hub.docker.com/v2/repositories/{namespace}/{name}/"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get("pull_count")
    except Exception as exc:
        print(f"  docker error ({namespace}/{name}): {exc}", file=sys.stderr)
    return None


def fetch_smithery_use_count(display_name: str) -> int | None:
    url = f"https://registry.smithery.ai/servers?q={requests.utils.quote(display_name)}&pageSize=5"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            servers = r.json().get("servers", [])
            if servers:
                return servers[0].get("useCount")
    except Exception as exc:
        print(f"  smithery error ({display_name}): {exc}", file=sys.stderr)
    return None


def fetch_github_commits_90d(owner: str, repo: str, path: str | None) -> int | None:
    if not GITHUB_TOKEN:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params: dict = {"since": since, "per_page": 100}
    if path:
        params["path"] = path
    try:
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            headers=GITHUB_HEADERS,
            params=params,
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json())
    except Exception as exc:
        print(f"  github commits error ({owner}/{repo}): {exc}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _recency_bonus(last_commit_date: str | None) -> float:
    if not last_commit_date:
        return 0.0
    try:
        # Accept ISO 8601 with or without fractional seconds
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(last_commit_date[:26], fmt[:len(fmt)])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return 0.0
        age_days = (datetime.now(timezone.utc) - dt).days
        if age_days < 90:
            return 2.0
        if age_days < 365:
            return 0.0
        return -2.0
    except Exception:
        return 0.0


def compute_base_score(
    npm_weekly: int | None,
    pypi_monthly: int | None,
    docker_pulls: int | None,
    smithery_use: int | None,
    is_official: bool,
    last_commit_date: str | None,
) -> float:
    npm = npm_weekly or 0
    pypi_equiv = (pypi_monthly or 0) // 4
    dist = max(npm, pypi_equiv)
    dist_w = 3.0 if dist > 0 else 0.0
    dist_score = math.log10(dist + 1) * dist_w

    docker_score = math.log10((docker_pulls or 0) + 1) * 1.0

    smithery = min(smithery_use or 0, SMITHERY_CAP)
    smithery_score = math.log10(smithery + 1) * 1.5

    official_bonus = OFFICIAL_BONUS if is_official else 0.0
    recency = _recency_bonus(last_commit_date)

    return dist_score + docker_score + smithery_score + official_bonus + recency


# ---------------------------------------------------------------------------
# Per-server probe
# ---------------------------------------------------------------------------

def probe_server(
    server_key: str,
    display_name: str,
    is_official: bool,
    repository_url: str | None,
    existing_stats: dict,
) -> dict:
    cfg = SIGNAL_CONFIG.get(server_key, {})
    signals: dict = {}

    print(f"  [{server_key}] ", end="", flush=True)

    # npm
    if cfg.get("npm"):
        signals["npmWeeklyDownloads"] = fetch_npm_weekly(cfg["npm"])
        print("npm ", end="", flush=True)
        time.sleep(RATE_SLEEP)
    else:
        signals["npmWeeklyDownloads"] = None

    # pypi
    if cfg.get("pypi"):
        signals["pypiMonthlyDownloads"] = fetch_pypi_monthly(cfg["pypi"])
        print("pypi ", end="", flush=True)
        time.sleep(RATE_SLEEP)
    else:
        signals["pypiMonthlyDownloads"] = None

    # docker
    if cfg.get("docker"):
        ns, name = cfg["docker"]
        signals["dockerTotalPulls"] = fetch_docker_pulls(ns, name)
        print("docker ", end="", flush=True)
        time.sleep(RATE_SLEEP)
    else:
        signals["dockerTotalPulls"] = None

    # smithery
    signals["smitheryUseCount"] = fetch_smithery_use_count(display_name)
    print("smithery ", end="", flush=True)
    time.sleep(RATE_SLEEP)

    # github commits-by-path
    commits_90d = None
    if repository_url and GITHUB_TOKEN:
        import re
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repository_url)
        if m:
            owner, repo = m.group(1), m.group(2)
            commits_90d = fetch_github_commits_90d(owner, repo, cfg.get("gh_path"))
            print("gh-commits ", end="", flush=True)
        time.sleep(RATE_SLEEP)
    signals["githubCommits90d"] = commits_90d

    # shared stars flag
    signals["githubStarsIsShared"] = server_key in SHARED_REPO_KEYS

    # base score (uses last_commit_date from existing stats, which refresh.py owns)
    last_commit = existing_stats.get("lastCommitDate")
    signals["baseScore"] = round(compute_base_score(
        npm_weekly=signals["npmWeeklyDownloads"],
        pypi_monthly=signals["pypiMonthlyDownloads"],
        docker_pulls=signals["dockerTotalPulls"],
        smithery_use=signals["smitheryUseCount"],
        is_official=is_official,
        last_commit_date=last_commit,
    ), 2)

    signals["signalsRefreshedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print()
    return signals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch MCP catalog signals and update stats.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing")
    parser.add_argument("--server", metavar="KEY",
                        help="Process only this serverKey")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("NOTE: GITHUB_TOKEN not set — commits-by-path skipped, rate limit 60/hr\n")

    # Load servers.json
    with open(SERVERS_FILE) as f:
        catalog = json.load(f)
    entries = catalog.get("entries", [])

    # Load existing stats.json
    stats_data: dict = {}
    if STATS_FILE.exists():
        with open(STATS_FILE) as f:
            raw = json.load(f)
        stats_data = raw.get("servers", {})
        metadata = raw.get("metadata", {})
    else:
        metadata = {"schemaVersion": "3", "computedAt": ""}

    # Filter to requested server if --server given
    if args.server:
        entries = [e for e in entries if e["serverKey"] == args.server]
        if not entries:
            print(f"No entry found for serverKey '{args.server}'", file=sys.stderr)
            sys.exit(1)

    print(f"Probing {len(entries)} server(s)...\n")

    updated: dict[str, dict] = {}
    for entry in entries:
        key = entry["serverKey"]
        existing = stats_data.get(key, {})
        signals = probe_server(
            server_key=key,
            display_name=entry["displayName"],
            is_official=entry.get("isOfficial", False),
            repository_url=entry.get("repositoryURL"),
            existing_stats=existing,
        )
        # Merge: preserve all existing fields, overwrite only signal keys
        merged = dict(existing)
        merged.update(signals)
        # Ensure serverKey is present
        merged["serverKey"] = key
        updated[key] = merged

    # Incorporate any servers present in stats_data but not in entries
    for key, existing in stats_data.items():
        if key not in updated:
            updated[key] = existing

    new_stats = {
        "metadata": {
            "schemaVersion": "3",
            "computedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        },
        "servers": updated,
    }

    if args.dry_run:
        print("\n--- DRY RUN: would write stats.json ---")
        for key, data in sorted(updated.items()):
            print(f"  {key}: baseScore={data.get('baseScore')}, "
                  f"npm={data.get('npmWeeklyDownloads')}, "
                  f"smithery={data.get('smitheryUseCount')}")
        return

    with open(STATS_FILE, "w") as f:
        json.dump(new_stats, f, indent=2)
        f.write("\n")

    print(f"\nWrote {STATS_FILE}")


if __name__ == "__main__":
    main()
