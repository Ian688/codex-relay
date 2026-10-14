"""Presentation contract: raw observations, decisions and action evidence stay separate."""
from __future__ import annotations
import json
import time
from .rules import state_name, quota_failure, strategy_gate, quota_gate, stale_goal_failure, quota_used, job_safe, expected_turn_id


def describe(thread, policy, jobs, *, limits=None, monitoring=False, selected=False,
             check=None, observed_at=None, goal_control=False,goal_pause_control=False):
    latest = (thread.get('turns') or [{}])[-1]
    goal = thread.get('goal') or {}
    related = [j for j in jobs if j['thread_id'] == thread['id']]
    # Only a job for the current interrupted/follow-up turn can override live facts.
    current = next((j for j in related if latest.get('id') in
                    {expected_turn_id(j),j.get('resumed_turn_id')}), None)
    if current is None and goal:
        identity={k:goal.get(k) for k in ('threadId','createdAt','objective')}
        current=next((j for j in related if j['status']=='done' and
                      json.loads(j.get('context') or '{}').get('action_kind')=='goal_resume' and
                      json.loads(j.get('context') or '{}').get('goal_episode',{}).get('identity')==identity),None)
    check = check or {}
    context = json.loads(current.get('context') or '{}') if current else {}
    result = dict(turn_status=state_name(latest.get('status')) or 'unknown',
                  goal_status=goal.get('status') or ('unknown' if goal else 'none'),
                  relay_status='observing' if monitoring and selected else 'paused',
                  reason='',reason_code='',action_kind=context.get('action_kind',''),
                  checked_at=check.get('checked_at'),observed_at=observed_at or time.time(),
                  source='desktop_snapshot',goal_control=goal_control,goal_pause_control=goal_pause_control,
                  evidence={'turn_id':latest.get('id'),'goal_updated_at':goal.get('updatedAt'),
                            'job_status':current.get('status') if current else None,
                            'interruption_key':current.get('failed_turn_id') if current else None,
                            'expected_turn_id':expected_turn_id(current) if current else None,
                            'action_receipt':context.get('action_receipt'),
                            'action_failure':context.get('action_failure')})
    def explain(code, message, status=None):
        result.update(reason_code=code,reason=message)
        if status: result['relay_status']=status
        return result
    if not selected:
        return explain('not_selected','未加入监控；Relay 不会操作这个会话。','unselected')
    if check.get('error'):
        return explain('read_error','最近监控检查未成功；未据此发送新的恢复请求。','unavailable')
    if policy.get('goal_only') and not goal:
        return explain('goal_filter','此会话没有正式 Goal，已被目标筛选排除。','filtered')
    goal_record=context.get('recovery_kind')=='goal_resume' or context.get('action_kind')=='goal_resume'
    if not goal_record and stale_goal_failure(thread,related) and (not current or current['status'] not in {'running','done','resuming','pausing'}):
        return explain('stale_goal','旧 Goal 中断已处理；历史等待记录不符合恢复条件，启动监控后将取消。','stale_record')
    owned_pause = bool(current and current.get('error_code')=='relayBudgetPause' and job_safe(thread,current))
    if not owned_pause and goal.get('status') in {'paused','complete','blocked','budgetLimited'} and (not current or current['status'] not in {'running','done','resuming','pausing'}):
        return explain('goal_not_eligible','Goal 已暂停、完成或需人工处理；Relay 不会自动恢复。','paused')
    if goal and (not current or current['status'] not in {'running','done','resuming','pausing'}):
        used=quota_used(limits,policy.get('window','primary')) if limits else None
        budget_stop=policy.get('mode')=='reserve' and used is not None and 100-used<=policy.get('stop_remaining',20)
        if (not goal_control and (owned_pause or quota_failure(thread)) and not stale_goal_failure(thread,related)) or (budget_stop and not goal_pause_control and state_name(latest.get('status'))=='inProgress'):
            if goal_control:
                return explain('goal_budget_pause_unavailable','Goal 辅助功能恢复已启用；当前不会按预算主动暂停 Goal。')
            result['evidence']['capability']='goal_control_unavailable'
            return explain('goal_control_unavailable','当前通道只能读取 Goal，无法确认恢复。请在 Codex 操作目标；Relay 不会用普通“继续”替代，也不会主动暂停 Goal。','manual_goal')
    if current:
        status = current['status']; result['relay_status']=status
        result['evidence']['detail']=current.get('detail')
        result['evidence']['attempts']=current.get('attempts',0)
        if status == 'waiting':
            result['retry_at']=current['retry_at']
            detail=current.get('detail') or ''
            if 'cooldown' in detail:
                return explain('cooldown','恢复正在等待防重复冷却；到时仍会重新核验。')
            if limits is not None:
                allowed,_,why=quota_gate(limits)
                if not allowed:
                    message='额度接口尚未允许使用，等待恢复。'
                    if 'exhausted' in why: message='仍有账号额度窗口耗尽，等待服务端恢复。'
                    if 'spend control' in why: message='账号支出限制已触发，需要人工处理。'
                    return explain('quota_wait',message)
                ready,_=strategy_gate(limits,policy,current)
                if not ready:
                    return explain('strategy_wait','等待所选额度窗口下一次刷新确认。' if policy.get('resume_mode')=='reset'
                                   else f"等待剩余额度达到 {policy.get('resume_remaining',50):g}%。")
            return explain('pending_check','已记录受限 Goal，等待下一次监控核验。' if goal_record else '已记录额度中断，等待下一次监控核验。')
        if status in {'resuming','pausing'}:
            return explain('action_pending','动作正在提交；结果未确认前不会重复发送。')
        if status == 'running':
            return explain('goal_resumed' if result['action_kind']=='goal_resume' else 'message_started',
                'Goal 恢复已确认；正在观察后续执行。' if result['action_kind']=='goal_resume' else '已发送“继续”并确认新轮次；不代表 Goal 已恢复。')
        if status == 'done':
            if result['action_kind']=='goal_resume':
                return explain('goal_resumed','已确认 Goal 恢复并启动新轮次；当前目标状态与轮次进度分别显示。','goal_restored')
            return explain('turn_ended','续接轮次已结束；Goal 是否完成以上方独立状态为准。')
        if status == 'needs_attention':
            failure=context.get('action_failure') or {}
            if result['action_kind']=='goal_resume' and failure.get('submitted') is False:
                return explain('goal_not_clicked','Goal 恢复按钮未通过核验，未点击。请检查 Codex 窗口后手动处理。')
            if result['action_kind']=='goal_resume' and failure.get('submitted') is True:
                return explain('goal_unconfirmed','已尝试点击 Goal 恢复，但运行结果尚未确认；不会自动重复点击。')
            return explain('needs_attention','上次动作结果不明确或执行异常，需要检查原会话；不会自动重发。')
        if status in {'superseded','disabled'}:
            if 'Old Goal quota episode' in (current.get('detail') or ''):
                return explain('stale_goal','旧 Goal 中断已处理，已取消重复恢复。')
            return explain('cancelled','原恢复记录已取消；当前状态将重新检查。')
    if goal.get('status') in {'paused','complete','blocked','budgetLimited'}:
        return explain('goal_not_eligible','Goal 已暂停、完成或需人工处理；Relay 不会自动恢复。')
    if stale_goal_failure(thread,related):
        return explain('stale_goal','旧 Goal 中断已处理，尚无新的中断依据；不会重复续接。')
    if quota_failure(thread):
        return explain('quota_detected','检测到额度中断；启动监控后按策略核验恢复条件。')
    if goal.get('status')=='usageLimited':
        return explain('goal_unresolved','Goal 仍显示额度受限，尚未确认新的额度中断。')
    if state_name(latest.get('status'))=='inProgress':
        return explain('no_intervention','正在运行；尚未触发额度干预。')
    return explain('no_intervention','最近一轮已结束或会话空闲；未触发额度干预。')


def event_message(event):
    action=event['action']
    labels={'watch':'已加入监控','unwatch':'已移出监控','policy':'已保存额度策略',
            'pause':'Relay 已按额度策略暂停','resume':'已发送“继续”并确认新轮次',
            'goal_resume':'已调用 Goal 恢复并确认执行',
            'monitor_start':'已开始监控所选会话','monitor_stop':'已暂停监控，Codex 任务继续运行',
            'stale_goal':'旧 Goal 中断已处理，已取消重复恢复',
            'attention':'恢复结果需人工检查', 'goal_control':'Goal 辅助功能控制设置已更新', 'goal_manual':'Goal 需要在 Codex 中恢复；未发送普通继续'}
    return labels.get(action,action)
