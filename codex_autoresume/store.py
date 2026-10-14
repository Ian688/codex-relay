"""SQLite persistence with durable at-most-once reservations per failed turn."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watched (
    thread_id TEXT PRIMARY KEY, account_id TEXT, label TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    thread_id TEXT NOT NULL, failed_turn_id TEXT NOT NULL, account_id TEXT,
    status TEXT NOT NULL, error_code TEXT, retry_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, resumed_turn_id TEXT,
    detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (thread_id, failed_turn_id)
);
CREATE TABLE IF NOT EXISTS policies (
    thread_id TEXT PRIMARY KEY, goal_only INTEGER NOT NULL DEFAULT 0,
    pause_used REAL, resume_used REAL, window TEXT NOT NULL DEFAULT 'primary'
);
CREATE TABLE IF NOT EXISTS checks (thread_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, thread_id TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL
);
"""

class Store:
    def __init__(self, path: str | Path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if 'context' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN context TEXT")

        columns = {r[1] for r in self.db.execute("PRAGMA table_info(policies)")}
        for name, declaration in {
            'mode': "TEXT", 'stop_remaining': 'REAL',
            'resume_mode': 'TEXT', 'resume_remaining': 'REAL'
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE policies ADD COLUMN {name} {declaration}")
        self.db.execute("""UPDATE policies SET
            mode=CASE WHEN pause_used IS NULL THEN 'quota' ELSE 'reserve' END,
            stop_remaining=CASE WHEN pause_used IS NULL THEN 20 ELSE 100-pause_used END,
            resume_mode='remaining',
            resume_remaining=CASE WHEN resume_used IS NULL THEN 50 ELSE 100-resume_used END
            WHERE mode IS NULL""")

    def close(self):
        self.db.close()

    def setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value):
        self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def watch(self, thread_id, account_id=None, label=None):
        self.db.execute("INSERT INTO watched VALUES(?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET account_id=excluded.account_id,label=excluded.label", (thread_id, account_id, label, time.time()))

    def unwatch(self, thread_id):
        self.db.execute("DELETE FROM watched WHERE thread_id=?", (thread_id,))
        self.db.execute("DELETE FROM policies WHERE thread_id=?", (thread_id,))
        self.db.execute("UPDATE jobs SET status='disabled',detail='User removed watch' WHERE thread_id=? AND status IN ('waiting','pausing')", (thread_id,))

    def watched(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM watched ORDER BY created_at DESC")]

    def enqueue(self, thread_id, failed_id, error_code, account_id=None):
        now = time.time()
        self.db.execute("INSERT OR IGNORE INTO jobs (thread_id,failed_turn_id,account_id,status,error_code,retry_at,created_at,updated_at) VALUES (?,?,?,'waiting',?,?,?,?)", (thread_id, failed_id, account_id, error_code, now, now, now))

    def enqueue_goal(self, tid, key, account_id, episode, turn_id):
        """Separate actual Goal actions from historical ordinary-message attempts.

        Attempts are never reset. Only untouched synthetic records can follow
        the same Goal episode to a newly observed idle terminal turn.
        """
        self.enqueue(tid,key,'usageLimitExceeded',account_id)
        row = next(j for j in self.thread_jobs(tid) if j['failed_turn_id']==key)
        if row['attempts']: return
        context = json.loads(row.get('context') or '{}')
        context.update(recovery_kind='goal_resume', goal_episode=episode, expected_turn_id=turn_id)
        self.context(tid,key,context)
        if row['status']=='superseded':
            self.update(tid,key,'waiting',retry_at=0,detail='Goal control enabled; untouched episode revalidated')

    def jobs(self, statuses=None):
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            return [dict(r) for r in self.db.execute(f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY retry_at ASC", tuple(statuses))]
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 100")]

    def thread_jobs(self, tid):
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE thread_id=? ORDER BY created_at DESC", (tid,))]

    def check(self, tid):
        row = self.db.execute("SELECT payload FROM checks WHERE thread_id=?", (tid,)).fetchone()
        return json.loads(row['payload']) if row else {}

    def record_check(self, tid, **payload):
        previous = self.check(tid)
        now = time.time()
        if payload.get('error'):
            previous.update(error=str(payload['error']), error_at=now)
        else:
            previous = dict(payload, checked_at=now, observed_at=now, error=None)
        self.db.execute("INSERT INTO checks VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET payload=excluded.payload", (tid, json.dumps(previous)))

    def update(self, thread_id, failed_id, status, *, retry_at=None, detail=None, resumed_turn_id=None):
        self.db.execute("UPDATE jobs SET status=?,retry_at=COALESCE(?,retry_at),detail=?,resumed_turn_id=COALESCE(?,resumed_turn_id),updated_at=? WHERE thread_id=? AND failed_turn_id=?", (status, retry_at, detail, resumed_turn_id, time.time(), thread_id, failed_id))

    def reserve(self, thread_id, failed_id, *, allow_early=False):
        """Reserve atomically. allow_early is only for a freshly revalidated untouched job."""
        now = time.time()
        row = self.db.execute("UPDATE jobs SET status='resuming', attempts=attempts+1, updated_at=? WHERE thread_id=? AND failed_turn_id=? AND status='waiting' AND attempts=0 AND (retry_at<=? OR ?)", (now, thread_id, failed_id, now, int(allow_early)))
        return row.rowcount == 1

    def abandoned(self):
        # Startup cannot know whether a remote turn was sent before a crash.
        # Human review is safer than a duplicate request.
        self.db.execute("UPDATE jobs SET status='needs_attention',detail='Interrupted while resuming; inspect original Codex thread before any retry',updated_at=? WHERE status IN ('resuming','pausing')", (time.time(),))

    def policy(self, tid):
        row = self.db.execute("SELECT * FROM policies WHERE thread_id=?", (tid,)).fetchone()
        p = dict(row) if row else dict(thread_id=tid, goal_only=False, mode='quota',
            window='primary', stop_remaining=20, resume_mode='reset', resume_remaining=50)
        # Compatibility is deliberately derived from the canonical strategy.
        p['pause_used'] = 100-p['stop_remaining'] if p['mode'] == 'reserve' else None
        p['resume_used'] = 100-p['resume_remaining'] if p['mode'] == 'reserve' else None
        return p

    def set_strategy(self, tid, *, mode='quota', window='primary', stop_remaining=20,
                     resume_mode='reset', resume_remaining=50, goal_only=None):
        import math
        if tid not in {w['thread_id'] for w in self.watched()}:
            raise ValueError('Select the thread with watch add first')
        if mode not in {'quota', 'reserve'}: raise ValueError('Unknown strategy mode')
        if window not in {'primary', 'secondary'}: raise ValueError('Unknown quota window')
        if resume_mode not in {'reset', 'remaining'}: raise ValueError('Unknown recovery mode')
        values = (stop_remaining, resume_remaining)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError('Quota percentages must be finite numbers')
        if not 0 <= stop_remaining < 100 or not 0 <= resume_remaining <= 100:
            raise ValueError('Require 0 <= stop remaining < 100 and 0 <= resume remaining <= 100')
        if mode == 'reserve' and resume_mode == 'remaining' and not stop_remaining < resume_remaining:
            raise ValueError('Resume remaining must be greater than stop remaining')
        old = self.policy(tid)
        goal_only = old['goal_only'] if goal_only is None else bool(goal_only)
        pause_used = 100-stop_remaining if mode == 'reserve' else None
        resume_used = 100-resume_remaining if mode == 'reserve' else None
        self.db.execute("""INSERT INTO policies
            (thread_id,goal_only,pause_used,resume_used,window,mode,stop_remaining,resume_mode,resume_remaining)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET
            goal_only=excluded.goal_only,pause_used=excluded.pause_used,resume_used=excluded.resume_used,
            window=excluded.window,mode=excluded.mode,stop_remaining=excluded.stop_remaining,
            resume_mode=excluded.resume_mode,resume_remaining=excluded.resume_remaining""",
            (tid,goal_only,pause_used,resume_used,window,mode,stop_remaining,resume_mode,resume_remaining))
        self.db.execute("UPDATE jobs SET retry_at=0 WHERE thread_id=? AND status='waiting'", (tid,))
        self.event(tid, 'policy', json.dumps(self.policy(tid)))

    def set_policy(self, tid, *, goal_only=None, pause_used=None, resume_used=None, window='primary', budget=False):
        p = self.policy(tid)
        if budget:
            if pause_used is None:
                p['mode'] = 'quota'
            else:
                if resume_used is None or not 0 <= resume_used < pause_used <= 100:
                    raise ValueError('Require 0 <= resume-used < pause-used <= 100')
                p.update(mode='reserve', stop_remaining=100-pause_used,
                         resume_remaining=100-resume_used, resume_mode='remaining', window=window)
        self.set_strategy(tid, **{k:p[k] for k in ('mode','window','stop_remaining','resume_mode','resume_remaining')}, goal_only=goal_only)

    def pause_reserve(self, tid, turn_id, account_id):
        now = time.time()
        row = self.db.execute("INSERT OR IGNORE INTO jobs (thread_id,failed_turn_id,account_id,status,error_code,retry_at,created_at,updated_at) VALUES (?,?,?,'pausing','relayBudgetPause',?,?,?)", (tid, turn_id, account_id, now, now, now))
        return row.rowcount == 1

    def context(self, tid, turn_id, value):
        self.db.execute("UPDATE jobs SET context=? WHERE thread_id=? AND failed_turn_id=?", (json.dumps(value), tid, turn_id))

    def merge_context_once(self, tid, turn_id, key, value):
        row = self.db.execute("SELECT context FROM jobs WHERE thread_id=? AND failed_turn_id=?", (tid,turn_id)).fetchone()
        if not row: return
        context = json.loads(row['context'] or '{}')
        if key not in context:
            context[key] = value
            self.context(tid, turn_id, context)

    def event(self, tid, action, detail):
        self.db.execute("INSERT INTO events(at,thread_id,action,detail) VALUES(?,?,?,?)", (time.time(), tid, action, str(detail)))

    def events(self, count=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (count,))]
