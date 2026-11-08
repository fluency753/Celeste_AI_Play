"""Engine-independent HUD controls; protocol operations live in runner adapters.

Adapters provide _interrupt_user, _steer_user and _validate_effort. Game calls
share action_lock with this controller, so pause waits for the current call.
"""
from __future__ import annotations

import asyncio
import json
import time
import threading
from dataclasses import dataclass

from .control import RunControl
from .button_prompts import notification


@dataclass(frozen=True)
class RuntimeCapabilities:
    fast: bool = False
    live_steering: bool = False
    effort_levels: tuple[str, ...] | None = None


class SessionControls:
    capabilities = RuntimeCapabilities()

    def _begin_model_turn(self, **timing):
        if tracker := getattr(self, 'usage_tracker', None):
            tracker.begin_turn()
        # Chat keeps the game gate closed while model work is independently active.
        if self.chat_only:
            self.metrics.resume(quota=False)
        self.metrics.turn_begin(**timing)
        if self.chat_only:
            self.metrics.update(status='thinking')

    def _end_model_turn(self):
        self.metrics.turn_end()
        if self.chat_only and not getattr(self, 'quota_until', None):
            self.metrics.pause()
            self.metrics.update(status='chat_idle')

    def close_events(self):
        events = getattr(self, '_events', None)
        if events and not events.closed:
            events.close()

    def _init_controls(self, *, disabled=False, start_paused=False):
        if self.metrics.state.tool_calls is None:
            self.metrics.update(tool_calls=0)
        from .execution import workspace_capabilities
        from .note_reminders import NoteReminders
        readable = workspace_capabilities(self.cfg).workspace_read and not getattr(self, 'naming_only', False)
        workspace = getattr(self.session, 'workspace', None) if self.session is not None else None
        if readable and workspace is None:
            workspace = getattr(self.cfg, 'workspace_dir', None)
        self.note_reminders = NoteReminders(workspace if readable else None,
                                            lambda: getattr(self.cfg, 'notes_warn_percent', 60),
                                            None if getattr(self, 'naming_only', False)
                                            else getattr(self.cfg, '_user_note', None))
        self.completed_preparation = False
        self.control = None if disabled else RunControl(self.run_dir)
        if self.control:
            permissions = self.control.action_permissions_preference()
            if permissions is not None:
                from .action_policy import ActionPermissions, configure
                configure(self.cfg, ActionPermissions.read(permissions))
            detour = self.control.detour_preference()
            if detour is not None:
                self.cfg.allow_detour = detour
        self.fast_mode = self.control.fast_preference() if self.control else None
        self.manual_effort = self.control.effort_preference() if self.control else None
        self.turn_fast = None
        self._fast_yield_requested = None
        self.stop_requested = False
        self.stop_reason = ""
        self.interrupted_for_resume = False
        self.parked_for_switch = False
        self.interrupt_ready = asyncio.Event()
        self.goal_finishing = False
        self.paused, self.turn_active, self.user_interrupted = start_paused or self.chat_only, False, False
        self.pending_messages = []
        self.resume_notice = ''
        self.action_lock = asyncio.Lock()
        self.pause_at, self.paused_seconds = None, 0.0
        self.control_task = None
        self.heartbeat_task, self.pausing = None, False
        self.token_drift = None
        self._token_task, self._token_closing = None, False
        self._token_compacting = False
        self._token_pause_serial = getattr(self.metrics, 'pause_serial', 0)
        self._token_event = asyncio.Event()
        if (getattr(self.cfg, 'token_drift', False) and self.session is not None
                and not self.chat_only and not getattr(self, 'prepare_only', False)):
            from pathlib import Path
            from .token_drift import TokenDrift
            previous = getattr(self.metrics.state, 'resumed_from', None)
            self.token_drift = TokenDrift(self.run_dir / 'token_drift.json', self.metrics,
                self.cfg.tokens_per_frame, Path(previous) / 'token_drift.json' if previous else None)
            if not self.metrics.playing:
                self.token_drift.exclude_window('preparation')
            self.session.token_guard = self._token_motion_eligible
            self.session.flush_token_drift = self._flush_token_drift_locked
        if self.session is not None:
            self.session.note_reminders = self.note_reminders
            self.session.control_guard = self._game_guard
            self.session.tool_guard = self._tool_guard
            self.session.request_pause = self._agent_pause
            self.session.effort_policy = self._agent_effort_policy
            self.session.effort_levels = lambda: self._effort_status()['effort_levels'] or []

    def _token_motion_eligible(self):
        return bool(self.token_drift is not None and not self._token_closing
                    and not self._token_compacting and not self._game_guard()
                    and self.metrics.playing and self.metrics._pause_start is None
                    and not getattr(self, 'net_down_since', None)
                    and not self.token_drift.unknown
                    and not getattr(self.session, 'reload_observation_required', False)
                    and not getattr(self.session, 'token_observation_required', False))

    def _token_epoch(self):
        obs = getattr(self.session, 'last_obs', None) or {}
        level = obs.get('level') or {}
        return (getattr(self.session, 'game_process_id', None), getattr(self.session, 'reload_epoch', 0),
                getattr(self.session, 'token_motion_epoch', 0), level.get('sid'), level.get('room'))

    def _record_output_usage(self, total, latest):
        if self.token_drift is None:
            return
        self._sync_token_pause()
        eligible = bool(self.turn_active and self._token_motion_eligible())
        self.token_drift.receive(total, latest, f'{self.cfg.engine}:{self.session_id}', eligible, self._token_epoch())
        if self.token_drift.pending:
            self._token_event.set()

    def _exclude_token_window(self, reason):
        if self.token_drift is not None:
            self.token_drift.exclude_window(reason)

    def _sync_token_pause(self):
        serial = getattr(self.metrics, 'pause_serial', 0)
        if serial != self._token_pause_serial:
            self._exclude_token_window('pause_window')
            self._token_pause_serial = serial

    def _start_token_worker(self):
        if self.token_drift is None:
            return
        if self.token_drift.unknown:
            self.paused = True
            self.metrics.pause()
            self.metrics.update(status='paused')
            self._public_message('token-unknown', '上次 Token 推进的执行范围未确认，游戏保持暂停；请核验账本，不要重发该批输入。')
        self._token_task = asyncio.create_task(self._token_worker())

    async def _finish_token_worker(self):
        self._token_closing = True
        if self.token_drift is None:
            return
        self.session._token_cancel.set()
        self._token_event.set()
        task = self._token_task
        cancelled = False
        if task is not None:
            # A to_thread call cannot be cancelled safely. Keep the connection
            # and action ownership until its result has been committed/marked unknown.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
            task.result()
        self.token_drift.discard('runner_finished')
        if cancelled:
            raise asyncio.CancelledError

    async def _token_worker(self):
        while not self._token_closing:
            await self._token_event.wait()
            self._token_event.clear()
            if self._token_closing:
                return
            try:
                async with self.action_lock:
                    tool_lock = getattr(self.session, 'tool_lock', None)
                    if tool_lock is not None and tool_lock is not self.action_lock:
                        async with tool_lock:
                            await self._flush_token_drift_locked()
                    else:
                        await self._flush_token_drift_locked()
            except Exception as error:
                self.paused = True
                self.metrics.pause()
                self.metrics.update(status='paused')
                self._public_message('token-error', f'Token 推进未完成，游戏保持暂停：{error}。已执行范围见 token_drift.json，不自动重放。')
                # Unknown batches stay ineligible; retain the consumer for
                # recoverable pre-execution failures after user inspection.
                continue

    async def _flush_token_drift_locked(self):
        """The caller owns both existing async game locks; never reacquire them."""
        ledger = self.token_drift
        if ledger is None:
            return
        self._sync_token_pause()
        while ledger.pending:
            if not self._token_motion_eligible():
                ledger.exclude_window('control_state_changed')
                return
            before = getattr(self.session, 'last_obs', None) or {}
            frames = ledger.begin(self._token_epoch(), {'frame': before.get('frame'), 'epoch': self._token_epoch()})
            if frames is None:
                return
            worker = asyncio.create_task(asyncio.to_thread(self.session.token_frame_step, frames))
            try:
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    self.session._token_cancel.set()
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                    result = worker.result()
                    ledger.commit(result)
                    raise
                ledger.commit(result)
            except Exception as error:
                ledger.fail(error)
                self.paused = True
                self.metrics.pause()
                self.session.token_observation_required = True
                raise
            if not self._token_closing and self._token_motion_eligible():
                self.metrics.update(status='tool' if self.metrics._tools else 'thinking')

    async def _agent_effort_policy(self, value):
        from .game_policy import effort_denial
        if note := effort_denial(self, value):
            raise ValueError(note)
        await self._validate_effort(value)

    def _public_message(self, ident, text):
        from .telemetry import publish
        if text.strip():
            publish(self.run_dir, 'public:' + str(ident), text)
            if self.control:
                self.control.reply(str(ident), text)

    def _notes_window(self):
        from .model_profile import model_context_limit
        windows = [w for w in (self.metrics.state.context_window, model_context_limit(getattr(self, 'cfg', None))) if w]
        return min(windows) if windows else None

    def _notes_usage(self):
        notes = getattr(self, 'note_reminders', None)
        if notes is not None:
            notes.usage(self.metrics.state.context_tokens, self._notes_window())

    def _notes_response(self, response):
        notes = getattr(self, 'note_reminders', None)
        text = notes.take() if notes is not None else ''
        if text:
            response['contentItems'].append({'type': 'inputText', 'text': text})
        return response

    def _public_compaction(self, ident, *, completed=False, reminder_key=None):
        """ident：历史里这次压缩的编号（开始与完成用同一个，完成时替换"正在压缩"那一条）；
        reminder_key：笔记提醒按它去重（Claude Code 的 compact_boundary 带 uuid），不给时用 ident。"""
        self._token_compacting = not completed
        if not completed and self.token_drift is not None:
            self._exclude_token_window('compacting')
        notes = getattr(self, 'note_reminders', None)
        resend = False
        if completed and notes is not None:
            resend = notes.compacted(ident if reminder_key is None else reminder_key)
        from .telemetry import publish
        done = '上下文压缩完成。' + ('局间留言原文会在智能体压缩后的第一次工具调用时重发给它。' if resend else '')
        publish(self.run_dir, 'system:compaction:' + str(ident),
                done if completed else '正在压缩上下文，完成后继续处理对话…',
                role='system', kind='系统')

    def _game_guard(self):
        return self._tool_guard('act')

    def _tool_guard(self, name, arguments=None):
        from .game_policy import denial
        return denial(self, name, arguments)

    def _turn_message(self, fallback, rows, *, opening=False, retain_fallback=False, context=''):
        """Choose one lifecycle instruction, keeping user input and separate control facts."""
        supplied = '\n\n'.join(row['text'] for row in rows)
        notice, self.resume_notice = self.resume_notice, ''
        if opening:
            goals = getattr(self.session, 'goals', None)
            goal = goals.snapshot() if goals is not None else None
            facts = ''
            if goal and goal['active'] and getattr(self, 'announce_goal', True):
                from .goals import describe_goal
                facts = '（主控）当前已登记的试玩目标：' + describe_goal(goal) + '。'
                if goal['conditions']:
                    facts += ('自动停止条件将在首次进入关卡后启用，由主控核验。' if goal.get('awaiting_entry')
                              else '自动停止条件由主控核验。')
            parts = [fallback, facts, supplied]
        elif retain_fallback:
            parts = [fallback, supplied]
        elif notice:
            parts = [supplied, notice]
        else:
            parts = [supplied or fallback]
        return '\n\n'.join(part for part in [context, *parts] if part)

    async def _stopped_reason(self):
        if self.interrupted_for_resume:
            await self.interrupt_ready.wait()
        return self.stop_reason or "聊天会话切换"

    def _goal_report_grace(self):
        accepted = getattr(self.session, 'goal_reported_at', None)
        # Allow the acceptance to reach the originating turn before closing the executor.
        return max(0, 15 - (time.monotonic() - accepted)) if accepted is not None else None

    def _agent_pause(self, reason):
        """智能体用 stop_run 报告任务阻塞（在工具线程里调用）：与用户在 HUD 暂停相同，试玩暂停、章节内保持冻结，菜单正常运行，
        由用户在 HUD 继续或停止；只是不打断当前轮（工具结果已让智能体结束本轮）。"""
        if getattr(self, 'prepare_only', False) or self.chat_only:
            return False
        self.paused = True
        if self.pause_at is None:
            self.pause_at = time.monotonic()
            self.metrics.pause()
        self.metrics.update(status='paused')
        return True

    def _workspace_notice(self):
        from .execution import workspace_capabilities
        caps = workspace_capabilities(self.cfg)
        facts = ''
        if caps.workspace_read:
            facts += '可以按用户要求读取、查找工作区文件。'
        if caps.workspace_write:
            facts += '可以编辑工作区文件。'
        if caps.programs:
            facts += '工作区程序可按要求运行，继续遵守沙箱和单次时限。'
        if not caps.workspace_read:
            facts += '当前执行环境没有工作区工具通道，只能依据已有对话回复。'
        return facts

    def _paused_notice(self):
        facts = '试玩仍处于用户暂停状态。不观察、推进或操作游戏，不请求在线模拟预测。'
        facts += self._workspace_notice()
        return notification('paused', facts=facts, cfg=self.cfg)

    async def _heartbeat(self):
        while True:
            self.control.heartbeat(paused=self.paused and not self.pausing,
                phase="pausing" if self.pausing else "chatting" if self.turn_active else "idle",
                session_id=self.session_id, chat_only=self.chat_only,
                **self._fast_status(),
                **self._effort_status(), **self._goal_status(), **self._cutscene_status(), **self._chapter_status(), **self._sim_status(), **self._act_limit_status(), **self._chapter_reset_status(), **self._detour_status(), **self._bridge_reload_status())
            await asyncio.sleep(.5)

    async def _controls(self):
        try:
            while True:
                await self._check_goal()
                for row in self.control.pending():
                    try:
                        if row["kind"] in {"goal", "goal_clear", "stop"}:
                            if self.session is None or self.session.done:
                                raise ValueError("当前未在试玩，不能修改停止目标")
                            if row["kind"] == "stop":
                                self.session.goals.stop_now()
                                await self._check_goal()
                                self.control.mark(row["id"], "submitted", "已停止游戏，正在保存录像")
                            else:
                                if row["kind"] == "goal_clear":
                                    self.session.goals.clear()
                                    note = notification('goal_clear', facts="用户已清除自定义目标及停止条件，撤回先前自定义要求；保留主控最初指定的章节范围。", cfg=self.cfg)
                                else:
                                    goal = json.loads(row["text"])
                                    await asyncio.to_thread(self.session.set_goal, goal)
                                    from .goals import describe_goal
                                    note = notification('goal', facts="用户更新了目标：" + describe_goal(goal) + "。停止条件由主控核验。", cfg=self.cfg)
                                await self._check_goal()
                                if not self.session.done:
                                    self.pending_messages.append({**row, "text": note})
                                    self.control.mark(row["id"], "waiting", "目标已生效，正在通知 agent")
                                else:
                                    self.control.mark(row["id"], "submitted", "目标条件已满足，正在保存录像")
                        elif row["kind"] == "chapters":
                            if self.session is None or self.session.done or self.chat_only:
                                raise ValueError("当前没有在试玩，不能改章节")
                            data = json.loads(row["text"])
                            if "segments" in data:
                                note = await asyncio.to_thread(self.session.set_plan, data["segments"])
                            else:
                                note = await asyncio.to_thread(self.session.set_chapter_queue, data.get("queue", []))
                            self.pending_messages.append({**row, "text": notification('chapters', facts=note, cfg=self.cfg)})
                            self.control.mark(row["id"], "waiting", "要通关的范围已更新，正在通知 agent")
                        elif row['kind'] == 'bridge_reload':
                            await self._reload_bridge(row)
                        elif row['kind'] in {'chapter_reset', 'action_permissions'}:
                            if self.session is None or self.session.done or self.chat_only:
                                raise ValueError('当前没有在试玩，不能改章节重置策略')
                            if row['kind'] == 'action_permissions' and not self._chapter_reset_status()['action_policy_supported']:
                                raise ValueError('当前会话未加载新版操作策略，不能独立修改权限')
                            from .action_policy import ActionPermissions, configure, permissions_for
                            permissions = (ActionPermissions.legacy(row['text'] == 'allow') if row['kind'] == 'chapter_reset'
                                           else permissions_for(self.cfg).update(json.loads(row['text'])))
                            from .detour import enabled as detour_enabled
                            if detour_enabled(self.cfg) and not permissions.return_to_map:
                                raise ValueError('计划外地图开着时必须允许返回地图；要禁止返回地图，请先关闭计划外地图')
                            async with self.action_lock:
                                self.control.set_action_permissions(permissions.snapshot())
                                configure(self.cfg, permissions)
                                configure(self.session.cfg, permissions)
                            note = notification('action_permissions', facts='用户更新了' + permissions.instructions(experiments=getattr(self.session.cfg, 'play_scope', 'chapter') == 'visited'), cfg=self.cfg)
                            self.pending_messages.append({**row, 'text': note})
                            self.control.mark(row['id'], 'waiting', '章节重置策略已生效，正在通知 agent')
                        elif row['kind'] == 'detour':
                            if not self._detour_status()['detour_control']:
                                raise ValueError('当前没有在单章或顺序范围内试玩，或会话未加载新版操作策略，不能修改计划外地图许可')
                            allowed = row['text'] == 'on'
                            if allowed and not getattr(self.session, 'map_entry_set_supported', False):
                                raise ValueError('计划外地图需要桥接 1.10.0 的地图集合门禁，请更新桥接后再开启')
                            from .action_policy import configure, permissions_for
                            from .detour import notice
                            permissions_note = ''
                            async with self.action_lock:    # 与权限相同：只在动作之间生效，正在执行的动作沿用原门禁
                                permissions = permissions_for(self.cfg)
                                if allowed and not permissions.return_to_map:
                                    # 开启时一并允许暂停菜单的"返回地图"，其他两项不变
                                    permissions = permissions.update({'return_to_map': True})
                                    self.control.set_action_permissions(permissions.snapshot())
                                    configure(self.cfg, permissions)
                                    configure(self.session.cfg, permissions)
                                    permissions_note = '同时开启了返回地图权限。' + permissions.instructions()
                                self.control.set_detour(allowed)
                                self.cfg.allow_detour = self.session.cfg.allow_detour = allowed
                            self.pending_messages.append({**row, 'text': notification('action_permissions', facts=notice(self.session.cfg, allowed, permissions_note, travel=bool(getattr(self.session, 'travel_tool', False))), cfg=self.cfg)})
                            self.control.mark(row['id'], 'waiting', '计划外地图许可已生效，正在通知 agent')
                        elif row['kind'] == 'act_limit':
                            if not self._act_limit_status()['act_limit_control']:
                                raise ValueError('当前没有在试玩，不能修改动作帧数上限')
                            async with self.action_lock:
                                snapshot = await asyncio.to_thread(self.session.update_act_limit, json.loads(row['text']))
                                setattr(self.cfg, snapshot['key'], snapshot['frames'])
                                self.cfg.tuning = dict(self.session.cfg.tuning)
                            self.control.mark(row['id'], 'submitted', '动作帧数上限已生效，无需重启主控或录制')
                            if snapshot['changed']:
                                self.pending_messages.append({**row, 'next_turn': True,
                                    'text': notification('act_limit', facts=f"单次 act 帧数范围：{snapshot['min_frames']}～{snapshot['frames']} 帧，配置项 {snapshot['key']}。"
                                                         '新范围优先于旧工具说明中的数字；自写推演和模拟额度未改变。', cfg=self.cfg)})
                        elif row['kind'] == 'sim_update':
                            if not self._sim_status()['sim_control']:
                                raise ValueError('当前主控未开启有限模拟辅助，不能在线修改推演设置')
                            async with self.action_lock:
                                snapshot = await asyncio.to_thread(self.session.update_simulation, json.loads(row['text']))
                            self.control.mark(row['id'], 'submitted', '推演设置已生效，无需重启主控或录制')
                            # 只是通知：不能向进行中的一轮追加输入的执行器（Claude）不为它打断，下一轮带上
                            self.pending_messages.append({**row, 'next_turn': True, 'text': notification('sim_update', facts='用户已实时更新推演设置：' + snapshot['rules'] +
                                f"当前余额{snapshot['balance']}。新规则从下一次推演生效。", cfg=self.cfg)})
                        elif row["kind"] == "message":
                            self.pending_messages.append(row)
                            self.control.mark(row["id"], "waiting", "等待提交给当前会话")
                        elif row["kind"] == "park":
                            if not self._can_park():
                                raise ValueError("请先暂停试玩、等当前轮次结束，并确认使用了独立存档槽")
                            async with self.action_lock:
                                obs = (await asyncio.to_thread(self.session.bridge_call, "observe",
                                                               image=False, entities=False, tiles=False))["obs"]
                                from .parked_run import stable_menu
                                if not stable_menu(obs):
                                    raise ValueError("请切到 agent 剧情模式并允许保存退出，让 player 原生保存退出到菜单后再暂停并挂起")
                                self.stop_reason = "挂起以切换工作区"
                                self.parked_frame = obs['frame']
                                self.parked_for_switch = True
                                self.stop_requested = True
                                ending = "App Server" if getattr(self.cfg, 'engine', None) == 'codex' else "Claude Code 会话"
                                self.control.mark(row["id"], "submitted", f"正在结束 {ending}、保存录像并交还游戏")
                            return
                        elif row["kind"] == "interrupt":
                            # 显式中断独立于普通停止；收尾时保留接管，可 --resume 接着玩。
                            if not getattr(self, "supports_interrupt", False):
                                raise ValueError("当前执行器不支持中断后续跑，请用“停止并保存”")
                            if self.chat_only or self.session is None or self.session.done:
                                raise ValueError("当前没有在试玩")
                            self.stop_reason = "用户中断（章节内保持冻结，菜单正常运行，可续跑）"
                            self.interrupted_for_resume = True
                            self.stop_requested = True
                            try:
                                await self._interrupt_user()
                                async with self.action_lock:   # 正在执行的动作先走完，游戏停在动作之间
                                    if self.session.done:
                                        self.interrupted_for_resume = False
                                        self.stop_reason = self.session.done_reason
                                        raise ValueError("本次试玩已结束，中断请求未生效")
                                self.control.mark(row["id"], "submitted", "正在结束本次运行，章节内保持冻结，菜单正常运行")
                            finally:
                                self.interrupt_ready.set()
                        elif row["kind"] == "detach":
                            if not self.chat_only:
                                raise ValueError("试玩期间不能切走会话")
                            self.stop_requested = True
                            await self._interrupt_user()
                            self.control.mark(row["id"], "submitted", "聊天会话已释放")
                        elif row["kind"] == "fast":
                            if not self.capabilities.fast:
                                raise ValueError("当前执行器不支持 Fast")
                            self.fast_mode = row["text"] == "on"
                            self.control.set_fast(self.fast_mode)
                            self.control.mark(row["id"], "submitted", "Fast 设置已保存，等待模型换轮" if self.turn_active else "Fast 设置已保存，下次模型请求生效")
                        elif row['kind']=='cutscene_mode':
                            if row['text'] == 'agent' and not getattr(self.session, 'cutscene_tool', True):
                                raise ValueError('本次运行没有开放剧情工具，剧情由主控自动跳过')
                            if row['text'] == 'agent' and not getattr(self.session, 'story_input_supported', True):
                                raise ValueError('剧情交给智能体需要桥接 Mod 1.0.4 或更新版本（按输入节点交接剧情），请更新桥接后重启游戏')
                            if row['text'] == 'auto' and (getattr(self.cfg, 'play_scope', 'chapter') == 'visited' or getattr(self.cfg, 'completion_policy', 'stop') == 'continue'):
                                raise ValueError('自由探索或通关后继续由智能体操作结算与选章，不能切换为自动剧情模式')
                            self.cfg.cutscene_mode=row['text']
                            self.control.set_cutscene_mode(row['text'])
                            self.control.mark(row['id'],'submitted','剧情模式已更新，当前菜单操作完成后生效')
                            if self.session is not None:
                                self.session.cfg.cutscene_mode=row['text']
                                self.pending_messages.append({**row,'text':notification('cutscene_mode', facts='用户将剧情处理切换为 '+row['text']+'。agent模式下自行决定观看、推进、选择选项或跳过；不得用advance代选对话选项。', cfg=self.cfg)})
                        elif row['kind'] == 'effort':
                            value=None if row['text']=='auto' else row['text']
                            if value is not None:
                                await self._validate_effort(value)
                            self.manual_effort=value
                            self.control.set_effort(value)
                            self.metrics.request_effort(value if value != self.metrics.state.effort else None,
                                                        '用户在 HUD 手动切换推理强度')
                            self.control.mark(row['id'],'submitted','已恢复自动调整' if value is None else f'已固定为 {value}，下一轮生效')
                            if self.turn_active and self.turn_id and not self.turn_done.is_set():
                                note=(f'用户已手动固定推理强度为 {value}。当前工具或动作完成后，请简短结束本轮，系统将按新档位自动继续。不要因此暂停或结束试玩。'
                                      if value else '用户已恢复自动推理强度，你可以按需调整；之前的手动固定要求已撤回。')
                                self.pending_messages.append({**row,'text':notification('effort', facts=note, cfg=self.cfg)})
                        elif row["kind"] == "pause":
                            self.paused = True
                            self.pausing = True
                            self.control.heartbeat(paused=False, phase="pausing", session_id=self.session_id)
                            interrupt_error = None
                            try:
                                await self._interrupt_user()
                            except Exception as exc:
                                interrupt_error = exc
                            # An RPC failure does not mean the current game call
                            # returned. Keep the transition guard until its boundary.
                            async with self.action_lock:
                                if self.pause_at is None:
                                    self.pause_at = time.monotonic()
                                    self.metrics.pause()
                                self.metrics.update(status="net_wait" if getattr(self, 'model_waiting', False) else "paused")
                            if interrupt_error is not None:
                                raise RuntimeError('游戏操作已归还并保持暂停，但模型中断未确认：' + str(interrupt_error)) from interrupt_error
                            self.control.mark(row["id"], "submitted", "当前动作已结束，试玩已暂停")
                        elif row["kind"] == "resume":
                            if self.token_drift is not None and self.token_drift.unknown:
                                raise ValueError('Token 推进执行范围未确认，请先人工核验 token_drift.json；保持暂停，不自动重放。')
                            if getattr(self, '_bridge_reload_failed', False):
                                raise ValueError('桥接重载尚未接回，请先重试重载；不能直接恢复游戏输入')
                            if self.chat_only:
                                raise ValueError("本次试玩已经完成。当前只能聊天，开始新试玩需要另行指定目标。")
                            was_paused = self.paused
                            if self.pause_at is not None and not getattr(self, 'model_waiting', False):
                                self.paused_seconds += time.monotonic() - self.pause_at
                                self.pause_at = None
                                self.metrics.resume(quota=False)
                            self.paused = False
                            if was_paused:
                                self.resume_notice = notification('resume', facts='用户已恢复试玩；任务范围、权限和停止条件未改变。', cfg=self.cfg)
                            self.control.mark(row["id"], "submitted", "已继续试玩")
                    except Exception as exc:
                        self.control.mark(row["id"], "error", str(exc))
                    finally:
                        if row["kind"] == "pause":
                            self.pausing = False
                if self.turn_active and self.turn_id and not self.turn_done.is_set():
                    await self._yield_for_fast_change()
                    for row in list(self.pending_messages):
                        if row.get("attempted_turn") == self.turn_id:
                            continue
                        if row.get("next_turn") and not self.capabilities.live_steering:
                            continue          # 系统通知（推演设置已生效）不打断这一轮，留到下一轮
                        row["attempted_turn"] = self.turn_id
                        # 提交期间先移出待启动队列，避免轮次结束时被重复带入下一轮。
                        self.pending_messages.remove(row)
                        try:
                            text = row["text"] + ('\n' + self._paused_notice() if self.paused and not self.chat_only else '')
                            if not self.capabilities.live_steering:
                                raise RuntimeError('执行器不支持向活动轮追加输入')
                            await self._steer_user(text)
                            self.control.mark(row["id"], "submitted", "已提交给当前 agent 会话")
                        except Exception:
                            self.pending_messages.append(row)
                            # 当前轮次刚结束或暂不能追加输入：下一轮带入，不丢消息。
                            self.control.mark(row["id"], "waiting", "等待下一轮提交")
                            await self._interrupt_user()
                self.control.heartbeat(paused=self.paused, phase="chatting" if self.turn_active else "idle",
                    session_id=self.session_id, chat_only=self.chat_only,
                    **self._fast_status(),
                    **self._effort_status(), **self._goal_status(), **self._cutscene_status(), **self._chapter_status(), **self._sim_status(), **self._act_limit_status(), **self._chapter_reset_status(), **self._detour_status(), **self._bridge_reload_status())
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise

    def _owns_bridge_connection(self):
        client = getattr(self.session, 'client', None) if self.session is not None else None
        if client is None:
            return False
        sock = getattr(client, 'sock', None)
        return sock is None or sock.fileno() >= 0

    async def _reload_bridge(self, row):
        if not self._owns_bridge_connection():
            raise ValueError('当前主控已没有游戏连接，不能重载桥接；不自动另连或接管游戏')
        from .bridge_reload import reload_bridge, options
        request = options(json.loads(row['text'] or '{}'), fill_defaults=False)
        was_paused = self.paused
        started_pause = self.pause_at is None
        self.paused, self.pausing = True, True
        self.control.mark(row['id'], 'waiting', '等待当前动作完成，再撤销尚未提交的重载交接' if request.get('abort')
                          else '等待当前动作完成，再重载桥接并保持原录像')
        try:
            async with self.action_lock:
                if started_pause:
                    self.pause_at = time.monotonic()
                    self.metrics.pause()
                self.metrics.update(status='paused')
                cancel = threading.Event()
                worker = asyncio.create_task(asyncio.to_thread(reload_bridge, self.session, self.run_dir, request, cancel=cancel,
                                                              owner_was_paused=was_paused,
                                                              progress=lambda text: self.control.mark(row['id'], 'waiting', text)))
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # to_thread cancellation does not stop the worker. Keep the
                    # action lock until it acknowledges cancellation, so cleanup
                    # cannot race an unfinished commit on the owner connection.
                    cancel.set()
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    self.paused = True
                    self.cfg._keep_frozen = True
                    worker_error = worker.exception() if not worker.cancelled() else None
                    completed = not worker.cancelled() and worker_error is None and worker.result().get('phase') in {'finished', 'aborted'}
                    self._bridge_reload_failed = getattr(worker_error, 'reload_held', not completed)
                    self.metrics.update(status='paused')
                    self.control.mark(row['id'], 'error', '重载已完成，但控制任务取消，试玩保持暂停' if completed
                                      else '重载等待已取消，原录像与交接保留，试玩保持暂停')
                    raise
                self._bridge_reload_failed = False
                self.paused = True if self.session.done else result.get('owner_was_paused', was_paused)
                if not self.paused and self.pause_at is not None:
                    self.paused_seconds += time.monotonic() - self.pause_at
                    self.pause_at = None
                    self.metrics.resume(quota=False)
            if result.get('phase') == 'aborted':
                # Nothing was committed: the original bridge, room and observation stay valid.
                self.control.mark(row['id'], 'submitted', '已撤销尚未提交的重载交接，原桥接继续运行，游戏与录像未改变')
                return
            recording_note = ('原录像继续' if (result.get('before') or {}).get('recording', {}).get('active')
                              else '原录制状态保持不变')
            self.control.mark(row['id'], 'submitted', f'桥接 {result.get("version")} 已接回，{recording_note}')
            if not self.session.done:
                scene_note = ('游戏场景保持原对象与进度，未代按确认。' if result.get('scene_mode') == 'preserve' else
                              '当前房间经 Everest 正常重载，主控仍核验原存档与房间。')
                self.pending_messages.append({**row, 'id': 'bridge_reload:' + str(row['id']), 'next_turn': True,
                    'text': notification('bridge_reload', facts='桥接代码已重载并接回，' + recording_note + '。' + scene_note +
                            '请先重新 observe 核对状态，不沿用旧实体编号，不重复已经执行的动作。', cfg=self.cfg)})
        except Exception as exc:
            self._bridge_reload_failed = getattr(exc, 'reload_held', False)
            self.paused = True
            self.cfg._keep_frozen = True
            self.metrics.update(status='paused')
            raise
        finally:
            self.pausing = False

    def _bridge_reload_status(self):
        return {'bridge_reload_control': bool(self._owns_bridge_connection()
                                             and getattr(self.session, 'bridge_reload_supported', False)),
                'bridge_reload_failed': getattr(self, '_bridge_reload_failed', False)}

    def _act_limit_status(self):
        enabled = (not self.chat_only and not getattr(self, 'prepare_only', False)
                   and self.session is not None and not self.session.done
                   and callable(getattr(self.session, 'update_act_limit', None)))
        return {'act_limit_control': bool(enabled),
                'act_limit_settings': self.session.act_limit_settings() if enabled else None}

    def _sim_status(self):
        from dataclasses import fields
        from .sim_assist import AssistRules
        from .tuning import TUNABLES
        enabled = (not self.chat_only and self.session is not None and not self.session.done
                   and bool(getattr(self.session, 'sim_energy', None))
                   and callable(getattr(self.session, 'update_simulation', None)))
        if not enabled:
            return {'sim_control': False}
        return {'sim_control': True, 'sim_settings': {
            'rules': {'assist_' + item.name: getattr(self.session.assist_rules, item.name) for item in fields(AssistRules)},
            'energy': self.session.sim_energy.snapshot(),
            'parameters': [{'key': item.key, 'label': item.label, 'min': item.low, 'max': item.high, 'unit': item.unit}
                           for item in TUNABLES if item.key.startswith('assist_')]}}

    def _fast_status(self):
        return {'fast_control':self.capabilities.fast, 'fast':self.fast_mode,
                'fast_current':self.turn_fast if self.turn_active else None,
                'fast_pending':bool(self.capabilities.fast and self.turn_active and self.turn_fast != self.fast_mode)}

    def _chapter_reset_status(self):
        from .action_policy import permissions_for
        permissions = permissions_for(self.cfg).snapshot()
        return {'chapter_reset_control': bool(self.session is not None and not self.session.done and not self.chat_only),
                'action_permissions': permissions,
                'action_policy_supported': getattr(self.session, 'action_policy_supported', None) == 1,
                'allow_chapter_reset': all(permissions.values()),
                'chapter_reset_guard_supported': bool(getattr(self.session, 'chapter_reset_guard_supported', False))}

    def _detour_status(self):
        """计划外地图开关：与三项权限同样只在试玩期间、新版操作策略下可改；已到访地图自由探索不使用。"""
        session = self.session
        enabled = (session is not None and not session.done and not self.chat_only
                   and getattr(self.cfg, 'play_scope', 'chapter') != 'visited'
                   and getattr(session, 'action_policy_supported', None) == 1)
        return {'detour_control': bool(enabled), 'allow_detour': bool(getattr(self.cfg, 'allow_detour', False)),
                'detour_supported': bool(getattr(session, 'map_entry_set_supported', False))}

    async def _yield_for_fast_change(self):
        if not self.capabilities.fast or not self.capabilities.live_steering or self.turn_fast == self.fast_mode:
            return
        key=(self.turn_id,self.fast_mode)
        if key == self._fast_yield_requested:
            return
        try:
            await self._steer_user(notification('fast', facts=f'用户更新了 Fast 设置：{"开启" if self.fast_mode else "关闭"}，下一轮生效。当前试玩设置：{"暂停" if self.paused else "运行"}。', cfg=self.cfg))
        except Exception:
            return  # a turn/start or turn/completed race; recheck state on the next control tick
        self._fast_yield_requested=key

    def _effort_status(self):
        return {'effort_control':True, 'interrupt_control': bool(getattr(self, 'supports_interrupt', False)),'effort':self.metrics.state.effort,
                'park_control': self._can_park(),
                'effort_pending':self.metrics.state.effort_pending,'effort_manual':self.manual_effort,
                'effort_levels':getattr(self, 'runtime_efforts', self.capabilities.effort_levels)}

    def _can_park(self):
        # 挂起以切换：两个执行器相同（原生保存退出到菜单、交还游戏，之后接回原会话续跑）
        return (not self.chat_only and self.session is not None
                and not self.session.done and self.paused and not self.pausing and not self.turn_active
                and not self.stop_requested and not getattr(self, '_bridge_reload_failed', False)
                and type(getattr(self.cfg, 'save_slot', None)) is int)

    def _cutscene_status(self):
        return {'cutscene_control':getattr(self.session, 'cutscene_tool', True),
                'cutscene_mode':self.control.cutscene_preference() or self.cfg.cutscene_mode}

    def _chapter_status(self):
        if self.session is None or not hasattr(self.session, "chapter_plan") or self.chat_only:
            return {"chapters_control": False}
        return {"chapters_control": not self.session.done, "chapter_plan": self.session.chapter_plan()}

    def _goal_status(self):
        if self.session is None or not hasattr(self.session, "goals"):
            return {"goals_enabled": False, "goal": None}
        return {"goals_enabled": not self.session.done and not self.goal_finishing,
                "goal": self.session.goals.snapshot()}

    async def _check_goal(self):
        if self.session is None or not hasattr(self.session, "goals") or self.goal_finishing:
            return
        if self.session.goals.check(self.session.goal_values()):
            self.goal_finishing = True
            self.paused = True
            await self._interrupt_user()
            async with self.action_lock:
                await asyncio.to_thread(self.session.finish_user_stop)
