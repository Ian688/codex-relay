"""Offline AX adapter checks: no native UI or model requests."""
import copy
import unittest
from unittest.mock import patch
from codex_autoresume.desktop import DesktopClient, DesktopBridge, GoalControlError
from codex_autoresume.protocol import ProtocolError

GOAL = dict(threadId='thread', createdAt=1, updatedAt=2, objective='Test objective', status='usageLimited',timeUsedSeconds=1)

def snapshot(*, goal=GOAL, turn='old', status='failed', runtime='idle', requests=None):
    return dict(id='thread',name='Test thread',goal=copy.deepcopy(goal),
                turns=[dict(id=turn,status=status)],status={'type':runtime},requests=requests or [])

class Bridge:
    def __init__(self, snapshots): self.snapshots=list(snapshots)
    async def read_thread(self, tid):
        if len(self.snapshots)>1: return self.snapshots.pop(0)
        return self.snapshots[0]

class Client(DesktopClient):
    def __init__(self, snapshots=None, *, trusted=True, supported=True, clicked=True):
        super().__init__()
        self.bridge=Bridge(snapshots or [snapshot()])
        self.calls=[]
        self.probe=dict(trusted=trusted,supported=supported)
        self.click=dict(clicked=clicked,source='accessibility')
        self.goal_confirmation_timeout=0
    async def _invoke_goal_helper(self, payload):
        self.calls.append(payload)
        return self.probe if payload['action']=='probe' else self.click

class GoalAccessibilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_by_default_no_action(self):
        c=Client()
        self.assertFalse(c.goal_control)
        with self.assertRaises(ProtocolError): await c.resume_goal('thread','old',GOAL)
        self.assertEqual(c.calls,[])

    async def test_enable_needs_supported_and_trusted(self):
        for trusted,supported in [(False,True),(True,False)]:
            c=Client(trusted=trusted,supported=supported)
            result=await c.configure_goal_control(True,request_permission=True)
            self.assertFalse(c.goal_control)
            self.assertTrue(result['enabled'])
            self.assertTrue(c.calls[0]['request_permission'])
        c=Client();await c.configure_goal_control(False,request_permission=True)
        self.assertFalse(c.calls[0]['request_permission'])

    async def test_probe_error_blocks_control_even_when_trusted(self):
        c=Client();c.probe['error']='codex_not_running_or_ambiguous'
        await c.configure_goal_control(True)
        self.assertFalse(c.goal_control)
        with self.assertRaises(ProtocolError): await c.resume_goal('thread','old',GOAL)
        self.assertNotIn('resume',[p['action'] for p in c.calls])

    async def test_reprobe_permission_revoked_blocks_click(self):
        c=Client();await c.configure_goal_control(True)
        c.probe['trusted']=False
        with self.assertRaises(ProtocolError): await c.resume_goal('thread','old',GOAL)
        self.assertNotIn('resume',[p['action'] for p in c.calls])

    async def test_changed_snapshot_busy_and_invalid_goal_block(self):
        variants=[snapshot(turn='other'),snapshot(goal={**GOAL,'updatedAt':3}),
                  snapshot(runtime='active'),snapshot(requests=[{'id':'pending'}]),
                  snapshot(goal={**GOAL,'status':'complete'})]
        for state in variants:
            c=Client([state]);await c.configure_goal_control(True)
            expected=state['goal'] if state['goal']['status']=='complete' else GOAL
            with self.assertRaises(ProtocolError): await c.resume_goal('thread','old',expected)
            self.assertNotIn('resume',[p['action'] for p in c.calls])

    async def test_click_requires_new_turn_and_owner_active(self):
        after=snapshot(goal={**GOAL,'status':'active'},turn='new',status='inProgress',runtime='active')
        c=Client([snapshot(),after]);await c.configure_goal_control(True)
        result=await c.resume_goal('thread','old',GOAL)
        self.assertTrue(result['goal_confirmed'])
        self.assertEqual(result['turn']['id'],'new')
        action=c.calls[-1]
        self.assertEqual(action['objective'],GOAL['objective'])
        self.assertEqual(action['thread_id'],'thread')
        self.assertEqual(action['expected_goal_status'],'usageLimited')
        self.assertEqual(action['method'],'verified_click')
        self.assertEqual(action['expected_time_used_seconds'],1)

    async def test_metadata_active_alone_and_click_ack_not_success(self):
        for after in [snapshot(),snapshot(goal={**GOAL,'status':'active'})]:
            c=Client([snapshot(),after]);await c.configure_goal_control(True)
            with self.assertRaisesRegex(GoalControlError,'尚未确认') as failure:
                await c.resume_goal('thread','old',GOAL)
            self.assertTrue(failure.exception.action_submitted)
            self.assertEqual(failure.exception.reason_code,'goal_execution_unconfirmed')
            self.assertEqual(sum(p['action']=='resume' for p in c.calls),1)

    async def test_new_turn_with_old_paused_goal_not_success(self):
        c=Client([snapshot(),snapshot(goal={**GOAL,'status':'paused'},turn='new',status='completed')])
        await c.configure_goal_control(True)
        with self.assertRaisesRegex(GoalControlError,'尚未确认') as failure: await c.resume_goal('thread','old',GOAL)

    async def test_quick_complete_or_clear_requires_new_completed_turn(self):
        for goal in [None,{**GOAL,'status':'complete'}]:
            c=Client([snapshot(),snapshot(goal=goal,turn='new',status='completed')])
            await c.configure_goal_control(True)
            result=await c.resume_goal('thread','old',GOAL)
            self.assertTrue(result['goal_confirmed'])
        c=Client([snapshot(),snapshot(goal=None)]);await c.configure_goal_control(True)
        with self.assertRaises(ProtocolError): await c.resume_goal('thread','old',GOAL)

    async def test_replaced_goal_and_failed_helper_not_success(self):
        c=Client([snapshot(),snapshot(goal={**GOAL,'createdAt':99,'status':'active'},turn='new',status='inProgress')])
        await c.configure_goal_control(True)
        with self.assertRaisesRegex(GoalControlError,'目标发生变化') as failure: await c.resume_goal('thread','old',GOAL)
        self.assertTrue(failure.exception.action_submitted)
        c=Client(clicked=False);c.click['error']='goal_resume_control_not_verified'
        await c.configure_goal_control(True)
        with self.assertRaises(GoalControlError) as failure: await c.resume_goal('thread','old',GOAL)
        self.assertFalse(failure.exception.action_submitted)
        self.assertEqual(failure.exception.reason_code,'goal_resume_control_not_verified')

    async def test_postclick_read_failure_is_submitted(self):
        c=Client();await c.configure_goal_control(True)
        original=c.bridge.read_thread
        reads=0
        async def read(tid):
            nonlocal reads
            reads+=1
            if reads>1: raise ProtocolError('owner disconnected')
            return await original(tid)
        c.bridge.read_thread=read
        with self.assertRaises(GoalControlError) as failure:
            await c.resume_goal('thread','old',GOAL)
        self.assertTrue(failure.exception.action_submitted)
        self.assertEqual(failure.exception.reason_code,'goal_confirmation_read_failed')

    async def test_helper_missing_probe_reports_capability(self):
        c=DesktopClient()
        with patch.dict('os.environ',{'CODEX_RELAY_NATIVE_HELPER':'/no/such/relay-helper'}):
            result=await c.configure_goal_control(True)
        self.assertFalse(c.goal_control)
        self.assertFalse(result['supported'])
        self.assertIn('原生辅助功能控制器',result['reason'])
        self.assertEqual(result['reason_code'],'Goal accessibility helper is unavailable')

class InterruptBridge(DesktopBridge):
    def __init__(self, snapshots):
        super().__init__()
        self.fake=Bridge(snapshots)
        self.requests=[]
        self.owners={'thread':'owner'}
    async def read_thread(self, tid): return await self.fake.read_thread(tid)
    async def request(self, method, params, target=None, timeout=15):
        self.requests.append((method,params,target))
        return {'result':{'interruptedTurnId':params.get('expectedTurnId','old')}}

class GoalPauseTest(unittest.IsolatedAsyncioTestCase):
    def active(self, **changes):
        return snapshot(goal={**GOAL,'status':'active',**changes},status='inProgress',runtime='active')

    async def test_default_bridge_rejects_any_goal(self):
        b=InterruptBridge([self.active()])
        with self.assertRaises(ProtocolError): await b.interrupt_turn('thread','old')
        self.assertEqual(b.requests,[])

    async def test_ready_goal_pause_uses_system_without_expected_turn(self):
        c=Client();c.bridge=InterruptBridge([self.active()])
        await c.configure_goal_control(True)
        self.assertTrue(c.goal_pause_control)
        await c.interrupt_turn('thread','old')
        method,params,target=c.bridge.requests[0]
        self.assertEqual(method,'thread-follower-interrupt-turn')
        self.assertEqual(params,{'conversationId':'thread','mode':'system'})
        self.assertEqual(target,'owner')
        self.assertFalse(c.calls[-1]['request_permission'])

    async def test_no_goal_uses_expected_turn(self):
        c=Client();c.bridge=InterruptBridge([snapshot(goal=None,status='inProgress',runtime='active')])
        await c.interrupt_turn('thread','old')
        self.assertEqual(c.bridge.requests[0][1]['expectedTurnId'],'old')

    async def test_goal_appearing_between_reads_is_blocked(self):
        c=Client();c.bridge=InterruptBridge([snapshot(goal=None,status='inProgress',runtime='active'),self.active()])
        with self.assertRaises(ProtocolError): await c.interrupt_turn('thread','old')
        self.assertEqual(c.bridge.requests,[])

    async def test_goal_changed_or_disappeared_between_reads_is_blocked(self):
        for after in [self.active(updatedAt=99),snapshot(goal=None,status='inProgress',runtime='active')]:
            c=Client();c.bridge=InterruptBridge([self.active(),after])
            await c.configure_goal_control(True)
            with self.assertRaises(ProtocolError): await c.interrupt_turn('thread','old')
            self.assertEqual(c.bridge.requests,[])

    async def test_disabled_or_revoked_permission_stops_goal_pause(self):
        c=Client();c.bridge=InterruptBridge([self.active()])
        with self.assertRaises(ProtocolError): await c.interrupt_turn('thread','old')
        self.assertEqual(c.bridge.requests,[])
        await c.configure_goal_control(True)
        c.probe['trusted']=False
        with self.assertRaises(ProtocolError): await c.interrupt_turn('thread','old')
        self.assertFalse(c.goal_pause_control)
        self.assertEqual(c.bridge.requests,[])

    async def test_paused_goal_and_pending_request_never_interrupt(self):
        c=Client();c.bridge=InterruptBridge([snapshot()]);await c.configure_goal_control(True)
        with self.assertRaises(ProtocolError): await c.interrupt_turn('thread','old')
        b=InterruptBridge([{**self.active(),'requests':[{'id':'approval'}]}])
        with self.assertRaises(ProtocolError):
            await b.interrupt_turn('thread','old',expected_goal=self.active()['goal'],allow_goal=True)
        self.assertEqual(b.requests,[])

if __name__=='__main__': unittest.main()
