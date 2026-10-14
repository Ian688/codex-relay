import io,tempfile,time,unittest
from pathlib import Path
from codex_autoresume.polling import polling_settings,next_delay
from codex_autoresume.gui_service import RelayService,write_response
from test_gui_service import GUIClient
class PollingTest(unittest.TestCase):
 def test_defaults_and_ranges(self):
  self.assertEqual(polling_settings()['check_seconds'],30)
  for bad in [True,9,301,float('nan'),30.5]:
   with self.assertRaises(ValueError):polling_settings({'check_seconds':bad})
 def test_idle_exhaustion_slows_until_near_reset(self):
  s=polling_settings();limits={'rateLimits':{'primary':{'usedPercent':100,'resetsAt':2000}}}
  self.assertEqual(next_delay(s,[{'turn_status':'failed'}],limits,now=1000),300)
  self.assertEqual(next_delay(s,[],limits,now=1990),42)
 def test_active_tasks_keep_normal_interval(self):
  self.assertEqual(next_delay(polling_settings(),[{'turn_status':'inProgress'}],{'rateLimits':{'primary':{'usedPercent':100}}},now=0),30)
 def test_error_backoff_is_bounded(self):
  self.assertEqual([next_delay(polling_settings(),[],None,n) for n in [1,2,3,4,5,30]],[30,60,120,240,300,300])
 def test_closed_ui_pipe_is_normal_shutdown(self):
  class Closed:
   def write(self,value):raise BrokenPipeError()
  self.assertFalse(write_response({'id':1},Closed()))
  stream=io.StringIO();self.assertTrue(write_response({'ok':True},stream));self.assertIn('true',stream.getvalue())
class ServicePollingTest(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=GUIClient();self.s=RelayService(Path(self.tmp.name)/'s.db',self.c)
 async def asyncTearDown(self):
  await self.s.close();self.tmp.cleanup()
 async def test_automatic_ui_uses_cache_manual_refresh_reads(self):
  calls=[];original=self.c.limits
  async def limits():calls.append(1);return await original()
  self.c.limits=limits
  await self.s.dispatch('state',{'force':True});count=len(calls)
  response=await self.s.dispatch('state',{'force':False})
  self.assertTrue(response['cached']);self.assertEqual(len(calls),count)
  await self.s.dispatch('state',{'force':True});self.assertGreater(len(calls),count)
 async def test_settings_persist_and_wake_wait(self):
  await self.s.dispatch('polling',{'check_seconds':45,'ui_seconds':90,'quota_wait_seconds':600})
  self.assertEqual(self.s.store.setting('polling')['quota_wait_seconds'],600)
  self.assertTrue(self.s.wake.is_set())

 async def test_quota_rpc_failure_is_not_reported_as_successful_cycle(self):
  self.s.store.watch('s1','acct-A')
  self.s.last_cycle=123
  async def failed_quota():
   self.s.store.record_check('s1',turn_status='failed')
   self.s.watcher.last_quota_error='quota RPC timeout'
   self.s.monitoring=False
  self.s.watcher.once=failed_quota
  self.s.monitoring=True
  await self.s.monitor_loop()
  self.assertEqual(self.s.last_cycle,123)
  self.assertEqual(self.s.poll_failures,1)
  self.assertIn('Quota read failed',self.s.last_error)
