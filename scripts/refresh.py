#!/usr/bin/env python3
"""
refresh.py — Weekly stats refresh for mcp-catalog.

For each entry in servers.json:
  1. Fetches GitHub stats (stars, forks, last commit, open issues, archived).
  2. Folds usage counts from usage.json into per-entry metrics.
  3. Writes updated stats.json (preserving existing sentiment fields).
  4. Opens drift PRs for archived repos or significant env-var changes.

Environment variables:
  GITHUB_TOKEN        Required.
  ANTHROPIC_API_KEY   Required (for env-var drift detection via Claude).
  REPO                GitHub repo in "owner/name" format.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import anthropic
import requests
from github import Github

SERVERS_FILE = "servers.json"
STATS_FILE = "stats.json"
USAGE_FILE = "usage.json"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# GitHub stats
# ---------------------------------------------------------------------------

def fetch_github_stats(repo_url: str, token: str) -> dict | None:
    """Return GitHub stats dict for a repo URL, or None if unreachable."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    if not m:
        return None
    repo_path = m.group(1).rstrip("/")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    r = requests.get(f"https://api.github.com/repos/{repo_path}", headers=headers, timeout=15)
    if r.status_code == 404:
        return {"isArchived": True, "not_found": True}
    if r.status_code != 200:
        return None
    data = r.json()

    # Fetch latest commit date
    last_commit = None
    commits_r = requests.get(
        f"https://api.github.com/repos/{repo_path}/commits",
        params={"per_page": 1},
        headers=headers,
        timeout=15
    )
    if commits_r.status_code == 200 and commits_r.json():
        last_commit = commits_r.json()[0]["commit"]["committer"]["date"]

    return {
        "starCount": data.get("stargazers_count", 0),
        "forkCount": data.get("forks_count", 0),
        "openIssueCount": data.get("open_issues_count", 0),
        "isArchived": data.get("archived", False),
        "lastCommitDate": last_commit,
    }


def fetch_readme(repo_url: str, token: str) -> str:
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    if not m:
        return ""
    repo_path = m.group(1).rstrip("/")
    headers = {"Authorization": f"token {token}"}
    for branch in ["main", "master"]:
        r = requests.get(
            f"https://raw.githubusercontent.com/{repo_path}/{branch}/README.md",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            return r.text[:6000]
    return ""


# ---------------------------------------------------------------------------
# Env-var drift detection
# ---------------------------------------------------------------------------

DRIFT_PROMPT = """\
Compare the environment variables documented in this README with the catalog's
current env var list. Report ONLY if there are new required env vars in the
README that are NOT in the catalog list.

README (excerpt):
{readme}

Catalog env vars:
{catalog_vars}

Reply with JSON: {{"has_drift": bool, "new_vars": ["VAR_NAME", ...]}}
Only flag env vars that are clearly required and not in the catalog list.
If no drift, return {{"has_drift": false, "new_vars": []}}.
Output ONLY the JSON object.
"""


def detect_env_drift(readme: str, catalog_vars: list, client: anthropic.Anthropic) -> dict:
    if not readme:
        return {"has_drift": False, "new_vars": []}
    catalog_var_names = [v["name"] for v in catalog_vars]
    prompt = DRIFT_PROMPT.format(
        readme=readme[:4000],
        catalog_vars=", ".join(catalog_var_names) or "(none)"
    )
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
    except Exception:
        return {"has_drift": False, "new_vars": []}


# ---------------------------------------------------------------------------
# Usage fold-in
# ---------------------------------------------------------------------------

def load_usage() -> dict:
    if not os.path.exists(USAGE_FILE):
        return {}
    with open(USAGE_FILE) as f:
        data = json.load(f)
    return data.get("servers", {})


# ---------------------------------------------------------------------------
# PR helpers
# ---------------------------------------------------------------------------

def open_drift_pr(gh: Github, repo_name: str, entry: dict, drift_info: dict):
    repo = gh.get_repo(repo_name)
    server_id = entry["id"]
    branch_name = f"drift/{server_id}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

    # Check if branch already exists
    try:
        repo.get_branch(branch_name)
        return  # PR already open for this cycle
    except Exception:
        pass

    new_vars = drift_info.get("new_vars", [])
    archived = drift_info.get("archived", False)

    title = f"[Drift] {entry['displayName']}: {'archived' if archived else 'env var changes'}"
    body_parts = [f"## Drift detected: {entry['displayName']}\n"]
    if archived:
        body_parts.append("⚠️ **This repository has been archived or deleted on GitHub.**\n")
        body_parts.append("Consider removing or replacing this catalog entry.")
    if new_vars:
        body_parts.append("**New required env vars found in README not in catalog:**")
        for v in new_vars:
            body_parts.append(f"- `{v}`")
    body_parts.append(f"\nEntry: `{server_id}` | Repository: {entry.get('repositoryURL', 'N/A')}")

    # Create a minimal diff — just open a PR with a note in the body
    # The curator updates the entry manually
    try:
        default_branch = repo.default_branch
        ref = repo.get_git_ref(f"heads/{default_branch}")
        repo.create_git_ref(f"refs/heads/{branch_name}", ref.object.sha)
        repo.create_pull(
            title=title,
            body="\n".join(body_parts),
            head=branch_name,
            base=default_branch,
            draft=True
        )
        print(f"  Opened drift PR for {server_id}")
    except Exception as e:
        print(f"  Failed to open drift PR for {server_id}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    repo_name = os.environ.get("REPO", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    gh = Github(gh_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)

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

    # Load usage counts
    usage = load_usage()

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_metrics = {}

    for entry in entries:
        server_key = entry["serverKey"]
        repo_url = entry.get("repositoryURL") or ""
        print(f"Processing {server_key}...")

        # Preserve existing sentiment fields
        existing = existing_metrics.get(server_key, {})
        metrics = {
            "serverKey": server_key,
            "repositoryURL": repo_url or None,
            # Preserve sentiment fields from last sentiment run
            "isTrending": existing.get("isTrending", False),
            "trendingScore": existing.get("trendingScore"),
            "sentimentSummary": existing.get("sentimentSummary"),
            "mentionCount": existing.get("mentionCount"),
            "periodDays": existing.get("periodDays"),
            "sentimentComputedAt": existing.get("sentimentComputedAt"),
        }

        # Fetch GitHub stats
        if repo_url and "github.com" in repo_url:
            stats = fetch_github_stats(repo_url, gh_token)
            if stats:
                metrics["starCount"] = stats.get("starCount")
                metrics["forkCount"] = stats.get("forkCount")
                metrics["openIssueCount"] = stats.get("openIssueCount")
                metrics["isArchived"] = stats.get("isArchived", False)
                metrics["lastCommitDate"] = stats.get("lastCommitDate")
                metrics["githubFetchedAt"] = now_iso

                # Check for archived
                if stats.get("isArchived") or stats.get("not_found"):
                    print(f"  {server_key} is archived — opening drift PR")
                    open_drift_pr(gh, repo_name, entry, {"archived": True})
                else:
                    # Check env-var drift (only for non-archived repos)
                    readme = fetch_readme(repo_url, gh_token)
                    if readme:
                        drift = detect_env_drift(readme, entry.get("envVars", []), claude)
                        if drift.get("has_drift"):
                            print(f"  {server_key} has env-var drift: {drift['new_vars']}")
                            open_drift_pr(gh, repo_name, entry, drift)
        else:
            metrics["isArchived"] = False
            metrics["isTrending"] = existing.get("isTrending", False)

        # Fold in usage counts
        server_usage = usage.get(server_key, {})
        if server_usage:
            metrics["userCount"] = server_usage.get("userCount")
            metrics["enabledCount"] = server_usage.get("enabledCount")
            metrics["weeklyActiveCount"] = server_usage.get("weeklyActiveCount")
            metrics["usageAggregatedAt"] = now_iso

        # Remove None values for cleaner JSON
        metrics = {k: v for k, v in metrics.items() if v is not None}
        # But keep isArchived and isTrending even if False
        metrics.setdefault("isArchived", False)
        metrics.setdefault("isTrending", False)
        metrics["serverKey"] = server_key

        updated_metrics[server_key] = metrics
        time.sleep(0.5)  # Be gentle with GitHub API rate limits

    # Write updated stats.json
    stats_data["metadata"] = {
        "schemaVersion": "2",
        "computedAt": now_iso,
    }
    stats_data["servers"] = updated_metrics

    with open(STATS_FILE, "w") as f:
        json.dump(stats_data, f, indent=2)

    print(f"Refresh complete. Updated {len(updated_metrics)} entries.")


if __name__ == "__main__":
    main()
