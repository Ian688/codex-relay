# Quick start

Build and launch using the commands in the README. Open Codex first, then refresh Relay.

## Test automatic Goal pause

Create a dedicated Goal in Codex with this objective:

> This is a Codex Relay test. Repeat: run a terminal command that waits 20 seconds, briefly report the iteration number, then start the next iteration. Do not edit files, access the network, or install software. Keep the Goal active until I pause or cancel it.

In Relay, enable Goal Controls, select that session, choose Reserve Quota in Advance, and save. Set the pause threshold slightly above the current **remaining** quota. If 16% remains, 17% is an unambiguous test threshold. Start Monitoring and observe the next valid check.

Expected: the Goal becomes paused, the running turn becomes interrupted, and Relay records its own quota-policy pause. A selected checkbox or “Goal Controls: Ready” does not mean monitoring has started.

To test recovery without exhausting quota, lower the pause threshold to 5% and set recovery to a remaining-quota threshold below the current quota but above 5%. Save and leave monitoring running. This avoids immediate re-pause. Do not use a production task for this test.

## Two independent controls

- Start/Pause Monitoring controls Relay, not Codex execution.
- Goal Controls enables explicit Accessibility operations. It is off by default.

A manually paused Goal will not be resumed automatically. A reset policy waits for an observed reset and service permission; it does not promise execution at an exact second.
