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

    def close_events(self):
        events = getattr(self, '_events', None)
        if events and not events.closed:
            events.close()

    def _init_controls(self, *, disabled=False, start_paused=False):
        if self.metrics.state.tool_calls is None:
            self.metrics.update(tool_calls=0)
        self.completed_preparation = False
        self.control = None if disabled else RunControl(self.run_dir)
        self.fast_mode = self.control.fast_preference() if self.control else None
        self.manual_effort = self.control.effort_preference() if self.control else None
        self.turn_fast = None
        self.stop_requested = False
        self.goal_finishing = False
        self.paused, self.turn_active, self.user_interrupted = start_paused or self.chat_only, False, False
        self.pending_messages = []
        self.action_lock = asyncio.Lock()
        self.pause_at, self.paused_seconds = None, 0.0
        self.control_task = None
        self.heartbeat_task, self.pausing = None, False
        if self.session is not None:
            self.session.control_guard = self._game_guard
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

    def _public_compaction(self, ident, *, completed=False):
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

    def _program_guard(self):
        # Preparation can organize notes in the sandbox; the MCP game gate is
        # still closed. Idle chat and user-paused play cannot run planners.
        return None if getattr(self, 'prepare_only', False) else self._game_guard()

    async def _heartbeat(self):
        while True:
            self.control.heartbeat(paused=self.paused and not self.pausing,
                phase="pausing" if self.pausing else "chatting" if self.turn_active else "idle",
                session_id=self.session_id, chat_only=self.chat_only,
                fast_control=self.capabilities.fast, fast=self.fast_mode, fast_pending=self.turn_active and self.turn_fast != self.fast_mode,
                **self._effort_status(), **self._goal_status(), **self._cutscene_status())
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
                        elif row["kind"] == "message":
                            self.pending_messages.append(row)
                            self.control.mark(row["id"], "waiting", "等待提交给当前会话")
                        elif row["kind"] == "interrupt":
                            # 控制台的"中断"：与 Ctrl+C 相同，结束这次运行但不算通关——Claude 执行器的运行会让游戏
                            # 保持冻结在当前帧，可以 --resume 接着玩（见 run.py 的 _keeps_game_frozen）
                            if not getattr(self, "supports_interrupt", False):
                                raise ValueError("当前执行器不支持中断后续跑，请用“停止并保存”")
                            if self.chat_only or self.session is None or self.session.done:
                                raise ValueError("当前没有在试玩")
                            self.stop_reason = "用户中断（游戏保持冻结，可续跑）"
                            self.stop_requested = True
                            await self._interrupt_user()
                            async with self.action_lock:   # 正在执行的动作先走完，游戏停在动作之间
                                pass
                            self.control.mark(row["id"], "submitted", "正在结束本次运行，游戏保持冻结")
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
                            self.control.mark(row["id"], "submitted", "Fast 设置已保存，下一轮生效")
                        elif row['kind']=='cutscene_mode':
                            if row['text'] == 'agent' and not getattr(self.session, 'cutscene_tool', True):
                                raise ValueError('本次运行没有开放剧情工具，剧情由主控自动跳过')
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
                    for row in list(self.pending_messages):
                        if row.get("attempted_turn") == self.turn_id:
                            continue
                        row["attempted_turn"] = self.turn_id
                        # 提交期间先移出待启动队列，避免轮次结束时被重复带入下一轮。
                        self.pending_messages.remove(row)
                        try:
                            text = row["text"] + ("\n（试玩仍处于用户暂停状态。只回复，不调用游戏操作，回复后结束本轮。）" if self.paused else "")
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
                    fast_control=self.capabilities.fast, fast=self.fast_mode, fast_pending=self.turn_active and self.turn_fast != self.fast_mode,
                    **self._effort_status(), **self._goal_status(), **self._cutscene_status())
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise

    def _effort_status(self):
        return {'effort_control':True, 'interrupt_control': bool(getattr(self, 'supports_interrupt', False)),'effort':self.metrics.state.effort,
                'effort_pending':self.metrics.state.effort_pending,'effort_manual':self.manual_effort,
                'effort_levels':getattr(self, 'runtime_efforts', self.capabilities.effort_levels)}

    def _cutscene_status(self):
        return {'cutscene_control':getattr(self.session, 'cutscene_tool', True),
                'cutscene_mode':self.control.cutscene_preference() or self.cfg.cutscene_mode}

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
