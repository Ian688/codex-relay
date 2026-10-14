# Polling, resources, and diagnostics

## Settings

Open **Codex Relay → Monitoring Settings…** (also available in the menu bar). Defaults:

| Setting | Default | Range |
|---|---:|---:|
| Normal monitoring check | 30 seconds | 10–300 seconds |
| Dashboard refresh | 60 seconds | 20–300 seconds |
| Idle quota-exhaustion check | 300 seconds | 60–1800 seconds |

These are scheduler intervals, not a strict global RPC rate limiter. One check may perform multiple read-only requests for quota, session state and action verification.

Saved values live in SQLite, not a watched text file. The running service reads them and wakes its scheduler; no Python restart is required. Per-session quota policy edits also apply to subsequent checks. Language changes require reopening the app.

An active task keeps the normal interval. When inactive work is waiting on unavailable quota, checks slow down and wake near the quota gate's reset time (which includes a safety buffer), while still periodically revalidating conditions. Connection/check failures back off up to five minutes. A reset timestamp is a scheduling hint, never permission to resume.

Automatic dashboard refresh reuses a cached snapshot until due. Manual Refresh forces a read. Cached display timestamps remain tied to the original snapshot. Existing connections are reused while healthy; refreshing is not a new model inference request or necessarily a new connection.

There is no documented universally safe polling frequency for this local, version-sensitive integration. The defaults are engineering choices, not an OpenAI service guarantee. For reserve policies, a 30-second check is a practical compromise; use 60 seconds for less urgency. Account-wide readings can lag and in-flight work can consume quota beyond the threshold.

## Runtime

- Swift/AppKit handles UI and Accessibility; Swift uses ARC.
- Python asyncio handles scheduling/protocols; CPython uses reference counting and cyclic garbage collection.
- SQLite uses its native C engine for local state.
- Codex CLI runs as a separate installed process; its memory is additional to Relay's UI and Python processes.

A single read-only startup on the development Mac measured Python peak RSS at 21.4 MiB and the separate Codex app-server process at about 35.2 MiB. This excludes the native UI and the main Codex app, and is not a long-running leak benchmark.

No custom memory allocator or forced garbage collection is used. Lower polling, cached UI reads, bounded queues, and avoiding repeated episode-baseline reads reduce work. Long conversation histories can still be expensive to load; this integration does not claim constant memory regardless of task size.

## Logs

- `~/Library/Logs/CodexRelay/engine.log`: current backend lifecycle, version, timestamped checks and warnings. Rotates at 2 MiB, keeps three backups, private permissions.
- `~/Library/Logs/CodexRelay/backend.log`: stderr, including older versions' unstructured tracebacks.
- `~/.local/share/codex-autoresume/state.sqlite3`: policies, monitoring membership, checks and action receipts. Keep private.

Closing the UI pipe is handled as normal backend shutdown rather than an uncaught BrokenPipeError. An unknown action result remains needs-attention and is not resent automatically. Do not attach raw databases or private logs to public issues.
