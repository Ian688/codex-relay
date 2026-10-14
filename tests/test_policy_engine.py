import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from codex_autoresume.store import Store
from codex_autoresume.daemon import Watchdog
from codex_autoresume.rules import strategy_gate
from test_daemon import FakeClient
from test_relay import BudgetClient


class StrategyPersistence(unittest.TestCase):
    def test_legacy_migration_preserves_zero_used_as_full_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE policies(thread_id TEXT PRIMARY KEY,goal_only INTEGER,pause_used REAL,resume_used REAL,window TEXT)')
            db.execute("INSERT INTO policies VALUES('s1',1,80,0,'primary')")
            db.commit(); db.close()
            store = Store(path)
            p = store.policy('s1')
            self.assertEqual((p['mode'],p['stop_remaining'],p['resume_mode'],p['resume_remaining']), ('reserve',20,'remaining',100))
            store.close()

    def test_future_deadline_revision_is_not_a_completed_reset(self):
        policy = dict(mode='reserve',window='primary',stop_remaining=20,resume_mode='reset')
        job = dict(context=json.dumps({'reset_baseline':{'primary':1000}}))
        limits = {'rateLimits':{'primary':{'usedPercent':5,'resetsAt':2000}}}
        self.assertFalse(strategy_gate(limits,policy,job,now=999)[0])
        self.assertTrue(strategy_gate(limits,policy,job,now=1001)[0])

    def test_reset_baseline_and_attempts_survive_strategy_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'db')
            store.watch('s1','acct-A')
            store.enqueue('s1','t1','usageLimitExceeded','acct-A')
            store.merge_context_once('s1','t1','reset_baseline',{'primary':100})
            store.merge_context_once('s1','t1','reset_baseline',{'primary':200})
            store.set_strategy('s1',mode='reserve')
            job = store.jobs()[0]
            self.assertEqual(json.loads(job['context'])['reset_baseline']['primary'],100)
            self.assertEqual(job['attempts'],0)
            self.assertTrue(store.reserve('s1','t1'))
            store.set_strategy('s1',mode='quota')
            self.assertEqual(store.jobs()[0]['attempts'],1)
            store.close()


class StrategyRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'db')
        self.store.watch('s1','acct-A')
    async def asyncTearDown(self):
        self.store.close(); self.temp.cleanup()
    async def test_genuine_quota_failure_obeys_same_remaining_restore_threshold(self):
        self.store.set_strategy('s1',mode='reserve',stop_remaining=20,resume_mode='remaining',resume_remaining=60)
        client = FakeClient()
        original = client.limits
        async def limits():
            data = await original(); data['rateLimits']['primary']['usedPercent']=50; return data
        client.limits=limits
        await Watchdog(self.store,client).once()
        self.assertFalse(client.calls)
        self.assertIn('remaining >= 60%',self.store.jobs()[0]['detail'])
    async def test_own_pause_waits_verified_reset_not_exact_zero(self):
        self.store.set_strategy('s1',mode='reserve',stop_remaining=20,resume_mode='reset')
        client = BudgetClient(); original=client.limits
        reset = [100]
        async def limits():
            data=await original(); data['rateLimits']['primary']['resetsAt']=reset[0]; return data
        client.limits=limits
        watcher=Watchdog(self.store,client)
        await watcher.once()
        client.used=5
        self.store.update('s1','active-t1','waiting',retry_at=0)
        await watcher.once()
        self.assertEqual(len(client.calls),1)
        reset[0]=200
        self.store.update('s1','active-t1','waiting',retry_at=0)
        await watcher.once()
        self.assertEqual(client.calls[-1],('start','s1','继续'))
    async def test_rescan_does_not_replace_failure_reset_baseline(self):
        self.store.set_strategy('s1',mode='reserve',resume_mode='reset')
        client=FakeClient(quota_ready=False); watcher=Watchdog(self.store,client)
        await watcher.once()
        baseline=json.loads(self.store.jobs()[0]['context'])['reset_baseline']
        client.quota_ready=True
        original=client.limits
        async def limits():
            data=await original(); data['rateLimits']['primary']['resetsAt']+=100; return data
        client.limits=limits
        self.store.update('s1','failed-t1','waiting',retry_at=0)
        with patch('codex_autoresume.rules.time.time', return_value=baseline['primary']+1):
            await watcher.once()
        self.assertEqual(json.loads(self.store.jobs()[0]['context'])['reset_baseline'],baseline)
        self.assertEqual(client.calls[-1],('start','s1','继续'))
    async def test_secondary_exhaustion_blocks_primary_strategy_recovery(self):
        self.store.set_strategy('s1',mode='reserve',resume_mode='remaining')
        client=FakeClient(); original=client.limits
        async def limits():
            data=await original(); data['rateLimits']['secondary']={'usedPercent':100,'resetsAt':2000000000}; return data
        client.limits=limits
        await Watchdog(self.store,client).once()
        self.assertFalse(client.calls)
