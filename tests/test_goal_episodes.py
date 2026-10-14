import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from codex_autoresume.daemon import Watchdog
from codex_autoresume.store import Store
from codex_autoresume.rules import quota_failure, goal_episode, stale_goal_failure

class Client:
    def __init__(self):
        self.notifications = asyncio.Queue()
        self.thread = {'id': 's', 'status': 'idle', 'goal': {'threadId': 's', 'createdAt': 100, 'updatedAt': 200, 'objective': 'work', 'status': 'usageLimited'}, 'turns': [{'id': 'old', 'status': 'completed'}]}
    async def list_threads(self, **kw): return [{'id': 's'}]
    async def read_thread(self, tid): return self.thread
    async def limits(self): return {'accountId': 'a', 'ordinaryUsageAllowed': False, 'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': time.time()+100}}}

class Episodes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'db'
        self.store = Store(self.path)
        self.store.watch('s', 'a')
        self.client = Client()
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def attempted(self, snapshot=True):
        self.store.enqueue('s', 'old', 'usageLimitExceeded', 'a')
        if snapshot: self.store.merge_context_once('s', 'old', 'goal_episode', goal_episode(self.client.thread))
        self.store.reserve('s', 'old')
        self.store.update('s', 'old', 'done', resumed_turn_id='followup')
    async def test_unchanged_goal_not_requeued_for_arbitrary_later_turns_or_restart(self):
        self.attempted()
        for index in range(4):
            self.client.thread['turns'] = [{'id': f'later-{index}', 'status': 'completed'}]
            await Watchdog(self.store, self.client).scan()
        self.store.close(); self.store = Store(self.path)
        await Watchdog(self.store, self.client).scan()
        self.assertEqual(len(self.store.thread_jobs('s')), 1)
        self.assertEqual(self.store.check('s')['reason'], 'stale_goal_episode')
    async def test_legacy_already_queued_stale_job_superseded_even_future_cooldown(self):
        self.attempted(False)
        self.client.thread['turns'] = [{'id': 'followup', 'status': 'completed'}]
        self.store.enqueue('s', 'followup', 'usageLimitExceeded', 'a')
        self.store.update('s', 'followup', 'waiting', retry_at=time.time()+900)
        await Watchdog(self.store, self.client).scan()
        jobs = {j['failed_turn_id']: j for j in self.store.thread_jobs('s')}
        self.assertEqual(jobs['followup']['status'], 'superseded')
    async def test_new_explicit_failed_turn_is_independent_evidence(self):
        self.attempted()
        self.client.thread['turns'] = [{'id': 'new', 'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}}]
        await Watchdog(self.store, self.client).scan()
        self.assertEqual(len(self.store.thread_jobs('s')), 2)
        self.assertEqual(self.store.check('s')['reason'], 'quota_interruption')
    def test_new_goal_identity_and_new_dated_revision_not_suppressed(self):
        self.attempted()
        self.client.thread['goal']['createdAt'] = 300
        self.assertFalse(stale_goal_failure(self.client.thread, self.store.thread_jobs('s')))
        self.client.thread['goal']['createdAt'] = 100
        self.client.thread['goal']['updatedAt'] = time.time()+10
        self.assertFalse(stale_goal_failure(self.client.thread, self.store.thread_jobs('s')))
    def test_interrupted_or_cancelled_user_turn_not_synthetic_quota_failure(self):
        for status in ('interrupted', 'cancelled', 'canceled'):
            self.client.thread['turns'][-1]['status'] = status
            self.assertIsNone(quota_failure(self.client.thread))
    async def test_error_preserves_last_successful_check_and_does_not_claim_success(self):
        watcher = Watchdog(self.store, self.client)
        await watcher.scan()
        first = self.store.check('s')
        async def fail(tid): raise RuntimeError('offline')
        self.client.read_thread = fail
        await watcher.scan()
        second = self.store.check('s')
        self.assertEqual(first['checked_at'], second['checked_at'])
        self.assertEqual(first['observed_at'], second['observed_at'])
        self.assertEqual(second['error'], 'offline')
    def test_history_not_limited_to_latest_hundred_jobs(self):
        self.attempted()
        for i in range(105): self.store.enqueue('other', str(i), 'usageLimitExceeded', 'a')
        self.assertEqual(len(self.store.jobs()), 100)
        self.assertTrue(stale_goal_failure(self.client.thread, self.store.thread_jobs('s')))
    def test_completed_manual_followup_after_goal_revision_not_failure(self):
        self.client.thread['turns'][-1]['startedAt'] = 201
        self.assertIsNone(quota_failure(self.client.thread))
        del self.client.thread['turns'][-1]['startedAt']
        self.client.thread['turns'][-1]['turnStartedAtMs'] = 201000
        self.assertIsNone(quota_failure(self.client.thread))
        # A new Goal restriction after the turn started is independently eligible.
        self.client.thread['goal']['updatedAt'] = 202
        self.assertIsNotNone(quota_failure(self.client.thread))
    def test_malformed_timestamp_never_proves_completed_followup(self):
        for value in (None, True, float('nan'), float('inf'), '201'):
            self.client.thread['turns'][-1]['startedAt'] = value
            self.assertIsNotNone(quota_failure(self.client.thread))
        self.client.thread['turns'][-1]['startedAt'] = 201
        self.client.thread['goal']['updatedAt'] = float('nan')
        self.assertIsNotNone(quota_failure(self.client.thread))
    async def test_same_id_legacy_waiting_cancelled_before_cooldown(self):
        self.client.thread['turns'][-1]['startedAt'] = 201
        self.store.enqueue('s', 'old', 'usageLimitExceeded', 'a')
        self.store.update('s', 'old', 'waiting', retry_at=time.time()+900)
        await Watchdog(self.store, self.client).scan()
        self.assertEqual(self.store.thread_jobs('s')[0]['status'], 'superseded')
    async def test_old_goal_revision_does_not_cancel_budget_or_smoke(self):
        self.client.thread['turns'][-1]['startedAt'] = 201
        for code in ('relayBudgetPause','relayTestCompleted'):
            self.store.db.execute('DELETE FROM jobs')
            self.store.enqueue('s', 'old', code, 'a')
            await Watchdog(self.store, self.client).scan()
            self.assertEqual(self.store.thread_jobs('s')[0]['status'], 'waiting')
    async def test_cooldown_uses_full_thread_history(self):
        self.attempted()
        for i in range(105): self.store.enqueue('other', str(i), 'usageLimitExceeded', 'a')
        self.client.thread['turns'] = [{'id': 'new', 'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}}]
        async def available(): return {'accountId': 'a', 'ordinaryUsageAllowed': True, 'rateLimits': {'primary': {'usedPercent': 0}}}
        self.client.limits = available
        watcher = Watchdog(self.store, self.client)
        await watcher.scan(); await watcher.process_queue()
        latest = next(j for j in self.store.thread_jobs('s') if j['failed_turn_id']=='new')
        self.assertEqual(latest['status'], 'waiting')
        self.assertIn('cooldown',latest['detail'])
    async def test_paused_goal_never_recovered(self):
        self.client.thread['goal']['status'] = 'paused'
        self.client.thread['turns'][-1].update(status='failed', error={'codexErrorInfo':'usageLimitExceeded'})
        self.store.enqueue('s', 'old', 'usageLimitExceeded', 'a')
        async def available(): return {'accountId': 'a', 'ordinaryUsageAllowed': True, 'rateLimits': {'primary': {'usedPercent': 0}}}
        self.client.limits = available
        await Watchdog(self.store, self.client).process_queue()
        self.assertEqual(self.store.thread_jobs('s')[0]['status'],'superseded')
    async def goal_client(self):
        self.client.goal_control = True
        self.client.calls = []
        async def available(): return {'accountId': 'a', 'ordinaryUsageAllowed': True, 'rateLimits': {'primary': {'usedPercent': 0}}}
        async def resume(tid, **kw):
            self.client.calls.append((tid,kw))
            return {'goal_confirmed':True,'turn':{'id':'goal-new'}}
        self.client.limits = available
        self.client.resume_goal = resume
        return Watchdog(self.store,self.client)
    async def test_goal_adopts_old_message_episode_without_reusing_attempt(self):
        self.attempted()
        self.client.thread['turns']=[{'id':'old','status':'completed','startedAt':201}]
        watcher=await self.goal_client()
        await watcher.once()
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.calls[0][1]['expected_turn_id'],'old')
        jobs=self.store.thread_jobs('s')
        self.assertEqual(len(jobs),2)
        self.assertEqual(next(j for j in jobs if j['failed_turn_id']=='old')['attempts'],1)
        synthetic=next(j for j in jobs if j['failed_turn_id'].startswith('goal:'))
        self.assertEqual(synthetic['attempts'],1)
        self.assertEqual(synthetic['status'],'done')
        await watcher.once()
        self.assertEqual(len(self.client.calls),1)
        self.store.close();self.store=Store(self.path)
        await Watchdog(self.store,self.client).once()
        self.assertEqual(len(self.client.calls),1)
    async def test_goal_migration_cancels_unattempted_ordinary_waiting(self):
        self.store.enqueue('s','old','usageLimitExceeded','a')
        watcher=await self.goal_client()
        await watcher.once()
        self.assertEqual(len(self.client.calls),1)
        old=next(j for j in self.store.thread_jobs('s') if j['failed_turn_id']=='old')
        self.assertEqual(old['status'],'superseded')
        self.assertEqual(old['attempts'],0)
    async def test_goal_uncertain_attempt_never_replayed(self):
        watcher=await self.goal_client()
        async def fail(*a,**kw): raise RuntimeError('uncertain')
        self.client.resume_goal=fail
        await watcher.once()
        job=self.store.thread_jobs('s')[0]
        self.assertEqual(job['attempts'],1)
        self.assertEqual(job['status'],'needs_attention')
        await watcher.once()
        self.assertEqual(len(self.store.thread_jobs('s')),1)
        self.assertEqual(self.store.thread_jobs('s')[0]['attempts'],1)
    async def test_goal_deleted_paused_or_changed_before_action_cancels(self):
        watcher=await self.goal_client()
        for replacement in (None, {'status':'paused'}, {'status':'complete'}, {'status':'blocked'}, {'status':'usageLimited','objective':'different'}):
            self.store.db.execute('DELETE FROM jobs')
            original=self.client.thread['goal']
            self.client.thread['goal']={'threadId':'s','createdAt':100,'updatedAt':200,'objective':'work','status':'usageLimited'}
            await watcher.scan()
            self.client.thread['goal']=replacement
            await watcher.process_queue()
            self.assertFalse(self.client.calls)
            self.assertEqual(self.store.thread_jobs('s')[0]['status'],'superseded')
            self.client.thread['goal']=original
    async def test_new_goal_identity_has_own_recovery(self):
        watcher=await self.goal_client()
        await watcher.once()
        self.client.thread['goal']['createdAt']=300
        # Cooldown remains valid across actual Goal actions, but enrollment is independent.
        await watcher.scan()
        self.assertEqual(len(self.store.thread_jobs('s')),2)
        self.assertEqual(sum(j['status']=='waiting' for j in self.store.thread_jobs('s')),1)
    async def test_goal_optin_required_and_pending_or_running_excluded(self):
        watcher=await self.goal_client()
        self.client.goal_control=False
        self.client.thread['turns'][-1]['startedAt']=201
        await watcher.scan()
        self.assertFalse(self.store.thread_jobs('s'))
        self.client.goal_control=True
        self.client.thread['requests']=[{'id':'approval'}]
        await watcher.scan()
        self.assertFalse(self.store.thread_jobs('s'))
        self.client.thread['requests']=[]
        self.client.thread['status']='active'
        await watcher.scan()
        self.assertFalse(self.store.thread_jobs('s'))
