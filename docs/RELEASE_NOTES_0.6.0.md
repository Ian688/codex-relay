# Codex Relay 0.6.0 — English-first release

- English is now the default interface, including menus, policy descriptions, state reasons and known errors.
- Chinese remains available through Language / 语言; restart to apply without silently restarting monitoring.
- Configurable normal/UI/quota-wait polling, slow idle waiting, error backoff, cached automatic UI reads, rotating private logs, and graceful pipe shutdown.
- Display timestamps use the local Mac timezone.
- Longer English controls and policy labels fit the dashboard.
- Added English quick start, architecture, validation, security guidance and showcase materials.
- The demo replays a previously verified real Goal control test, using controlled quota readings. It is clearly labeled as a replay and is not a new full reset-window test.

Build the app on your own Mac with `python3 scripts/build_macos.py`. The local bundle depends on installed Python 3.11+ and Codex. No notarization or cross-machine guarantee.

Goal control behavior retains v0.5 protections: explicit Accessibility opt-in, verified target, one action per episode, manual pause protection, and actual owner/new-turn confirmation.
