"""Strict classification and quota gating; deliberately favor false negatives."""
from __future__ import annotations
from datetime import datetime, timezone
import time

QUOTA_CODES = {"usageLimitExceeded", "rateLimitExceeded"}
TERMINAL = {"completed", "failed", "interrupted", "cancelled", "canceled"}


def state_name(v):
    return (v.get("type") or v.get("status") or "") if isinstance(v, dict) else (v or "")


def quota_failure(thread: dict):
    """Return (turn_id, error) only when latest turn *explicitly* failed on quota.

    An old failed turn isn't sufficient if a new turn is already present.
    Plain English 'rate limit' strings are intentionally not enough.
    """
    turns = thread.get("turns") or []
    if not turns:
        return None
    turn = turns[-1]
    goal = thread.get('goal') or {}
    if goal.get('status') in {'paused', 'complete', 'budgetLimited', 'blocked'}:
        return None
    if state_name(turn.get("status")) == "failed":
        err = turn.get("error") or {}
        code = err.get("codexErrorInfo") if isinstance(err, dict) else None
        if isinstance(code, str) and code in QUOTA_CODES and turn.get('id'):
            return turn['id'], err
    if goal.get('status') == 'usageLimited' and state_name(turn.get('status')) == 'completed' and not completed_after_goal_revision(thread):
        return (turn['id'], {'codexErrorInfo': 'usageLimitExceeded'}) if turn.get('id') else None
    return None


def completed_after_goal_revision(thread):
    """A later ordinary completed turn cannot reissue an older Goal limit.

    Explicit quota failure still wins. Only finite source timestamps count;
    absent or malformed dates remain unknown rather than inventing ordering.
    """
    import math
    latest = (thread.get('turns') or [{}])[-1]
    if state_name(latest.get('status')) != 'completed': return False
    goal = thread.get('goal') or {}
    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    revision = goal.get('updatedAt')
    if not finite(revision): return False
    if revision > 1e11: revision /= 1000
    started = latest.get('startedAt')
    if not finite(started):
        started_ms = latest.get('turnStartedAtMs')
        started = started_ms / 1000 if finite(started_ms) else None
    if not finite(started): return False
    return started > revision


def goal_episode(thread):
    goal = thread.get('goal') or {}
    if not goal: return None
    return {'identity': {key: goal.get(key) for key in ('threadId', 'createdAt', 'objective')},
            'revision': goal.get('updatedAt'), 'snapshot': goal}


def stale_goal_failure(thread, jobs):
    """A completed turn plus a lingering Goal flag is not a new interruption.

    Explicit structured failed turns are independent evidence. Legacy attempts
    have no episode snapshot: fail closed unless a dated Goal revision proves
    a later interruption. Query all historical jobs, never a UI-limited list.
    """
    import json
    latest = (thread.get('turns') or [{}])[-1]
    if state_name(latest.get('status')) == 'failed': return False
    episode = goal_episode(thread)
    if not episode or (thread.get('goal') or {}).get('status') != 'usageLimited': return False
    for job in jobs:
        if not job.get('attempts') or job.get('error_code') in {'relayBudgetPause', 'relayTestCompleted'}: continue
        context = json.loads(job.get('context') or '{}')
        old = context.get('goal_episode')
        if old:
            if old['identity'] != episode['identity']: continue
            if old.get('revision') == episode.get('revision'): return True
        # Only a newer server revision can prove a new Goal episode. Missing
        # revisions and backwards revisions do not reopen an old interruption.
        revision = episode.get('revision')
        if not isinstance(revision, (int, float)): return True
        revision = revision / 1000 if revision > 1e11 else revision
        if revision <= job['updated_at']: return True
    return False


def expected_turn_id(job):
    import json
    return json.loads(job.get('context') or '{}').get('expected_turn_id', job['failed_turn_id'])


def goal_recovery_key(thread):
    import hashlib, json
    episode = goal_episode(thread)
    payload = {'identity': episode['identity'], 'revision': episode['revision']}
    return 'goal:' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def goal_recovery_needed(thread, jobs):
    import json
    goal = thread.get('goal') or {}
    latest = (thread.get('turns') or [{}])[-1]
    if goal.get('status') != 'usageLimited' or not latest.get('id'): return False
    if state_name(thread.get('status')) not in {'idle','notLoaded'} or thread.get('requests'): return False
    if state_name(latest.get('status')) not in {'completed','failed'}: return False
    episode = goal_episode(thread)
    for job in jobs:
        context = json.loads(job.get('context') or '{}')
        if not job.get('attempts'): continue
        if context.get('action_kind') != 'goal_resume' and context.get('recovery_kind') != 'goal_resume': continue
        old = context.get('goal_episode')
        # Unknown identity for an actual Goal action cannot safely be replayed.
        if not old: return False
        if old.get('identity') == episode['identity'] and old.get('revision') == episode['revision']: return False
    return True


def is_idle(thread: dict):
    return state_name(thread.get("status")) == "idle"


def safe_to_resume(thread: dict, failed_turn_id: str):
    found = quota_failure(thread)
    return state_name(thread.get("status")) in {"idle", "notLoaded"} and found is not None and found[0] == failed_turn_id


def quota_gate(limits: dict, limit_id: str = "codex", now: float | None = None):
    """Return (allowed, retry_epoch, reason).

    `ordinaryUsageAllowed: null` is UNKNOWN, never counted as true. The
    timestamp alone does not prove quota recovery. No model requests are used.
    """
    now = time.time() if now is None else now
    buckets = limits.get("rateLimitsByLimitId") or {}
    snapshot = buckets.get(limit_id) if buckets else (limits.get("rateLimits") or {})
    if not snapshot:
        return False, now + 300, "no quota snapshot"
    dates = []
    reached = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if used is not None and float(used) >= 100:
            reached.append(key)
            reset = window.get("resetsAt")
            if isinstance(reset, (float, int)) and reset > now:
                dates.append(float(reset))
    if snapshot.get("spendControlReached") is True:
        return False, now + 3600, "spend control reached (manual review)"
    if reached:
        return False, max(dates, default=now + 300) + 30, "quota exhausted: " + ", ".join(reached)
    if limits.get("ordinaryUsageAllowed") is not True:
        return False, now + 300, "backend permission unavailable or denied"
    return True, now, "backend permits ordinary usage"


def epoch_string(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def quota_used(limits, window='primary', limit_id='codex'):
    """Unknown/malformed readings never imply available budget."""
    import math
    buckets = limits.get('rateLimitsByLimitId')
    snapshot = buckets.get(limit_id, {}) if buckets else limits.get('rateLimits', {})
    value = (snapshot.get(window) or {}).get('usedPercent')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 100 else None


def job_safe(thread, job):
    import json
    latest = (thread.get('turns') or [{}])[-1]
    if state_name(thread.get('status')) not in {'idle', 'notLoaded'} or thread.get('requests'):
        return False
    if latest.get('id') != expected_turn_id(job): return False
    context = json.loads(job.get('context') or '{}')
    if context.get('recovery_kind') == 'goal_resume':
        return (thread.get('goal') or {}).get('status') == 'usageLimited' and goal_episode(thread) == context.get('goal_episode') and state_name(latest.get('status')) in {'completed','failed'}
    goal = thread.get('goal') or {}
    code = job['error_code']
    if code == 'relayBudgetPause':
        # Resume only the exact interruption confirmed as Relay's own action.
        context = json.loads(job.get('context') or '{}')
        return latest.get('status') == 'interrupted' and goal == context.get('goal', {})
    if goal.get('status') in {'paused', 'complete', 'budgetLimited', 'blocked'}: return False
    context = json.loads(job.get('context') or '{}')
    episode = context.get('goal_episode')
    if episode and goal != episode.get('snapshot'): return False
    if code == 'relayTestCompleted': return latest.get('status') == 'completed' and not goal
    return safe_to_resume(thread, job['failed_turn_id'])


def reset_snapshot(limits, limit_id='codex'):
    """Capture server window identities; elapsed local time is not recovery proof."""
    import math
    buckets = limits.get('rateLimitsByLimitId')
    snapshot = buckets.get(limit_id, {}) if buckets else limits.get('rateLimits', {})
    result = {}
    for window in ('primary', 'secondary'):
        reset = (snapshot.get(window) or {}).get('resetsAt')
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
            result[window] = reset
    return result


def strategy_gate(limits, policy, job, limit_id='codex', now=None):
    """Shared recovery rule for genuine quota failures and Relay interruptions."""
    import json
    if policy['mode'] == 'quota':
        return True, 'Quota recovery: backend permits usage'
    used = quota_used(limits, policy['window'], limit_id)
    if used is None:
        return False, 'Reserve waiting: quota reading unavailable'
    remaining = 100-used
    if remaining <= policy['stop_remaining']:
        return False, f'Reserve waiting: remaining {remaining:g}% must exceed {policy["stop_remaining"]:g}%'
    if policy['resume_mode'] == 'remaining':
        return remaining >= policy['resume_remaining'], f'Reserve recovery requires remaining >= {policy["resume_remaining"]:g}% (now {remaining:g}%)'
    context = json.loads(job.get('context') or '{}')
    baseline = context.get('reset_baseline', {}).get(policy['window'])
    observed = reset_snapshot(limits, limit_id).get(policy['window'])
    if baseline is None or observed is None:
        return False, 'Reserve waiting: reset identity unavailable'
    now = time.time() if now is None else now
    return observed > baseline and now >= baseline, 'Reserve waiting for next verified quota window reset'
