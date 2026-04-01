"""
Claude Session Browser — browse and resume Claude Code sessions across all projects.
Usage: claude-sessions
Controls: ↑/↓ navigate · Enter resume · v browse chat · / search · x hide project · p pin project · c clear filters · q quit
"""
from __future__ import annotations

import json
import os
import glob
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Label, RichLog


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
FILTER_FILE = os.path.join(_XDG_CONFIG_HOME, "claude-sessions", "filters.json")
HOME_DIR = os.path.expanduser("~")

_CONFIG_FILE = os.path.join(_XDG_CONFIG_HOME, "claude-sessions", "config.json")
_CONFIG_DEFAULTS: dict = {
    "max_sessions": 500,
    "max_files": 2000,
    "max_preview": 120,
    "max_file_bytes": 5_000_000,
    "max_line_bytes": 1_000_000,
    "max_days": None,
    "launch_mode": "subprocess",
    "browse_layout": "overlay",  # "overlay" | "side" | "bottom"
}


def _validate_config_types(merged: dict) -> dict:
    """Return a copy of *merged* with per-key type checking; bad values fall back to defaults."""
    result = dict(merged)
    for key, default in _CONFIG_DEFAULTS.items():
        val = result[key]
        if default is None:
            # Accepts None or a non-bool int (max_days).
            if not (val is None or (isinstance(val, int) and not isinstance(val, bool))):
                result[key] = default
        elif isinstance(default, int):
            if not (isinstance(val, int) and not isinstance(val, bool)):
                result[key] = default
        elif isinstance(default, str):
            if not isinstance(val, str):
                result[key] = default
    return result


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return dict(_CONFIG_DEFAULTS)
        merged = dict(_CONFIG_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k in _CONFIG_DEFAULTS})
        return _validate_config_types(merged)
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
            with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(_CONFIG_DEFAULTS, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass
        return dict(_CONFIG_DEFAULTS)
    except (OSError, json.JSONDecodeError):
        return dict(_CONFIG_DEFAULTS)


_cfg = _load_config()
MAX_SESSIONS: int = _cfg["max_sessions"]
MAX_FILES: int = _cfg["max_files"]
MAX_PREVIEW: int = _cfg["max_preview"]
MAX_FILE_BYTES: int = _cfg["max_file_bytes"]
MAX_LINE_BYTES: int = _cfg["max_line_bytes"]
_MAX_DAYS: int | None = _cfg["max_days"]
_LAUNCH_MODE: str = _cfg["launch_mode"]
_BROWSE_LAYOUT: str = _cfg["browse_layout"]  # "overlay" | "side" | "bottom"

_MAX_CHAT_MSG_CHARS: int = 3000  # max chars shown per message in the chat browser

_current_uid = os.getuid()

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
# Belt-and-suspenders: UUID chars are [0-9a-f-] only — safe as subprocess arg.
_UUID_SAFE_RE = re.compile(r'^[0-9a-fA-F-]+$')

_ISO8601_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')


def _is_under_home(path: str) -> bool:
    real = os.path.realpath(path)
    return real == HOME_DIR or real.startswith(HOME_DIR + os.sep)


def decode_project_path(folder_name: str) -> str | None:
    """Best-effort: convert a ~/.claude/projects folder name to an absolute path.

    Returns None if the decoded path is outside the user's home directory,
    since the encoding is ambiguous when directory names contain hyphens.
    """
    candidate = folder_name.replace("-", "/")
    if os.path.isdir(candidate) and _is_under_home(candidate):
        return candidate
    fallback = "/" + folder_name.lstrip("-").replace("-", "/")
    return fallback if _is_under_home(fallback) else None


def _extract_text(content: object, limit: int = MAX_PREVIEW) -> str:
    """Extract plain text from a JSONL message content field, capped at *limit* chars."""
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                raw = c.get("text", "")
                return raw[:limit] if isinstance(raw, str) else ""
    elif isinstance(content, str):
        return content[:limit]
    return ""


def load_sessions() -> list[dict]:
    if not os.path.isdir(PROJECTS_DIR):
        return []

    sessions = []
    files_scanned = 0

    for proj_dir in os.listdir(PROJECTS_DIR):
        if files_scanned >= MAX_FILES:
            break

        # Skip hidden entries and names containing path separators (prevents traversal).
        if proj_dir.startswith(".") or "/" in proj_dir or "\\" in proj_dir:
            continue

        proj_path = os.path.join(PROJECTS_DIR, proj_dir)
        if not os.path.isdir(proj_path):
            continue

        for filepath in glob.glob(os.path.join(proj_path, "*.jsonl")):
            if files_scanned >= MAX_FILES:
                break
            files_scanned += 1

            uuid = os.path.splitext(os.path.basename(filepath))[0]
            if not _UUID_RE.match(uuid):
                continue

            first_msg = None
            timestamp = None
            cwd = None
            bytes_read = 0

            try:
                with open(filepath, encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        bytes_read += len(raw)
                        if bytes_read > MAX_FILE_BYTES:
                            break
                        if len(raw) > MAX_LINE_BYTES:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if cwd is None and isinstance(obj.get("cwd"), str):
                            cwd = obj["cwd"]

                        if obj.get("type") == "user" and not obj.get("isMeta"):
                            text = _extract_text(obj.get("message", {}).get("content", ""))

                            if text and not text.startswith("<local-command") and not text.startswith("<command"):
                                first_msg = text.replace("\n", " ").strip()
                                ts = obj.get("timestamp")
                                # Validate timestamp is ISO 8601 before storing.
                                if isinstance(ts, str) and _ISO8601_RE.match(ts):
                                    timestamp = ts
                                break
            except OSError:
                continue

            if first_msg and timestamp:
                if not cwd:
                    cwd = decode_project_path(proj_dir)

                sessions.append({
                    "uuid": uuid,
                    "project": os.path.basename(cwd) if cwd else proj_dir,
                    "preview": first_msg,
                    "timestamp": timestamp,
                    "cwd": cwd,
                    "filepath": filepath,
                })

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)

    if _MAX_DAYS is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - _MAX_DAYS * 86400
        filtered_sessions = []
        for s in sessions:
            try:
                dt = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
                if dt.timestamp() >= cutoff:
                    filtered_sessions.append(s)
            except Exception:
                filtered_sessions.append(s)
        sessions = filtered_sessions

    return sessions[:MAX_SESSIONS]


def format_age(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        days = diff.days
        if days == 0:
            hours = diff.seconds // 3600
            return f"{hours}h ago" if hours > 0 else "just now"
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        return f"{months}mo ago"
    except Exception:
        return ts_str[:10]


def load_chat_messages(filepath: str) -> list[dict]:
    """Load all conversation messages from a session JSONL file for the chat browser."""
    messages = []
    bytes_read = 0
    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                bytes_read += len(raw)
                if bytes_read > MAX_FILE_BYTES:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("isMeta"):
                    continue
                role = obj.get("type")
                if role not in ("user", "assistant"):
                    continue
                text = _extract_text(obj.get("message", {}).get("content", ""), _MAX_CHAT_MSG_CHARS)
                if not text:
                    continue
                if role == "user" and (text.startswith("<local-command") or text.startswith("<command")):
                    continue
                messages.append({"role": role, "text": text})
    except OSError:
        pass
    return messages


def load_filter() -> tuple[set[str], set[str]]:
    """Return (hidden_projects, pinned_projects)."""
    try:
        with open(FILTER_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data.get("hidden", [])), set(data.get("pinned", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return set(), set()


def save_filter(hidden: set[str], pinned: set[str]) -> None:
    """Atomically write filter state to disk."""
    try:
        dir_ = os.path.dirname(FILTER_FILE)
        os.makedirs(dir_, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
            json.dump({"hidden": sorted(hidden), "pinned": sorted(pinned)}, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, FILTER_FILE)
    except OSError:
        pass


def _render_chat_into(log: RichLog, messages: list[dict]) -> None:
    """Write formatted chat messages into a RichLog widget."""
    if not messages:
        log.write(Text("(no messages in this session)"))
        return
    for i, msg in enumerate(messages):
        if i > 0:
            log.write(Text(""))
        label = "You" if msg["role"] == "user" else "Claude"
        log.write(Text(f"─── {label} " + "─" * max(0, 44 - len(label)), style="bold"))
        log.write(Text(msg["text"]))


class ChatScreen(Screen):
    """Full-screen chat browser for a single session (overlay mode)."""

    CSS = """
    #chat-title {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    #chat-log {
        height: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Back", show=True)]

    def __init__(self, session: dict) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        title = f"{self._session['project']}  ·  {self._session['preview'][:80]}"
        yield Label(Text(title), id="chat-title")
        yield RichLog(id="chat-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        _render_chat_into(log, load_chat_messages(self._session["filepath"]))


class SessionBrowser(App):
    CSS = """
    Screen {
        background: $surface;
    }

    #search-bar {
        height: 3;
        border: tall $accent;
        margin: 0 0 0 0;
        padding: 0 1;
    }

    #search-bar:focus {
        border: tall $accent-lighten-1;
    }

    #table {
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $accent;
        color: $text;
    }

    #status {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
        text-align: right;
    }

    /* ── side layout ─────────────────────────────────────────── */
    #browse-split {
        height: 1fr;
    }

    #browse-split > #table {
        width: 1fr;
        height: 100%;
    }

    #chat-panel.side {
        width: 1fr;
        height: 100%;
        border-left: tall $primary-darken-2;
        padding: 0 1;
        background: $surface;
    }

    /* ── bottom layout ───────────────────────────────────────── */
    #chat-panel.bottom {
        height: 40%;
        border-top: tall $primary-darken-2;
        padding: 0 1;
        background: $surface;
    }

    /* shared chat panel focus ring */
    #chat-panel:focus {
        border: tall $accent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("/", "focus_search", "Search", show=True),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("x", "toggle_hide", "Hide project", show=True),
        Binding("p", "toggle_pin", "Pin project", show=True),
        Binding("c", "clear_filters", "Clear filters", show=True),
        Binding("v", "browse_chat", "Browse chat", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.all_sessions: list[dict] = []
        self.filtered: list[dict] = []
        self.hidden_projects: set[str] = set()
        self.pinned_projects: set[str] = set()
        self._chat_panel_uuid: str | None = None  # last session loaded into panel

    def compose(self) -> ComposeResult:
        yield Input(placeholder="  Search sessions...", id="search-bar")
        if _BROWSE_LAYOUT == "side":
            with Horizontal(id="browse-split"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield RichLog(id="chat-panel", classes="side", highlight=False, markup=False, wrap=True)
        else:
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            if _BROWSE_LAYOUT == "bottom":
                yield RichLog(id="chat-panel", classes="bottom", highlight=False, markup=False, wrap=True)
        yield Label("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Claude Sessions"
        self.sub_title = "Browse & resume sessions across all projects"

        table = self.query_one(DataTable)
        table.add_columns("Age", "Project", "Session")

        self.hidden_projects, self.pinned_projects = load_filter()
        self.all_sessions = load_sessions()
        self._refilter_and_refresh()
        table.focus()

    def _apply_filters(self, search: str = "") -> None:
        sessions = self.all_sessions
        if self.pinned_projects:
            sessions = [s for s in sessions if s["project"] in self.pinned_projects]
        sessions = [s for s in sessions if s["project"] not in self.hidden_projects]
        if search:
            q = search.lower()
            sessions = [s for s in sessions if q in s["project"].lower() or q in s["preview"].lower()]
        self.filtered = sessions

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for s in self.filtered:
            # Wrap in Text() to treat as plain text — prevents Rich markup injection.
            table.add_row(
                Text(format_age(s["timestamp"])),
                Text(s["project"]),
                Text(s["preview"]),
                key=s["uuid"],
            )
        parts = [
            f"{len(self.filtered)} sessions",
            f"{len({s['project'] for s in self.filtered})} projects",
        ]
        if self.pinned_projects:
            parts.append(f"pinned: {', '.join(sorted(self.pinned_projects))}")
        if self.hidden_projects:
            parts.append(f"hidden: {len(self.hidden_projects)}")
        self.query_one("#status", Label).update("  |  ".join(parts))

    def _refilter_and_refresh(self) -> None:
        self._apply_filters(search=self.query_one("#search-bar", Input).value.strip())
        self._refresh_table()

    def _toggle_filter_set(self, target: set[str], other: set[str], proj: str) -> None:
        if proj in target:
            target.discard(proj)
        else:
            target.add(proj)
            other.discard(proj)
        save_filter(self.hidden_projects, self.pinned_projects)
        self._refilter_and_refresh()

    @on(Input.Changed, "#search-bar")
    def filter_sessions(self, event: Input.Changed) -> None:
        self._apply_filters(search=event.value.strip())
        self._refresh_table()

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search-bar", Input)
        inp.value = ""
        self.query_one(DataTable).focus()

    def action_toggle_hide(self) -> None:
        session = self._get_selected_session()
        if session:
            self._toggle_filter_set(self.hidden_projects, self.pinned_projects, session["project"])

    def action_toggle_pin(self) -> None:
        session = self._get_selected_session()
        if session:
            self._toggle_filter_set(self.pinned_projects, self.hidden_projects, session["project"])

    def action_clear_filters(self) -> None:
        self.hidden_projects.clear()
        self.pinned_projects.clear()
        save_filter(self.hidden_projects, self.pinned_projects)
        self._refilter_and_refresh()

    def _get_selected_session(self) -> dict | None:
        table = self.query_one(DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.filtered):
            return None
        return self.filtered[table.cursor_row]

    @on(DataTable.RowSelected)
    def on_row_selected(self, _event: DataTable.RowSelected) -> None:
        session = self._get_selected_session()
        if session:
            self.exit(result=(session["uuid"], session["cwd"]))

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        if _BROWSE_LAYOUT in ("side", "bottom"):
            session = self._get_selected_session()
            if session:
                self._populate_chat_panel(session)

    def _populate_chat_panel(self, session: dict) -> None:
        if session["uuid"] == self._chat_panel_uuid:
            return
        self._chat_panel_uuid = session["uuid"]
        panel = self.query_one("#chat-panel", RichLog)
        panel.clear()
        _render_chat_into(panel, load_chat_messages(session["filepath"]))

    def action_browse_chat(self) -> None:
        session = self._get_selected_session()
        if not session:
            return
        if _BROWSE_LAYOUT == "overlay":
            self.push_screen(ChatScreen(session))
        else:
            self._populate_chat_panel(session)
            self.query_one("#chat-panel", RichLog).focus()


def _check_projects_dir() -> None:
    """Verify PROJECTS_DIR resolves inside ~ and is owned by the current user."""
    real = os.path.realpath(PROJECTS_DIR)
    if not _is_under_home(real):
        print(f"Error: projects directory resolves outside home: {real}", file=sys.stderr)
        sys.exit(1)
    try:
        if os.stat(real).st_uid != _current_uid:
            print(f"Error: projects directory not owned by current user: {real}", file=sys.stderr)
            sys.exit(1)
    except OSError as exc:
        print(f"Error: cannot stat projects directory: {exc}", file=sys.stderr)
        sys.exit(1)


def _launch_claude(claude_bin: str, uuid: str, safe_cwd: str | None) -> None:
    cmd = [claude_bin, "--resume", uuid]
    if _LAUNCH_MODE == "replace":
        if safe_cwd:
            try:
                os.chdir(safe_cwd)
            except OSError as exc:
                print(f"Error: cannot chdir to {safe_cwd}: {exc}", file=sys.stderr)
                sys.exit(1)
        try:
            os.execvp(claude_bin, cmd)
        except OSError as exc:
            print(f"Error: exec failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        result_proc = subprocess.run(cmd, cwd=safe_cwd)
        if result_proc.returncode != 0:
            print(f"claude exited with code {result_proc.returncode}", file=sys.stderr)


def main():
    if not os.path.isdir(PROJECTS_DIR):
        print(f"Error: Claude projects directory not found: {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    _check_projects_dir()

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        print("Error: 'claude' not found on PATH", file=sys.stderr)
        sys.exit(1)

    while True:
        app = SessionBrowser()
        result = app.run()

        if result:
            uuid, cwd = result
            # Belt-and-suspenders: UUID regex already guarantees [0-9a-f-] only.
            if not _UUID_SAFE_RE.match(uuid):
                continue

            safe_cwd = os.path.realpath(cwd) if cwd else None
            if safe_cwd and os.path.isdir(safe_cwd):
                # Reject cwd values not owned by the current user.
                try:
                    if os.stat(safe_cwd).st_uid != _current_uid:
                        safe_cwd = None
                except OSError:
                    safe_cwd = None

            _launch_claude(claude_bin, uuid, safe_cwd)
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()
