# Quota-aware pause and recovery for authorized Codex tasks and Goals

## Problem

A user can authorize a long-running task, but a usage limit interrupts execution. After quota recovers, the user must return to resume work. For formal Goals, an ordinary Continue message and an actual Goal Resume action are different operations. Stale usage-limit indicators can also make external automation repeat actions unnecessarily.

## Proposed native behavior

Offer an explicit, per-session “Resume when quota recovers” setting. Optionally allow a quota reserve policy that pauses at a remaining-quota threshold and resumes after a reset or a higher threshold.

Use internal quota permission and Goal lifecycle events. Distinguish quota interruptions from manual pauses, pending approvals, task completion, and Goal budget exhaustion. Preserve user intent, never bypass limits, and show why a task is paused and whether recovery actually started a new turn.

## Working prototype

[Codex Relay](https://github.com/Ian688/codex-relay) is an independent native macOS prototype, built with Codex, with English-first UI and Chinese support. It provides selected-session policies, separate turn/Goal/Relay states, read-only decision evidence, and episode-based duplicate prevention.

A dedicated real Goal has passed native pause, verified Accessibility Resume, and owner-confirmed new-turn execution. Quota readings were controlled in an isolated test database. Account exhaustion and waiting through a whole reset window were not tested.

## Why native support helps

Internal lifecycle events can remove version-sensitive external IPC and Accessibility button matching. The value is continuity of already-authorized work, with explicit user control and verifiable recovery.

This is a feature proposal with a reference prototype, not a request to merge external code. I would welcome product or engineering feedback.

Author: Ian · https://github.com/Ian688
