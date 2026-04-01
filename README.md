# claude-sessions

TUI browser for Claude Code sessions. Browse and resume sessions across all projects.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [Claude Code](https://claude.ai/code) CLI (`claude`) on your PATH

## Run

```
uv run claude_sessions.py
```

## Install globally

From GitHub (no clone needed):

```
uv tool install git+https://github.com/tanhaa/claude-sessions
claude-sessions
```

Or from a local clone:

```
uv tool install .
claude-sessions
```

## Development

After editing source, reinstall with:

```
uv tool install . --reinstall
```

## Testing

```
uv run --extra dev pytest tests/ -q
```

## Controls

| Key | Action |
|-----|--------|
| `↑/↓` | Navigate |
| `Enter` | Resume session |
| `v` | Show/hide chat panel |
| `b` | Toggle panel position (side ↔ bottom) |
| `/` | Search |
| `x` | Hide/unhide current project |
| `p` | Pin/unpin current project (focus mode) |
| `c` | Clear all filters |
| `Esc` | Clear search / return to list |
| `q` | Quit |

Filters persist across runs in `~/.config/claude-sessions/filters.json`.

## Configuration

On first run, a config file is created at `~/.config/claude-sessions/config.json`:

```json
{
  "max_sessions": 500,
  "max_files": 2000,
  "max_preview": 120,
  "max_file_bytes": 5000000,
  "max_line_bytes": 1000000,
  "max_days": null,
  "launch_mode": "subprocess",
  "browse_layout": "panel"
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `max_sessions` | `500` | Max sessions shown in the browser |
| `max_files` | `2000` | Max JSONL files scanned at startup |
| `max_preview` | `120` | Max characters shown in the preview column |
| `max_file_bytes` | `5000000` | Skip files larger than this (bytes) |
| `max_line_bytes` | `1000000` | Skip lines larger than this (bytes) |
| `max_days` | `null` | Hide sessions older than N days (`null` = show all) |
| `launch_mode` | `"subprocess"` | `"subprocess"` returns to the TUI after claude exits; `"replace"` hands the terminal to claude and exits to shell when done |
| `browse_layout` | `"panel"` | `"panel"` shows a toggleable side/bottom chat panel (`v` to show/hide, `b` to move); `"overlay"` opens a full-screen reader |

