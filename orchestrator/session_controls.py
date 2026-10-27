"""Engine-independent HUD controls; protocol operations live in runner adapters.

Adapters provide _interrupt_user, _steer_user and _validate_effort. Game calls
share action_lock with this controller, so pause waits for the current call.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from .control import RunControl


@dataclass(frozen=True)
class RuntimeCapabilities:
    fast: bool = False
    live_steering: bool = False
    effort_levels: tuple[str, ...] | None = None


class SessionControls:
    capabilities = RuntimeCapabilities()

    def _begin_model_turn(self, **timing):
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
                                            lambda: getattr(self.cfg, 'notes_warn_percent', 60))
        self.completed_preparation = False
        self.control = None if disabled else RunControl(self.run_dir)
        if self.control:
            permissions = self.control.action_permissions_preference()
            if permissions is not None:
                from .action_policy import ActionPermissions, configure
                configure(self.cfg, ActionPermissions.read(permissions))
        self.fast_mode = self.control.fast_preference() if self.control else None
        self.manual_effort = self.control.effort_preference() if self.control else None
        self.turn_fast = None
        self._fast_yield_requested = None
        self.stop_requested = False
        self.stop_reason = ""
        self.interrupted_for_resume = False
        self.interrupt_ready = asyncio.Event()
        self.goal_finishing = False
        self.paused, self.turn_active, self.user_interrupted = start_paused or self.chat_only, False, False
        self.pending_messages = []
        self.action_lock = asyncio.Lock()
        self.pause_at, self.paused_seconds = None, 0.0
        self.control_task = None
        self.heartbeat_task, self.pausing = None, False
        if self.session is not None:
            self.session.note_reminders = self.note_reminders
            self.session.control_guard = self._game_guard
            self.session.request_pause = self._agent_pause
            self.session.program_guard = self._program_guard
            self.session.effort_policy = self._agent_effort_policy
            self.session.effort_levels = lambda: self._effort_status()['effort_levels'] or []

    async def _agent_effort_policy(self, value):
        if self.manual_effort and value != self.manual_effort:
            raise ValueError(f'用户已固定推理强度为 {self.manual_effort}；先在 HUD 恢复自动调整。')
        await self._validate_effort(value)

    def _public_message(self, ident, text):
        from .telemetry import publish
        if text.strip():
            publish(self.run_dir, 'public:' + str(ident), text)
            if self.control:
                self.control.reply(str(ident), text)

    def _notes_window(self):
        return self.metrics.state.context_window

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
        notes = getattr(self, 'note_reminders', None)
        if completed and notes is not None:
            notes.compacted(ident if reminder_key is None else reminder_key)
        from .telemetry import publish
        publish(self.run_dir, 'system:compaction:' + str(ident),
                '上下文压缩完成。' if completed else '正在压缩上下文，完成后继续处理对话…',
                role='system', kind='系统')

    def _game_guard(self):
        if getattr(self, 'prepare_only', False):
            return '本次仅准备知识库，不允许操作游戏。'
        if self.paused or self.chat_only:
            return '用户已暂停试玩，不能执行游戏操作。'
        if self.session.done or self.goal_finishing:
            return '本次试玩已结束，不能执行游戏操作。'
        if self.stop_requested and not self.chat_only:
            return '用户已中断本次运行，不能再执行游戏操作。'
        return None

    async def _stopped_reason(self):
        if self.interrupted_for_resume:
            await self.interrupt_ready.wait()
        return self.stop_reason or "聊天会话切换"

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

    def _program_guard(self):
        # Preparation (and chat with --allow-programs) can organize notes and programs in the sandbox;
        # the MCP game gate is still closed. Idle chat and user-paused play cannot run planners.
        if getattr(self, 'prepare_only', False) or getattr(self, 'allow_programs', False):
            return None
        return self._game_guard()

    async def _heartbeat(self):
        while True:
            self.control.heartbeat(paused=self.paused and not self.pausing,
                phase="pausing" if self.pausing else "chatting" if self.turn_active else "idle",
                session_id=self.session_id, chat_only=self.chat_only,
                **self._fast_status(),
                **self._effort_status(), **self._goal_status(), **self._cutscene_status(), **self._chapter_status(), **self._sim_status(), **self._chapter_reset_status())
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
                                    note = "用户已清除自定义目标及停止条件，撤回先前自定义要求；保留主控最初指定的章节范围。"
                                else:
                                    goal = json.loads(row["text"])
                                    self.session.goals.set(goal, self.session.goal_values())
                                    note = "用户更新了目标：" + goal.get("text", "") + "。停止条件由主控核验；不要自行进入未指定章节。"
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
                            self.pending_messages.append({**row, "text": note})
                            self.control.mark(row["id"], "waiting", "要通关的范围已更新，正在通知 agent")
                        elif row['kind'] in {'chapter_reset', 'action_permissions'}:
                            if self.session is None or self.session.done or self.chat_only:
                                raise ValueError('当前没有在试玩，不能改章节重置策略')
                            if row['kind'] == 'action_permissions' and not self._chapter_reset_status()['action_policy_supported']:
                                raise ValueError('当前会话未加载新版操作策略，不能独立修改权限')
                            from .action_policy import ActionPermissions, configure, permissions_for
                            permissions = (ActionPermissions.legacy(row['text'] == 'allow') if row['kind'] == 'chapter_reset'
                                           else permissions_for(self.cfg).update(json.loads(row['text'])))
                            async with self.action_lock:
                                self.control.set_action_permissions(permissions.snapshot())
                                configure(self.cfg, permissions)
                                configure(self.session.cfg, permissions)
                            note = '用户更新了' + permissions.instructions()
                            self.pending_messages.append({**row, 'text': note})
                            self.control.mark(row['id'], 'waiting', '章节重置策略已生效，正在通知 agent')
                        elif row['kind'] == 'sim_update':
                            if not self._sim_status()['sim_control']:
                                raise ValueError('当前主控未开启有限模拟辅助，不能在线修改推演设置')
                            async with self.action_lock:
                                snapshot = await asyncio.to_thread(self.session.update_simulation, json.loads(row['text']))
                            self.control.mark(row['id'], 'submitted', '推演设置已生效，无需重启主控或录制')
                            # 只是通知：不能向进行中的一轮追加输入的执行器（Claude）不为它打断，下一轮带上
                            self.pending_messages.append({**row, 'next_turn': True, 'text': '用户已实时更新推演设置：' + snapshot['rules'] +
                                f"当前余额{snapshot['balance']}。新规则从下一次推演生效，继续当前任务，不需要重写笔记或停止试玩。"})
                        elif row["kind"] == "message":
                            self.pending_messages.append(row)
                            self.control.mark(row["id"], "waiting", "等待提交给当前会话")
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
                                    pass
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
                            self.cfg.cutscene_mode=row['text']
                            self.control.set_cutscene_mode(row['text'])
                            self.control.mark(row['id'],'submitted','剧情模式已更新，当前菜单操作完成后生效')
                            if self.session is not None:
                                self.session.cfg.cutscene_mode=row['text']
                                self.pending_messages.append({**row,'text':'用户将剧情处理切换为 '+row['text']+'。agent模式下自行决定观看、推进、选择选项或跳过；不得用advance代选对话选项。'})
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
                                self.pending_messages.append({**row,'text':note})
                        elif row["kind"] == "pause":
                            self.paused = True
                            self.pausing = True
                            self.control.heartbeat(paused=False, phase="pausing", session_id=self.session_id)
                            await self._interrupt_user()
                            async with self.action_lock:
                                if self.pause_at is None:
                                    self.pause_at = time.monotonic()
                                    self.metrics.pause()
                                self.metrics.update(status="paused")
                            self.control.mark(row["id"], "submitted", "当前动作已结束，试玩已暂停")
                        elif row["kind"] == "resume":
                            if self.chat_only:
                                raise ValueError("本次试玩已经完成。当前只能聊天，开始新试玩需要另行指定目标。")
                            if self.pause_at is not None:
                                self.paused_seconds += time.monotonic() - self.pause_at
                                self.pause_at = None
                                self.metrics.resume(quota=False)
                            self.paused = False
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
                            text = row["text"] + ("\n（试玩仍处于用户暂停状态。只回复，不调用游戏操作，回复后结束本轮。）" if self.paused and not self.chat_only else "")
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
                    **self._effort_status(), **self._goal_status(), **self._cutscene_status(), **self._chapter_status(), **self._sim_status(), **self._chapter_reset_status())
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise

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

    async def _yield_for_fast_change(self):
        if not self.capabilities.fast or not self.capabilities.live_steering or self.turn_fast == self.fast_mode:
            return
        key=(self.turn_id,self.fast_mode)
        if key == self._fast_yield_requested:
            return
        try:
            await self._steer_user('用户更新了 Fast 设置。请在当前工具或动作完成后简短结束本轮，主控会按最新设置继续同一试玩。保持用户当前的暂停或运行设置，不自行退出关卡或结束试玩。')
        except Exception:
            return  # a turn/start or turn/completed race; recheck state on the next control tick
        self._fast_yield_requested=key

    def _effort_status(self):
        return {'effort_control':True, 'interrupt_control': bool(getattr(self, 'supports_interrupt', False)),'effort':self.metrics.state.effort,
                'effort_pending':self.metrics.state.effort_pending,'effort_manual':self.manual_effort,
                'effort_levels':getattr(self, 'runtime_efforts', self.capabilities.effort_levels)}

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
