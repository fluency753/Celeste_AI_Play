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
        self.interrupt_ready = asyncio.Event()
        self.goal_finishing = False
        self.paused, self.turn_active, self.user_interrupted = start_paused or self.chat_only, False, False
        self.pending_messages = []
        self.resume_notice = ''
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

    def _turn_message(self, fallback, rows, *, opening=False, retain_fallback=False, context=''):
        """Choose one lifecycle instruction, keeping user input and separate control facts."""
        supplied = '\n\n'.join(row['text'] for row in rows)
        notice, self.resume_notice = self.resume_notice, ''
        if opening or retain_fallback:
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
        if self.chat_only:
            return '当前为只读聊天，未开放程序运行；可以继续读取文件、复盘并回复用户。'
        return self._game_guard()

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
                                    note = notification('goal_clear', facts="用户已清除自定义目标及停止条件，撤回先前自定义要求；保留主控最初指定的章节范围。")
                                else:
                                    goal = json.loads(row["text"])
                                    await asyncio.to_thread(self.session.set_goal, goal)
                                    from .goals import describe_goal
                                    note = notification('goal', facts="用户更新了目标：" + describe_goal(goal) + "。停止条件由主控核验。")
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
                            self.pending_messages.append({**row, "text": notification('chapters', facts=note)})
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
                            note = notification('action_permissions', facts='用户更新了' + permissions.instructions(experiments=getattr(self.session.cfg, 'play_scope', 'chapter') == 'visited'))
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
                            self.pending_messages.append({**row, 'text': notification('action_permissions', facts=notice(self.session.cfg, allowed, permissions_note, travel=bool(getattr(self.session, 'travel_tool', False))))})
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
                                                         '新范围优先于旧工具说明中的数字；自写推演和模拟额度未改变。')})
                        elif row['kind'] == 'sim_update':
                            if not self._sim_status()['sim_control']:
                                raise ValueError('当前主控未开启有限模拟辅助，不能在线修改推演设置')
                            async with self.action_lock:
                                snapshot = await asyncio.to_thread(self.session.update_simulation, json.loads(row['text']))
                            self.control.mark(row['id'], 'submitted', '推演设置已生效，无需重启主控或录制')
                            # 只是通知：不能向进行中的一轮追加输入的执行器（Claude）不为它打断，下一轮带上
                            self.pending_messages.append({**row, 'next_turn': True, 'text': notification('sim_update', facts='用户已实时更新推演设置：' + snapshot['rules'] +
                                f"当前余额{snapshot['balance']}。新规则从下一次推演生效。")})
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
                            if row['text'] == 'auto' and (getattr(self.cfg, 'play_scope', 'chapter') == 'visited' or getattr(self.cfg, 'completion_policy', 'stop') == 'continue'):
                                raise ValueError('自由探索或通关后继续由智能体操作结算与选章，不能切换为自动剧情模式')
                            self.cfg.cutscene_mode=row['text']
                            self.control.set_cutscene_mode(row['text'])
                            self.control.mark(row['id'],'submitted','剧情模式已更新，当前菜单操作完成后生效')
                            if self.session is not None:
                                self.session.cfg.cutscene_mode=row['text']
                                self.pending_messages.append({**row,'text':notification('cutscene_mode', facts='用户将剧情处理切换为 '+row['text']+'。agent模式下自行决定观看、推进、选择选项或跳过；不得用advance代选对话选项。')})
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
                                self.pending_messages.append({**row,'text':notification('effort', facts=note)})
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
                            if getattr(self, '_bridge_reload_failed', False):
                                raise ValueError('桥接重载尚未接回，请先重试重载；不能直接恢复游戏输入')
                            if self.chat_only:
                                raise ValueError("本次试玩已经完成。当前只能聊天，开始新试玩需要另行指定目标。")
                            was_paused = self.paused
                            if self.pause_at is not None:
                                self.paused_seconds += time.monotonic() - self.pause_at
                                self.pause_at = None
                                self.metrics.resume(quota=False)
                            self.paused = False
                            if was_paused:
                                self.resume_notice = notification('resume', facts='用户已恢复试玩；任务范围、权限和停止条件未改变。')
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
                            '请先重新 observe 核对状态，不沿用旧实体编号，不重复已经执行的动作。')})
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
            await self._steer_user(notification('fast', facts=f'用户更新了 Fast 设置：{"开启" if self.fast_mode else "关闭"}，下一轮生效。当前试玩设置：{"暂停" if self.paused else "运行"}。'))
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
