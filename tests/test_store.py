import tempfile
import time
import unittest
from pathlib import Path
from codex_autoresume.store import Store

class StoreTest(unittest.TestCase):
    def test_duplicate_failure_reserved_only_once_even_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.sqlite"
            s = Store(db)
            s.enqueue("thread-1", "turn-1", "usageLimitExceeded", "account-A")
            s.enqueue("thread-1", "turn-1", "usageLimitExceeded", "account-A")
            self.assertEqual(len(s.jobs()), 1)
            self.assertTrue(s.reserve("thread-1", "turn-1"))
            self.assertFalse(s.reserve("thread-1", "turn-1"))
            s.close()
            s = Store(db)
            s.abandoned()
            self.assertEqual(s.jobs()[0]["status"], "needs_attention")
            self.assertFalse(s.reserve("thread-1", "turn-1"))
            s.close()

    def test_settings_and_watch_persist(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.sqlite"
            s = Store(db)
            s.set_setting("watch_all", True)
            s.watch("thread-A", "acct-A", "My work")
            s.close()
            s = Store(db)
            self.assertTrue(s.setting("watch_all"))
            self.assertEqual(s.watched()[0]["account_id"], "acct-A")
            s.close()

if __name__ == "__main__": unittest.main()
