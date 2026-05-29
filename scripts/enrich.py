#!/usr/bin/env python3
"""
enrich.py — AI enrichment pipeline for mcp-catalog server submissions.

Reads a GitHub Issue body, resolves the server identifier, fetches available
documentation, calls Claude to produce a catalog entry, and writes it to a
temp file for the create-pull-request action to stage.

Environment variables (set by GitHub Actions):
  ANTHROPIC_API_KEY   Required.
  GITHUB_TOKEN        Required (for GitHub API calls and issue comments).
  ISSUE_NUMBER        GitHub issue number.
  ISSUE_BODY          Raw issue body text.
  ISSUE_TITLE         Issue title.
  REPO                GitHub repo in "owner/name" format.
"""

import json
import os
import re
import sys
import time

import anthropic
import requests
from github import Github

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVERS_FILE = "servers.json"
ENRICHED_ENTRY_PATH = "scripts/.enriched_entry.json"

KNOWN_COMMANDS = {"npx", "uvx", "node", "python", "python3", "docker", "brew", "pip"}

CLAUDE_MODEL = "claude-opus-4-7"

# ---------------------------------------------------------------------------
# Identifier resolution
# ---------------------------------------------------------------------------

def detect_identifier_type(identifier: str) -> str:
    """Return 'repo', 'npm', 'pypi', 'docker', or 'unknown'."""
    if re.match(r"https?://", identifier):
        return "repo"
    if identifier.startswith("@") or re.match(r"^[a-zA-Z0-9_-]+$", identifier):
        # Could be npm or pypi — we'll try both
        return "npm_or_pypi"
    if "/" in identifier and not identifier.startswith("http"):
        return "docker"
    return "unknown"


def resolve_identifier(identifier: str) -> dict:
    """
    Try to resolve an identifier to useful documentation.
    Returns a dict with keys: type, content, command, args, repository_url.
    """
    id_type = detect_identifier_type(identifier)
    result = {"type": id_type, "content": "", "command": "", "args": [], "repository_url": None}

    if id_type == "repo":
        data = _fetch_repo(identifier)
        result.update(data)

    elif id_type == "npm_or_pypi":
        # Try npm first, then PyPI
        npm_data = _fetch_npm(identifier)
        if npm_data.get("content"):
            result.update(npm_data)
            result["type"] = "npm"
        else:
            pypi_data = _fetch_pypi(identifier)
            if pypi_data.get("content"):
                result.update(pypi_data)
                result["type"] = "pypi"
            else:
                result["type"] = "unknown"

    elif id_type == "docker":
        data = _fetch_docker(identifier)
        result.update(data)

    return result


def _fetch_repo(url: str) -> dict:
    """Fetch README from a GitHub/GitLab/Bitbucket repo URL."""
    result = {"content": "", "command": "", "args": [], "repository_url": url}

    # GitHub
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if m:
        repo_path = m.group(1).rstrip("/")
        token = os.environ.get("GITHUB_TOKEN", "")
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.raw"}
        for branch in ["main", "master"]:
            for readme in ["README.md", "readme.md", "README.rst", "README"]:
                r = requests.get(
                    f"https://raw.githubusercontent.com/{repo_path}/{branch}/{readme}",
                    headers=headers, timeout=15
                )
                if r.status_code == 200:
                    result["content"] = r.text[:8000]
                    result["repository_url"] = url
                    return result
        result["content"] = f"(no README found at {url})"
        return result

    # Generic fallback — return URL only
    result["content"] = f"(non-GitHub repo: {url})"
    return result


def _fetch_npm(package: str) -> dict:
    """Fetch npm registry metadata for a package."""
    result = {"content": "", "command": "npx", "args": ["-y", package], "repository_url": None}
    pkg_encoded = package.replace("/", "%2F")
    r = requests.get(f"https://registry.npmjs.org/{pkg_encoded}", timeout=15)
    if r.status_code != 200:
        return result

    data = r.json()
    latest = data.get("dist-tags", {}).get("latest", "")
    version_data = data.get("versions", {}).get(latest, {})
    description = data.get("description", "")
    readme = data.get("readme", "")[:6000]
    repo = version_data.get("repository", {})
    repo_url = repo.get("url", "") if isinstance(repo, dict) else ""
    if repo_url.startswith("git+"):
        repo_url = repo_url[4:]
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4]

    result["content"] = f"Package: {package}\nDescription: {description}\n\n{readme}"
    result["repository_url"] = repo_url or None
    return result


def _fetch_pypi(package: str) -> dict:
    """Fetch PyPI metadata for a package."""
    result = {"content": "", "command": "uvx", "args": [package], "repository_url": None}
    r = requests.get(f"https://pypi.org/pypi/{package}/json", timeout=15)
    if r.status_code != 200:
        return result

    data = r.json().get("info", {})
    description = data.get("summary", "")
    home_page = data.get("home_page", "") or data.get("project_url", "")
    long_desc = data.get("description", "")[:6000]

    result["content"] = f"Package: {package}\nDescription: {description}\n\n{long_desc}"
    result["repository_url"] = home_page or None
    return result


def _fetch_docker(image: str) -> dict:
    """Fetch Docker Hub description for an image."""
    result = {"content": "", "command": "docker", "args": ["run", image], "repository_url": None}
    # Parse org/image from full reference
    parts = image.split("/")
    if len(parts) >= 2:
        namespace = parts[-2]
        repo = parts[-1].split(":")[0]
        r = requests.get(
            f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/",
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            result["content"] = (
                f"Image: {image}\n"
                f"Description: {data.get('description','')}\n\n"
                f"{data.get('full_description','')[:5000]}"
            )
    return result

# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def load_existing_servers() -> list:
    if not os.path.exists(SERVERS_FILE):
        return []
    with open(SERVERS_FILE) as f:
        data = json.load(f)
    return data.get("entries", [])


def find_duplicate(identifier: str, resolved: dict, existing: list) -> dict | None:
    """Return an existing entry if the identifier is already cataloged."""
    repo_url = resolved.get("repository_url") or ""
    # Normalize URLs for comparison
    def norm(u):
        return u.rstrip("/").lower().removesuffix(".git")

    for entry in existing:
        if repo_url and norm(repo_url) == norm(entry.get("repositoryURL", "")):
            return entry
        # Check npm/pypi package name in args
        if identifier in entry.get("args", []):
            return entry
    return None


def find_same_service(identifier: str, resolved: dict, existing: list) -> dict | None:
    """
    Return an existing entry for the same service but a different implementation.
    Heuristic: same repo org/project name after stripping mcp- prefix.
    """
    # Simplified: only flag if curator note explicitly mentions same service
    return None  # Let Claude handle this in the prompt

# ---------------------------------------------------------------------------
# AI enrichment
# ---------------------------------------------------------------------------

ENRICHMENT_PROMPT = """\
You are a curator for mcp-catalog, a curated list of MCP (Model Context Protocol) servers.

Given the following information about an MCP server, produce a complete catalog entry as JSON.

## Server information

Identifier submitted: {identifier}
Resolved type: {id_type}
Repository URL: {repo_url}

## Documentation fetched

{content}

## Existing catalog entries for reference (to check for same-service alternatives)

{existing_summary}

## Instructions

Produce a JSON object for this MCP server with EXACTLY these fields:

{{
  "id": "<kebab-case unique id, e.g. stripe-mcp>",
  "displayName": "<human-readable name>",
  "category": "<one of: Code & Development, Productivity, Data & Analytics, Communication, Infrastructure, AI & LLMs, Web & Browser>",
  "shortDescription": "<one sentence, max 120 chars, no marketing language>",
  "curatorNote": "<1-3 sentences: what this server does well and any gotchas>",
  "transportType": "<stdio or http>",
  "command": "<executable command, e.g. npx, uvx, docker>",
  "args": ["<arg1>", "<arg2>"],
  "url": "<remote URL if http transport, else empty string>",
  "envVars": [
    {{
      "name": "<ENV_VAR_NAME>",
      "description": "<what it is and how to get it>",
      "isRequired": true,
      "isSensitive": true
    }}
  ],
  "requiredArgs": [
    {{
      "name": "<arg-name>",
      "description": "<what it represents>",
      "placeholder": "<example value>",
      "isRequired": true
    }}
  ],
  "documentationURL": "<URL or null>",
  "repositoryURL": "<GitHub/GitLab URL or null>",
  "isVerified": false,
  "isFirstParty": <true if the maintainer org IS the company that owns the service, else false>,
  "alternativeTo": null,
  "serverKey": "<same as id>"
}}

Rules:
- envVars: list ALL env vars the server requires or accepts, in order (required first)
- requiredArgs: only if the server needs positional CLI arguments (e.g. a path)
- isFirstParty: true only if the GitHub org/maintainer IS the company (e.g. @stripe maintaining a Stripe server)
- If you cannot determine a field from the available information, use null for optional fields
- Output ONLY the JSON object, no markdown, no explanation

Also: if you see an existing catalog entry for the SAME service (different implementation),
add a "comparison_note" field (not part of the schema) explaining the difference for the curator.
"""


def call_claude(identifier: str, resolved: dict, existing: list) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    existing_summary = "\n".join(
        f"- {e['id']}: {e['shortDescription']}" for e in existing[:20]
    )

    prompt = ENRICHMENT_PROMPT.format(
        identifier=identifier,
        id_type=resolved["type"],
        repo_url=resolved.get("repository_url") or "unknown",
        content=resolved["content"][:7000] or "(no documentation available)",
        existing_summary=existing_summary or "(none)",
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def extract_identifier_from_body(body: str) -> str:
    """Pull the server_identifier field out of the GitHub issue form body."""
    # GitHub issue forms produce markdown with field headers
    for line in body.splitlines():
        stripped = line.strip()
        # Skip the field label line itself
        if "server identifier" in stripped.lower():
            continue
        # First non-empty, non-header line after the field label
        if stripped and not stripped.startswith("#") and not stripped.startswith("_"):
            return stripped
    return ""


def post_comment(gh: Github, repo_name: str, issue_number: int, body: str):
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(int(issue_number))
    issue.create_comment(body)


def main():
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")
    issue_body = os.environ.get("ISSUE_BODY", "")
    repo_name = os.environ.get("REPO", "")

    gh = Github(gh_token)

    # 1. Extract identifier
    identifier = extract_identifier_from_body(issue_body).strip()
    if not identifier:
        post_comment(gh, repo_name, issue_number,
            "Could not find a server identifier in the issue body. "
            "Please reopen using the submission template and provide a repo URL, "
            "npm package name, uvx package name, or Docker image name.")
        sys.exit(0)

    print(f"Processing identifier: {identifier}")

    # 2. Resolve identifier
    try:
        resolved = resolve_identifier(identifier)
    except Exception as e:
        post_comment(gh, repo_name, issue_number,
            f"Failed to resolve `{identifier}`: {e}\n\n"
            "If this is a private or intranet-only server, it cannot be added via "
            "the automated pipeline. Contact a curator directly.")
        sys.exit(0)

    if not resolved.get("content") or "no README found" in resolved.get("content", ""):
        post_comment(gh, repo_name, issue_number,
            f"Could not fetch any documentation for `{identifier}`. "
            f"Tried: {resolved['type']} resolution.\n\n"
            "Please check the identifier and resubmit, or contact a curator.")
        sys.exit(0)

    # 3. Load existing servers
    existing = load_existing_servers()

    # 4. Duplicate check
    duplicate = find_duplicate(identifier, resolved, existing)
    if duplicate:
        post_comment(gh, repo_name, issue_number,
            f"This server (`{identifier}`) is already in the catalog as "
            f"**{duplicate['displayName']}** (`{duplicate['id']}`). "
            f"No PR will be created.\n\n"
            f"If you think the existing entry is outdated or incorrect, "
            f"please open a regular issue describing what needs to change.")
        sys.exit(0)

    # 5. Call Claude
    print("Calling Claude for enrichment...")
    try:
        entry = call_claude(identifier, resolved, existing)
    except Exception as e:
        post_comment(gh, repo_name, issue_number,
            f"AI enrichment failed: {e}\n\n"
            "A curator will need to add this entry manually. "
            "Please keep this issue open.")
        sys.exit(1)

    # 6. Extract comparison note before writing entry
    comparison_note = entry.pop("comparison_note", None)

    # 7. Write enriched entry to temp file for the PR action
    with open(ENRICHED_ENTRY_PATH, "w") as f:
        json.dump(entry, f, indent=2)

    # 8. Set GitHub Actions outputs
    server_name = entry.get("displayName", identifier)
    with open(os.environ.get("GITHUB_ENV", "/dev/null"), "a") as env_file:
        env_file.write(f"ENRICHED_ENTRY_PATH={ENRICHED_ENTRY_PATH}\n")
        env_file.write(f"ENRICHED_SERVER_NAME={server_name}\n")

    # 9. Post comparison note if same service exists
    if comparison_note:
        post_comment(gh, repo_name, issue_number,
            f"**Note for curator**: A similar service may already exist in the catalog.\n\n"
            f"{comparison_note}")

    print(f"Enrichment complete: {server_name}")


if __name__ == "__main__":
    main()
