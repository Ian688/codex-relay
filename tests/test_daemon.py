import asyncio
import tempfile
import unittest
from pathlib import Path
from codex_autoresume.daemon import Watchdog
from codex_autoresume.store import Store

class FakeClient:
    def __init__(self, *, quota_ready=True, thread_status="notLoaded", owner="acct-A"):
        self.notifications = asyncio.Queue()
        self.quota_ready = quota_ready
        self.owner = owner
        self.thread = {"id": "s1", "status": thread_status, "turns": [
            {"id": "failed-t1", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}]}
        self.calls = []
    async def list_threads(self, **kwargs):
        return [{"id": "s1", "updatedAt": 2000000000}]
    async def read_thread(self, id):
        return self.thread
    async def limits(self):
        return {"accountId": self.owner,
                "ordinaryUsageAllowed": self.quota_ready,
                "rateLimits": {"primary": {"usedPercent": 0 if self.quota_ready else 100, "resetsAt": 2000000000}}}
    async def resume_thread(self, id):
        self.calls.append(("resume", id))
        return {"thread": {"id": "s1", "status": "idle"}}
    async def start_turn(self, id, message):
        self.calls.append(("start", id, message))
        return {"turn": {"id": "new-turn"}}

class WatchdogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"db.sqlite")
        self.store.watch("s1", "acct-A")
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_resumes_once_then_no_duplicate_on_next_cycle(self):
        client = FakeClient()
        agent = Watchdog(self.store, client)
        await agent.once()
        self.assertEqual(client.calls, [("resume", "s1"), ("start", "s1", "继续")])
        self.assertEqual(self.store.jobs()[0]["status"], "running")
        await agent.once()
        self.assertEqual(len(client.calls), 2)

    async def test_quota_block_prevents_calls(self):
        client = FakeClient(quota_ready=False)
        await Watchdog(self.store, client).once()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.jobs()[0]["status"], "waiting")

    async def test_account_change_prevents_resume(self):
        client = FakeClient(owner="acct-B")
        await Watchdog(self.store, client).once()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.jobs()[0]["status"], "needs_attention")

    async def test_active_thread_never_resumed(self):
        client = FakeClient(thread_status="active")
        await Watchdog(self.store, client).once()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.jobs()[0]["status"], "superseded")

    async def test_dry_run_doesnt_launch(self):
        client = FakeClient()
        await Watchdog(self.store, client, dry_run=True).once()
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.jobs()[0]["attempts"], 0)

    async def test_second_quota_failure_respects_anti_loop_cooldown(self):
        client = FakeClient()
        agent = Watchdog(self.store, client)
        await agent.once()
        client.thread = {"id": "s1", "status": "notLoaded", "turns": [
            {"id": "new-fail", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}]}
        await agent.once()
        self.assertEqual(len(client.calls), 2)
        later = [j for j in self.store.jobs() if j["failed_turn_id"] == "new-fail"][0]
        self.assertEqual(later["status"], "waiting")
        self.assertIn("cooldown", later["detail"])

    async def test_unwatch_prevents_already_queued_recovery(self):
        client = FakeClient(quota_ready=False)
        agent = Watchdog(self.store, client)
        await agent.once()
        self.store.unwatch("s1")
        client.quota_ready = True
        for job in self.store.jobs():
            self.store.update("s1", "failed-t1", "waiting", retry_at=0)
        await agent.once()
        self.assertFalse(client.calls)

    async def test_auto_watch_off_no_calls(self):
        self.store.unwatch("s1")
        client = FakeClient()
        await Watchdog(self.store, client).once()
        self.assertFalse(self.store.jobs())

if __name__ == "__main__": unittest.main()
