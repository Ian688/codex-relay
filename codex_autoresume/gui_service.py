"""Private JSON-lines service for the native macOS app (stdin/stdout only).

No HTTP port, no credentials in UI messages. GUI actions and monitor cycles
share one lock. Pausing waits for the current cycle to finish, rather than
cancelling a possibly accepted resume RPC.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from . import __version__
from .polling import polling_settings, next_delay

log = logging.getLogger(__name__)
from .daemon import Watchdog
from .desktop import DesktopClient
from .rules import state_name
from .store import Store
from .lease import MonitorLease
from .observations import describe, event_message


def friendly_error(exc):
    message = str(exc)
    if 'no-client-found' in message or 'No live Desktop' in message:
        return '请先在 Codex 中打开这个会话，再重新选择。'
    if isinstance(exc, FileNotFoundError):
        return '未连接到 Codex。请打开 Codex Desktop，并确认已安装 Codex CLI。'
    if 'Resume remaining' in message:
        return '恢复时的剩余额度必须高于暂停时的剩余额度。'
    if 'remaining' in message and ('Require' in message or 'finite' in message):
        return '请输入有效的额度百分比；暂停值须小于 100%。'
    if 'resume-used' in message:
        return '恢复阈值必须低于停止阈值，且两者都在 0–100% 之间。'
    if isinstance(exc, TimeoutError) or 'request-timeout' in message or 'start-turn-timeout' in message:
        return '读取超时，请稍后刷新；未重复发送任何续接请求。'
    return message[:240] or '操作未完成，请刷新后重试。'


class RelayService:
    def __init__(self, db=None, client=None):
        self.db_path = db or os.getenv('CODEX_RELAY_DB', os.getenv(
            'CODEX_AUTORESUME_DB', '~/.local/share/codex-autoresume/state.sqlite3'))
        self.store = Store(self.db_path)
        self.lease = MonitorLease(self.db_path)
        self.client = client or DesktopClient(os.getenv('CODEX_BINARY', 'codex'))
        self.lock = asyncio.Lock()
        self.monitoring = False
        self.monitor_task = None
        self.last_error = None
        self.last_cycle = None
        self.watcher = Watchdog(self.store, self.client, interval=30)
        self.wake = asyncio.Event()
        self.next_check_at = None
        self.poll_failures = 0
        self._state_cache = None
        self._state_cache_until = 0
        self._last_capability_probe = 0
        self._last_goal_enabled = None

    async def connect(self):
        api_ready = not hasattr(self.client, 'proc') or bool(self.client.proc and self.client.proc.returncode is None)
        if not (getattr(self.client,'connected',False) and api_ready):
            await self.client.close()
            await self.client.start()
        configure=getattr(self.client,'configure_goal_control',None)
        enabled = bool(self.store.setting('goal_accessibility_enabled',False))
        if configure and (enabled != self._last_goal_enabled or time.time()-self._last_capability_probe >= 60):
            await configure(enabled)
            self._last_capability_probe = time.time()
            self._last_goal_enabled = enabled

    async def state(self, force=True):
        if not force and self._state_cache is not None and time.time() < self._state_cache_until:
            return dict(self._state_cache,monitoring=self.monitoring,last_cycle=self.last_cycle,
                        next_check_at=self.next_check_at,error=self.last_error or self._state_cache.get('error'),cached=True)
        quotas, threads = None, []
        connected = False
        error = self.last_error
        try:
            await self.connect()
            quotas = await self.client.limits()
            threads = await self.client.list_threads(pages=2, limit=50)
            connected = True
            if not self.monitoring: error = None
        except Exception as exc:
            error = friendly_error(exc)
        watched = {w['thread_id']: w for w in self.store.watched()}
        jobs = sorted([j for tid in watched for j in self.store.thread_jobs(tid)],
                      key=lambda j:j['updated_at'],reverse=True)
        latest_jobs = {}
        for j in jobs: latest_jobs.setdefault(j['thread_id'], j)
        result = []
        seen = set()
        for t in threads:
            tid = t['id']
            seen.add(tid)
            result.append({'id': tid, 'name': t.get('name') or (t.get('preview') or '未命名会话')[:100],
                'cwd': t.get('cwd', ''), 'updated_at': t.get('updatedAt'),
                'selected': tid in watched, 'status': state_name(t.get('status')),
                'job': latest_jobs.get(tid), 'policy': self.store.policy(tid)})
        for tid, w in watched.items():
            if tid not in seen:
                result.append({'id':tid, 'name':w['label'] or '已选择的会话', 'cwd':'',
                    'selected':True, 'status':'notLoaded', 'job':latest_jobs.get(tid), 'policy':self.store.policy(tid)})
        if connected:
            for row in result:
                if not row['selected']: continue
                try:
                    async with asyncio.timeout(5):
                        live = await self.client.read_thread(row['id'])
                    row['status'] = state_name(live.get('status'))
                    row['turn_status'] = (live.get('turns') or [{}])[-1].get('status')
                    row['model'] = live.get('model')
                    row['goal_status'] = (live.get('goal') or {}).get('status')
                    row['observation'] = describe(live,row['policy'],self.store.thread_jobs(row['id']),
                        limits=quotas,monitoring=self.monitoring,selected=True,
                        check=self.store.check(row['id']),goal_control=getattr(self.client,'goal_control',False),goal_pause_control=getattr(self.client,'goal_pause_control',False))
                except Exception as exc:
                    row['status'] = 'unavailable'
                    row['live_error'] = friendly_error(exc)
        for row in result:
            if row['selected'] and 'observation' not in row:
                row['observation'] = dict(turn_status='unknown',goal_status='unknown',relay_status='unavailable',
                    reason=row.get('live_error') or '当前无法读取会话，尚未确认状态。',checked_at=self.store.check(row['id']).get('checked_at'))
        names={r['id']:r['name'] for r in result}
        events=[{**e,'message':event_message(e) + (' · '+names.get(e['thread_id'],e['thread_id'][:8]) if e['thread_id']!='-' else ''),
                 'name':names.get(e['thread_id'],'')} for e in self.store.events(30)]
        result_state = {'connected': connected, 'monitoring':self.monitoring, 'external_monitor':self.lease.busy(), 'error':error,
            'quotas':quotas, 'threads':result,
            'goal_control':getattr(self.client,'goal_capability',{'supported':False,'enabled':False,'trusted':False,'reason':'当前运行环境未连接辅助功能控制器。'}), 'watched_count':len(watched),
            'waiting_count':sum(j['status']=='waiting' and j['thread_id'] in watched for j in jobs),
            'attention_count':sum(j['status']=='needs_attention' and j['thread_id'] in watched for j in jobs),
            'unavailable_count':sum(r['selected'] and r['status'] in {'unavailable','notLoaded'} for r in result),
            'last_cycle':self.last_cycle, 'updated_at':time.time(), 'events':events,
            'polling':polling_settings(self.store.setting('polling',{})), 'next_check_at':self.next_check_at,'cached':False}
        self._state_cache = result_state
        self._state_cache_until = max(time.time()+result_state['polling']['ui_seconds'],self.next_check_at or 0)
        return result_state

    async def dispatch(self, action, params):
        if action == 'monitor' and not params.get('enabled'):
            await self.stop_monitor()
            async with self.lock: return await self.state()
        async with self.lock:
            if action == 'state': return await self.state(force=bool(params.get('force',True)))
            if action == 'polling':
                settings=polling_settings(params)
                self.store.set_setting('polling',settings)
                self.store.event('-','polling','Polling intervals updated')
                self.wake.set()
            elif action == 'watch':
                tid = params['thread_id']
                if params.get('enabled'):
                    await self.connect()
                    thread = await self.client.read_thread(tid)
                    limits = await self.client.limits()
                    if not limits.get('accountId'): raise ValueError('无法确认当前账号，未启用此会话。')
                    self.store.watch(tid, limits['accountId'], thread.get('name'))
                    self.store.event(tid, 'watch', '已加入监控列表')
                else:
                    self.store.unwatch(tid)
                    self.store.event(tid, 'unwatch', '已移出监控列表')
            elif action == 'policy':
                tid = params['thread_id']
                if 'mode' in params:
                    self.store.set_strategy(tid, mode=params['mode'],
                        window=params.get('window','primary'),
                        stop_remaining=float(params.get('stop_remaining',20)),
                        resume_mode=params.get('resume_mode','reset'),
                        resume_remaining=float(params.get('resume_remaining',50)),
                        goal_only=bool(params.get('goal_only')))
                else:
                    enabled = bool(params.get('budget_enabled'))
                    self.store.set_policy(tid, goal_only=bool(params.get('goal_only')),
                        pause_used=float(params.get('pause_used',80)) if enabled else None,
                        resume_used=float(params.get('resume_used',50)) if enabled else None,
                        window=params.get('window','primary'), budget=True)
            elif action == 'goal_control':
                enabled=bool(params.get('enabled'))
                configure=getattr(self.client,'configure_goal_control',None)
                if not configure: raise ValueError('请使用新版原生应用配置 Goal 辅助功能控制。')
                # Explicit opt-in only; startup/state reads never prompt for OS permission.
                self.store.set_setting('goal_accessibility_enabled',enabled)
                await configure(enabled, request_permission=bool(params.get('request_permission')) and enabled)
                if enabled:
                    for watched in self.store.watched():
                        for job in self.store.thread_jobs(watched['thread_id']):
                            context=json.loads(job.get('context') or '{}')
                            if job['status']=='needs_attention' and job['attempts']==0 and (job['detail'] or '').startswith('Goal control unavailable') and (context.get('recovery_kind')=='goal_resume' or job['error_code']=='relayBudgetPause'):
                                self.store.update(job['thread_id'],job['failed_turn_id'],'waiting',retry_at=0,detail='Goal control explicitly enabled; eligibility will be checked again')
                self.store.event('-','goal_control','已启用 Goal 辅助功能控制' if enabled else '已关闭 Goal 辅助功能控制')
            elif action == 'detail':
                await self.connect()
                t = await self.client.read_thread(params['thread_id'])
                last = (t.get('turns') or [{}])[-1]
                return {'id':t['id'], 'name':t.get('name'), 'status':state_name(t.get('status')),
                    'goal_status':(t.get('goal') or {}).get('status'), 'model':t.get('model'),
                    'turn_status':last.get('status'), 'turn_id':last.get('id'),
                    'observation':describe(t,self.store.policy(t['id']),self.store.thread_jobs(t['id']),
                        monitoring=self.monitoring,selected=t['id'] in {w['thread_id'] for w in self.store.watched()},
                        check=self.store.check(t['id']),goal_control=getattr(self.client,'goal_control',False),goal_pause_control=getattr(self.client,'goal_pause_control',False))}
            elif action == 'monitor':
                if not self.store.watched(): raise ValueError('先在会话列表中勾选至少一个会话。')
                await self.connect()
                # Lease is shared with CLI: never report paused while silently starting a second controller.
                if not self.monitoring:
                    self.lease.acquire()
                    self.store.abandoned()
                    self.store.set_setting('watch_all', False)
                    self.monitoring = True
                    self.last_error = None
                    self.store.event('-', 'monitor_start', '开始监控所选会话')
                    self.monitor_task = asyncio.create_task(self.monitor_loop())
            else:
                raise ValueError('不支持的操作')
            self.wake.set()
            return await self.state()

    async def monitor_loop(self):
        while self.monitoring:
            settings=polling_settings(self.store.setting('polling',{}))
            self.watcher.interval=settings['check_seconds']
            checks=[]
            self.wake.clear()
            try:
                async with self.lock:
                    if not self.monitoring: break
                    await self.connect()
                    cycle_start = time.time()
                    await self.watcher.once()
                    checks=[self.store.check(w['thread_id']) for w in self.store.watched()]
                    if checks and all(not c.get('error') and c.get('checked_at',0)>=cycle_start for c in checks):
                        self.last_cycle=time.time();self.last_error=None;self.poll_failures=0
                    else:
                        self.last_error='部分会话监控检查未成功，请查看会话状态。'
                        self.poll_failures+=1
            except Exception as exc:
                self.last_error=friendly_error(exc);self.poll_failures+=1
                log.warning('Monitor check failed: %s',exc)
            if not self.monitoring: return
            delay=next_delay(settings,checks,getattr(self.watcher,'last_limits',None),self.poll_failures)
            self.next_check_at=time.time()+delay
            # Invalidate once per actual cycle, not once per UI timer tick.
            self._state_cache_until=0
            log.info('Monitor cycle: failures=%s next_check_in=%ss',self.poll_failures,round(delay))
            try: await asyncio.wait_for(self.wake.wait(),timeout=delay)
            except asyncio.TimeoutError: pass

    async def stop_monitor(self):
        if self.monitoring: self.store.event('-', 'monitor_stop', '已暂停监控，未停止 Codex 任务')
        self.monitoring = False
        self.wake.set()
        self.next_check_at = None
        task = self.monitor_task
        try:
            if task: await task
        finally:
            self.monitor_task = None
            self.lease.release()

    async def close(self):
        await self.stop_monitor()
        await self.client.close()
        self.store.close()


def write_response(response, stream):
    try:
        print(json.dumps(response,ensure_ascii=False),file=stream,flush=True)
        return True
    except (BrokenPipeError,ConnectionResetError):
        return False


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        stream=super()._open()
        os.chmod(self.baseFilename,0o600)
        return stream


def configure_logging():
    directory=Path.home()/'Library/Logs/CodexRelay'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'engine.log'
    handler=PrivateRotatingHandler(path,maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
    path.chmod(0o600)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)
    log.info('Relay backend start version=%s pid=%s',__version__,os.getpid())


async def main():
    configure_logging()
    service = RelayService()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line: break
            request = {}
            try:
                request = json.loads(line)
                async with asyncio.timeout(90):
                    result = await service.dispatch(request['action'], request.get('params') or {})
                response = {'id':request.get('id'), 'ok':True, 'result':result}
            except Exception as exc:
                response = {'id':request.get('id'), 'ok':False, 'error':friendly_error(exc)}
            if not write_response(response,sys.stdout):
                log.info('UI pipe closed; stopping backend normally')
                # Avoid a second BrokenPipeError during interpreter shutdown.
                sys.stdout=open(os.devnull,'w')
                break
    finally:
        await service.close()
        log.info("Relay backend stopped")

if __name__ == '__main__': asyncio.run(main())
