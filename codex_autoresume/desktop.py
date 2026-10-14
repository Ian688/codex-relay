"""Opt-in, version-sensitive adapter for the installed Codex Desktop IPC.

Uses the existing thread owner's follower interface. Never takes over a thread
with a second app-server. Missing owner, unknown schema or protocol mismatch
stops the action. Public app-server remains responsible for quota reads.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import stat
import struct
import uuid
from .protocol import ProtocolError

VERSIONS = {'initialize': 0, 'thread-owner-discovery': 1,
            'thread-stream-following-changed': 1,
            'thread-follower-load-complete-history': 1,
            'thread-follower-start-turn': 2,
            'thread-follower-interrupt-turn': 4}


def normalize_snapshot(state):
    if not isinstance(state, dict) or not isinstance(state.get('threadRuntimeStatus'), dict):
        raise ProtocolError('Unknown Desktop snapshot schema')
    goal = state.get('threadGoal')
    if goal is not None and (not isinstance(goal,dict) or not goal):
        raise ProtocolError('Unknown Desktop Goal schema')
    history = state.get('turnHistory')
    if history and history.get('kind') == 'canonical':
        h = history.get('history', {})
        entities = h.get('entitiesByKey', {})
        turns = [entities[e['value']] for island in h.get('islands', [])
                 for e in island.get('entries', []) if e.get('value') in entities]
    else:
        turns = state.get('turns', [])
    turns = [{**t, 'id': t.get('turnId') or t.get('id')} for t in turns]
    return {'id': state['id'], 'status': state['threadRuntimeStatus'], 'turns': turns,
            'goal': state.get('threadGoal'), 'requests': state.get('requests', []),
            'name': state.get('title'), 'cwd': state.get('cwd'),
            'model': state.get('latestModel'), 'effort': state.get('latestReasoningEffort')}


class GoalControlError(ProtocolError):
    """Action evidence distinguishes no click from unconfirmed post-click effects."""
    def __init__(self, message, *, action_submitted, reason_code):
        super().__init__(message)
        self.action_submitted = action_submitted
        self.reason_code = reason_code


def goal_control_reason(value):
    """Display known helper diagnostics without exposing untranslated codes."""
    messages = {
        'Ready': '辅助功能通道已就绪；每次恢复仍会核验目标并确认执行。',
        'unsupported_codex_installation': '当前 Codex 安装位置或身份不受支持，未执行操作。',
        'codex_not_running_or_ambiguous': '未找到唯一的 Codex 运行实例，请打开 Codex 后重试。',
        'accessibility_permission_required': '尚未获得辅助功能权限，请在系统设置中授权后刷新。',
        'Goal accessibility permission or support unavailable': '辅助功能权限或通道不可用，未执行操作。',
        'Goal accessibility helper is unavailable': '未找到原生辅助功能控制器，请使用新版桌面应用。',
        'Goal accessibility helper failed': '辅助功能控制器运行失败，请检查应用后刷新。',
        'Invalid Goal accessibility helper response': '辅助功能控制器返回无效信息，未确认可用。',
        'Goal accessibility control is disabled': 'Goal 辅助功能控制未启用。',
        'thread_navigation_failed': '无法打开指定 Codex 会话，请手动打开后重试。',
        'no_focused_codex_window': '未找到 Codex 当前窗口，未执行操作。',
        'accessibility_tree_incomplete': '无法完整读取 Codex 窗口，未执行操作。',
        'target_thread_not_verified': '无法确认当前窗口就是所选会话，未执行操作。',
        'goal_resume_control_not_verified': '无法确认目标恢复按钮，未执行操作。',
        'ambiguous_goal_resume_control': '发现多个可能的目标恢复按钮，未执行操作。',
        'goal_control_not_pressable': '目标恢复按钮当前不可操作。',
        'accessibility_press_failed': '目标恢复按钮操作未确认，请检查原会话。',
    }
    return messages.get(value, '辅助功能通道尚未确认可用，请检查 Codex 与系统权限。')


class DesktopBridge:
    def __init__(self, socket_path=None):
        self.path = Path(socket_path or Path(os.getenv('CODEX_HOME', '~/.codex')).expanduser() / 'ipc/ipc.sock')
        self.reader = self.writer = self.task = None
        self.client_id = 'initializing-client'
        self.pending = {}
        self.snapshots = {}
        self.owners = {}
        self.expected = {}
        self.notifications = asyncio.Queue()

    async def start(self):
        # The adapter must not attach to a socket owned by another account.
        for path in (self.path.parent, self.path):
            info = path.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise ProtocolError('Desktop IPC ownership/permissions check failed')
        if not stat.S_ISSOCK(self.path.stat().st_mode):
            raise ProtocolError('Desktop IPC path is not a socket')
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.path))
        self.task = asyncio.create_task(self._read())
        response = await self.request('initialize', {'clientType': 'codex-relay'})
        self.client_id = response['result']['clientId']
        return self

    async def _send(self, value):
        if not self.writer or self.writer.is_closing():
            raise ProtocolError('Desktop disconnected')
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.writer.write(struct.pack('<I', len(raw)) + raw)
        await self.writer.drain()

    async def request(self, method, params, target=None, timeout=15):
        ident = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self._send({'type': 'request', 'requestId': ident,
                'sourceClientId': self.client_id, 'version': VERSIONS[method],
                'method': method, 'params': params,
                **({'targetClientId': target} if target else {})})
            result = await asyncio.wait_for(future, timeout)
            if result.get('resultType') != 'success':
                raise ProtocolError('Desktop: ' + str(result.get('error', 'unknown response')))
            return result
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while True:
                size = struct.unpack('<I', await self.reader.readexactly(4))[0]
                if not 0 < size <= 64 * 1024 * 1024:
                    raise ProtocolError('Desktop IPC frame too large')
                msg = json.loads(await self.reader.readexactly(size))
                if msg.get('type') == 'response':
                    f = self.pending.get(msg.get('requestId'))
                    if f and not f.done(): f.set_result(msg)
                elif msg.get('type') == 'client-discovery-request':
                    await self._send({'type': 'client-discovery-response',
                        'requestId': msg['requestId'], 'response': {'canHandle': False}})
                elif msg.get('type') == 'broadcast' and msg.get('method') == 'thread-stream-state-changed':
                    if msg.get('version') != 11: continue
                    if msg.get('targetClientIds') is not None and self.client_id not in msg['targetClientIds']: continue
                    p = msg.get('params', {})
                    change = p.get('change', {})
                    tid = p.get('conversationId')
                    if change.get('type') == 'snapshot' and msg.get('sourceClientId') == self.owners.get(tid):
                        self.snapshots[tid] = change
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for f in self.pending.values():
                if not f.done(): f.set_exception(ProtocolError(f'Desktop IPC disconnected: {exc}'))
            self.snapshots.clear()
            if self.writer: self.writer.close()

    async def owner(self, tid):
        response = await self.request('thread-owner-discovery', {'conversationId': tid, 'hostId': 'local'})
        owner = response.get('handledByClientId')
        if not owner: raise ProtocolError('No live Desktop thread owner')
        self.owners[tid] = owner
        return owner

    async def read_thread(self, tid):
        owner = await self.owner(tid)
        self.snapshots.pop(tid, None)
        await self._send({'type': 'broadcast', 'method': 'thread-stream-following-changed',
            'sourceClientId': self.client_id, 'version': 1,
            'params': {'conversationId': tid, 'hostId': 'local', 'following': True}})
        response = await self.request('thread-follower-load-complete-history', {'conversationId': tid}, owner)
        revision = response['result'].get('revision')
        if not isinstance(revision, int): raise ProtocolError('Desktop response has no snapshot revision')
        # Response and broadcast may arrive in either order. Require this request's revision.
        async with asyncio.timeout(5):
            while tid not in self.snapshots or self.snapshots[tid].get('revision', -1) < revision:
                await asyncio.sleep(.02)
        snapshot = normalize_snapshot(self.snapshots[tid]['conversationState'])
        if snapshot['id'] != tid: raise ProtocolError('Desktop snapshot thread mismatch')
        return snapshot

    async def resume_thread(self, tid):
        # Already owned by Desktop; no thread/resume in an independent process.
        thread = await self.read_thread(tid)
        self.expected[tid] = ((thread.get('turns') or [{}])[-1].get('id'), thread.get('goal'))
        return {'thread': thread}

    async def start_turn(self, tid, message):
        before = await self.read_thread(tid)
        expected = self.expected.pop(tid, None)
        actual = ((before.get('turns') or [{}])[-1].get('id'), before.get('goal'))
        if expected is not None and actual != expected:
            raise ProtocolError('Thread advanced after recovery was reserved')
        if before['status'].get('type') != 'idle' or before['requests']:
            raise ProtocolError('Desktop thread is busy or waiting for user input')
        response = await self.request('thread-follower-start-turn', {'conversationId': tid,
            'turnStart': {'request': {'threadId': tid, 'input': [{'type': 'text', 'text': message, 'text_elements': []}],
                                     'clientUserMessageId': str(uuid.uuid4()), 'turnTrigger': 'codex_relay'},
                          'context': {'inheritThreadSettings': True}}}, self.owners[tid])
        return response['result'].get('result') or {}

    async def interrupt_turn(self, tid, turn_id, *, expected_goal=None, allow_goal=False):
        before = await self.read_thread(tid)
        latest = (before['turns'] or [{}])[-1]
        goal = before.get('goal')
        if goal is not None:
            if not allow_goal or goal.get('status') != 'active' or goal != expected_goal:
                raise ProtocolError('Goal changed or guarded Goal budget pause is not authorized')
        elif expected_goal is not None:
            raise ProtocolError('Goal disappeared before interruption')
        if before.get('id') != tid or before.get('requests'):
            raise ProtocolError('Thread identity changed or waiting for user input before interruption')
        if latest.get('id') != turn_id or latest.get('status') != 'inProgress':
            raise ProtocolError('Turn changed before interruption')
        response = await self.request('thread-follower-interrupt-turn',
            {'conversationId': tid, 'mode': 'system',
             **({} if (before.get('goal') or {}).get('status') == 'active' else {'expectedTurnId': turn_id})}, self.owners[tid])
        result = response['result']
        if result.get('interruptedTurnId') != turn_id: raise ProtocolError('Interrupted turn mismatch')
        return result

    async def close(self):
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception): await self.writer.wait_closed()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.task


class DesktopClient:
    """Public quota client + live Desktop thread controller."""
    # Verified on installed Desktop: external goal/set mutates persisted metadata
    # but does not wake the existing owner. Never advertise that as Goal recovery.
    goal_control = False

    def __init__(self, binary='codex', socket_path=None):
        from .protocol import AppServer
        self.api = AppServer(binary)
        self.bridge = DesktopBridge(socket_path)
        self.notifications = self.bridge.notifications
        self.goal_control_enabled = False
        self.goal_control = False
        self.goal_pause_control = False
        self.goal_capability = {'supported': False, 'enabled': False, 'trusted': False,
                                'reason': goal_control_reason('Goal accessibility control is disabled')}
        self.goal_confirmation_timeout = 15
        self.goal_confirmation_interval = .25

    @property
    def proc(self): return self.api.proc

    @property
    def connected(self):
        return bool(self.bridge.writer and not self.bridge.writer.is_closing() and self.bridge.task and not self.bridge.task.done())

    async def start(self):
        await self.api.start()
        try: await self.bridge.start()
        except Exception:
            await self.api.close()
            raise
        return self

    async def close(self):
        await self.bridge.close()
        await self.api.close()

    async def limits(self): return await self.api.limits()
    async def list_threads(self, **kwargs): return await self.api.list_threads(**kwargs)
    async def read_thread(self, tid): return await self.bridge.read_thread(tid)
    async def resume_thread(self, tid): return await self.bridge.resume_thread(tid)
    async def start_turn(self, tid, message): return await self.bridge.start_turn(tid, message)
    async def interrupt_turn(self, tid, turn_id):
        before = await self.bridge.read_thread(tid)
        expected_goal = before.get('goal')
        if expected_goal is not None:
            if not self.goal_control_enabled or expected_goal.get('status') != 'active':
                raise ProtocolError('Goal budget pause requires explicitly enabled, ready Goal recovery')
            await self.configure_goal_control(True)
            if not self.goal_pause_control:
                raise ProtocolError('Goal accessibility recovery unavailable; Goal was not paused')
        return await self.bridge.interrupt_turn(tid, turn_id,
            expected_goal=expected_goal, allow_goal=expected_goal is not None and self.goal_pause_control)

    async def _invoke_goal_helper(self, payload):
        """Invoke only the explicitly configured local native helper, never a shell."""
        executable = os.getenv('CODEX_RELAY_NATIVE_HELPER')
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise ProtocolError('Goal accessibility helper is unavailable')
        proc = await asyncio.create_subprocess_exec(executable, '--goal-accessibility',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), 40)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0 or len(stdout) > 1024 * 1024:
            raise ProtocolError('Goal accessibility helper failed')
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError('Invalid Goal accessibility helper response') from exc
        if not isinstance(result, dict):
            raise ProtocolError('Invalid Goal accessibility helper response')
        return result

    async def configure_goal_control(self, enabled, request_permission=False):
        self.goal_control_enabled = bool(enabled)
        try:
            result = await self._invoke_goal_helper({'action': 'probe',
                'request_permission': bool(request_permission and enabled)})
            supported, trusted = result.get('supported') is True, result.get('trusted') is True
            available = not result.get('error') and result.get('running') is not False
            reason = result.get('reason') or result.get('error') or (
                'Ready' if supported and trusted else 'Goal accessibility permission or support unavailable')
        except Exception as exc:
            supported = trusted = available = False
            reason = str(exc)
        self.goal_control = self.goal_control_enabled and supported and trusted and available
        self.goal_pause_control = self.goal_control
        self.goal_capability = dict(supported=supported, enabled=self.goal_control_enabled,
                                    trusted=trusted, ready=self.goal_control, pause_ready=self.goal_pause_control, reason=goal_control_reason(reason) if self.goal_control_enabled else 'Goal 自动控制尚未启用；开启后才会按所选策略操作目标。', reason_code=reason)
        return self.goal_capability

    async def resume_goal(self, tid, expected_turn_id, expected_goal):
        """Press the verified native Goal control and confirm owner execution.

        A button acknowledgement or metadata-only active response is not success.
        Eligibility for user-paused goals remains the watchdog's responsibility.
        """
        if not self.goal_control_enabled:
            raise GoalControlError('Goal 辅助功能控制未启用，未点击恢复。', action_submitted=False, reason_code='goal_control_disabled')
        await self.configure_goal_control(True)
        if not self.goal_control:
            raise GoalControlError(self.goal_capability['reason'], action_submitted=False, reason_code='goal_control_unavailable')
        before = await self.bridge.read_thread(tid)
        latest = (before.get('turns') or [{}])[-1]
        if before.get('id') != tid or latest.get('id') != expected_turn_id or before.get('goal') != expected_goal:
            raise GoalControlError('目标或轮次已经变化，未点击恢复。', action_submitted=False, reason_code='goal_or_turn_changed_before_click')
        if not isinstance(expected_goal, dict) or expected_goal.get('status') not in {'usageLimited', 'paused'}:
            raise GoalControlError('目标当前不符合恢复条件，未点击恢复。', action_submitted=False, reason_code='goal_not_eligible')
        if before.get('status', {}).get('type') != 'idle' or before.get('requests'):
            raise GoalControlError('会话正在运行或等待用户输入，未点击恢复。', action_submitted=False, reason_code='thread_busy_before_click')
        objective = expected_goal.get('objective')
        if not isinstance(objective, str) or not objective.strip():
            raise GoalControlError('无法读取完整目标内容，未点击恢复。', action_submitted=False, reason_code='goal_objective_unavailable')
        elapsed=expected_goal.get('timeUsedSeconds')
        import math
        if isinstance(elapsed,bool) or not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0 or elapsed!=int(elapsed):
            raise GoalControlError('目标计时信息不可验证，未点击恢复。',action_submitted=False,reason_code='goal_duration_unavailable')
        clicked = await self._invoke_goal_helper({'action': 'resume', 'method':'verified_click', 'thread_id': tid,
            'name': before.get('name'), 'objective': objective,
            'expected_goal_status': expected_goal['status'],'expected_time_used_seconds':int(elapsed)})
        if clicked.get('clicked') is not True or clicked.get('source') != 'accessibility':
            code = clicked.get('error') or clicked.get('reason') or 'goal_action_acknowledgement_invalid'
            raise GoalControlError(goal_control_reason(code),
                action_submitted=False if clicked.get('clicked') is False else None,
                reason_code=code)
        try:
            identity = {key: expected_goal.get(key) for key in ('threadId', 'createdAt', 'objective')}
            deadline = asyncio.get_running_loop().time() + self.goal_confirmation_timeout
            while True:
                after = await self.bridge.read_thread(tid)
                if after.get('id') != tid:
                    raise GoalControlError('已操作目标恢复按钮，但会话身份变化，执行结果待确认。', action_submitted=True, reason_code='thread_changed_after_click')
                goal = after.get('goal')
                turn = (after.get('turns') or [{}])[-1]
                if goal is not None and {key: goal.get(key) for key in identity} != identity:
                    raise GoalControlError('已操作目标恢复按钮，但目标发生变化，执行结果待确认。', action_submitted=True, reason_code='goal_changed_after_click')
                advanced = bool(turn.get('id') and turn.get('id') != expected_turn_id)
                active = isinstance(goal, dict) and goal.get('status') == 'active'
                finished = turn.get('status') == 'completed' and (
                    goal is None or goal.get('status') == 'complete')
                if advanced and ((active and turn.get('status') in {'inProgress', 'completed'}) or finished):
                    return {'goal': goal, 'turn': {'id': turn['id'], 'status': turn.get('status')},
                            'goal_confirmed': True, 'source': 'accessibility',
                        'control_method':'verified_click','acknowledgement':clicked.get('acknowledgement')}
                if asyncio.get_running_loop().time() >= deadline:
                    raise GoalControlError('已操作目标恢复按钮，但尚未确认新的目标执行轮次；不会自动重试。', action_submitted=True, reason_code='goal_execution_unconfirmed')
                await asyncio.sleep(self.goal_confirmation_interval)
        except GoalControlError:
            raise
        except Exception as exc:
            raise GoalControlError('已操作目标恢复按钮，但后续状态读取失败；执行结果待确认，不会自动重试。',
                action_submitted=True, reason_code='goal_confirmation_read_failed') from exc
