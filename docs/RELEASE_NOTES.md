# Codex Relay 0.7.0 — Adaptive waiting

- Per-session wake deadlines: running work does not pull waiting sessions into frequent checks, and repeated active checks do not postpone another session's deadline.
- Relay-confirmed reserve pauses enter policy waiting even when account quota is still usable.
- Default low-frequency early-reset probes, with an optional strict wait until a known reset deadline. Missing deadlines and remaining-quota recovery still require probes.
- Automatic dashboard refresh renders local monitor results only. Manual Refresh forces reads and wakes eligibility checks without overriding the saved policy.
- Early quota recovery can recheck untouched jobs before an old retry timestamp. Atomic reservation, attempt counts, account/turn/Goal checks, strict reset policy, and anti-loop cooldown remain enforced.
- Manual Goal pauses receive status-only checks, with no automatic recovery or quota query for the paused Goal.
- UI shows the waiting mode and next check for the selected session. Quota tooltips retain the actual read timestamp.

This release changes scheduling, not the quota service or Goal button adapter. It does not rely on unverified push notifications. Validation uses deterministic clocks, isolated databases and test clients; no production Goal was interrupted for this change. The older published English video remains a labeled replay of the earlier Goal-control test, not a demonstration of every new scheduler setting.

Build locally with `python3 scripts/build_macos.py`. The app depends on installed Python/Codex and is locally signed, not Apple-notarized. Desktop scheduling settings do not replace the standalone CLI's `--interval` loop.
