"""Watchdog scheduler.

NOTE: A separately launched app-server is not necessarily attached to an
already-live Codex Desktop process. We only act on persisted history that the
app-server can read; that interoperability requires testing on the user's Mac.
"""
from __future__ import annotations
import asyncio
import logging
import time
import json
from .protocol import AppServer, ProtocolError
from .rules import quota_failure, safe_to_resume, is_idle, quota_gate, state_name, quota_used, job_safe, reset_snapshot, strategy_gate, goal_episode, stale_goal_failure, completed_after_goal_revision, expected_turn_id, goal_recovery_key, goal_recovery_needed
from .store import Store

log = logging.getLogger(__name__)

class Watchdog:
    def __init__(self, store: Store, client: AppServer, *, interval=45,
                 limit_id="codex", message="继续", max_threads=100,
                 recent_hours=24, dry_run=False):
        self.store = store
        self.client = client
        self.interval = max(interval, 10)
        self.limit_id = limit_id
        self.message = message
        self.max_threads = max_threads
        self.recent_hours = recent_hours
        self.dry_run = dry_run
        self.last_limits = None
        self.last_quota_error = None
        self.last_limits_at = None
        self.check_ids = None
        self.recheck_waiting = False
        self.latest_threads = {}
        self._turn_errors: dict[str, dict] = {}
        self._recent_auto_ids: set[str] = set()

    def _watched_ids(self, candidates):
        watched = {w["thread_id"] for w in self.store.watched()}
        self._recent_auto_ids = set()
        if self.store.setting("watch_all", False):
            cutoff = time.time() - self.recent_hours * 3600
            for info in candidates:
                updated = info.get("updatedAt")
                if isinstance(updated, (int, float)) and updated >= cutoff and info.get("id"):
                    watched.add(info["id"])
                    self._recent_auto_ids.add(info["id"])
        return watched

    def remember(self, thread):
        import copy
        latest=(thread.get('turns') or [{}])[-1]
        self.latest_threads[thread['id']]=copy.deepcopy(dict(id=thread['id'],name=thread.get('name'),
            model=thread.get('model'),status=thread.get('status'),goal=thread.get('goal'),
            requests=[{}] if thread.get('requests') else [],turns=[{k:latest[k] for k in
                ('id','status','error','startedAt','turnStartedAtMs') if k in latest}]))

    async def scan(self):
        try:
            candidates = await self.client.list_threads(
                pages=max(1, (self.max_threads + 49)//50), limit=50) if self.store.setting('watch_all',False) else []
        except Exception as exc:
            log.warning("thread/list unavailable; checking explicit watched threads: %s", exc)
            candidates = []
        ids = self._watched_ids(candidates)
        if self.check_ids is not None: ids &= self.check_ids
        if not ids:
            log.info("No watched threads. Try: codex-autoresume watch add THREAD_ID / watch all on")
        for thread_id in sorted(ids):
            try:
                thread = await self.client.read_thread(thread_id)
                policy = self.store.policy(thread_id)
                failure = quota_failure(thread)
                stale = stale_goal_failure(thread, self.store.thread_jobs(thread_id))
                if stale: failure = None
                goal_adoption = getattr(self.client,'goal_control',False) and goal_recovery_needed(thread,self.store.thread_jobs(thread_id))
                if goal_adoption:
                    for legacy in self.store.thread_jobs(thread_id):
                        legacy_context=json.loads(legacy.get('context') or '{}')
                        if (legacy['status']=='waiting' or (legacy['status']=='needs_attention' and (legacy['detail'] or '').startswith('Goal control unavailable'))) and legacy['attempts']==0 and legacy['error_code'] not in {'relayBudgetPause','relayTestCompleted'} and legacy_context.get('recovery_kind')!='goal_resume':
                            self.store.update(thread_id,legacy['failed_turn_id'],'superseded',detail='Adopted by separate Goal recovery episode; no ordinary substitution')
                    failure = (goal_recovery_key(thread), {'codexErrorInfo':'usageLimitExceeded'})
                elif getattr(self.client,'goal_control',False) and thread.get('goal'):
                    failure = None
                latest = (thread.get('turns') or [{}])[-1]
                self.store.record_check(thread_id, source='desktop', runtime_status=state_name(thread.get('status')),
                    turn_id=latest.get('id'), turn_status=state_name(latest.get('status')),
                    goal_status=(thread.get('goal') or {}).get('status'),
                    reason='goal_recovery_adoption' if goal_adoption else ('stale_goal_episode' if stale else ('quota_interruption' if failure else 'no_new_quota_interruption')))
                if policy['goal_only'] and not thread.get('goal'):
                    failure = None
                if failure:
                    turn_id, error = failure
                    owner = self._owner_for(thread_id)
                    if not owner:
                        # Bind auto-discovered failures to the observed account,
                        # avoiding accidental resume after user account switches.
                        try:
                            owner = (await self.client.limits()).get("accountId")
                        except Exception:
                            owner = None
                    if goal_adoption:
                        self.store.enqueue_goal(thread_id,turn_id,owner,goal_episode(thread),(thread.get('turns') or [{}])[-1].get('id'))
                    else:
                        self.store.enqueue(thread_id, turn_id, error["codexErrorInfo"], owner)
                    if goal_adoption:
                        self.store.merge_context_once(thread_id,turn_id,'recovery_kind','goal_resume')
                        self.store.merge_context_once(thread_id,turn_id,'expected_turn_id',(thread.get('turns') or [{}])[-1].get('id'))
                    episode = goal_episode(thread)
                    if episode:
                        self.store.merge_context_once(thread_id, turn_id, 'goal_episode', episode)
                    # Capture once, even if this turn is encountered on later scans.
                    try:
                        job_record = next(j for j in self.store.thread_jobs(thread_id) if j['failed_turn_id']==turn_id)
                        baseline = None if 'reset_baseline' in json.loads(job_record.get('context') or '{}') else reset_snapshot(await self.client.limits(), self.limit_id)
                        if baseline:
                            self.store.merge_context_once(thread_id, turn_id, 'reset_baseline', baseline)
                    except Exception:
                        pass
                self._reconcile_thread(thread_id, thread)
                self.remember(thread)
            except Exception as exc:
                self.store.record_check(thread_id, error=str(exc))
                log.warning("Could not inspect %s: %s", thread_id[:16], exc)

    def _owner_for(self, thread_id):
        for w in self.store.watched():
            if w["thread_id"] == thread_id:
                return w["account_id"]
        return None

    def _reconcile_thread(self, thread_id, thread):
        turns = thread.get("turns") or []
        latest = turns[-1] if turns else {}
        for job in self.store.jobs(["waiting", "running"]):
            if job["thread_id"] != thread_id:
                continue
            if not latest:
                continue
            last_id, last_status = latest.get("id"), state_name(latest.get("status"))
            context = json.loads(job.get('context') or '{}')
            if context.get('recovery_kind') == 'goal_resume' and job['status'] == 'waiting':
                if not job_safe(thread,job) or not goal_recovery_needed(thread,self.store.thread_jobs(thread_id)):
                    self.store.update(thread_id,job['failed_turn_id'],'superseded',detail='Goal episode or expected turn changed; no replay')
                continue
            obsolete_goal = (thread.get('goal') or {}).get('status') == 'usageLimited' and completed_after_goal_revision(thread)
            if job['status'] == 'waiting' and job['error_code'] not in {'relayBudgetPause','relayTestCompleted'} and (obsolete_goal or stale_goal_failure(thread, self.store.thread_jobs(thread_id))):
                self.store.update(thread_id, job['failed_turn_id'], 'superseded', detail='Old Goal quota episode already handled; no new interruption evidence')
                self.store.event(thread_id,'stale_goal','旧 Goal 中断已处理，已取消重复恢复。')
            elif job["status"] == "waiting" and last_id != expected_turn_id(job):
                self.store.update(thread_id, job["failed_turn_id"], "superseded", detail="Thread advanced outside AutoResume")
            elif job["status"] == "running":
                if last_id == job["resumed_turn_id"]:
                    if last_status == "completed":
                        self.store.update(thread_id, job["failed_turn_id"], "done", detail="Follow-up turn completed (not a claim task goals are done)")
                    elif last_status in {"failed", "interrupted", "cancelled", "canceled"}:
                        self.store.update(thread_id, job["failed_turn_id"], "needs_attention", detail="Follow-up turn ended unsuccessfully")
                elif last_id != expected_turn_id(job) and last_status in {"completed", "failed", "interrupted"}:
                    self.store.update(thread_id, job["failed_turn_id"], "needs_attention", detail="Thread advanced but follow-up turn ID differs")

    async def drain(self):
        while True:
            try:
                msg = self.client.notifications.get_nowait()
            except asyncio.QueueEmpty:
                return
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "error":
                err = params.get("error") or {}
                turn_id = params.get("turnId")
                if turn_id and isinstance(err, dict):
                    self._turn_errors[turn_id] = err
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                tid = turn.get("id")
                for job in self.store.jobs(["running"]):
                    if tid != job["resumed_turn_id"] or params.get("threadId") != job["thread_id"]:
                        continue
                    status = state_name(turn.get("status"))
                    err = turn.get("error") or self._turn_errors.pop(tid, {})
                    code = err.get("codexErrorInfo") if isinstance(err, dict) else None
                    if isinstance(code, str) and code in {"usageLimitExceeded", "rateLimitExceeded"}:
                        self.store.update(job["thread_id"], job["failed_turn_id"], "needs_attention", detail="Follow-up again hit quota; inspect before retry")
                    elif status == "completed" and not err:
                        self.store.update(job["thread_id"], job["failed_turn_id"], "done", detail="Follow-up turn completed")
                    elif status in {"failed", "interrupted", "cancelled", "canceled"} or err:
                        self.store.update(job["thread_id"], job["failed_turn_id"], "needs_attention", detail=f"Follow-up ended: {status or 'error'}")

    async def process_queue(self, recheck_waiting=False):
        jobs = self.store.jobs(["waiting"])
        if not jobs:
            return
        now = time.time()
        due = [j for j in jobs if (j["retry_at"] <= now or recheck_waiting) and j["attempts"] == 0
               and (self.check_ids is None or j['thread_id'] in self.check_ids)]
        if not due:
            return
        try:
            limits = await self.client.limits()
            self.last_limits = limits
            self.last_limits_at = time.time()
        except Exception as exc:
            self.last_quota_error = str(exc)
            log.warning("Quota read failed (fail closed): %s", exc)
            for j in due:
                self.store.update(j["thread_id"], j["failed_turn_id"], "waiting", retry_at=now+300, detail="Rate-limit RPC unavailable")
            return
        allowed, retry, why = quota_gate(limits, self.limit_id, now)
        for job in due:
            tid, failed = job["thread_id"], job["failed_turn_id"]
            # Removing a watched thread / disabling all-thread mode must
            # also deactivate already queued recoveries.
            explicit = {w["thread_id"] for w in self.store.watched()}
            auto = self._recent_auto_ids if self.store.setting("watch_all", False) else set()
            if tid not in explicit | auto:
                continue
            # Guard against backend versions which emit quota errors but
            # report turns 'completed' and/or lag on quota availability.
            # Never create a rapid chain of new turns in a quota loop.
            previous_attempts = [x for x in self.store.thread_jobs(tid)
                                 if x["thread_id"] == tid and x["attempts"] > 0
                                 and x["updated_at"] > now - 86400
                                 and x["error_code"] not in {"relayBudgetPause", "relayTestCompleted"}]
            if json.loads(job.get('context') or '{}').get('recovery_kind') == 'goal_resume':
                previous_attempts = [x for x in previous_attempts if json.loads(x.get('context') or '{}').get('action_kind') == 'goal_resume' or json.loads(x.get('context') or '{}').get('recovery_kind') == 'goal_resume']
            if job["error_code"] in {"relayBudgetPause", "relayTestCompleted"}: previous_attempts = []
            if len(previous_attempts) >= 3:
                self.store.update(tid, failed, "needs_attention", detail="Circuit breaker: 3 recovery attempts in 24 h")
                continue
            if previous_attempts:
                earliest = max(x["updated_at"] for x in previous_attempts) + 900
                if now < earliest:
                    self.store.update(tid, failed, "waiting", retry_at=earliest, detail="15-minute anti-loop cooldown")
                    continue
            if not allowed:
                self.store.update(tid, failed, "waiting", retry_at=retry, detail=why)
                continue
            account_id = limits.get("accountId")
            if not account_id or not job["account_id"]:
                self.store.update(tid, failed, "needs_attention", detail="Account identity could not be verified safely")
                continue
            if job["account_id"] != account_id:
                self.store.update(tid, failed, "needs_attention", detail="Authenticated account changed or cannot be verified")
                continue
            try:
                policy = self.store.policy(tid)
                context = json.loads(job.get('context') or '{}')
                if 'reset_baseline' not in context:
                    baseline = reset_snapshot(limits, self.limit_id)
                    if baseline:
                        self.store.merge_context_once(tid, failed, 'reset_baseline', baseline)
                        context['reset_baseline'] = baseline
                        job['context'] = json.dumps(context)
                ready, reason = strategy_gate(limits, policy, job, self.limit_id)
                if not ready:
                    self.store.update(tid, failed, 'waiting', retry_at=now+self.interval, detail=reason)
                    continue
                thread = await self.client.read_thread(tid)
                if policy['goal_only'] and not thread.get('goal'):
                    self.store.update(tid, failed, 'superseded', detail='Goal-only selection no longer matches')
                    continue
                if not job_safe(thread, job):
                    self.store.update(tid, failed, "superseded", detail="Turn changed or thread not idle at recovery time")
                    continue
                goal_action=bool(thread.get('goal'))
                if goal_action and not getattr(self.client,'goal_control',False):
                    self.store.update(tid, failed, 'needs_attention', detail='Goal control unavailable: open Codex and resume the Goal; no ordinary message substituted')
                    self.store.event(tid, 'goal_manual', '当前通道不能确认 Goal 恢复，请在 Codex 点击目标播放按钮。')
                    continue
                if self.dry_run:
                    log.info("DRY RUN would resume %s", tid)
                    self.store.update(tid, failed, "waiting", retry_at=now, detail="Dry run: no message sent")
                    continue
                if not self.store.reserve(tid, failed, allow_early=recheck_waiting):
                    continue
                # Durable reservation happens *before* network I/O. If a
                # timeout occurs we can't prove whether a turn was created.
                self.store.merge_context_once(tid, failed, 'action_kind', 'goal_resume' if goal_action else 'message')
                if goal_action:
                    result = await self.client.resume_goal(tid, expected_turn_id=expected_turn_id(job), expected_goal=thread['goal'])
                    if result.get('goal_confirmed') is not True:
                        raise ProtocolError('Goal execution was not confirmed after accessibility action')
                else:
                    response = await self.client.resume_thread(tid)
                    resumed = response.get("thread") or {}
                    if not is_idle(resumed) or (resumed.get("turns") is not None and not job_safe(resumed, job)):
                        self.store.update(tid, failed, "needs_attention", detail="Resumed thread not idle; no turn/start sent")
                        continue
                    result = await self.client.start_turn(tid, self.message)
                new_turn_id = (result.get("turn") or {}).get("id")
                if not new_turn_id:
                    self.store.update(tid, failed, "needs_attention", detail="turn/start response lacks turn id (ambiguous)")
                    continue
                self.store.update(tid, failed, "done" if goal_action else "running", resumed_turn_id=new_turn_id,
                                  detail="Goal execution confirmed; not a claim Goal is complete" if goal_action else "Follow-up turn accepted; waiting for completion")
                self.store.merge_context_once(tid, failed, 'action_receipt', {'turn_id':new_turn_id,'accepted_at':time.time(),'source':result.get('source','desktop_ipc'),'control_method':result.get('control_method'),'acknowledgement':result.get('acknowledgement')})
                self.store.event(tid, 'goal_resume' if goal_action else 'resume', f"{job['error_code']}: {failed} -> {new_turn_id}")
                log.info("Started Relay follow-up: thread=%s turn=%s", tid, new_turn_id)
            except Exception as exc:
                # Do NOT automatically retry: RPC timeout may have occurred
                # after the server executed it.
                self.store.merge_context_once(tid, failed, "action_failure", {"submitted":getattr(exc,"action_submitted",None), "reason_code":getattr(exc,"reason_code",None), "reason":str(exc)[:240], "at":time.time()})
                self.store.update(tid, failed, "needs_attention", detail=f"Recovery ambiguous; inspect original thread: {str(exc)[:200]}")
                self.store.event(tid, 'attention', '恢复请求结果待确认，已停止自动重发。')
                log.exception("Resume requires human review for thread %s", tid)

    async def enforce_budgets(self):
        selected = [w for w in self.store.watched() if self.store.policy(w['thread_id'])['mode'] == 'reserve'
                    and (self.check_ids is None or w['thread_id'] in self.check_ids)
                    and (self.check_ids is None or not self.store.check(w['thread_id']) or
                         self.store.check(w['thread_id']).get('turn_status')=='inProgress')]
        if not selected: return
        limits = await self.client.limits()
        self.last_limits = limits
        self.last_limits_at = time.time()
        for watch in selected:
            tid = watch['thread_id']
            policy = self.store.policy(tid)
            if not watch['account_id'] or watch['account_id'] != limits.get('accountId'):
                continue
            used = quota_used(limits, policy['window'], self.limit_id)
            if used is None or 100-used > policy['stop_remaining']: continue
            try:
                thread = await self.client.read_thread(tid)
                if policy['goal_only'] and (thread.get('goal') or {}).get('status') != 'active': continue
                if thread.get('goal') and not getattr(self.client,'goal_pause_control',False):
                    # Do not strand a Goal without explicitly enabled, currently trusted recovery.
                    continue
                latest = (thread.get('turns') or [{}])[-1]
                if state_name(thread.get('status')) != 'active' or latest.get('status') != 'inProgress':
                    continue
                turn_id = latest.get('id')
                if not turn_id or thread.get('requests'): continue
                if self.dry_run:
                    log.info('DRY RUN budget would stop %s', tid)
                    continue
                if not hasattr(self.client, 'interrupt_turn'):
                    raise RuntimeError('Budget stop requires a live Desktop connection')
                if not self.store.pause_reserve(tid, turn_id, watch['account_id']): continue
                original_goal=thread.get('goal')
                if original_goal:
                    self.store.merge_context_once(tid,turn_id,'goal_episode',goal_episode(thread))
                self.store.merge_context_once(tid, turn_id, 'reset_baseline', reset_snapshot(limits, self.limit_id))
                await self.client.interrupt_turn(tid, turn_id)
                # Acceptance is not completion. Verify terminal interruption.
                async with asyncio.timeout(15):
                    while True:
                        stopped = await self.client.read_thread(tid)
                        last = (stopped.get('turns') or [{}])[-1]
                        if last.get('id') != turn_id: raise RuntimeError('Thread advanced while pausing')
                        if last.get('status') == 'interrupted' and is_idle(stopped): break
                        if last.get('status') != 'inProgress': raise RuntimeError('Turn finished before budget stop')
                        await asyncio.sleep(.2)
                if original_goal:
                    original_identity=goal_episode(thread)['identity']
                    stopped_episode=goal_episode(stopped)
                    if not stopped_episode or stopped_episode['identity']!=original_identity or stopped['goal'].get('status')!='paused':
                        raise ProtocolError('Goal changed or was not paused by the budget interruption')
                self.store.merge_context_once(tid, turn_id, 'goal', stopped.get('goal') or {})
                self.store.update(tid, turn_id, 'waiting', detail=f"Budget paused at {used}% {policy['window']}")
                self.remember(stopped)
                self.store.record_check(tid,source='desktop',runtime_status=state_name(stopped.get('status')),
                    turn_id=turn_id,turn_status='interrupted',goal_status=(stopped.get('goal') or {}).get('status'),reason='relay_budget_pause')
                self.store.event(tid, 'pause', f'{turn_id}: {used}% {policy["window"]}')
            except Exception as exc:
                for job in self.store.jobs(['pausing']):
                    if job['thread_id'] == tid:
                        self.store.update(tid, job['failed_turn_id'], 'needs_attention', detail=f'Pause uncertain: {exc}')
                log.warning('Budget pause requires attention for %s: %s', tid, exc)

    async def once(self):
        self.last_quota_error = None
        await self.drain()
        await self.scan()
        await self.enforce_budgets()
        await self.process_queue(recheck_waiting=self.recheck_waiting)
        await self.drain()

    async def run(self):
        self.store.abandoned()
        for job in self.store.jobs(["running"]):
            self.store.update(job["thread_id"], job["failed_turn_id"], "needs_attention", detail="Daemon restarted while follow-up was running; inspect original session")
        log.info("AutoResume running; automatic actions only for explicitly watched threads, or after 'watch all on'")
        while True:
            try:
                if not self.client.proc or self.client.proc.returncode is not None or not getattr(self.client, "connected", True):
                    await self.client.close()
                    await self.client.start()
                await self.once()
            except Exception:
                log.exception("Watchdog iteration failed; retrying")
            await asyncio.sleep(self.interval)
