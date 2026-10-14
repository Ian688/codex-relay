import asyncio,copy,json,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from codex_autoresume.polling import polling_settings,thread_plan,schedule_plan
from codex_autoresume.daemon import Watchdog
from codex_autoresume.store import Store
from codex_autoresume.gui_service import RelayService
from test_daemon import FakeClient
from test_gui_service import GUIClient

def waiting_record(tid='b',mode='reset',baseline=1000):
 return {'thread_id':tid,'check':{'turn_id':'paused-turn','turn_status':'interrupted','runtime_status':'idle','goal_status':'paused'},
         'policy':{'mode':'reserve','resume_mode':mode,'window':'primary','stop_remaining':20,'resume_remaining':50},
         'jobs':[{'thread_id':tid,'failed_turn_id':'paused-turn','status':'waiting','attempts':0,'error_code':'relayBudgetPause','context':json.dumps({'reset_baseline':{'primary':baseline}})}]}
def limits(used=70,reset=1000):
 return {'ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':used,'resetsAt':reset}}}

class SchedulingContract(unittest.TestCase):
 def test_reserve_pause_waits_slowly_even_with_usable_account_quota(self):
  p=thread_plan(polling_settings(),waiting_record(),limits(),100)
  self.assertEqual(p['mode'],'reset_wait');self.assertEqual(p['next_check_at'],400)
 def test_strict_reset_wait_uses_original_baseline_not_moved_future_window(self):
  p=thread_plan(polling_settings({'early_reset_checks':False}),waiting_record(),limits(10,2000),100)
  self.assertEqual(p['next_check_at'],1002)
 def test_multiple_exhausted_windows_wait_for_latest_blocking_reset(self):
  q=limits(100,1000);q['rateLimits']['secondary']={'usedPercent':100,'resetsAt':2000}
  p=thread_plan(polling_settings({'early_reset_checks':False}),waiting_record(),q,100)
  self.assertEqual(p['next_check_at'],2032)
 def test_past_or_missing_deadlines_never_create_busy_loop(self):
  for baseline in [10,None,float('nan')]:
   p=thread_plan(polling_settings({'early_reset_checks':False}),waiting_record(baseline=baseline),limits(70,10),100)
   self.assertEqual(p['delay_seconds'],300)
 def test_remaining_threshold_requires_periodic_observation(self):
  p=thread_plan(polling_settings({'early_reset_checks':False}),waiting_record(mode='remaining'),limits(),100)
  self.assertEqual(p['mode'],'remaining_wait');self.assertEqual(p['delay_seconds'],300)
 def test_manual_pause_only_checks_status_without_recovery_job(self):
  r=waiting_record();r['jobs']=[]
  self.assertEqual(thread_plan(polling_settings(),r,limits(),100)['mode'],'manual_pause')
 def test_mixed_workload_preserves_waiting_deadline_across_active_checks(self):
  a={'thread_id':'a','check':{'turn_status':'inProgress'},'jobs':[]}
  s=polling_settings({'early_reset_checks':False});p=schedule_plan(s,[a,waiting_record()],limits(),now=100)
  for now in [130,160,190,220]:
   p=schedule_plan(s,[a,waiting_record()],limits(),previous=p['threads'],checked_ids={'a'},now=now)
   self.assertEqual(p['threads']['b']['next_check_at'],1002)
   self.assertEqual(p['next_check_at'],now+30)
 def test_one_unavailable_thread_does_not_slow_healthy_running_work(self):
  a={'thread_id':'a','check':{'turn_status':'inProgress'}}
  b={'thread_id':'b','check':{'error':'offline'},'failures':5}
  p=schedule_plan(polling_settings(),[a,b],limits(),now=100)
  self.assertEqual(p['threads']['b']['delay_seconds'],300);self.assertEqual(p['delay_seconds'],30)
 def test_invalid_early_reset_option_is_rejected(self):
  with self.assertRaises(ValueError):polling_settings({'early_reset_checks':'false'})

class RecoveryRechecks(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'s.db');self.store.watch('s1','acct-A')
 async def asyncTearDown(self):self.store.close();self.tmp.cleanup()
 async def test_early_reset_rechecks_untouched_job_before_old_retry_timestamp(self):
  c=FakeClient(thread_status='idle');self.store.enqueue('s1','failed-t1','usageLimitExceeded','acct-A')
  self.store.update('s1','failed-t1','waiting',retry_at=time.time()+3600,detail='quota exhausted: primary')
  w=Watchdog(self.store,c);await w.process_queue()
  self.assertFalse(c.calls)
  await w.process_queue(recheck_waiting=True)
  self.assertEqual(c.calls[-1],('start','s1','继续'));self.assertEqual(self.store.jobs()[0]['attempts'],1)
 async def test_early_quota_availability_does_not_override_next_reset_policy(self):
  c=FakeClient(thread_status='idle');self.store.set_strategy('s1',mode='reserve',resume_mode='reset')
  self.store.enqueue('s1','failed-t1','usageLimitExceeded','acct-A')
  self.store.merge_context_once('s1','failed-t1','reset_baseline',{'primary':2000000000})
  await Watchdog(self.store,c).process_queue(recheck_waiting=True)
  self.assertFalse(c.calls);self.assertEqual(self.store.jobs()[0]['attempts'],0)
 async def test_uncertain_submission_is_never_replayed_by_early_recheck(self):
  c=FakeClient(thread_status='idle');self.store.enqueue('s1','failed-t1','usageLimitExceeded','acct-A')
  self.store.reserve('s1','failed-t1');self.store.update('s1','failed-t1','needs_attention')
  await Watchdog(self.store,c).process_queue(recheck_waiting=True)
  self.assertFalse(c.calls);self.assertEqual(self.store.jobs()[0]['attempts'],1)
 async def test_non_due_threads_are_not_read_when_another_thread_is_active(self):
  self.store.watch('b','acct-A');c=FakeClient();calls=[]
  original=c.read_thread
  async def read(tid):calls.append(tid);return await original(tid)
  c.read_thread=read;w=Watchdog(self.store,c);w.check_ids={'s1'}
  await w.once();self.assertNotIn('b',calls)
 async def test_remembered_monitor_view_excludes_full_transcript(self):
  w=Watchdog(self.store,FakeClient());t={'id':'s1','status':'active','turns':[{'id':'t','status':'inProgress','items':[{'text':'large private transcript'}]}]}
  w.remember(t)
  self.assertNotIn('items',w.latest_threads['s1']['turns'][0])

 # Remaining in this class: no automatic quota reads for a manually paused Goal.
 async def test_manual_goal_pause_status_probe_does_not_read_quota(self):
  self.store.set_strategy('s1',mode='reserve',stop_remaining=20)
  c=FakeClient(thread_status='idle');c.thread['goal']={'status':'paused'}
  c.thread['turns']=[{'id':'t','status':'interrupted'}];calls=[];original=c.limits
  async def counted():calls.append(1);return await original()
  c.limits=counted;w=Watchdog(self.store,c);w.check_ids={'s1'}
  await w.once();self.assertEqual(calls,[])
  c.thread['status']='active';c.thread['goal']['status']='active';c.thread['turns'][-1]['status']='inProgress'
  await w.once();self.assertEqual(len(calls),1)
 async def test_early_recheck_still_respects_anti_loop_cooldown(self):
  c=FakeClient(thread_status='idle');self.store.enqueue('s1','old','usageLimitExceeded','acct-A')
  self.store.reserve('s1','old');self.store.update('s1','old','done')
  self.store.enqueue('s1','failed-t1','usageLimitExceeded','acct-A')
  await Watchdog(self.store,c).process_queue(recheck_waiting=True)
  self.assertFalse(c.calls)
  job=next(j for j in self.store.jobs() if j['failed_turn_id']=='failed-t1')
  self.assertEqual(job['attempts'],0);self.assertIn('cooldown',job['detail'])

class CacheAndWake(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=GUIClient();self.s=RelayService(Path(self.tmp.name)/'s.db',self.c)
 async def asyncTearDown(self):await self.s.close();self.tmp.cleanup()
 async def test_automatic_ui_refresh_never_requeries_codex_even_after_old_ttl(self):
  await self.s.state();later=time.time()+3600
  async def forbidden(*args,**kw):raise AssertionError('Automatic display attempted a Codex RPC')
  self.c.limits=self.c.read_thread=self.c.list_threads=self.c.start=forbidden
  with patch('codex_autoresume.gui_service.time.time',return_value=later):
   for _ in range(3):self.assertTrue((await self.s.dispatch('state',{'force':False}))['cached'])
 async def test_manual_refresh_wakes_scheduled_eligibility_check(self):
  await self.s.state();self.s.monitoring=True;self.s.force_monitor_check=False
  await self.s.dispatch('state',{'force':True})
  self.assertTrue(self.s.force_monitor_check);self.assertTrue(self.s.wake.is_set());self.s.monitoring=False
 async def test_cached_render_keeps_actual_quota_observation_time(self):
  v=await self.s.state();at=v['quota_checked_at']
  cached=await self.s.state(force=False);self.assertEqual(cached['quota_checked_at'],at)
 async def test_manual_quota_read_wins_over_an_older_monitor_snapshot(self):
  await self.s.state();self.s.watcher.last_limits=limits(99);self.s.watcher.last_limits_at=self.s._quota_observed_at-5
  self.assertNotEqual((await self.s.state(force=False))['quotas']['rateLimits']['primary']['usedPercent'],99)

class PauseWaitIntegration(unittest.IsolatedAsyncioTestCase):
 async def test_actual_reserve_pause_enters_deadline_sleep_and_ui_makes_no_rpc(self):
  from test_relay import BudgetClient
  class Client(BudgetClient):
   def __init__(self):super().__init__();self.connected=False;self.reads=0;self.quota_reads=0
   async def start(self):self.connected=True
   async def close(self):self.connected=False
   async def read_thread(self,tid):self.reads+=1;return await super().read_thread(tid)
   async def limits(self):
    self.quota_reads+=1;data=await super().limits();data['rateLimits']['primary']['resetsAt']=deadline;return data
  deadline=time.time()+3600
  with tempfile.TemporaryDirectory() as d:
   c=Client();s=RelayService(Path(d)/'s.db',c)
   try:
    s.store.watch('s1','acct-A');s.store.set_strategy('s1',mode='reserve',resume_mode='reset')
    s.store.set_setting('polling',polling_settings({'early_reset_checks':False}))
    await s.state();s.monitoring=True;observed=[]
    async def sleep_without_waiting(awaitable,timeout):
     awaitable.close()
     self.assertEqual(s.schedule['mode'],'reset_wait')
     self.assertGreater(timeout,3500)
     count=(c.reads,c.quota_reads)
     for _ in range(6):
      rendered=await s.dispatch('state',{'force':False})
      self.assertEqual(rendered['threads'][0]['observation']['schedule']['mode'],'reset_wait')
     self.assertEqual((c.reads,c.quota_reads),count)
     observed.append(s.next_check_at);s.monitoring=False
    with patch('codex_autoresume.gui_service.asyncio.wait_for',side_effect=sleep_without_waiting):
     await s.monitor_loop()
    self.assertEqual(len(observed),1)
    self.assertAlmostEqual(observed[0],deadline+2,delta=.1)
    self.assertEqual(c.calls,[('interrupt','s1','active-t1')])
    self.assertEqual(s.store.jobs()[0]['attempts'],0)
   finally:await s.close()
