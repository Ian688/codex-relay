"""Codex Relay CLI."""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import json
import time
import contextlib
from pathlib import Path
from .protocol import AppServer
from .rules import quota_gate, epoch_string
from .store import Store
from .daemon import Watchdog
from .desktop import DesktopClient
from .rules import is_idle
from .lease import MonitorLease

DEFAULT_DB = Path(os.getenv("CODEX_RELAY_DB", os.getenv("CODEX_AUTORESUME_DB", "~/.local/share/codex-autoresume/state.sqlite3"))).expanduser()

def parser():
    p = argparse.ArgumentParser(prog="codex-relay", description="Codex Relay: selected-session quota recovery and budget control")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database")
    p.add_argument("--codex", default=os.environ.get("CODEX_BINARY", "codex"), help="Codex CLI executable")
    p.add_argument("--backend", choices=["desktop", "app-server"], default="desktop", help="Desktop provides live control; app-server is read-only diagnostics")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Check app-server, account, rate limit, and session access (no model turns)")
    sub.add_parser("limits", help="Show current quota and refresh times")
    sp = sub.add_parser("threads", help="List recent Codex threads")
    sp.add_argument("--count", type=int, default=30)
    watch = sub.add_parser("watch", help="Manage explicitly opted-in sessions")
    w = watch.add_subparsers(dest="command", required=True)
    add = w.add_parser("add", help="Watch one thread")
    add.add_argument("thread_id")
    add.add_argument("--label")
    add.add_argument("--goal-only", action="store_true", help="Recover only sessions with a persisted Codex goal")
    rem = w.add_parser("remove", help="Stop watching a thread")
    rem.add_argument("thread_id")
    w.add_parser("list", help="List watched sessions")
    allp = w.add_parser("all", help="Opt in/out of recent-thread discovery")
    allp.add_argument("setting", choices=["on", "off"])
    sub.add_parser("status", help="Show persistent recovery queue")
    run = sub.add_parser("run", help="Run watchdog continuously (foreground)")
    run.add_argument("--interval", type=float, default=45)
    run.add_argument("--recent-hours", type=float, default=24)
    run.add_argument("--limit-id", default="codex")
    run.add_argument("--message", default="继续")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--once", action="store_true", help="One cycle for diagnostics; not a persistent agent")
    strategy = sub.add_parser('strategy', help='Quota recovery or reserve strategy (remaining percentages)')
    s = strategy.add_subparsers(dest='command', required=True)
    spolicy = s.add_parser('set', help='Configure one selected thread')
    spolicy.add_argument('thread_id')
    spolicy.add_argument('--mode', choices=['quota', 'reserve'], default='quota')
    spolicy.add_argument('--window', choices=['primary', 'secondary'], default='primary')
    spolicy.add_argument('--stop-remaining', type=float, default=20)
    spolicy.add_argument('--resume-mode', choices=['reset', 'remaining'], default='reset')
    spolicy.add_argument('--resume-remaining', type=float, default=50)
    s.add_parser('list', help='List selected-thread strategies')
    budget = sub.add_parser('budget', help='Legacy compatibility: thresholds are USED percentages; prefer strategy')
    b = budget.add_subparsers(dest='command', required=True)
    bp = b.add_parser('set')
    bp.add_argument('thread_id')
    bp.add_argument('--pause-used', type=float, required=True)
    bp.add_argument('--resume-used', type=float, required=True)
    bp.add_argument('--window', choices=['primary', 'secondary'], default='primary')
    bo = b.add_parser('off')
    bo.add_argument('thread_id')
    b.add_parser('list')
    sub.add_parser('events', help='Show local audit log')
    smoke = sub.add_parser('smoke', help='One bounded real continuation of a completed TEST thread')
    smoke.add_argument('thread_id')
    smoke.add_argument('--timeout', type=float, default=120)
    smoke.add_argument('--report', help='Write test evidence JSON')
    return p

async def with_codex(args, fn):
    cli = DesktopClient(args.codex) if args.backend == "desktop" else AppServer(args.codex)
    try:
        await cli.start()
        return await fn(cli)
    finally:
        await cli.close()

async def run_cli(args):
    store = Store(args.db)
    try:
        if args.action == 'events':
            for event in store.events(): print(epoch_string(event['at']), event['thread_id'], event['action'], event['detail'])
            return
        if args.action == 'strategy':
            if args.command == 'set':
                store.set_strategy(args.thread_id, mode=args.mode, window=args.window,
                    stop_remaining=args.stop_remaining, resume_mode=args.resume_mode,
                    resume_remaining=args.resume_remaining)
                print('Strategy configured:', store.policy(args.thread_id))
            else:
                for w in store.watched(): print(store.policy(w['thread_id']))
            return
        if args.action == 'budget':
            if args.command == 'set':
                store.set_policy(args.thread_id, pause_used=args.pause_used, resume_used=args.resume_used, window=args.window, budget=True)
                print('Budget configured:', store.policy(args.thread_id))
            elif args.command == 'off':
                store.set_policy(args.thread_id, budget=True)
                print('Switched to quota recovery; pending recoveries follow the current strategy.')
            else:
                for w in store.watched(): print(store.policy(w['thread_id']))
            return
        if args.action == "status":
            for job in store.jobs():
                print(f"{job['status']:17} {job['thread_id']} failure={job['failed_turn_id']} attempts={job['attempts']} detail={job['detail'] or '-'}")
            if not store.jobs(): print("No recoveries yet.")
            return
        if args.action == "watch" and args.command in {"remove", "list", "all"}:
            if args.command == "remove":
                store.unwatch(args.thread_id)
                print("Stopped watching:", args.thread_id)
            elif args.command == "all":
                store.set_setting("watch_all", args.setting == "on")
                print("Recent-thread discovery:", args.setting, "(requires daemon run)")
            else:
                print("Recent-thread discovery:", "ON" if store.setting("watch_all", False) else "OFF")
                for w in store.watched():
                    print(w["thread_id"], "account=" + str(w["account_id"] or "unknown"), "label=" + str(w["label"] or ""), "policy=", store.policy(w["thread_id"]))
            return
        if args.backend == 'app-server' and args.action in {'run', 'smoke'}:
            raise ValueError('Automatic control requires --backend desktop; independent app-server status cannot prove Desktop is idle')
        async def connected(client):
            if args.action in {"doctor", "limits"}:
                quotas = await client.limits()
                ok, next_time, why = quota_gate(quotas)
                print("Backend quota permission:", quotas.get("ordinaryUsageAllowed", "unavailable"))
                print("Account ID:", quotas.get("accountId") or "unavailable")
                print("Relay quota gate:", "READY" if ok else "WAIT", "-", why)
                if not ok: print("Next check not before:", epoch_string(next_time))
                print("Quota windows:")
                buckets = quotas.get("rateLimitsByLimitId") or {"default": quotas.get("rateLimits", {})}
                for name, snap in buckets.items():
                    for kind in ("primary", "secondary"):
                        win = snap.get(kind) or {}
                        if win:
                            reset = win.get("resetsAt")
                            print("  ", name, kind, f"used={win.get('usedPercent')}%", "reset=" + (epoch_string(reset) if reset else "unknown"))
                if args.action == "doctor":
                    items = await client.list_threads(pages=1)
                    print("Recent thread count (first page):", len(items))
                    if items:
                        print("First thread readable:", bool((await client.read_thread(items[0]["id"])) .get("id")))
                    print("Doctor only reads state; no turn started.")
            elif args.action == "threads":
                items = await client.list_threads(pages=max(1, (args.count+49)//50))
                for item in items[:args.count]:
                    print(item.get("id"), item.get("name") or item.get("preview", "")[:60], item.get("cwd", ""), "updated=", item.get("updatedAt"), "status=", item.get("status"))
            elif args.action == "watch" and args.command == "add":
                thread = await client.read_thread(args.thread_id)
                if not thread.get("id"):
                    raise RuntimeError("Thread not readable")
                limits = await client.limits()
                store.watch(args.thread_id, limits.get("accountId"), args.label)
                store.set_policy(args.thread_id, goal_only=args.goal_only)
                print("Watched:", args.thread_id)
                print("Only a verified quota-failed latest turn can be resumed.")
            elif args.action == 'smoke':
                if args.backend != 'desktop': raise ValueError('Smoke requires Desktop live status')
                if store.setting('watch_all', False) or any(w['thread_id'] != args.thread_id for w in store.watched()):
                    raise ValueError('Use a separate --db for smoke; it must contain only the test thread')
                thread = await client.read_thread(args.thread_id)
                latest = (thread.get('turns') or [{}])[-1]
                if not is_idle(thread) or latest.get('status') != 'completed' or thread.get('goal'):
                    raise ValueError('Smoke requires a completed, idle, non-goal TEST thread')
                limits = await client.limits()
                if not limits.get('accountId'): raise ValueError('Unknown account')
                store.watch(args.thread_id, limits['accountId'], 'Codex Relay smoke test')
                store.enqueue(args.thread_id, latest['id'], 'relayTestCompleted', limits['accountId'])
                watcher = Watchdog(store, client, interval=10)
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    await watcher.once()
                    job = next(j for j in store.jobs() if j['failed_turn_id'] == latest['id'])
                    if job['status'] == 'done':
                        final = await client.read_thread(args.thread_id)
                        resumed = next(t for t in final['turns'] if t['id'] == job['resumed_turn_id'])
                        report = {'name': 'Codex Relay', 'thread_id': args.thread_id,
                                  'model': final['model'], 'effort': final['effort'],
                                  'source_turn_id': latest['id'], 'resumed_turn_id': resumed['id'],
                                  'status': resumed['status'], 'attempts': job['attempts'],
                                  'answer': [i.get('text') for i in resumed.get('items', []) if i.get('type') == 'agentMessage']}
                        if args.report:
                            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
                        print(json.dumps(report, ensure_ascii=False, indent=2))
                        return
                    if job['status'] in {'needs_attention', 'superseded'}:
                        raise RuntimeError(job['detail'])
                    await asyncio.sleep(.5)
                raise RuntimeError('Smoke observation timed out; inspect this same thread before another attempt')
            elif args.action == "run":
                watcher = Watchdog(store, client, interval=args.interval,
                    limit_id=args.limit_id, message=args.message,
                    recent_hours=args.recent_hours, dry_run=args.dry_run)
                if args.once:
                    # A one-shot process cannot keep an accepted Codex turn
                    # alive, so one-cycle diagnostics are ALWAYS read-only.
                    watcher.dry_run = True
                    await watcher.once()
                    print("One diagnostic cycle completed. No resume message sent; use continuous 'run' for recovery.")
                else:
                    with (contextlib.nullcontext() if args.dry_run else MonitorLease(args.db)):
                        await watcher.run()
        return await with_codex(args, connected)
    finally:
        store.close()

def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        return
    except (OSError, RuntimeError, ValueError, KeyError, TimeoutError) as exc:
        raise SystemExit(f"Relay error: {exc}") from exc

if __name__ == "__main__":
    main()
