# Adaptive scheduling plan — v0.7.0

These scheduling settings apply to the native desktop service. The standalone CLI retains its `--interval` loop.

## Decisions

- Schedule each selected session independently. Running work must not pull a waiting session into frequent checks.
- Run normal checks while work is executing. A manually paused Goal receives status-only checks so a user restart can be detected; it is never automatically resumed.
- A Relay-confirmed reserve pause enters policy waiting even when account quota is still usable.
- Reset recovery schedules against the saved interruption's reset baseline; it does not replace that identity with a later timestamp. Known account-exhaustion windows also constrain recovery.
- Remaining-quota recovery uses low-frequency checks because no exact recovery time can be predicted.
- Default: periodically check for early resets (existing wait interval, default 300 seconds), or wake at the known reset deadline, whichever is earlier. Advanced option: disable early reset checks for reset-based waiting. Unknown deadlines and remaining-quota policies still need periodic checks.
- Discovering early usable quota can recover quota/remaining-threshold policies. It does not bypass an explicit next-reset policy.
- Manual Refresh, saved policy changes, and membership changes wake a new eligibility check. A scheduled time is only a hint; all existing quota, account, Goal, turn, cooldown and action-confirmation gates remain mandatory.
- Dashboard timer ticks render cached monitor results and countdowns. They never initiate Codex requests. Manual Refresh discovers new sessions and forces fresh reads.
- When a wake occurs, recheck untouched waiting jobs even if their old retry timestamp is later. This is necessary to discover early quota resets. Never reset attempts or replay uncertain submissions.
- Do not claim reliable push events. This release uses periodic probes/manual wake; a future verified event channel can provide an additional wake source.

Known reset waits describe normal operation. Explicit refreshes, settings changes, connection errors and reconnection may trigger state resynchronization. These checks still cannot override the saved recovery policy.

## Display

Show the selected session's wait mode, reason and next check, plus the next scheduler wake globally. Preserve actual quota/snapshot timestamps when rendering cached data. The global successful-check timestamp refers to the latest successful cycle for due sessions; it does not imply every waiting session was reread at that moment.

## Acceptance

Test available-quota reserve waits, exact reset scheduling, early quota recovery, strict next-reset protection, missing/stale reset times, multiple quota windows, active/waiting coexistence, manual pauses, cache-only UI updates, explicit wakes, and non-retry of attempted actions. No production tasks need to be interrupted for this change.
