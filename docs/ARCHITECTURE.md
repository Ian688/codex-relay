# Architecture and information contract

AppKit dashboard/menu bar → local JSON-lines Python service → quota policy engine → Codex Desktop connection and optional native Accessibility helper.

## Three states

1. Turn: execution in progress, completed, interrupted, or failed.
2. Goal: active, paused, usage limited, budget limited, complete, or absent.
3. Relay: monitoring, waiting, submitting, recovery confirmed, or needs attention.

None implies the others. Old quota banners and natural-language answers do not independently prove current restriction or recovery.

## Decision inputs

Each decision uses account identity, the selected quota window, service usage permission, the saved policy, current turn identity, a full Goal snapshot, pending-input status, and prior episode history. Evidence records source and observation time separately from check and action time.

## Goal actions

Accessibility is explicitly enabled, off by default, and rechecked before actions. Recovery verifies the installed app, native page/session, full objective and duration, Goal status, unique enabled Resume control, bounds, and hit test. One verified mouse event pair is followed by owner-state polling. Recovery requires actual new-turn evidence, not an accepted click alone.

Only Relay-confirmed policy pauses qualify for automatic paused-Goal recovery. User pauses remain manual. Ambiguous or potentially submitted actions do not automatically replay. No Goal deletion is performed.

## Localization

`locales/en.json` is the English catalog for app-authored Chinese source messages. `macos/Localization.swift` embeds that catalog for relocation-safe startup and helper modes. The language preference affects presentation only. User session names, objectives, file paths, and raw evidence are never translated. Display timestamps use the Mac's local timezone.
