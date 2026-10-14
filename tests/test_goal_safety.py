import tempfile
import unittest
from pathlib import Path
from codex_autoresume.store import Store
from codex_autoresume.daemon import Watchdog
from codex_autoresume.observations import describe
from test_relay import BudgetClient
from test_daemon import FakeClient

class GoalSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db');self.store.watch('s1','acct-A')
    def tearDown(self):self.store.close();self.tmp.cleanup()
    async def test_active_goal_not_paused_when_no_confirmed_resume_channel(self):
        self.store.set_strategy('s1',mode='reserve',stop_remaining=20)
        c=BudgetClient();c.thread['goal']=dict(status='active',objective='real work')
        await Watchdog(self.store,c).once()
        self.assertFalse(c.calls);self.assertEqual(c.thread['goal']['status'],'active')
        v=describe(c.thread,self.store.policy('s1'),[],limits=await c.limits(),monitoring=True,selected=True)
        self.assertEqual(v['relay_status'],'manual_goal')
    async def test_native_goal_unavailable_no_ordinary_message_substitute(self):
        c=FakeClient();c.thread['goal']=dict(status='usageLimited',objective='work')
        await Watchdog(self.store,c).once()
        self.assertFalse(c.calls)
        j=self.store.jobs()[0];self.assertEqual(j['attempts'],0);self.assertEqual(j['status'],'needs_attention')
        self.assertEqual(self.store.events()[0]['action'],'goal_manual')
    async def test_user_pause_cancels_pending_restore_instead_of_manual_restore_prompt(self):
        c=FakeClient(quota_ready=False);c.thread['goal']=dict(status='usageLimited',objective='work')
        w=Watchdog(self.store,c);await w.once()
        c.thread['goal']['status']='paused';c.quota_ready=True
        self.store.update('s1','failed-t1','waiting',retry_at=0);await w.once()
        self.assertFalse(c.calls);self.assertEqual(self.store.jobs()[0]['status'],'superseded')
    async def test_ordinary_resume_receipt_persisted(self):
        c=FakeClient();await Watchdog(self.store,c).once()
        import json
        context=json.loads(self.store.jobs()[0]['context']);self.assertEqual(context['action_kind'],'message')
        self.assertEqual(context['action_receipt']['turn_id'],'new-turn')

    async def test_goal_uses_native_resume_and_never_ordinary_continue(self):
        class GoalClient(FakeClient):
            goal_control=True
            async def resume_goal(inner,tid,expected_turn_id,expected_goal):
                self.assertEqual(expected_turn_id,'failed-t1')
                inner.calls.append(('goal_resume',tid))
                inner.thread['goal']={**expected_goal,'status':'active','updatedAt':999}
                inner.thread['turns'].append(dict(id='goal-turn',status='inProgress'))
                inner.thread['status']='active'
                return dict(goal_confirmed=True,turn=dict(id='goal-turn',status='inProgress'))
        c=GoalClient();c.thread['goal']=dict(status='usageLimited',objective='work',createdAt=10,updatedAt=20)
        w=Watchdog(self.store,c);await w.once();await w.once()
        self.assertEqual(c.calls,[('goal_resume','s1')])
        import json
        job=self.store.jobs()[0];self.assertEqual(job['attempts'],1)
        self.assertEqual(json.loads(job['context'])['action_kind'],'goal_resume')
    async def test_beta_does_not_budget_pause_goal_even_when_resume_enabled(self):
        self.store.set_strategy('s1',mode='reserve',stop_remaining=20)
        c=BudgetClient();c.goal_control=True;c.thread['goal']=dict(status='active',objective='work')
        await Watchdog(self.store,c).once();self.assertFalse(c.calls)

    async def test_uncertain_goal_action_never_replayed_or_rearmed(self):
        from codex_autoresume.desktop import GoalControlError
        class GoalClient(FakeClient):
            goal_control=True
            async def resume_goal(inner,*args,**kwargs):
                inner.calls.append(('goal_attempt',))
                raise GoalControlError('执行待确认',action_submitted=True,reason_code='goal_execution_unconfirmed')
        c=GoalClient();c.thread['goal']=dict(status='usageLimited',objective='work',createdAt=10,updatedAt=20)
        w=Watchdog(self.store,c);await w.once();await w.once()
        self.assertEqual(c.calls,[('goal_attempt',)])
        job=self.store.jobs()[0];self.assertEqual(job['status'],'needs_attention');self.assertEqual(job['attempts'],1)
        import json
        self.assertTrue(json.loads(job['context'])['action_failure']['submitted'])

    async def test_changed_goal_cannot_restore_old_job(self):
        from codex_autoresume.rules import job_safe,goal_episode
        import json
        c=FakeClient();c.thread['goal']=dict(status='usageLimited',objective='old work',createdAt=10,updatedAt=20)
        job=dict(failed_turn_id='failed-t1',error_code='usageLimitExceeded',context=json.dumps(dict(goal_episode=goal_episode(c.thread))))
        self.assertTrue(job_safe(c.thread,job))
        c.thread['goal']['objective']='new work'
        self.assertFalse(job_safe(c.thread,job))
