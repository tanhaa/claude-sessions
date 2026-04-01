"""Tests for claude_sessions: decode_project_path, UUID regexes, and filter round-trip."""
import os
import sys

import pytest

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import claude_sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# 1. decode_project_path
# ---------------------------------------------------------------------------

class TestDecodeProjectPath:
    def test_valid_path_under_home(self, tmp_path):
        """A folder name that encodes a real directory under home resolves correctly."""
        # Create a real subdirectory under tmp_path inside HOME so that
        # _is_under_home passes.  We re-home the module to tmp_path for this test.
        target = tmp_path / "myproject"
        target.mkdir()

        # tmp_path is typically under /var/... on macOS (a symlink to /private/var/...).
        # Resolve it so the comparison against HOME_DIR is accurate.
        real_home = os.path.realpath(str(tmp_path.parent))

        # Patch HOME_DIR inside the module so _is_under_home uses our fake home.
        original_home = claude_sessions.HOME_DIR
        try:
            claude_sessions.HOME_DIR = real_home

            # Build the encoded folder name: absolute path with leading / stripped,
            # then every / replaced by -.
            real_target = os.path.realpath(str(target))
            folder_name = real_target.lstrip("/").replace("/", "-")

            result = claude_sessions.decode_project_path(folder_name)
            assert result is not None
            assert os.path.realpath(result) == real_target
        finally:
            claude_sessions.HOME_DIR = original_home

    def test_path_outside_home_returns_none(self):
        """/etc encodes to a path outside HOME — must return None."""
        # "-etc" decodes to "/etc" which is not under HOME.
        result = claude_sessions.decode_project_path("-etc")
        assert result is None

    def test_empty_string_returns_none(self):
        """An empty folder name must not raise and should return None."""
        result = claude_sessions.decode_project_path("")
        assert result is None

    def test_only_hyphens_returns_none(self):
        """A string of only hyphens decodes to '/' which is outside HOME."""
        result = claude_sessions.decode_project_path("---")
        assert result is None

    def test_path_traversal_attempt_returns_none(self):
        """A folder name that might resolve outside home returns None."""
        # Encoding of /etc/passwd
        result = claude_sessions.decode_project_path("-etc-passwd")
        assert result is None


# ---------------------------------------------------------------------------
# 2. UUID regex validation
# ---------------------------------------------------------------------------

class TestUUIDRegexes:
    VALID_UUIDS = [
        "550e8400-e29b-41d4-a716-446655440000",
        "00000000-0000-0000-0000-000000000000",
        "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    ]

    INVALID_STRINGS = [
        # path traversal
        "../etc/passwd",
        "/etc/passwd",
        "../../root",
        # spaces
        "550e8400 e29b 41d4 a716 446655440000",
        # special characters
        "550e8400-e29b-41d4-a716-44665544000!",
        "uuid;rm -rf /",
        # too short / malformed
        "not-a-uuid",
        "",
        "550e8400-e29b-41d4-a716",       # missing last group
        "550e8400-e29b-41d4-a716-4466554400001",  # last group too long
    ]

    @pytest.mark.parametrize("uuid_str", VALID_UUIDS)
    def test_uuid_re_matches_valid(self, uuid_str):
        assert claude_sessions._UUID_RE.match(uuid_str) is not None

    @pytest.mark.parametrize("uuid_str", VALID_UUIDS)
    def test_uuid_safe_re_matches_valid(self, uuid_str):
        assert claude_sessions._UUID_SAFE_RE.match(uuid_str) is not None

    @pytest.mark.parametrize("bad", INVALID_STRINGS)
    def test_uuid_re_rejects_invalid(self, bad):
        assert claude_sessions._UUID_RE.match(bad) is None

    @pytest.mark.parametrize("bad", INVALID_STRINGS)
    def test_uuid_safe_re_rejects_path_traversal_and_specials(self, bad):
        # _UUID_SAFE_RE allows only [0-9a-fA-F-]; anything else must not match.
        if bad and all(c in "0123456789abcdefABCDEF-" for c in bad):
            # This string happens to pass the safe pattern — that's fine, it's
            # not supposed to be an exhaustive UUID validator on its own.
            return
        assert claude_sessions._UUID_SAFE_RE.match(bad) is None

    def test_path_traversal_uuid_re(self):
        assert claude_sessions._UUID_RE.match("../etc/passwd") is None

    def test_path_traversal_uuid_safe_re(self):
        assert claude_sessions._UUID_SAFE_RE.match("../etc/passwd") is None

    def test_spaces_uuid_re(self):
        assert claude_sessions._UUID_RE.match("550e8400 e29b 41d4 a716 446655440000") is None

    def test_spaces_uuid_safe_re(self):
        assert claude_sessions._UUID_SAFE_RE.match("hello world") is None


# ---------------------------------------------------------------------------
# 3. Filter JSON round-trip (save_filter / load_filter)
# ---------------------------------------------------------------------------

class TestFilterRoundTrip:
    def test_roundtrip_hidden_and_pinned(self, tmp_path, monkeypatch):
        """save_filter then load_filter should return the exact same sets."""
        filter_file = tmp_path / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        hidden = {"project-alpha", "project-beta"}
        pinned = {"project-gamma"}

        claude_sessions.save_filter(hidden, pinned)

        loaded_hidden, loaded_pinned = claude_sessions.load_filter()

        assert loaded_hidden == hidden
        assert loaded_pinned == pinned

    def test_roundtrip_empty_sets(self, tmp_path, monkeypatch):
        """Empty hidden and pinned sets survive the round-trip."""
        filter_file = tmp_path / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        claude_sessions.save_filter(set(), set())
        loaded_hidden, loaded_pinned = claude_sessions.load_filter()

        assert loaded_hidden == set()
        assert loaded_pinned == set()

    def test_load_filter_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """load_filter on a non-existent file returns two empty sets (no exception)."""
        filter_file = tmp_path / "nonexistent" / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        hidden, pinned = claude_sessions.load_filter()

        assert hidden == set()
        assert pinned == set()

    def test_save_creates_parent_directories(self, tmp_path, monkeypatch):
        """save_filter must create missing parent directories atomically."""
        filter_file = tmp_path / "deep" / "nested" / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        claude_sessions.save_filter({"a"}, {"b"})

        assert filter_file.exists()

    def test_load_filter_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        """A corrupted JSON file should not raise — load_filter returns empty sets."""
        filter_file = tmp_path / "filters.json"
        filter_file.write_text("not valid json {{{", encoding="utf-8")
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        hidden, pinned = claude_sessions.load_filter()

        assert hidden == set()
        assert pinned == set()

    def test_multiple_saves_last_write_wins(self, tmp_path, monkeypatch):
        """Calling save_filter twice: the second write fully replaces the first."""
        filter_file = tmp_path / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        claude_sessions.save_filter({"old-project"}, set())
        claude_sessions.save_filter({"new-project"}, {"pinned-project"})

        hidden, pinned = claude_sessions.load_filter()

        assert hidden == {"new-project"}
        assert pinned == {"pinned-project"}
        assert "old-project" not in hidden

    def test_real_config_never_touched(self, tmp_path, monkeypatch):
        """Verify the real FILTER_FILE path is not written during tests."""
        import json as _json

        real_path = os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "claude-sessions",
            "filters.json",
        )
        filter_file = tmp_path / "filters.json"
        monkeypatch.setattr(claude_sessions, "FILTER_FILE", str(filter_file))

        claude_sessions.save_filter({"test"}, set())

        # The real config file should not have been modified by this test.
        # We can only assert it doesn't contain our sentinel value.
        if os.path.exists(real_path):
            with open(real_path, encoding="utf-8") as fh:
                data = _json.load(fh)
            assert "test" not in data.get("hidden", [])
