"""Explicit live test: selected TEST thread only, two short inference turns.

Run from the project root with: PYTHONPATH=. python3 scripts/live_budget_test.py THREAD_ID
Creates an isolated test DB and report, never alters production budget settings.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
from codex_autoresume.desktop import DesktopClient
from codex_autoresume.daemon import Watchdog
from codex_autoresume.store import Store
from codex_autoresume.rules import is_idle, quota_used

async def main(tid):
    root = Path('.diagnostics')
    root.mkdir(exist_ok=True)
    store = Store(root / f'budget-{int(time.time())}.sqlite3')
    client = DesktopClient()
    report = {'thread_id': tid, 'test': 'real budget interruption and continuation', 'steps': []}
    def record(action, data):
        report['steps'].append({'action': action, **data})
        (root / 'budget-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(action, json.dumps(data, ensure_ascii=False), flush=True)
    try:
        await client.start()
        thread = await client.read_thread(tid)
        if not is_idle(thread) or thread.get('goal'):
            raise RuntimeError('Test thread must be idle and have no goal')
        limits = await client.limits()
        used = quota_used(limits)
        if used is None or not 1 <= used < 90: raise RuntimeError('Need known usage between 1 and 90 percent for this test')
        store.watch(tid, limits['accountId'], 'Relay budget live TEST')
        store.set_policy(tid, pause_used=1, resume_used=0, budget=True)
        turn = (await client.start_turn(tid, '现在测试预算启停。只回答“测试运行”，不要调用工具。收到“继续”时仍按本会话原约定回复。'))['turn']
        record('started', {'turn_id': turn['id'], 'used_percent': used})
        watcher = Watchdog(store, client, interval=10)
        await watcher.enforce_budgets()
        await watcher.once()
        jobs = [j for j in store.jobs() if j['failed_turn_id'] == turn['id']]
        if len(jobs) != 1 or jobs[0]['status'] != 'waiting': raise RuntimeError(f'Pause not confirmed: {jobs}')
        state = await client.read_thread(tid)
        assert state['turns'][-1]['id'] == turn['id'] and state['turns'][-1]['status'] == 'interrupted'
        record('budget_stopped', {'turn_id': turn['id'], 'status': state['turns'][-1]['status']})
        await watcher.once()
        assert not store.jobs()[0]['resumed_turn_id']
        record('held', {'resume_sent': False, 'reason': 'usage still above resume threshold'})
        store.set_policy(tid, pause_used=95, resume_used=90, budget=True)
        record('budget_released', {'pause_used': 95, 'resume_used': 90, 'note': 'User-authorized test raises thresholds; account quota is real and unchanged'})
        await watcher.once()
        job = store.jobs()[0]
        if not job['resumed_turn_id']: raise RuntimeError('Continuation not started')
        record('continued', {'turn_id': job['resumed_turn_id'], 'attempts': job['attempts']})
        async with asyncio.timeout(90):
            while True:
                await watcher.once()
                job = store.jobs()[0]
                if job['status'] == 'done': break
                if job['status'] != 'running': raise RuntimeError(job['detail'])
                await asyncio.sleep(.5)
        final = await client.read_thread(tid)
        last = final['turns'][-1]
        record('completed', {'turn_id': last['id'], 'status': last['status'], 'model': final['model'], 'effort': final['effort'],
               'answer': [i.get('text') for i in last.get('items',[]) if i.get('type') == 'agentMessage']})
        for _ in range(2): await watcher.once()
        assert store.jobs()[0]['attempts'] == 1
        assert (await client.read_thread(tid))['turns'][-1]['id'] == last['id']
        record('no_duplicate', {'attempts': 1})
    finally:
        # No daemon or test budget remains armed after this bounded script.
        store.unwatch(tid)
        await client.close()
        store.close()

if __name__ == '__main__': asyncio.run(main(sys.argv[1]))
