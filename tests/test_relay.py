import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from codex_autoresume.daemon import Watchdog
from codex_autoresume.desktop import normalize_snapshot
from codex_autoresume.rules import quota_failure, job_safe, quota_used
from codex_autoresume.store import Store
from test_daemon import FakeClient


class BudgetClient(FakeClient):
    def __init__(self):
        super().__init__(thread_status='active')
        self.used = 81
        self.thread['turns'][-1] = {'id': 'active-t1', 'status': 'inProgress'}
    async def limits(self):
        data = await super().limits()
        data['rateLimits']['primary']['usedPercent'] = self.used
        return data
    async def interrupt_turn(self, tid, turn_id):
        self.calls.append(('interrupt', tid, turn_id))
        self.thread['status'] = 'idle'
        self.thread['turns'][-1]['status'] = 'interrupted'
    async def resume_thread(self, tid):
        self.calls.append(('resume', tid))
        return {'thread': self.thread}


class RelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.store.watch('s1', 'acct-A')
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()
    async def test_budget_stop_wait_recover_and_no_duplicate(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        watcher = Watchdog(self.store, client)
        await watcher.once()
        self.assertEqual(client.calls, [('interrupt', 's1', 'active-t1')])
        self.assertEqual(self.store.jobs()[0]['status'], 'waiting')
        await watcher.once()
        self.assertEqual(len(client.calls), 1)
        client.used = 49
        self.store.update('s1', 'active-t1', 'waiting', retry_at=0)
        await watcher.once()
        self.assertEqual(client.calls[-1], ('start', 's1', '继续'))
        await watcher.once()
        self.assertEqual(len(client.calls), 3)
    async def test_zero_resume_threshold_allows_zero_usage(self):
        self.store.set_policy('s1', pause_used=80, resume_used=0, budget=True)
        client = BudgetClient()
        watcher = Watchdog(self.store, client)
        await watcher.once()
        client.used = 0
        self.store.update('s1', 'active-t1', 'waiting', retry_at=0)
        await watcher.once()
        self.assertEqual(client.calls[-1], ('start', 's1', '继续'))
    async def test_budget_unknown_or_changed_account_never_stops(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        for account in [None, 'different-account']:
            client = BudgetClient()
            client.owner = account
            await Watchdog(self.store, client).once()
            self.assertFalse(client.calls)
    async def test_budget_never_resumes_user_interruption(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        client.thread['turns'][-1]['status'] = 'interrupted'
        client.thread['status'] = 'idle'
        await Watchdog(self.store, client).once()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.jobs(), [])
    async def test_budget_read_unknown_does_not_stop(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        client.used = None
        await Watchdog(self.store, client).once()
        self.assertFalse(client.calls)
    async def test_budget_removal_prevents_resume(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        watcher = Watchdog(self.store, client)
        await watcher.once()
        self.store.unwatch('s1')
        client.used = 0
        await watcher.once()
        self.assertEqual(len(client.calls), 1)
    async def test_goal_only_filters_plain_threads(self):
        self.store.set_policy('s1', goal_only=True)
        client = FakeClient()
        await Watchdog(self.store, client).once()
        self.assertFalse(client.calls)
    async def test_usage_limited_goal_requires_native_control_after_permission_returns(self):
        self.store.set_policy('s1', goal_only=True)
        client = FakeClient(quota_ready=False)
        client.thread['goal'] = {'status': 'usageLimited', 'objective': 'test'}
        client.thread['turns'][-1]['status'] = 'completed'
        watcher = Watchdog(self.store, client)
        await watcher.once()
        self.assertFalse(client.calls)
        client.quota_ready = True
        self.store.update('s1', 'failed-t1', 'waiting', retry_at=0)
        await watcher.once()
        self.assertFalse(client.calls)
        self.assertEqual(self.store.jobs()[0]['status'],'needs_attention')
        self.assertIn('Goal control unavailable',self.store.jobs()[0]['detail'])
    async def test_paused_goal_not_resumed(self):
        for status in ['paused', 'complete', 'blocked', 'budgetLimited']:
            with self.subTest(status=status):
                client = FakeClient()
                client.thread['goal'] = {'status': status}
                await Watchdog(self.store, client).once()
                self.assertFalse(client.calls)
    async def test_pending_user_request_prevents_continuation(self):
        client = FakeClient()
        client.thread['requests'] = [{'id': 'approval-request'}]
        await Watchdog(self.store, client).once()
        self.assertFalse(client.calls)
    async def test_unselected_active_thread_is_not_stopped(self):
        self.store.unwatch('s1')
        client = BudgetClient()
        await Watchdog(self.store, client).once()
        self.assertFalse(client.calls)
    async def test_policy_persists_restart(self):
        self.store.set_policy('s1', goal_only=True, pause_used=80, resume_used=50, budget=True)
        other = Store(Path(self.temp.name) / 'state.sqlite3')
        try: self.assertEqual(other.policy('s1')['pause_used'], 80)
        finally: other.close()
    async def test_budget_changed_goal_cancels_recovery(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        watcher = Watchdog(self.store, client)
        await watcher.once()
        client.thread['goal'] = {'status': 'paused', 'updatedAt': 123}
        client.used = 0
        self.store.update('s1', 'active-t1', 'waiting', retry_at=0)
        await watcher.once()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.jobs()[0]['status'], 'superseded')
    async def test_invalid_thresholds_and_unselected_policy(self):
        for pause,resume in [(50,80),(0,0),(101,50),(80,-1),(float('nan'),50)]:
            with self.assertRaises(ValueError):
                self.store.set_policy('s1', pause_used=pause, resume_used=resume, budget=True)
        with self.assertRaises(ValueError): self.store.set_policy('unknown', pause_used=80, resume_used=50,budget=True)
    async def test_dry_run_never_stops_or_starts(self):
        self.store.set_policy('s1', pause_used=80, resume_used=50, budget=True)
        client = BudgetClient()
        await Watchdog(self.store, client, dry_run=True).once()
        self.assertFalse(client.calls)


class SnapshotTest(unittest.TestCase):
    def test_canonical_order_and_runtime(self):
        snapshot = {'id': 's1', 'threadRuntimeStatus': {'type': 'active'},
                    'turnHistory': {'kind': 'canonical', 'history': {
                        'entitiesByKey': {'later': {'turnId': 't2', 'status': 'inProgress'},
                                          'older': {'turnId': 't1', 'status': 'completed'}},
                        'islands': [{'entries': [{'value': 'older'}, {'value': 'later'}]}]}}}
        thread = normalize_snapshot(snapshot)
        self.assertEqual([t['id'] for t in thread['turns']], ['t1', 't2'])
        self.assertEqual(thread['status']['type'], 'active')
    def test_unknown_snapshot_rejected(self):
        with self.assertRaises(RuntimeError): normalize_snapshot({'id':'s1'})
    def test_unknown_usage_is_not_budget(self):
        for value in [None, '80', True, float('nan'), -2, 120]:
            self.assertIsNone(quota_used({'rateLimits':{'primary':{'usedPercent':value}}}))
