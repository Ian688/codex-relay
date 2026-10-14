# Validation

## Previously verified control behavior (v0.5.0)

- 118 Python tests passed for policy migration, interruption episodes, account binding, manual pause protection, permission checks, stale-state cancellation, and uncertain-action handling.
- 43 native AppKit UI checks and 30 pure Accessibility matching checks passed.
- In a dedicated real Codex Goal, Relay interrupted execution, verified paused Goal/interrupted turn, used the verified Resume control, and confirmed a new turn under the same Goal. Exactly one recovery attempt was recorded.
- That test used controlled primary-window readings (81% used, then 49%) and an isolated database, while using the real Desktop owner and actual controls. The final test output was `RELAY_GOAL_BUDGET_OK`; the turn completed and the Goal was cleared.
- Independent App Server metadata updates alone did not wake the Desktop executor. Production Goal recovery does not treat setter acknowledgements as success.

## What is not established

No deliberate account exhaustion or full reset-window end-to-end test. No cross-machine, Apple Silicon, or future Codex version guarantee. No notarization. A confirmed recovery means execution resumed, not that an arbitrary user task is complete.

## English release

See `release-checks.json` for current Python, English UI, Chinese UI, Accessibility fixture, build and signing checks. Public assets contain sanitized content; private raw runtime evidence is intentionally excluded from this repository.
