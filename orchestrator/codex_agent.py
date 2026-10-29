"""原生独立 GPT：Codex 管理会话，动态工具转发至现有游戏主控和零网络 Docker 沙箱。"""
from __future__ import annotations

import asyncio
from copy import copy
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from .prompting import play_prompt
from .modes import game_tools
from .codex_rpc import CodexRPC
from .codex_workspace import workspace_call
from .session_controls import RuntimeCapabilities, SessionControls
from .sandbox import run_program
from .telemetry import public_event


def spec(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


TEXT = {"type": "string"}



def codex_tool_mapping(cfg) -> str:
    """原生 GPT 的提示词在 play_prompt 之后另加的工具映射（控制台的提示词预览也显示它）。"""
    return ("# 原生 Codex 工具映射\n"
            "这里的 Read/Write/Bash 分别是 celeste_read/celeste_write/celeste_bash；"
            "游戏工具均加 celeste_ 前缀。Edit/Glob/Grep 用沙箱 Bash 完成。"
            "所有自写程序都必须经 celeste_bash 在 Docker 中执行。不要调用任何宿主 shell、apply_patch、"
            "MCP、浏览器或子智能体；宿主文件系统是只读的。"
            "只输出给观众的简短公开解说，不输出内部推理正文。"
            "公开 commentary 会自动实时送到 HUD，可替代单纯为了更新文字的 narrate 调用；"
            "每次操作后仍应立即公开说明下一步打算，不要持续静默。"
            "游戏图像由应用直接作为当前轮的图像附件提供，工具结果只含文字；"
            "附件的帧号、房间和screenshot/collision标签对应本次工具观测，不是新任务。"
            "不要尝试从Base64文字看图或把图像编码打印进聊天。")


def codex_prompt(cfg, *, guide=None) -> str:
    from .startup_prompt import runtime_prompt
    return runtime_prompt(cfg, guide=guide).replace("调用 simulate 工具", "调用 celeste_simulate 工具") + "\n\n" + codex_tool_mapping(cfg)


def program_cap(cfg) -> float:
    """原生执行器里单次程序（celeste_bash）的最长秒数：探索模式用 explore_program_s（提示词里写的上限），
    速通模式用 Bash 最长时长 bash_max_s（与 Claude Code 的 Bash 相同）。"""
    if getattr(cfg, "mode", "speedrun") == "explore":
        from .agent import UNLIMITED_PROGRAM_S
        return float(cfg.explore_program_s if cfg.explore_program_s is not None else UNLIMITED_PROGRAM_S)
    return float(cfg.bash_max_s)

class CodexRunner(SessionControls):
    capabilities = RuntimeCapabilities(fast=True, live_steering=True)
    supports_interrupt = True

    def __init__(self, cfg, run_dir, session, metrics, container, study=None, *, game_server=None,
                 session_id=None, resume=False, resume_note=None, gate=None, playback=None,
                 prepare_only=False, start_paused=False, chat_only=False, naming_only=False):
        self.cfg, self.run_dir, self.session, self.metrics = cfg, run_dir, session, metrics
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
        self._init_controls(disabled=prepare_only, start_paused=start_paused)

    async def tool_specs(self):
        tools = [spec("celeste_read", "Read：读取容器内 UTF-8 文件。知识库阅读只按实际返回的完整行计入。",
                      {"path": TEXT, "offset": {"type": "integer", "minimum": 1},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]),
                 spec("celeste_write", "Write：写入 /workspace 内的文件。不能改知识库、模拟器或宿主机。",
                      {"path": TEXT, "content": TEXT}, ["path", "content"]),
                 spec("celeste_bash", f"Bash：在 Docker 的零网络沙箱运行命令。最长 {program_cap(self.cfg):g} 秒；长程序每 1–2 秒用 show.status 报告进度。输出超过2000字符时仅返回带标记的首尾预览，完整输出见stdout_path/stderr_path，可用Read分页，超长单行用Bash筛选或分段读取。",
                      {"command": TEXT, "description": TEXT, "timeout_s": {"type": "number", "minimum": 1}},
                      ["command", "description"])]
        from .server import build_server
        # Codex persists the initial tool catalog across prepare/chat/play and
        # later setting changes. Register the complete interface on a descriptor
        # fixture; the real server and _request retain current execution policy.
        schema_cfg = copy(self.cfg)
        schema_cfg.live, schema_cfg.sim_assist, schema_cfg.cutscene_mode = True, True, 'agent'
        schema_source = build_server(SimpleNamespace(
            cfg=schema_cfg, metrics=self.metrics, workspace=self.cfg.workspace_dir))
        for tool in await schema_source.list_tools():
            data = tool.model_dump(by_alias=True)
            tools.append({"type": "function", "name": "celeste_" + data["name"],
                          "description": data.get("description", "") +
                          "\n接口登记跨阶段保留；实际可用工具及动作权限由当前配置和阶段决定。",
                          "inputSchema": data["inputSchema"]})
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
            self._notes_usage()
        elif method == "item/agentMessage/delta":
            ident = params.get("itemId", "public")
            text = self.message_text.get(ident, "") + params.get("delta", "")
            self.message_text[ident] = text
            if self.cfg.live and time.monotonic() - self._last_public > 0.4:
                latest = " ".join(text.split())[-self.cfg.thought_max_chars:]
                if getattr(self.cfg, "formal_thought", False):
                    self.metrics.set_temp_thought(latest)      # 仅正式思考：commentary 是临时思考
                else:
                    self.metrics.set_thought(latest)
                self._last_public = time.monotonic()
        elif method in ("item/started", "item/completed"):
            if method == "item/completed" and item.get("type") == "agentMessage" and self.control:
                self._public_message(item.get("id", "reply"), item.get("text", ""))
                if self.naming_only:
                    self.naming_reply = item.get('text', '')
            if item.get("type") == "contextCompaction":
                self._public_compaction(item.get("id"), completed=method == "item/completed")
                starts = self.__dict__.setdefault("_compaction_started", {})
                if method == "item/started":
                    starts.setdefault(item.get("id"), time.monotonic())
                    self.metrics.update(status="compacting")
                elif item.get("id") not in self.compacted:
                    self.compacted.add(item.get("id"))
                    began = starts.pop(item.get("id"), None)
                    # 压缩本身的时长（与 Claude Code 一样记进 compact_s；思考僵直从思考时间里扣掉它）
                    self.metrics.add(compactions=1, compact_s=round(time.monotonic() - began, 3) if began else 0.0)
                    if self.metrics.state.status == 'compacting' and (self.chat_only or not self.paused):
                        self.metrics.update(status='tool' if self.metrics.tools_running else 'thinking')
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
        reload_ticket = (getattr(self.session, 'reload_epoch', 0),
                         getattr(self.session, 'reload_observation_required', False))
        if not isinstance(args, dict):
            return self._notes_response({"contentItems": [{"type": "inputText", "text": "工具参数必须是 JSON 对象"}], "success": False})
        if name == 'celeste_cutscene' and 'frames' in args:
            # Existing Codex threads retain their original optional tool fields on resume.
            # Waiting now belongs to the host; the cached frame field has no effect.
            args = {key: value for key, value in args.items() if key != 'frames'}
        ident = params.get("callId") or name
        self.called_tools.append(name)
        self.metrics.add(tool_calls=1)
        game = name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_narrate", "celeste_stop_run", "celeste_exit_level", "celeste_cutscene", "celeste_simulate")
        detail = args.get("description") or args.get("path") or ""
        self.metrics.tool_begin(ident, game_tool=game, label=name + (" · " + detail if detail else ""))
        try:
            workspace_program = (name == 'celeste_bash' and self.chat_only
                                 and getattr(self, 'allow_programs', False))
            if name == 'celeste_set_effort' and self.manual_effort is not None:
                raise ValueError(f'用户已手动固定推理强度为 {self.manual_effort}，请保留用户设置；恢复自动后才可自行调整。')
            if self.session is not None and hasattr(self.session, "goals") and self.session.goals.stop_motion.is_set() and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene", "celeste_simulate"}:
                reason = self.session.goals.snapshot().get('stop_reason', '停止目标已触发')
                raise ValueError("停止目标已触发（" + reason + "）。正在保存录像，本次工具未执行，请结束本轮。")
            if self.chat_only and not workspace_program and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene", "celeste_simulate", "celeste_stop_run"}:
                raise ValueError("本次试玩已经结束，当前只提供聊天，不连接或操作游戏。")
            if self.paused and not workspace_program and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene", "celeste_simulate"}:
                raise ValueError("用户已暂停试玩。请只回复用户，不推进游戏或运行规划；结束本轮并等待继续。")
            if game and not self.prepare_only and not self.chat_only and name.removeprefix('celeste_') not in game_tools(self.cfg):
                raise ValueError(f"当前试玩配置未启用 {name}，本次请求未执行；请使用当前已启用的工具。")
            if self.gate and self.gate.enabled and name != "celeste_stop_run":
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
                if self.session is not None and hasattr(self.session, "program_run_denied") and (
                        denied := self.session.program_run_denied()):
                    raise ValueError(denied)
                limit = min(float(args.get("timeout_s", 10)), program_cap(self.cfg))
                result = await asyncio.to_thread(run_program, self.container, command, limit, save_output=True)
                text = json.dumps(result.tool_payload(), ensure_ascii=False)
            elif name == "celeste_narrate" and self.game_server is None:
                note = str(args.get("thought", ""))[:self.cfg.thought_max_chars]
                if getattr(self.cfg, "formal_thought", False):
                    self.metrics.set_temp_thought(note)
                else:
                    self.metrics.set_thought(note)
                self.metrics.add(narrates=1)
                self.metrics.narrated()
                text = "解说已更新"
            elif name == "celeste_finish_preparation" and self.prepare_only:
                if self.study is None or not self.study.complete():
                    raise ValueError(self.study.reminder() if self.study else "没有准备阶段")
                text = "全部知识库已读完；结束本次准备，不启动游戏。"
            elif name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene", "celeste_simulate", "celeste_stop_run") and self.prepare_only:
                raise ValueError("本次只做知识库准备，不开放游戏操作，不会进入关卡。")
            elif game and self.game_server is not None and not self.prepare_only:
                origin_turn = self.turn_id
                if name in ("celeste_act", "celeste_observe") and self.study and not self.study.complete():
                    raise ValueError(self.study.reminder())
                if name in {"celeste_act", "celeste_observe", "celeste_exit_level", "celeste_cutscene", "celeste_simulate"}:
                    async with self.action_lock:
                        if self.paused:
                            raise ValueError("用户已暂停试玩，不能执行游戏动作")
                        if name != 'celeste_observe' and (reload_ticket[1]
                                or reload_ticket[0] != getattr(self.session, 'reload_epoch', 0)
                                or getattr(self.session, 'reload_observation_required', False)):
                            raise ValueError('桥接已重载，请先默认 observe 获取新观测后重新决定动作；排队的旧计划未执行。')
                        if (not origin_turn or self.turn_id != origin_turn or not self.turn_active
                                or self.turn_done.is_set() or self.stop_requested or self.pausing):
                            raise ValueError('发起此请求的轮次已结束，动作未执行；等待当前轮次的新指令。')
                        result = await self.game_server.call_tool(name.removeprefix("celeste_"), args)
                        return await self._game_response(result, name, ident, origin_turn)
                else:
                    result = await self.game_server.call_tool(name.removeprefix("celeste_"), args)
                    return await self._game_response(result, name, ident, origin_turn)
            else:
                raise ValueError(f"未开放的工具：{name}")
            if self.cfg.live and name != "celeste_narrate":
                elapsed = self.metrics.take_live_reminder(self.cfg.live_remind_s)
                if elapsed:
                    text += "\n（直播提醒）请先更新一句公开解说，再继续规划。"
            return self._notes_response({"contentItems": [{"type": "inputText", "text": text}], "success": True})
        except Exception as exc:
            return self._notes_response({"contentItems": [{"type": "inputText", "text": str(exc)}], "success": False})
        finally:
            if self.playback and name == "celeste_bash":
                self.playback.note_tool_result(ident)
            self.metrics.tool_end(ident)

    async def _game_response(self, result, tool, call_id, origin_turn):
        """Code-mode may stringify any tool value: images must never enter that value."""
        content, attachments = [], []
        for block in result.content:
            data=block.model_dump(by_alias=True)
            if data['type']=='text':content.append({'type':'inputText','text':data['text']})
            elif data['type']=='image':
                source = (data.get('_meta') or {}).get('celeste') or {'role': 'observation'}
                label = json.dumps({'tool': tool, 'call_id': str(call_id), **source}, ensure_ascii=False)
                attachments.extend([
                    {'type': 'text', 'text': '（应用观测附件；属于本次工具结果，不是新任务）' + label},
                    {'type': 'image', 'url': f"data:{data['mimeType']};base64,{data['data']}"}])
        if attachments:
            try:
                if (not origin_turn or self.turn_id != origin_turn or not self.turn_active
                        or self.turn_done.is_set() or self.paused or self.pausing or self.stop_requested):
                    raise RuntimeError('原轮次已停止或试玩已暂停')
                response = await self.rpc.request('turn/steer', {'threadId': self.session_id,
                    'expectedTurnId': origin_turn, 'input': attachments}, timeout=15)
                if not isinstance(response, dict) or response.get('turnId') != origin_turn:
                    raise RuntimeError('图像交付未确认原轮次')
            except (RuntimeError, OSError, TimeoutError) as exc:
                if tool == 'celeste_observe' and getattr(self.session, 'reload_epoch', 0):
                    self.session.reload_observation_required = True
                self._agent_pause('观测图像交付失败')
                content.append({'type': 'inputText', 'text':
                    '图像交付失败：' + str(exc) + '。上面的游戏执行结果仍有效，勿重复已执行动作；'
                    '试玩保持暂停，请结束本轮并等待处理。恢复后重新observe，不沿用旧图。'})
                return self._notes_response({'contentItems': content, 'success': False})
            content.append({'type': 'inputText', 'text': '观测图像已作为同轮图像附件送达。'})
        return self._notes_response({'contentItems':content,'success':not result.is_error})

    def _elapsed(self, started):
        if self.naming_only:
            return time.monotonic() - started
        now = time.monotonic()
        return now - started - self.paused_seconds - (now - self.pause_at if self.pause_at else 0)

    async def _steer_user(self, text):
        await self.rpc.request("turn/steer", {"threadId": self.session_id,
            "expectedTurnId": self.turn_id, "input": [{"type": "text", "text": text}]})

    async def _validate_effort(self, value):
        from .model_catalog import resolve_selection
        await asyncio.to_thread(resolve_selection, self.metrics.state.model, value)

    async def _interrupt_user(self):
        if self.turn_active and self.turn_id and not self.turn_done.is_set():
            self.user_interrupted = True
            await self.rpc.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})

    def _record_turn_effort(self, effort, pending):
        queued=self.metrics.state.effort_pending
        if pending:
            self.metrics.effort_applied(effort,'codex turn/start')
        next_effort=self.manual_effort or (queued if queued != effort else None)
        if next_effort and next_effort != effort:
            self.metrics.request_effort(next_effort,'用户设置等待下一轮生效')

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
        from .prompting import first_play_prompt
        return self.resume_note or first_play_prompt(self.cfg).replace('observe', 'celeste_observe').replace('act ', 'celeste_act ')

    async def _resume_thread(self, params):
        # Resume the model's history without hydrating the complete transcript into
        # one JSON-RPC response. Long play sessions exceed the transport line limit.
        params = {**params, 'excludeTurns': True}
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

    def _validate_resumed_tools(self, thread):
        # The protocol persists dynamic tools at thread/start and cannot replace
        # them on resume. Inspect only this runner's returned session metadata.
        path = thread.get('path')
        if not path:
            raise RuntimeError('恢复会话未返回工具登记信息，不能确认游戏接口；请新建准备会话。')
        with Path(path).open(encoding='utf-8') as stream:
            meta = json.loads(stream.readline())
        names = {tool['name'] for tool in meta.get('payload', {}).get('dynamic_tools', [])}
        from .modes import game_tools
        required = {'celeste_' + name for name in game_tools(self.cfg)}
        missing = required - names
        if missing:
            raise RuntimeError('旧会话缺少正式游戏工具：' + ', '.join(sorted(missing))
                               + '。停止宿主后运行 python tools/sync_codex_session_env.py --run "'
                               + str(self.run_dir) + '" --apply 同步旧会话环境，再续跑。')

    async def run(self):
        from .prompt_guide import publish_guide
        guide = publish_guide(self.cfg)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.run_dir / "codex_events.jsonl").open("a", encoding="utf-8")
        self.metrics.update(telemetry_source="codex_app_server", chat_thread_id=None,
                            tool_calls=self.metrics.state.tool_calls or 0)
        prompt = codex_prompt(self.cfg, guide=guide)
        if self.chat_only:
            prompt += (("\n\n# 试玩前聊天\n这是新会话，已完成入门资料准备，还没有开始试玩。" if getattr(self, "ready_to_play", False)
                        else "\n\n# 试玩结束后的聊天\n本次试玩已经结束。") +
                       ("可以按用户要求用 Read/Write/Bash 整理工作区，在零网络沙箱里运行程序；"
                        "若已挂载 /sim，可从显式保存的快照做离线模拟。"
                        if getattr(self, 'allow_programs', False) else
                        "现在仅回答用户的消息、复盘试玩，可以读取原工作区笔记；不运行规划程序。") +
                       "不连接游戏，不调用游戏观察、动作或在线模拟工具，不自行开始下一章。用户没有发消息时安静等待。")
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
                self._validate_resumed_tools(result['thread'])
            else:
                result = await self.rpc.request("thread/start", params)
            if result.get("sandbox", {}).get("type") != "readOnly":
                raise RuntimeError("Codex 宿主工具权限不是 readOnly，拒绝运行")
            self.session_id = result["thread"]["id"]
            if self.fast_mode is None:
                self.fast_mode = result.get("serviceTier") in {"priority", "fast"}
            self.metrics.update(model=result["model"], effort=result.get("reasoningEffort") or self.cfg.effort)
            # Live controls must not use the catalog currently being browsed for
            # a different, new conversation in the HUD.
            if self.control:
                from .model_catalog import load_models
                try:
                    catalog = await asyncio.to_thread(load_models)
                    self.runtime_efforts = next((m['efforts'] for m in catalog['models'] if m['id'] == result['model']), [])
                except (RuntimeError, OSError, ValueError, TimeoutError):
                    self.runtime_efforts = []
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
                    return await self._stopped_reason()
                if self.session is not None and self.session.done:
                    return self.session.done_reason
                while self.paused and not self.pending_messages:
                    if self.session is not None and self.session.done:
                        return self.session.done_reason
                    if self.stop_requested:
                        return await self._stopped_reason()
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
                if self.paused and not self.chat_only:
                    message += "\n（用户已暂停试玩。只能回复用户，不调用 observe、act 或 Bash；回复后结束本轮，等待用户继续。）"
                self.turn_done.clear()
                self.turn_result = None
                self.turns += 1
                self.message_text.clear()
                self.turn_active, self.turn_id = True, None
                pending = self.metrics.state.effort_pending
                effort = pending or self.metrics.state.effort
                self.turn_fast = self.fast_mode
                tier = "priority" if self.turn_fast else "default"
                self._begin_model_turn(fast=self.turn_fast, service_tier=tier, effort=effort, runtime="codex")
                offered_notes = self.note_reminders.peek()
                turn_input = message + ('\n\n' + '\n'.join(text for _, text in offered_notes) if offered_notes else '')
                response = await self.rpc.request("turn/start", {"threadId": self.session_id, "effort": effort,
                    "serviceTierForTurn": tier,
                    "input": [{"type": "text", "text": turn_input}]})
                self.note_reminders.acknowledge(offered_notes)
                self.turn_id = response["turn"]["id"]
                self.metrics.turn_acknowledged(self.turn_id)
                for row in assigned:
                    self.control.mark(row["id"], "submitted", "已提交给当前 agent 会话")
                self._record_turn_effort(effort,pending)
                while not self.turn_done.is_set():
                    if self.stop_requested:
                        return await self._stopped_reason()
                    if self.session is not None and self.session.done and getattr(self.session, "agent_stop", None):
                        return self.session.done_reason
                    if self.rpc.reader.done():
                        raise ConnectionError("Codex app-server 已断开；检查 codex_stderr.log")
                    if self._elapsed(started) >= self.cfg.budget_seconds:
                        await self.rpc.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id})
                        return "现实时间预算用完"
                    await asyncio.sleep(0.1)
                self._end_model_turn()
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
                    self.completed_preparation = True
                    return "知识库准备完成"
                from .prompting import continue_play_prompt, controller_turn_prompt
                message = (controller_turn_prompt("继续准备，尚未完整读完的行必须用 celeste_read 阅读。")
                           if self.study and not self.study.complete() else continue_play_prompt(self.cfg))
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
            self._end_model_turn()
            await self.rpc.close()
            if self._events:
                self._events.close()
