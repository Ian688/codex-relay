import time
import unittest
from codex_autoresume.rules import quota_failure, safe_to_resume, quota_gate


def thread(status="notLoaded", turn_status="failed", code="usageLimitExceeded", id="t1"):
    return {"id": "s1", "status": status, "turns": [
        {"id": id, "status": turn_status, "error": {"codexErrorInfo": code}}]}

class RulesTest(unittest.TestCase):
    def test_detects_explicit_quota_failure(self):
        self.assertEqual(quota_failure(thread())[0], "t1")
        self.assertEqual(quota_failure(thread(code="rateLimitExceeded"))[0], "t1")

    def test_rejects_non_quota_error(self):
        for code in ("other", "serverOverloaded", "unauthorized", "network"):
            self.assertIsNone(quota_failure(thread(code=code)))

    def test_rejects_completed_even_if_error_looks_quota(self):
        self.assertIsNone(quota_failure(thread(turn_status="completed")))

    def test_latest_turn_must_be_failed(self):
        t = thread()
        t["turns"].append({"id": "later", "status": "completed"})
        self.assertIsNone(quota_failure(t))

    def test_not_loaded_readable_before_resume_but_active_not(self):
        self.assertTrue(safe_to_resume(thread(status="notLoaded"), "t1"))
        self.assertFalse(safe_to_resume(thread(status="active"), "t1"))
        self.assertFalse(safe_to_resume(thread(), "different"))

    def test_gate_waits_until_later_of_both_exhausted_windows(self):
        now = 1000000
        quotas = {"ordinaryUsageAllowed": False, "rateLimits": {
            "primary": {"usedPercent": 100, "resetsAt": now + 50},
            "secondary": {"usedPercent": 100, "resetsAt": now + 900}}}
        ready, when, why = quota_gate(quotas, now=now)
        self.assertFalse(ready)
        self.assertEqual(when, now + 930)

    def test_gate_requires_backend_permission(self):
        for permission in (None, False):
            ready, *_ = quota_gate({"rateLimits": {"primary": {"usedPercent": 4}}, "ordinaryUsageAllowed": permission}, now=1)
            self.assertFalse(ready)

    def test_gate_allows_verified_available_quota(self):
        ready, *_ = quota_gate({"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 99}, "secondary": {"usedPercent": 0}}}, now=1)
        self.assertTrue(ready)

    def test_gate_rejects_missing_meter_and_spend_control(self):
        limits = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 0}}, "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 0}}}}
        self.assertFalse(quota_gate(limits, "not-codex", now=1)[0])
        limits["rateLimitsByLimitId"]["codex"]["spendControlReached"] = True
        self.assertFalse(quota_gate(limits, "codex", now=1)[0])

if __name__ == "__main__": unittest.main()
