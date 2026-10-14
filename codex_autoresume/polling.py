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
