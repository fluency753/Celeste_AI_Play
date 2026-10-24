"""原生独立 GPT：Codex 管理会话，动态工具转发至现有游戏主控和零网络 Docker 沙箱。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from .agent import compose_prompt
from .codex_rpc import CodexRPC
from .codex_workspace import workspace_call
from .control import RunControl
from .sandbox import run_program


def spec(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


TEXT = {"type": "string"}


def public_event(value):
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() == "reasoning":
            return {k: value[k] for k in ("type", "id") if k in value}
        return {k: ("<image omitted>" if k == "imageUrl" and str(v).startswith("data:") else public_event(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [public_event(v) for v in value]
    return value


class CodexRunner:
    def __init__(self, cfg, run_dir, session, metrics, container, study=None, *, game_server=None,
                 session_id=None, resume=False, resume_note=None, gate=None, playback=None,
                 prepare_only=False, start_paused=False, chat_only=False, naming_only=False):
        self.cfg, self.run_dir, self.session, self.metrics = cfg, run_dir, session, metrics
        if self.metrics.state.tool_calls is None:
            self.metrics.update(tool_calls=0)
        self.container, self.study, self.game_server = container, study, game_server
        self.session_id, self.resume, self.resume_note = session_id, resume, resume_note
        self.gate, self.playback, self.prepare_only = gate, playback, prepare_only
        self.chat_only = chat_only
        self.naming_only, self.name_result, self.naming_reply = naming_only, None, None
        self.turns = 0
        self.turn_id = None
        self.turn_done = asyncio.Event()
        self.turn_result = None
        self.fatal = None
        self.message_text = {}
        self.compacted = set()
        self.called_tools = []
        self.rpc = CodexRPC(run_dir / "codex-control", self._event, self._request)
        self._events = None
        self._last_public = 0.0
        self.control = None if prepare_only else RunControl(run_dir)
        self.fast_mode = self.control.fast_preference() if self.control else None
        self.manual_effort = self.control.effort_preference() if self.control else None
        self.turn_fast = None
        self.stop_requested = False
        self.goal_finishing = False
        self.paused, self.turn_active, self.user_interrupted = start_paused or chat_only, False, False
        self.pending_messages = []
        self.action_lock = asyncio.Lock()
        self.pause_at, self.paused_seconds = None, 0.0
        self.control_task = None
        self.heartbeat_task, self.pausing = None, False

    async def tool_specs(self):
        tools = [spec("celeste_read", "Read：读取容器内 UTF-8 文件。知识库阅读只按实际返回的完整行计入。",
                      {"path": TEXT, "offset": {"type": "integer", "minimum": 1},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]),
                 spec("celeste_write", "Write：写入 /workspace 内的文件。不能改知识库、模拟器或宿主机。",
                      {"path": TEXT, "content": TEXT}, ["path", "content"]),
                 spec("celeste_bash", "Bash：在 Docker 的零网络沙箱运行命令。直播时最长 15 秒；长程序每 1–2 秒用 show.status 报告进度。",
                      {"command": TEXT, "description": TEXT, "timeout_s": {"type": "number", "minimum": 1}},
                      ["command", "description"])]
        from .server import build_server
        # 部分 CLI 会固定保存初建线程的动态工具；准备和游玩必须使用同一份 schema。
        # 准备阶段只创建描述，不创建游戏连接；执行权限仍由下面的阶段门禁控制。
        schema_source = self.game_server or build_server(SimpleNamespace(
            cfg=self.cfg, metrics=self.metrics, workspace=self.cfg.workspace_dir))
        for tool in await schema_source.list_tools():
            data = tool.model_dump(by_alias=True)
            tools.append({"type": "function", "name": "celeste_" + data["name"],
                          "description": data.get("description", ""), "inputSchema": data["inputSchema"]})
        tools.append(spec("celeste_finish_preparation", "仅准备模式可用：全库阅读完成后结束，不启动游戏。", {}))
        return tools

    def _event(self, method, params):
        # 不记录 reasoning 正文或摘要；公开消息和结构化用量可以用于 HUD。
        item = params.get("item") or {}
        if "reasoning" not in method.lower() and item.get("type") != "reasoning" and self._events:
            safe = {"method": method, "params": params}
            if item.get("type") == "dynamicToolCall":
                safe = {"method": method, "params": {"item": {
                    k: item.get(k) for k in ("id", "type", "tool", "status", "success", "durationMs")}}}
            self._events.write(json.dumps(public_event(safe), ensure_ascii=False) + "\n")
            self._events.flush()
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") or {}
            total = usage.get("total") or {}
            self.metrics.update(context_tokens=last.get("inputTokens", 0) + last.get("outputTokens", 0),
                                context_window=usage.get("modelContextWindow"),
                                output_tokens=total.get("outputTokens", 0),
                                thinking_tokens=total.get("reasoningOutputTokens", 0))
        elif method == "item/agentMessage/delta":
            ident = params.get("itemId", "public")
            text = self.message_text.get(ident, "") + params.get("delta", "")
            self.message_text[ident] = text
            if self.cfg.live and time.monotonic() - self._last_public > 0.4:
                self.metrics.set_thought(" ".join(text.split())[-120:])
                self._last_public = time.monotonic()
        elif method in ("item/started", "item/completed"):
            if method == "item/completed" and item.get("type") == "agentMessage" and self.control:
                self.control.reply(item.get("id", "reply"), item.get("text", ""))
                if self.naming_only:
                    self.naming_reply = item.get('text', '')
            if item.get("type") == "contextCompaction":
                if method == "item/started":
                    self.metrics.update(status="compacting")
                elif item.get("id") not in self.compacted:
                    self.compacted.add(item.get("id"))
                    self.metrics.add(compactions=1)
            elif item.get("type") in ("commandExecution", "fileChange", "mcpToolCall", "collabToolCall"):
                self.fatal = f"独立游戏执行器出现未允许的宿主工具：{item['type']}，已停止"
                self.turn_done.set()
        elif method == "turn/completed":
            self.turn_result = params.get("turn") or {}
            self.turn_done.set()

    def _read_progress(self, result):
        if self.study is None:
            return
        before = self.study.finished
        self.study.note_result({"tool_use_result": {"file": {**result, "filePath": result["path"]}}})
        self.metrics.update(kb_progress=round(self.study.progress(), 4))
        if before is None and self.study.finished is not None:
            self.metrics.kb_finished()
            print(f"[codex] 知识库读完：{len(self.study.files)} 个文件", flush=True)

    async def _request(self, method, params):
        if method != "item/tool/call":
            # 拒绝任何宿主权限扩张/工具授权请求；不代替用户批准。
            raise RuntimeError(f"只接受客户端动态工具请求，拒绝 {method}")
        name, args = params.get("tool", ""), params.get("arguments") or {}
        if not isinstance(args, dict):
            return {"contentItems": [{"type": "inputText", "text": "工具参数必须是 JSON 对象"}], "success": False}
        ident = params.get("callId") or name
        self.called_tools.append(name)
        self.metrics.add(tool_calls=1)
        game = name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_narrate", "celeste_exit_level", "celeste_cutscene")
        detail = args.get("description") or args.get("path") or ""
        self.metrics.tool_begin(ident, game_tool=game, label=name + (" · " + detail if detail else ""))
        try:
            if name == 'celeste_set_effort' and self.manual_effort is not None:
                raise ValueError(f'用户已手动固定推理强度为 {self.manual_effort}，请保留用户设置；恢复自动后才可自行调整。')
            if self.session is not None and hasattr(self.session, "goals") and self.session.goals.stop_motion.is_set() and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene"}:
                raise ValueError("停止目标已触发，正在保存录像，请结束本轮。")
            if self.chat_only and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene"}:
                raise ValueError("本次试玩已经结束，当前只提供聊天，不连接或操作游戏。")
            if self.paused and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene"}:
                raise ValueError("用户已暂停试玩。请只回复用户，不推进游戏或运行规划；结束本轮并等待继续。")
            if self.gate and self.gate.enabled:
                # app-server 的动态调用没有 Claude 消息 ID；每次调用独立记一轮。
                self.gate.note_tool_use(ident, ident)
                gate_name = {"celeste_read": "Read", "celeste_write": "Write", "celeste_bash": "Bash"}.get(
                    name, "mcp__celeste__" + name.removeprefix("celeste_"))
                allowed, why = await self.gate.decide(gate_name, ident)
                if not allowed:
                    raise ValueError(why)
            if name == "celeste_read":
                result = await asyncio.to_thread(workspace_call, self.container, "read", args)
                self._read_progress(result)
                text = (f"{result['path']}：第 {result['startLine']} 行起，共返回 {result['numLines']} 行，"
                        f"总计 {result['totalLines']} 行\n{result['text']}")
                if self.study:
                    text += f"\n（知识库实际阅读进度：{self.study.progress():.1%}）"
            elif name == "celeste_write":
                text = json.dumps(await asyncio.to_thread(workspace_call, self.container, "write", args))
            elif name == "celeste_bash":
                if self.playback:
                    self.playback.note_tool_use(ident, ident, "Bash", args)
                command = args["command"].strip()
                if command == 'game exit' or command.startswith('game story '):
                    if self.prepare_only or self.session is None or self.game_server is None:
                        raise ValueError('当前未在试玩，不能退出关卡')
                    if self.study and not self.study.complete():
                        raise ValueError(self.study.reminder())
                    async with self.action_lock:
                        if self.paused:
                            raise ValueError('用户已暂停试玩，不能退出关卡')
                        result = await self.game_server.call_tool('cutscene', json.loads(command[len('game story '):])) if command.startswith('game story ') else await self.game_server.call_tool('exit_level', {'reason':args.get('description') or '按用户要求退出关卡'})
                    if result.is_error:
                        raise ValueError(('剧情动作失败：' if command.startswith('game story ') else '退出关卡失败：') + str(result.content))
                    return self._game_response(result)
                elif command == "simulate" or command.startswith("simulate "):
                    if self.prepare_only or self.session is None or not getattr(self.session, "sim_energy", None):
                        raise ValueError("限额模拟辅助尚未开启；准备阶段不可预测游戏")
                    if self.study and not self.study.complete():
                        raise ValueError(self.study.reminder())
                    payload = command[len("simulate"):].strip()
                    if payload == "status":
                        result = self.session.sim_energy.snapshot()
                    else:
                        if payload.startswith("'") and payload.endswith("'"):
                            payload = payload[1:-1]
                        async with self.action_lock:
                            if self.paused:
                                raise ValueError("用户已暂停试玩，不能运行模拟")
                            result = await asyncio.to_thread(self.session.simulate, json.loads(payload))
                    text = json.dumps(result, ensure_ascii=False)
                else:
                    limit = min(float(args.get("timeout_s", 10)), 15 if self.cfg.live else 60)
                    result = await asyncio.to_thread(run_program, self.container, command, limit)
                    text = json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                                       "stderr": result.stderr, "seconds": round(result.seconds, 2),
                                       "timed_out": result.timed_out}, ensure_ascii=False)
            elif name == "celeste_narrate" and self.game_server is None:
                self.metrics.set_thought(str(args.get("thought", ""))[:120])
                self.metrics.add(narrates=1)
                self.metrics.narrated()
                text = "解说已更新"
            elif name == "celeste_finish_preparation" and self.prepare_only:
                if self.study is None or not self.study.complete():
                    raise ValueError(self.study.reminder() if self.study else "没有准备阶段")
                text = "全部知识库已读完；结束本次准备，不启动游戏。"
            elif name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene") and self.prepare_only:
                raise ValueError("本次只做知识库准备，不开放游戏操作，不会进入关卡。")
            elif game and self.game_server is not None and not self.prepare_only:
                if name in ("celeste_act", "celeste_observe") and self.study and not self.study.complete():
                    raise ValueError(self.study.reminder())
                if name in {"celeste_act", "celeste_observe", "celeste_exit_level", "celeste_cutscene"}:
                    async with self.action_lock:
                        if self.paused:
                            raise ValueError("用户已暂停试玩，不能执行游戏动作")
                        result = await self.game_server.call_tool(name.removeprefix("celeste_"), args)
                else:
                    result = await self.game_server.call_tool(name.removeprefix("celeste_"), args)
                return self._game_response(result)
            else:
                raise ValueError(f"未开放的工具：{name}")
            if self.cfg.live and name != "celeste_narrate":
                elapsed = self.metrics.take_live_reminder(self.cfg.live_remind_s)
                if elapsed:
                    text += "\n（直播提醒）请先更新一句公开解说，再继续规划。"
            return {"contentItems": [{"type": "inputText", "text": text}], "success": True}
        except Exception as exc:
            return {"contentItems": [{"type": "inputText", "text": str(exc)}], "success": False}
        finally:
            if self.playback and name == "celeste_bash":
                self.playback.note_tool_result(ident)
            self.metrics.tool_end(ident)

    @staticmethod
    def _game_response(result):
        content=[]
        for block in result.content:
            data=block.model_dump(by_alias=True)
            if data['type']=='text':content.append({'type':'inputText','text':data['text']})
            elif data['type']=='image':
                content.append({'type':'inputImage','imageUrl':f"data:{data['mimeType']};base64,{data['data']}"})
        return {'contentItems':content,'success':not result.is_error}

    def _elapsed(self, started):
        if self.naming_only:
            return time.monotonic() - started
        now = time.monotonic()
        return now - started - self.paused_seconds - (now - self.pause_at if self.pause_at else 0)

    async def _interrupt_user(self):
        if self.turn_active and self.turn_id and not self.turn_done.is_set():
            self.user_interrupted = True
            await self.rpc.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})

    async def _heartbeat(self):
        while True:
            self.control.heartbeat(paused=self.paused and not self.pausing,
                phase="pausing" if self.pausing else "chatting" if self.turn_active else "idle",
                session_id=self.session_id, chat_only=self.chat_only,
                fast=self.fast_mode, fast_pending=self.turn_active and self.turn_fast != self.fast_mode,
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
                        elif row["kind"] == "detach":
                            if not self.chat_only:
                                raise ValueError("试玩期间不能切走会话")
                            self.stop_requested = True
                            await self._interrupt_user()
                            self.control.mark(row["id"], "submitted", "聊天会话已释放")
                        elif row["kind"] == "fast":
                            self.fast_mode = row["text"] == "on"
                            self.control.set_fast(self.fast_mode)
                            self.control.mark(row["id"], "submitted", "Fast 设置已保存，下一轮生效")
                        elif row['kind']=='cutscene_mode':
                            self.cfg.cutscene_mode=row['text']
                            self.control.set_cutscene_mode(row['text'])
                            self.control.mark(row['id'],'submitted','剧情模式已更新，当前菜单操作完成后生效')
                            if self.session is not None:
                                self.session.cfg.cutscene_mode=row['text']
                                self.pending_messages.append({**row,'text':'用户将剧情处理切换为 '+row['text']+'。agent模式下自行决定观看、推进、选择选项或跳过；不得用advance代选对话选项。'})
                        elif row['kind'] == 'effort':
                            value=None if row['text']=='auto' else row['text']
                            if value is not None:
                                from .model_catalog import resolve_selection
                                await asyncio.to_thread(resolve_selection,self.metrics.state.model,value)
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
                            await self.rpc.request("turn/steer", {"threadId": self.session_id,
                                "expectedTurnId": self.turn_id, "input": [{"type": "text", "text": text}]})
                            self.control.mark(row["id"], "submitted", "已提交给当前 agent 会话")
                        except Exception:
                            self.pending_messages.append(row)
                            # 当前轮次刚结束或暂不能追加输入：下一轮带入，不丢消息。
                            self.control.mark(row["id"], "waiting", "等待下一轮提交")
                            await self._interrupt_user()
                self.control.heartbeat(paused=self.paused, phase="chatting" if self.turn_active else "idle",
                    session_id=self.session_id, chat_only=self.chat_only,
                    fast=self.fast_mode, fast_pending=self.turn_active and self.turn_fast != self.fast_mode,
                    **self._effort_status(), **self._goal_status(), **self._cutscene_status())
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise

    def _effort_status(self):
        return {'effort_control':True,'effort':self.metrics.state.effort,
                'effort_pending':self.metrics.state.effort_pending,'effort_manual':self.manual_effort}

    def _record_turn_effort(self, effort, pending):
        queued=self.metrics.state.effort_pending
        if pending:
            self.metrics.effort_applied(effort,'codex turn/start')
        next_effort=self.manual_effort or (queued if queued != effort else None)
        if next_effort and next_effort != effort:
            self.metrics.request_effort(next_effort,'用户设置等待下一轮生效')

    def _cutscene_status(self):
        return {'cutscene_control':True,'cutscene_mode':self.control.cutscene_preference() or self.cfg.cutscene_mode}

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
                self.session.finish_user_stop()

    def _first_prompt(self):
        if self.study and not self.study.complete():
            paths = "\n".join(f"- {p}" for p in self.study.container_paths())
            return ("先调用 celeste_narrate 说明准备进度，再用 celeste_read 按顺序完整阅读下列每个文件。"
                    "每页最多 500 行；按返回的实际行范围接着读到最后。不能用 Bash 扫描代替阅读。\n" + paths +
                    ("\n全部读完后，用 celeste_write 把工具约定与试玩原则整理进 /workspace/notes.md。"
                     + ("用 celeste_bash 执行 python3 -c \"import sys;sys.path.insert(0,'/sim');from sim import Sim;print('sim_import_ok')\" "
                        "验证模拟器模块可以导入（只导入，不读取游戏状态或运行推演）。" if self.cfg.sim else "") +
                     "最后调用 celeste_finish_preparation，再简短回复；本次只准备，不启动游戏。" if self.prepare_only
                     else "\n读完后整理 /workspace/notes.md，再调用 celeste_observe 开始试玩。"))
        return self.resume_note or "知识库准备阶段已完成。现在开始本次指定章节的试玩，先调用 celeste_narrate 和 celeste_observe；剧情按当前模式使用 celeste_cutscene，正常游玩用 celeste_act。"

    async def _resume_thread(self, params):
        try:
            return await self.rpc.request("thread/resume", params)
        except RuntimeError as exc:
            if "already has an active writer" not in str(exc):
                raise
            # 桌面聊天页可能已经加载原会话。保留完整历史，使用独立写入者续跑。
            fork_params = {key: value for key, value in params.items()
                           if key != "selectedCapabilityRoots"}
            fork_params["excludeTurns"] = True
            result = await self.rpc.request("thread/fork", fork_params)
            (self.run_dir / "session_recovery.json").write_text(json.dumps({
                "source_thread": params["threadId"], "thread": result["thread"]["id"],
                "reason": "original thread already has an active writer"}, ensure_ascii=False), encoding="utf-8")
            print("[codex] 原会话被其他客户端占用；已复制历史，独立续跑", flush=True)
            return result

    async def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.run_dir / "codex_events.jsonl").open("a", encoding="utf-8")
        self.metrics.update(telemetry_source="codex_app_server", chat_thread_id=None,
                            tool_calls=self.metrics.state.tool_calls or 0)
        prompt = compose_prompt("container", self.cfg.sim, self.cfg.chapters, self.cfg.pause_limit,
                                self.cfg.rounds_per_room, self.cfg.death_bonus, self.cfg.playback, self.cfg.live)
        if self.cfg.knowledge_profile == "explore":
            prompt = (Path(__file__).parent / "prompts/explore_zh.md").read_text(encoding="utf-8")
            prompt += "\n本次指定章节：" + str(self.cfg.chapters) + "（未指定时由主控决定，不能自行扩展）。"
        prompt += ("\n\n# 原生 Codex 工具映射\n"
                   "这里的 Read/Write/Bash 分别是 celeste_read/celeste_write/celeste_bash；"
                   "observe/act/narrate/set_effort 均加 celeste_ 前缀。Edit/Glob/Grep 用沙箱 Bash 完成。"
                   "所有自写程序都必须经 celeste_bash 在 Docker 中执行。不要调用任何宿主 shell、apply_patch、"
                   "MCP、浏览器或子智能体；宿主文件系统是只读的。"
                   "只输出给观众的简短公开解说，不输出内部推理正文。"
                   "用户要求退出关卡时调用 celeste_exit_level；旧会话没有该工具时，可用 celeste_bash 执行专用命令 game exit。剧情可用 celeste_cutscene 选择wait/advance/choose/skip；旧会话可用 celeste_bash 执行 game story 后跟相同参数JSON。公开 commentary 会自动实时送到 HUD，可替代单纯为了更新文字的 narrate 调用；"
                   "每次操作后仍应立即公开说明下一步打算，不要持续静默。")
        prompt += f'\n当前剧情模式：{self.cfg.cutscene_mode}。auto由主控自动跳过，agent由你调用cutscene的wait/advance/choose/skip动作决定。遇到分支选项必须明确choose，不要盲目确认。'
        if self.chat_only:
            prompt += (("\n\n# 试玩前聊天\n这是新会话，已完成入门资料准备，还没有开始试玩。" if getattr(self, "ready_to_play", False)
                        else "\n\n# 试玩结束后的聊天\n本次试玩已经结束。") +
                       "现在仅回答用户的消息、复盘试玩，可以读取原工作区笔记；"
                       "不调用游戏观察、动作或规划程序，不自行开始下一章。用户没有发消息时安静等待。")
        elif self.cfg.sim_assist:
            from .sim_assist import INSTRUCTIONS
            prompt += INSTRUCTIONS
        (self.run_dir / ("chat_system_prompt.md" if self.chat_only else "system_prompt.md")).write_text(prompt, encoding="utf-8")
        started = time.monotonic()
        try:
            await self.rpc.start()
            params = {"cwd": str((self.run_dir / "codex-control").resolve()), "sandbox": "read-only",
                      "approvalPolicy": "never", "baseInstructions": prompt, "dynamicTools": await self.tool_specs(),
                      "config": {"model_reasoning_effort": self.manual_effort or self.cfg.effort}, "selectedCapabilityRoots": []}
            if self.cfg.model:
                params["model"] = self.cfg.model
            if self.resume and self.session_id:
                params["threadId"] = self.session_id
                # thread/resume 使用持久化的动态工具；这里不传该版本协议没有的字段。
                params.pop("dynamicTools", None)
                result = await self._resume_thread(params)
            else:
                result = await self.rpc.request("thread/start", params)
            if result.get("sandbox", {}).get("type") != "readOnly":
                raise RuntimeError("Codex 宿主工具权限不是 readOnly，拒绝运行")
            self.session_id = result["thread"]["id"]
            if self.fast_mode is None:
                self.fast_mode = result.get("serviceTier") in {"priority", "fast"}
            self.metrics.update(model=result["model"], effort=result.get("reasoningEffort") or self.cfg.effort)
            if self.manual_effort and self.manual_effort != self.metrics.state.effort:
                self.metrics.request_effort(self.manual_effort,'恢复用户的手动推理强度设置')
            (self.run_dir / "codex_session.json").write_text(json.dumps({
                "thread_id": self.session_id, "model": result["model"], "effort": self.metrics.state.effort},
                ensure_ascii=False, indent=2), encoding="utf-8")
            state_path = self.run_dir / "run_state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state.update(session_id=self.session_id, engine="codex", model=result["model"])
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[codex] 独立会话 {self.session_id} · {result['model']} · {self.metrics.state.effort}", flush=True)
            if self.control:
                if self.paused:
                    self.pause_at = time.monotonic()
                    self.metrics.pause()
                    self.metrics.update(status="chat_idle" if self.chat_only else "paused")
                self.control_task = asyncio.create_task(self._controls())
                self.heartbeat_task = asyncio.create_task(self._heartbeat())
            message = self._first_prompt()
            while self._elapsed(started) < self.cfg.budget_seconds:
                if self.stop_requested:
                    return "聊天会话切换"
                if self.session is not None and self.session.done:
                    return self.session.done_reason
                while self.paused and not self.pending_messages:
                    if self.session is not None and self.session.done:
                        return self.session.done_reason
                    if self.stop_requested:
                        return "聊天会话切换"
                    if self.rpc.reader.done():
                        raise ConnectionError("暂停期间 Codex app-server 已断开")
                    if self.control_task.done():
                        self.control_task.result()
                        raise RuntimeError("用户控制通道已停止")
                    self.metrics.update(status="chat_idle" if self.chat_only else "paused")
                    await asyncio.sleep(.2)
                if self.session is not None and self.session.done:
                    return self.session.done_reason
                assigned = list(self.pending_messages)
                self.pending_messages.clear()
                if assigned:
                    message = "\n\n".join(row["text"] for row in assigned)
                if self.paused:
                    message += "\n（用户已暂停试玩。只能回复用户，不调用 observe、act 或 Bash；回复后结束本轮，等待用户继续。）"
                self.turn_done.clear()
                self.turn_result = None
                self.turns += 1
                self.message_text.clear()
                self.metrics.turn_begin()
                self.turn_active, self.turn_id = True, None
                pending = self.metrics.state.effort_pending
                effort = pending or self.metrics.state.effort
                self.turn_fast = self.fast_mode
                response = await self.rpc.request("turn/start", {"threadId": self.session_id, "effort": effort,
                    "serviceTierForTurn": "priority" if self.turn_fast else "default",
                    "input": [{"type": "text", "text": message}]})
                self.turn_id = response["turn"]["id"]
                for row in assigned:
                    self.control.mark(row["id"], "submitted", "已提交给当前 agent 会话")
                self._record_turn_effort(effort,pending)
                while not self.turn_done.is_set():
                    if self.rpc.reader.done():
                        raise ConnectionError("Codex app-server 已断开；检查 codex_stderr.log")
                    if self._elapsed(started) >= self.cfg.budget_seconds:
                        await self.rpc.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})
                        return "现实时间预算用完"
                    await asyncio.sleep(0.1)
                self.metrics.turn_end()
                self.turn_active, self.turn_id = False, None
                if self.fatal:
                    raise RuntimeError(self.fatal)
                interrupted = self.turn_result.get("status") == "interrupted" and self.user_interrupted
                self.user_interrupted = False
                if self.turn_result.get("status") != "completed" and not interrupted:
                    raise RuntimeError(f"Codex 轮次未完成：{self.turn_result.get('error') or self.turn_result}")
                if self.naming_only:
                    if self.turn_result.get('status') != 'completed':
                        raise RuntimeError('存档命名被中断，没有开始试玩')
                    from .save_naming import parse_name
                    self.name_result = parse_name(self.naming_reply)
                    return '存档命名完成'
                if self.prepare_only and self.study and self.study.complete():
                    return "知识库准备完成"
                message = ("继续准备，尚未完整读完的行必须用 celeste_read 阅读。" if self.study and not self.study.complete()
                           else "继续调用游戏工具完成本次章节；先更新一句公开解说。")
            return "现实时间预算用完"
        finally:
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
                await asyncio.gather(self.heartbeat_task, return_exceptions=True)
            if self.control_task:
                self.control_task.cancel()
                await asyncio.gather(self.control_task, return_exceptions=True)
            if self.control:
                self.control.heartbeat(paused=self.paused, stopped=True, session_id=self.session_id)
            self.metrics.turn_end()
            await self.rpc.close()
            if self._events:
                self._events.close()
