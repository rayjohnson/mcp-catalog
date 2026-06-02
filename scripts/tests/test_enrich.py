"""Tests for enrich.py — identifier resolution, duplicate detection, and issue parsing."""

import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import enrich


# ---------------------------------------------------------------------------
# extract_identifier_from_body
# ---------------------------------------------------------------------------

class TestExtractIdentifier:
    def test_github_url_in_form_body(self):
        body = (
            "### Server Identifier\n\n"
            "https://github.com/anthropics/anthropic-tools\n\n"
            "### Why I like it\n\nGreat tool."
        )
        assert enrich.extract_identifier_from_body(body) == "https://github.com/anthropics/anthropic-tools"

    def test_npm_package_name(self):
        body = "### Server Identifier\n\n@modelcontextprotocol/server-filesystem\n\n### Why I like it\n\nUseful."
        assert enrich.extract_identifier_from_body(body) == "@modelcontextprotocol/server-filesystem"

    def test_pypi_package_name(self):
        body = "### Server Identifier\n\nmcp-server-sqlite\n\n### Notes\n\nNone."
        assert enrich.extract_identifier_from_body(body) == "mcp-server-sqlite"

    def test_empty_body_returns_empty(self):
        assert enrich.extract_identifier_from_body("") == ""

    def test_body_with_only_headers_returns_empty(self):
        body = "### Server Identifier\n\n### Why I like it\n\n"
        assert enrich.extract_identifier_from_body(body) == ""


# ---------------------------------------------------------------------------
# detect_identifier_type
# ---------------------------------------------------------------------------

class TestDetectIdentifierType:
    def test_github_url_is_repo(self):
        assert enrich.detect_identifier_type("https://github.com/org/repo") == "repo"

    def test_http_url_is_repo(self):
        assert enrich.detect_identifier_type("http://github.com/org/repo") == "repo"

    def test_scoped_npm_is_npm_or_pypi(self):
        assert enrich.detect_identifier_type("@modelcontextprotocol/server-git") == "npm_or_pypi"

    def test_plain_name_is_npm_or_pypi(self):
        assert enrich.detect_identifier_type("mcp-server-sqlite") == "npm_or_pypi"

    def test_docker_image_with_slash(self):
        assert enrich.detect_identifier_type("mcp/sqlite") == "docker"


# ---------------------------------------------------------------------------
# find_duplicate
# ---------------------------------------------------------------------------

EXISTING = [
    {
        "id": "stripe-mcp",
        "displayName": "Stripe",
        "shortDescription": "Stripe payments API.",
        "repositoryURL": "https://github.com/stripe/agent-toolkit",
        "args": ["-y", "@stripe/mcp"],
    },
    {
        "id": "filesystem",
        "displayName": "Filesystem",
        "shortDescription": "Read/write local files.",
        "repositoryURL": "https://github.com/modelcontextprotocol/servers",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
    },
]


class TestFindDuplicate:
    def test_matches_by_repository_url(self):
        resolved = {"repository_url": "https://github.com/stripe/agent-toolkit", "type": "repo"}
        dup = enrich.find_duplicate("https://github.com/stripe/agent-toolkit", resolved, EXISTING)
        assert dup is not None
        assert dup["id"] == "stripe-mcp"

    def test_matches_repo_url_with_trailing_slash(self):
        resolved = {"repository_url": "https://github.com/stripe/agent-toolkit/", "type": "repo"}
        dup = enrich.find_duplicate("anything", resolved, EXISTING)
        assert dup is not None

    def test_matches_repo_url_with_dot_git(self):
        resolved = {"repository_url": "https://github.com/stripe/agent-toolkit.git", "type": "repo"}
        dup = enrich.find_duplicate("anything", resolved, EXISTING)
        assert dup is not None

    def test_matches_by_package_name_in_args(self):
        resolved = {"repository_url": None, "type": "npm"}
        dup = enrich.find_duplicate("@modelcontextprotocol/server-filesystem", resolved, EXISTING)
        assert dup is not None
        assert dup["id"] == "filesystem"

    def test_no_match_returns_none(self):
        resolved = {"repository_url": "https://github.com/new/thing", "type": "repo"}
        dup = enrich.find_duplicate("https://github.com/new/thing", resolved, EXISTING)
        assert dup is None

    def test_empty_existing_returns_none(self):
        resolved = {"repository_url": "https://github.com/org/repo", "type": "repo"}
        assert enrich.find_duplicate("https://github.com/org/repo", resolved, []) is None


# ---------------------------------------------------------------------------
# resolve_identifier — GitHub URL (mocked HTTP)
# ---------------------------------------------------------------------------

class TestResolveGitHubURL:
    @patch("enrich.requests.get")
    def test_fetches_readme_from_github(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "# My MCP Server\nDoes great things."
        mock_get.return_value = mock_resp

        result = enrich.resolve_identifier("https://github.com/org/my-mcp")
        assert result["type"] == "repo"
        assert "My MCP Server" in result["content"]
        assert result["repository_url"] == "https://github.com/org/my-mcp"

    @patch("enrich.requests.get")
    def test_returns_no_readme_message_on_404(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = enrich.resolve_identifier("https://github.com/org/my-mcp")
        assert "no README found" in result["content"]


# ---------------------------------------------------------------------------
# resolve_identifier — npm package (mocked HTTP)
# ---------------------------------------------------------------------------

class TestResolveNpmPackage:
    @patch("enrich.requests.get")
    def test_resolves_npm_package(self, mock_get):
        npm_payload = {
            "description": "An MCP server for npm things",
            "readme": "# npm-mcp-server\nInstall with npx.",
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"repository": {"url": "git+https://github.com/org/npm-mcp.git"}}},
        }
        # First call: npm succeeds; no PyPI call needed
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = npm_payload
        mock_get.return_value = mock_resp

        result = enrich._fetch_npm("npm-mcp-server")
        assert result["command"] == "npx"
        assert "npm-mcp-server" in result["content"]
        assert result["repository_url"] == "https://github.com/org/npm-mcp"

    @patch("enrich.requests.get")
    def test_npm_not_found_returns_empty_content(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = enrich._fetch_npm("nonexistent-package-xyz")
        assert result["content"] == ""


# ---------------------------------------------------------------------------
# resolve_identifier — uvx/PyPI package (mocked HTTP)
# ---------------------------------------------------------------------------

class TestResolvePypiPackage:
    @patch("enrich.requests.get")
    def test_resolves_pypi_package(self, mock_get):
        pypi_payload = {
            "info": {
                "summary": "A PyPI MCP server",
                "home_page": "https://github.com/org/pypi-mcp",
                "description": "Full description here.",
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = pypi_payload
        mock_get.return_value = mock_resp

        result = enrich._fetch_pypi("mcp-server-sqlite")
        assert result["command"] == "uvx"
        assert result["args"] == ["mcp-server-sqlite"]
        assert "PyPI MCP server" in result["content"]
        assert result["repository_url"] == "https://github.com/org/pypi-mcp"

    @patch("enrich.requests.get")
    def test_pypi_not_found_returns_empty_content(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = enrich._fetch_pypi("nonexistent-xyz-package")
        assert result["content"] == ""


# ---------------------------------------------------------------------------
# resolve_identifier — unresolvable
# ---------------------------------------------------------------------------

class TestUnresolvable:
    @patch("enrich.requests.get")
    def test_unknown_identifier_has_empty_content(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = enrich.resolve_identifier("not-a-real-thing-xyz-abc-123")
        assert result["content"] == "" or "unknown" in result["type"]
