"""Polling policy. Waiting never assumes a timestamp proves usable quota."""
from .rules import quota_gate
DEFAULTS={'check_seconds':30,'ui_seconds':60,'quota_wait_seconds':300}
RANGES={'check_seconds':(10,300),'ui_seconds':(20,300),'quota_wait_seconds':(60,1800)}
def polling_settings(value=None):
    value=value or {};result={}
    for key,default in DEFAULTS.items():
        number=value.get(key,default);low,high=RANGES[key]
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not low<=number<=high or int(number)!=number:
            raise ValueError(f'{key} must be an integer in {low}..{high} seconds')
        result[key]=int(number)
    early=value.get('early_reset_checks',True)
    if not isinstance(early,bool):raise ValueError('early_reset_checks must be a boolean')
    result['early_reset_checks']=early
    return result

def next_delay(settings, checks, limits, failures=0, now=None):
    """Active tasks keep normal checks; idle quota waits and failures back off."""
    import time
    now=time.time() if now is None else now
    if failures:return min(300,max(settings['check_seconds'],30)*2**min(failures-1,4))
    if any(c.get('turn_status')=='inProgress' or c.get('runtime_status')=='active' for c in checks):
        return settings['check_seconds']
    if limits:
        allowed,retry,_=quota_gate(limits,now=now)
        if not allowed:
            # Periodic low-frequency checks detect changes; wake near a known reset.
            return max(5,min(settings['quota_wait_seconds'],max(5,retry-now+2)))
    return settings['check_seconds']


def thread_plan(settings, record, limits, now):
    """A session-local wake plan; does not authorize execution."""
    import json, math
    from .rules import expected_turn_id
    check=record.get('check') or {};policy=record.get('policy') or {}
    jobs=record.get('jobs') or []
    def plan(mode, delay, reason, target=None):
        return dict(mode=mode,delay_seconds=delay,next_check_at=now+delay,
                    reason=reason,target_reset_at=target)
    failures=record.get('failures',0)
    if check.get('error') or failures:
        return plan('connection_wait',next_delay(settings,[],None,max(1,failures),now),'Connection unavailable; bounded backoff')
    # A confirmed/accepted new turn can precede the next observation.
    advancing=any(j.get('status')=='running' or
        (j.get('status')=='done' and j.get('resumed_turn_id') and
         j.get('resumed_turn_id')!=check.get('turn_id') and
         j.get('updated_at',0)>check.get('checked_at',0)) for j in jobs)
    if advancing or check.get('turn_status')=='inProgress' or check.get('runtime_status')=='active':
        return plan('running',settings['check_seconds'],'Running work; normal checks')
    waiting=[j for j in jobs if j.get('status')=='waiting' and not j.get('attempts')
             and expected_turn_id(j)==check.get('turn_id')]
    if not waiting:
        if check.get('goal_status')=='paused':
            return plan('manual_pause',settings['check_seconds'],'Manual Goal pause; status checks only, no automatic recovery')
        return plan('idle',settings['quota_wait_seconds'],'No eligible recovery; low-frequency status observation')
    job=waiting[0]
    allowed,retry,quota_reason=quota_gate(limits or {},now=now)
    reset_policy=policy.get('mode')=='reserve' and policy.get('resume_mode')=='reset'
    remaining_policy=policy.get('mode')=='reserve' and policy.get('resume_mode')=='remaining'
    targets=[]
    def add(value):
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>now:
            targets.append(value)
    # quota_gate uses the latest reset of every exhausted account window, plus a safety buffer.
    snapshot=((limits or {}).get('rateLimitsByLimitId') or {}).get('codex') or (limits or {}).get('rateLimits',{})
    known_exhaustion=any(isinstance(w,dict) and isinstance(w.get('usedPercent'),(int,float)) and w['usedPercent']>=100
                         and isinstance(w.get('resetsAt'),(int,float)) and w['resetsAt']>now
                         for w in [snapshot.get('primary'),snapshot.get('secondary')])
    if not allowed and known_exhaustion:add(retry+2)
    baseline=None
    if reset_policy:
        context=json.loads(job.get('context') or '{}')
        baseline=context.get('reset_baseline',{}).get(policy.get('window','primary'))
        add(baseline+2 if isinstance(baseline,(int,float)) and not isinstance(baseline,bool) else None)
    # A later blocking reset wins for this session; other sessions retain their own wake times.
    target=max(targets) if targets else None
    mode='remaining_wait' if remaining_policy else ('reset_wait' if reset_policy else 'quota_wait')
    reason={'remaining_wait':'Waiting for the saved remaining-quota threshold',
            'reset_wait':'Waiting for the saved quota reset; early availability does not override this policy',
            'quota_wait':'Waiting for service-confirmed quota recovery'}[mode]
    if not allowed:reason+='; '+quota_reason
    periodic=remaining_policy or settings.get('early_reset_checks',True) or target is None
    delay=settings['quota_wait_seconds'] if periodic else target-now
    if target is not None:delay=min(delay,max(5,target-now))
    return plan(mode,max(5,delay),reason,target)


def schedule_plan(settings, records, limits, *, previous=None, checked_ids=None, failures=0, now=None):
    """Keep unvisited sessions' deadlines stable to avoid starvation in mixed workloads."""
    import time
    now=time.time() if now is None else now
    previous=previous or {};checked_ids=set(checked_ids) if checked_ids is not None else None
    plans={}
    for record in records:
        tid=record['thread_id']
        if checked_ids is not None and tid not in checked_ids and tid in previous:
            plans[tid]=dict(previous[tid])
        else:plans[tid]=thread_plan(settings,record,limits,now)
    if failures:
        delay=next_delay(settings,[],None,failures,now)
        for p in plans.values():p.update(next_check_at=now+delay,delay_seconds=delay,mode='connection_wait',reason='Backend unavailable; bounded backoff')
    if not plans:return dict(mode='idle',reason='No selected sessions',delay_seconds=settings['quota_wait_seconds'],next_check_at=now+settings['quota_wait_seconds'],threads={})
    earliest=min(plans.values(),key=lambda p:p['next_check_at'])
    return dict(mode=earliest['mode'],reason=earliest['reason'],delay_seconds=max(.1,earliest['next_check_at']-now),
                next_check_at=earliest['next_check_at'],threads=plans)
