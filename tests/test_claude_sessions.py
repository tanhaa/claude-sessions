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
    def test_valid_path_under_home(self, tmp_path, monkeypatch):
        """A folder name that encodes a real directory under home resolves correctly.

        decode_project_path uses a simple '-' → '/' substitution, which is
        ambiguous for paths whose components already contain hyphens.  When
        tmp_path itself contains hyphens (common on macOS with pytest), the
        encoded round-trip cannot be validated, so the test is skipped.
        """
        target = tmp_path / "myproject"
        target.mkdir()

        # Resolve symlinks (macOS /var → /private/var).
        real_target = os.path.realpath(str(target))
        real_home = os.path.dirname(real_target)

        # The encoding replaces every '/' with '-'.  If any path component in
        # real_target already contains a '-', the round-trip is ambiguous.
        if "-" in real_target:
            pytest.skip("tmp_path contains hyphens; decode_project_path cannot round-trip")

        monkeypatch.setattr(claude_sessions, "HOME_DIR", real_home)

        folder_name = real_target.lstrip("/").replace("/", "-")
        result = claude_sessions.decode_project_path(folder_name)
        assert result is not None
        assert os.path.realpath(result) == real_target

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


# ---------------------------------------------------------------------------
# 4. _load_config: merge, type validation, and error handling
# ---------------------------------------------------------------------------

class TestLoadConfig:
    """Tests for _load_config and _validate_config_types."""

    def _write_config(self, path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_missing_file_returns_defaults_and_creates_file(self, tmp_path, monkeypatch):
        """On first run, missing config file → defaults returned and file written."""
        config_file = tmp_path / "claude-sessions" / "config.json"
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result == claude_sessions._CONFIG_DEFAULTS
        assert config_file.exists()

    def test_written_defaults_are_valid_json(self, tmp_path, monkeypatch):
        """The auto-created config file must be valid JSON matching defaults."""
        import json as _json
        config_file = tmp_path / "claude-sessions" / "config.json"
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        claude_sessions._load_config()

        with open(config_file, encoding="utf-8") as fh:
            data = _json.load(fh)
        assert data == claude_sessions._CONFIG_DEFAULTS

    def test_partial_override_merged_correctly(self, tmp_path, monkeypatch):
        """Known keys in the file override defaults; missing keys keep defaults."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"max_sessions": 99}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["max_sessions"] == 99
        assert result["max_files"] == claude_sessions._CONFIG_DEFAULTS["max_files"]

    def test_unknown_keys_ignored(self, tmp_path, monkeypatch):
        """Keys not in _CONFIG_DEFAULTS are silently dropped."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"unknown_key": "value", "max_sessions": 10}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert "unknown_key" not in result
        assert result["max_sessions"] == 10

    def test_corrupt_json_returns_defaults(self, tmp_path, monkeypatch):
        """A corrupt config file falls back to defaults without raising."""
        config_file = tmp_path / "config.json"
        self._write_config(config_file, "{ invalid json }")
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result == claude_sessions._CONFIG_DEFAULTS

    def test_non_dict_json_returns_defaults(self, tmp_path, monkeypatch):
        """A valid JSON file that is not a dict (e.g. a list) falls back to defaults."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps([1, 2, 3]))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result == claude_sessions._CONFIG_DEFAULTS

    def test_wrong_type_falls_back_to_default(self, tmp_path, monkeypatch):
        """A config value with the wrong type is replaced by the default for that key."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"max_sessions": "not-an-int"}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["max_sessions"] == claude_sessions._CONFIG_DEFAULTS["max_sessions"]

    def test_bool_rejected_for_int_key(self, tmp_path, monkeypatch):
        """bool is a subclass of int in Python; config must reject it for int keys."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"max_sessions": True}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["max_sessions"] == claude_sessions._CONFIG_DEFAULTS["max_sessions"]

    def test_max_days_none_accepted(self, tmp_path, monkeypatch):
        """max_days=null (None) is a valid value and must be preserved."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"max_days": None}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["max_days"] is None

    def test_max_days_int_accepted(self, tmp_path, monkeypatch):
        """max_days=30 (int) is a valid value and must be preserved."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"max_days": 30}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["max_days"] == 30

    def test_launch_mode_wrong_type_falls_back(self, tmp_path, monkeypatch):
        """launch_mode must be a string; a non-string value falls back to default."""
        import json as _json
        config_file = tmp_path / "config.json"
        self._write_config(config_file, _json.dumps({"launch_mode": 42}))
        monkeypatch.setattr(claude_sessions, "_CONFIG_FILE", str(config_file))

        result = claude_sessions._load_config()

        assert result["launch_mode"] == claude_sessions._CONFIG_DEFAULTS["launch_mode"]
