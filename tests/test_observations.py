import unittest
from codex_autoresume.observations import describe

class ObservationTest(unittest.TestCase):
    def thread(self,goal=None,status='completed'):
        return dict(id='s1',status='idle',goal=goal,turns=[dict(id='t2',status=status)])
    def view(self,t,jobs=None,**kwargs):
        return describe(t,dict(goal_only=False,mode='quota'),jobs or [],selected=True,monitoring=True,**kwargs)
    def test_turn_ended_is_not_goal_complete(self):
        t=self.thread(dict(status='usageLimited'))
        job=dict(thread_id='s1',failed_turn_id='t1',resumed_turn_id='t2',status='done',context='{}')
        v=self.view(t,[job]);self.assertEqual(v['turn_status'],'completed');self.assertEqual(v['goal_status'],'usageLimited')
        self.assertEqual(v['reason_code'],'turn_ended')
    def test_read_refresh_does_not_invent_monitor_check(self):
        v=self.view(self.thread());self.assertIsNone(v['checked_at']);self.assertGreater(v['observed_at'],0)
    def test_unknown_goal_preserved(self):
        self.assertEqual(self.view(self.thread(dict(objective='x')))['goal_status'],'unknown')
    def test_filtered_thread_explained(self):
        v=describe(self.thread(),dict(goal_only=True),[],selected=True)
        self.assertEqual(v['relay_status'],'filtered')
    def test_read_error_not_successful_check(self):
        v=self.view(self.thread(),check=dict(checked_at=123,error='lost'))
        self.assertEqual(v['checked_at'],123);self.assertEqual(v['relay_status'],'unavailable')
    def test_historical_unrelated_job_cannot_override_running_turn(self):
        job=dict(thread_id='s1',failed_turn_id='old',resumed_turn_id='older',status='needs_attention')
        self.assertEqual(self.view(self.thread(status='inProgress'),[job])['reason_code'],'no_intervention')
    def test_user_paused_goal_never_displayed_as_waiting_for_quota(self):
        job=dict(thread_id='s1',failed_turn_id='t2',status='waiting',context='{}',retry_at=9999999999)
        v=self.view(self.thread(dict(status='paused')),[job])
        self.assertEqual(v['reason_code'],'goal_not_eligible');self.assertEqual(v['relay_status'],'paused')
    def test_stale_legacy_waiting_display_does_not_claim_new_interruption(self):
        t=self.thread(dict(status='usageLimited',updatedAt=100))
        jobs=[dict(thread_id='s1',failed_turn_id='t2',status='waiting',context='{}',retry_at=999999,attempts=0),
              dict(thread_id='s1',failed_turn_id='old',resumed_turn_id='middle',status='done',context='{}',attempts=1,error_code='usageLimitExceeded',updated_at=200)]
        v=self.view(t,jobs)
        self.assertEqual(v['relay_status'],'stale_record');self.assertNotIn('新的额度中断',v['reason'])
    def test_true_goal_job_not_hidden_by_historical_message_attempt(self):
        import json
        t=self.thread(dict(status='usageLimited',updatedAt=100))
        jobs=[dict(thread_id='s1',failed_turn_id='goal:key',status='waiting',context=json.dumps(dict(recovery_kind='goal_resume',expected_turn_id='t2')),retry_at=999999,attempts=0),
              dict(thread_id='s1',failed_turn_id='old',resumed_turn_id='middle',status='done',context='{}',attempts=1,error_code='usageLimitExceeded',updated_at=200)]
        v=self.view(t,jobs,goal_control=True)
        self.assertEqual(v['relay_status'],'waiting');self.assertNotEqual(v['reason_code'],'stale_goal')
