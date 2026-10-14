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

An active task keeps the normal interval. Each waiting session has a separate deadline; it is not polled simply because another session is active. Relay-confirmed reserve pauses enter waiting even when account quota remains usable. Default: low-frequency probes, or the known reset deadline, whichever is earlier. Disable Check for early quota resets to wait directly for a known reset deadline. Remaining-threshold policies and missing reset times still require probes. Manual Goal pauses receive status-only checks, with no automatic recovery or quota queries for that paused Goal. Connection/check failures back off up to five minutes. A reset timestamp is a scheduling hint, never permission to resume.

Automatic dashboard refresh renders cached monitor results only, even after the previous UI interval expires. It does not fetch quota or full histories. Manual Refresh forces a read, discovers new sessions, and wakes eligibility checks. Cached display timestamps remain tied to the original snapshot. Existing connections are reused while healthy; refreshing is not a new model inference request or necessarily a new connection.

There is no documented universally safe polling frequency for this local, version-sensitive integration. The defaults are engineering choices, not an OpenAI service guarantee. For reserve policies, a 30-second check is a practical compromise; use 60 seconds for less urgency. Account-wide readings can lag and in-flight work can consume quota beyond the threshold.

## Runtime

- Swift/AppKit handles UI and Accessibility; Swift uses ARC.
- Python asyncio handles scheduling/protocols; CPython uses reference counting and cyclic garbage collection.
- SQLite uses its native C engine for local state.
- Codex CLI runs as a separate installed process; its memory is additional to Relay's UI and Python processes.

As a v0.6.0 reference measurement, a single read-only startup on the development Mac measured Python peak RSS at 21.4 MiB and the separate Codex app-server process at about 35.2 MiB. This excludes the native UI and the main Codex app, and is not a long-running leak benchmark.

No custom memory allocator or forced garbage collection is used. Lower polling, cached UI reads, bounded queues, and avoiding repeated episode-baseline reads reduce work. Long conversation histories can still be expensive to load; this integration does not claim constant memory regardless of task size.

## Logs

- `~/Library/Logs/CodexRelay/engine.log`: current backend lifecycle, version, timestamped checks and warnings. Rotates at 2 MiB, keeps three backups, private permissions.
- `~/Library/Logs/CodexRelay/backend.log`: stderr, including older versions' unstructured tracebacks.
- `~/.local/share/codex-autoresume/state.sqlite3`: policies, monitoring membership, checks and action receipts. Keep private.

Closing the UI pipe is handled as normal backend shutdown rather than an uncaught BrokenPipeError. An unknown action result remains needs-attention and is not resent automatically. Do not attach raw databases or private logs to public issues.
