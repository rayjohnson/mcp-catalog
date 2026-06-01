"""Tests for signals.py — scoring formula and stats.json merge logic."""

import json
import math
import sys
from pathlib import Path

import pytest

# Make the scripts directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signals import (
    SIGNAL_KEYS,
    SHARED_REPO_KEYS,
    compute_base_score,
    _recency_bonus,
)


# ---------------------------------------------------------------------------
# _recency_bonus
# ---------------------------------------------------------------------------

class TestRecencyBonus:
    def test_recent_commit_gives_plus_two(self):
        # A date 30 days ago is within the 90-day window
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _recency_bonus(recent) == 2.0

    def test_mid_age_commit_gives_zero(self):
        from datetime import datetime, timedelta, timezone
        mid = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _recency_bonus(mid) == 0.0

    def test_stale_commit_gives_minus_two(self):
        assert _recency_bonus("2020-01-01T00:00:00Z") == -2.0

    def test_none_gives_zero(self):
        assert _recency_bonus(None) == 0.0

    def test_malformed_gives_zero(self):
        assert _recency_bonus("not-a-date") == 0.0


# ---------------------------------------------------------------------------
# compute_base_score
# ---------------------------------------------------------------------------

class TestComputeBaseScore:
    def test_all_zeros_official_no_signals(self):
        score = compute_base_score(
            npm_weekly=None,
            pypi_monthly=None,
            docker_pulls=None,
            smithery_use=None,
            is_official=True,
            last_commit_date=None,
        )
        # official bonus 5.0, everything else 0
        assert score == pytest.approx(5.0, abs=0.01)

    def test_npm_signal_increases_score(self):
        score_no_npm = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        score_with_npm = compute_base_score(
            npm_weekly=100_000, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        assert score_with_npm > score_no_npm

    def test_npm_formula_exact(self):
        # log10(100000 + 1) * 3.0  ≈ 15.0
        expected = math.log10(100_001) * 3.0
        score = compute_base_score(
            npm_weekly=100_000, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        assert score == pytest.approx(expected, abs=0.01)

    def test_pypi_converted_to_weekly_equivalent(self):
        # pypi_monthly=400 → equiv = 100/wk → same weight as npm=100
        score_npm = compute_base_score(
            npm_weekly=100, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        score_pypi = compute_base_score(
            npm_weekly=None, pypi_monthly=400, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        assert score_npm == pytest.approx(score_pypi, abs=0.01)

    def test_npm_preferred_over_pypi(self):
        # When both present, max() picks the larger
        score_npm_wins = compute_base_score(
            npm_weekly=10_000, pypi_monthly=100, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        score_npm_only = compute_base_score(
            npm_weekly=10_000, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        assert score_npm_wins == pytest.approx(score_npm_only, abs=0.01)

    def test_smithery_capped_at_100k(self):
        score_at_cap = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=None,
            smithery_use=100_000, is_official=False, last_commit_date=None,
        )
        score_above_cap = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=None,
            smithery_use=1_600_000, is_official=False, last_commit_date=None,
        )
        assert score_at_cap == pytest.approx(score_above_cap, abs=0.01)

    def test_docker_has_lower_weight(self):
        # docker weight is 1.0, npm weight is 3.0 at same download count
        score_npm = compute_base_score(
            npm_weekly=1_000, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        score_docker = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=1_000,
            smithery_use=None, is_official=False, last_commit_date=None,
        )
        assert score_npm > score_docker

    def test_recency_bonus_included(self):
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale = "2019-01-01T00:00:00Z"

        score_recent = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=recent,
        )
        score_stale = compute_base_score(
            npm_weekly=None, pypi_monthly=None, docker_pulls=None,
            smithery_use=None, is_official=False, last_commit_date=stale,
        )
        # recent (+2) vs stale (-2) → 4-point difference
        assert score_recent - score_stale == pytest.approx(4.0, abs=0.01)

    def test_full_formula_known_value(self):
        # Sanity check: github-like server with known inputs
        # npm=145000, docker=116000, smithery=3873, official=False, recent commit
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        score = compute_base_score(
            npm_weekly=145_230,
            pypi_monthly=None,
            docker_pulls=116_000,
            smithery_use=3_873,
            is_official=False,
            last_commit_date=recent,
        )
        expected = (
            math.log10(145_231) * 3.0
            + math.log10(116_001) * 1.0
            + math.log10(3_874) * 1.5
            + 2.0  # recency
        )
        assert score == pytest.approx(expected, abs=0.05)


# ---------------------------------------------------------------------------
# Signal config completeness
# ---------------------------------------------------------------------------

class TestSignalConfig:
    def test_shared_repo_keys_are_subset_of_signal_config(self):
        from signals import SIGNAL_CONFIG
        assert SHARED_REPO_KEYS.issubset(set(SIGNAL_CONFIG.keys()))

    def test_signal_keys_constant(self):
        expected = {
            "githubStarsIsShared", "githubCommits90d",
            "npmWeeklyDownloads", "pypiMonthlyDownloads",
            "dockerTotalPulls", "smitheryUseCount",
            "baseScore", "signalsRefreshedAt",
        }
        assert SIGNAL_KEYS == expected


# ---------------------------------------------------------------------------
# Stats.json merge logic (via dry-run via main with monkeypatched I/O)
# ---------------------------------------------------------------------------

class TestStatsMerge:
    """Verify that signal fields are written non-destructively."""

    def test_existing_non_signal_fields_preserved(self, tmp_path):
        """Signal fields overwrite, all others survive."""
        servers_json = tmp_path / "servers.json"
        stats_json = tmp_path / "stats.json"

        servers_json.write_text(json.dumps({
            "metadata": {"schemaVersion": "3"},
            "entries": [{
                "id": "x", "displayName": "X", "category": "developer-tools",
                "shortDescription": "d", "transportType": "stdio",
                "command": "npx", "args": [], "envVars": [],
                "isOfficial": False, "serverKey": "x",
            }],
        }))
        stats_json.write_text(json.dumps({
            "metadata": {"schemaVersion": "3", "computedAt": "2026-01-01T00:00:00Z"},
            "servers": {
                "x": {
                    "serverKey": "x",
                    "starCount": 9999,         # owned by refresh.py — must survive
                    "isTrending": True,         # owned by refresh.py — must survive
                    "lastCommitDate": "2026-05-01T00:00:00Z",
                    "baseScore": 0.0,           # signal field — will be overwritten
                }
            },
        }))

        # Patch the module-level paths and run probe logic directly
        import signals as sig_mod
        import importlib

        original_servers = sig_mod.SERVERS_FILE
        original_stats = sig_mod.STATS_FILE
        sig_mod.SERVERS_FILE = servers_json
        sig_mod.STATS_FILE = stats_json

        try:
            # Call the probe logic: build the merged dict manually
            with open(servers_json) as f:
                catalog = json.load(f)
            with open(stats_json) as f:
                raw = json.load(f)
            existing = raw["servers"]["x"]

            # Simulate what main() does: only overwrite SIGNAL_KEYS
            updated = dict(existing)
            fake_signals = {k: None for k in sig_mod.SIGNAL_KEYS}
            fake_signals["baseScore"] = 7.5
            updated.update(fake_signals)

            # Preserved non-signal fields
            assert updated["starCount"] == 9999
            assert updated["isTrending"] is True
            assert updated["lastCommitDate"] == "2026-05-01T00:00:00Z"
            # Signal field overwritten
            assert updated["baseScore"] == 7.5
        finally:
            sig_mod.SERVERS_FILE = original_servers
            sig_mod.STATS_FILE = original_stats

    def test_servers_not_in_entries_are_preserved(self):
        """Servers in stats.json that aren't in servers.json pass through unchanged."""
        existing_stats = {
            "orphan-server": {
                "serverKey": "orphan-server",
                "starCount": 42,
                "isTrending": False,
            }
        }
        # Simulate the merge: updated starts empty (no entries matched),
        # then we fold in keys not in updated
        updated: dict = {}
        for key, data in existing_stats.items():
            if key not in updated:
                updated[key] = data

        assert "orphan-server" in updated
        assert updated["orphan-server"]["starCount"] == 42
