import asyncio
from pathlib import Path
import tempfile
import unittest
from codex_autoresume.gui_service import RelayService
from codex_autoresume.lease import MonitorLease
from test_daemon import FakeClient

class GUIClient(FakeClient):
    def __init__(self):
        super().__init__(thread_status='idle')
        self.connected = False
        self.thread['name'] = '界面测试会话'
        self.thread['model'] = 'gpt-6-luna'
        self.thread['turns'][-1]['status'] = 'completed'
    async def start(self): self.connected = True
    async def close(self): self.connected = False
    async def list_threads(self, **kwargs): return [{'id':'s1','name':'界面测试会话','cwd':'/tmp/test','status':{'type':'notLoaded'}}]

class GUIServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = GUIClient()
        self.service = RelayService(Path(self.temp.name)/'gui.sqlite3',self.client)
    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()
    async def test_startup_is_read_only_and_paused(self):
        state = await self.service.dispatch('state',{})
        self.assertTrue(state['connected'])
        self.assertFalse(state['monitoring'])
        self.assertEqual(state['watched_count'],0)
        self.assertEqual(self.client.calls,[])
    async def test_choose_policy_start_and_pause(self):
        state = await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        self.assertEqual(state['watched_count'],1)
        self.assertEqual(state['threads'][0]['status'],'idle')
        await self.service.dispatch('policy',{'thread_id':'s1','budget_enabled':True,'pause_used':80,'resume_used':50,'goal_only':True})
        self.assertEqual(self.service.store.policy('s1')['pause_used'],80)
        self.assertFalse(self.service.monitoring)
        await self.service.dispatch('monitor',{'enabled':True})
        state = await self.service.dispatch('monitor',{'enabled':False})
        self.assertFalse(state['monitoring'])
        self.assertEqual(self.client.calls,[])
    async def test_canonical_strategy_roundtrip_and_validation(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        state = await self.service.dispatch('policy',{'thread_id':'s1','mode':'reserve','stop_remaining':20,'resume_mode':'reset','resume_remaining':50,'goal_only':True})
        policy = state['threads'][0]['policy']
        self.assertEqual((policy['mode'],policy['resume_mode'],policy['stop_remaining']),('reserve','reset',20))
        with self.assertRaises(ValueError):
            await self.service.dispatch('policy',{'thread_id':'s1','mode':'reserve','stop_remaining':50,'resume_mode':'remaining','resume_remaining':20})
        self.assertEqual(self.service.store.policy('s1')['resume_mode'],'reset')
        await self.service.dispatch('policy',{'thread_id':'s1','mode':'quota'})
        self.assertEqual(self.service.store.policy('s1')['mode'],'quota')
        self.assertFalse(self.client.calls)

    async def test_unselect_deactivates_waiting_jobs(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        self.service.store.enqueue('s1','failed','usageLimitExceeded','acct-A')
        state = await self.service.dispatch('watch',{'thread_id':'s1','enabled':False})
        self.assertEqual(state['watched_count'],0)
        self.assertEqual(self.service.store.jobs()[0]['status'],'disabled')
    async def test_unavailable_selected_session_is_visible(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        async def unavailable(tid): raise RuntimeError('no-client-found')
        self.client.read_thread = unavailable
        state = await self.service.dispatch('state',{})
        self.assertEqual(state['unavailable_count'],1)
        self.assertEqual(state['threads'][0]['status'],'unavailable')
        self.assertIn('Codex',state['threads'][0]['live_error'])
    async def test_another_controller_is_visible_and_blocks_start(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        other = MonitorLease(self.service.db_path)
        other.acquire()
        try:
            state = await self.service.dispatch('state',{})
            self.assertTrue(state['external_monitor'])
            with self.assertRaises(RuntimeError): await self.service.dispatch('monitor',{'enabled':True})
            self.assertFalse(self.service.monitoring)
        finally: other.release()
    async def test_pausing_releases_controller_lease(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        await self.service.dispatch('monitor',{'enabled':True})
        probe = MonitorLease(self.service.db_path)
        self.assertTrue(probe.busy())
        await self.service.dispatch('monitor',{'enabled':False})
        self.assertFalse(probe.busy())
    async def test_start_requires_selection(self):
        with self.assertRaises(ValueError): await self.service.dispatch('monitor',{'enabled':True})
        self.assertFalse(self.service.monitoring)
    async def test_unknown_account_rejects_selection(self):
        self.client.owner = None
        with self.assertRaises(ValueError): await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        self.assertEqual(self.service.store.watched(),[])
    async def test_policy_rejects_invalid_range(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        with self.assertRaises(ValueError):
            await self.service.dispatch('policy',{'thread_id':'s1','budget_enabled':True,'pause_used':50,'resume_used':80})
        self.assertIsNone(self.service.store.policy('s1')['pause_used'])
    async def test_pause_waits_for_inflight_resume(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.client.thread['turns'][-1]['status'] = 'failed'
        async def start(tid,message):
            entered.set()
            await release.wait()
            self.client.calls.append(('start',tid,message))
            return {'turn':{'id':'new-turn'}}
        self.client.start_turn = start
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        await self.service.dispatch('monitor',{'enabled':True})
        await asyncio.wait_for(entered.wait(),3)
        pause = asyncio.create_task(self.service.dispatch('monitor',{'enabled':False}))
        await asyncio.sleep(.02)
        self.assertFalse(pause.done())
        release.set()
        state = await asyncio.wait_for(pause,3)
        self.assertFalse(state['monitoring'])
        self.assertEqual(sum(c[0]=='start' for c in self.client.calls),1)
    async def test_start_disables_bulk_selection(self):
        self.service.store.set_setting('watch_all',True)
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        await self.service.dispatch('monitor',{'enabled':True})
        self.assertFalse(self.service.store.setting('watch_all'))
        await self.service.dispatch('monitor',{'enabled':False})

    async def test_ui_refresh_does_not_claim_monitor_check(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        state=await self.service.dispatch('state',{})
        self.assertIsNone(state['threads'][0]['observation']['checked_at'])
        self.assertIsNone(state['last_cycle'])

    async def test_full_history_attention_count(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        self.service.store.enqueue('s1','older','usageLimitExceeded','acct-A')
        self.service.store.update('s1','older','needs_attention')
        for n in range(105): self.service.store.enqueue('other',str(n),'usageLimitExceeded','acct-A')
        state=await self.service.dispatch('state',{})
        self.assertEqual(state['attention_count'],1)

    async def test_failed_cycle_keeps_previous_success_time(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        self.service.last_cycle=123
        async def failed_scan():
            self.service.store.record_check('s1',error='offline')
            self.service.monitoring=False
        self.service.watcher.once=failed_scan
        self.service.monitoring=True
        await self.service.monitor_loop()
        self.assertEqual(self.service.last_cycle,123)
        self.assertIsNotNone(self.service.last_error)

    async def test_goal_control_optin_is_persisted_and_permission_prompt_explicit(self):
        calls=[]
        async def configure(enabled,request_permission=False):
            calls.append((enabled,request_permission))
            self.client.goal_control=enabled
            self.client.goal_pause_control=enabled
            self.client.goal_capability=dict(enabled=enabled,trusted=True,supported=True,ready=enabled,reason='测试')
        self.client.configure_goal_control=configure
        await self.service.dispatch('state',{})
        self.assertTrue(all(not prompt for _,prompt in calls))
        await self.service.dispatch('goal_control',dict(enabled=True,request_permission=True))
        self.assertTrue(self.service.store.setting('goal_accessibility_enabled'))
        self.assertEqual(sum(prompt for _,prompt in calls),1)
        await self.service.dispatch('state',{})
        self.assertEqual(sum(prompt for _,prompt in calls),1)
        await self.service.dispatch('goal_control',dict(enabled=False))
        self.assertFalse(self.service.store.setting('goal_accessibility_enabled'))

    async def test_goal_optin_never_resets_uncertain_attempt(self):
        await self.service.dispatch('watch',{'thread_id':'s1','enabled':True})
        async def configure(enabled,request_permission=False): pass
        self.client.configure_goal_control=configure
        self.service.store.enqueue('s1','goal:attempted','usageLimitExceeded','acct-A')
        self.service.store.reserve('s1','goal:attempted')
        self.service.store.update('s1','goal:attempted','needs_attention',detail='Goal control unavailable')
        await self.service.dispatch('goal_control',dict(enabled=True))
        job=self.service.store.jobs()[0]
        self.assertEqual(job['attempts'],1);self.assertEqual(job['status'],'needs_attention')
