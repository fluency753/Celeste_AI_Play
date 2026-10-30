"""原生独立 GPT：Codex 管理会话，动态工具转发至现有游戏主控和零网络 Docker 沙箱。"""
from __future__ import annotations

import asyncio
from copy import copy
import json
import re
import time
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

from .prompting import play_prompt
from .modes import game_tools
from .codex_rpc import CodexRPC
from .codex_read import read_file, search_files
from .session_controls import RuntimeCapabilities, SessionControls
from .telemetry import public_event


def spec(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


TEXT = {"type": "string"}
LIVE_NOTE = "（直播提醒）请先更新一句公开解说，再继续规划。"
# read/glob/grep 与 Claude 的 Read/Glob/Grep 共用门禁名：都不算 Bash，不消耗程序次数。
FILE_TOOLS = {'read': 'Read', 'glob': 'Glob', 'grep': 'Grep'}
LEGACY_FILE_TOOLS = {'celeste_' + name: name for name in FILE_TOOLS}


def codex_tool_mapping(cfg) -> str:
    """原生 GPT 的提示词在 play_prompt 之后另加的工具映射（控制台的提示词预览也显示它）。"""
    return ("# 原生 Codex 工具映射\n"
            "你与原生 Shell、文件编辑工具都在 Docker 容器内，工作目录为 /workspace。"
            "使用原生 Shell 运行程序，使用原生 apply_patch 编辑文件；游戏工具加 celeste_ 前缀。"
            "读取与查找文件用 read（文件、目录列表、文本分页、图像）、glob（按文件名模式查找）"
            "和 grep（按正则搜索内容），它们与 Claude 的 Read/Glob/Grep 对应：不运行程序、不消耗程序次数，"
            "只读聊天和暂停时也可用；内容直接送达当前轮次。原生 Shell（包括 cat、ls、grep）一律按 Bash 管理并计次。"
            "必读知识库必须显式使用专用读取工具，Shell 读取不计入准备进度。"
            "恢复旧会话时，以主控本轮给出的文件工具名称映射及实际登记为准；不要调用未登记名称。"
            "原生工具与 Claude 共用主控门禁、程序额度和零网络沙箱；不得绕过这些限制。"
            "不连接宿主桥接、浏览器或其他服务，不启动子智能体。"
            "只输出给观众的简短公开解说，不输出内部推理正文。"
            "公开 commentary 会自动实时送到 HUD，可替代单纯为了更新文字的 narrate 调用；"
            "每次操作后仍应立即公开说明下一步打算，不要持续静默。"
            "游戏图像由应用直接作为当前轮的图像附件提供，工具结果只含文字；"
            "附件的帧号、房间和screenshot/collision标签对应本次工具观测，不是新任务。"
            "不要尝试从Base64文字看图或把图像编码打印进聊天。")


def codex_prompt(cfg, *, guide=None) -> str:
    from .startup_prompt import runtime_prompt
    return runtime_prompt(cfg, guide=guide) + "\n\n" + codex_tool_mapping(cfg)


def program_cap(cfg) -> float:
    """原生 Shell 单次程序的最长秒数：探索模式用 explore_program_s（提示词里写的上限），
    速通模式用 Bash 最长时长 bash_max_s（与 Claude Code 的 Bash 相同）。"""
    from .agent import bash_limits_ms
    return bash_limits_ms(cfg)[1] / 1000

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
        # 经 turn/steer 送达的内容按 clientUserMessageId 记回执：只有 Codex 把它记入历史（userMessage
        # 事件）才算模型收到；轮次中断时 Codex 会丢弃排队输入，对应回执作废。
        self._receipts = {}
        self._inflight_notes = set()
        self._receipt_tasks = set()
        self.registered_tools = None
        self.tool_notice = ''
        self.native_token = secrets.token_urlsafe(32)
        self.native_session = session or SimpleNamespace(cfg=cfg, metrics=metrics, workspace=cfg.workspace_dir)
        self._native_web = None
        self._native_web_task = None
        self.rpc = CodexRPC(run_dir / "codex-control", self._event, self._request, container=container,
                            before_start=self._start_native_gate,
                            environment={'MCP_PORT': str(cfg.mcp_port), 'CELESTE_NATIVE_TOKEN': self.native_token})
        self._events = None
        self._last_public = 0.0
        self._init_controls(disabled=prepare_only, start_paused=start_paused)
        self.native_session.program_guard = self._program_guard
        self.native_session.note_reminders = self.note_reminders
        self.native_session.native_tool_hook = self._native_hook
        self.native_session.file_tool_name = self._file_tool_name

    def _file_tool_name(self, canonical):
        if self.registered_tools is None or canonical in self.registered_tools:
            return canonical
        legacy = 'celeste_' + canonical
        return legacy if legacy in self.registered_tools else None

    def _reader_name(self):
        return self._file_tool_name('read') or '专用读取工具（本会话未登记）'

    async def _native_hook(self, data):
        from .codex_native import native_tool_hook
        return await native_tool_hook(self, data)

    async def _start_native_gate(self):
        if self.game_server is not None:
            return  # The game server already owns the shared /gate endpoint.
        from .run import add_gate_route
        from .pausegate import PauseGate
        from starlette.applications import Starlette
        import uvicorn
        app = Starlette()
        add_gate_route(app, self.gate or PauseGate(self.metrics, enabled=False), session=self.native_session)
        self._native_web = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.cfg.mcp_port,
                                                       log_level='warning'))
        self._native_web_task = asyncio.create_task(self._native_web.serve())
        async with asyncio.timeout(15):
            while not self._native_web.started:
                if self._native_web_task.done():
                    await self._native_web_task
                    raise RuntimeError('Codex 工具门禁未启动')
                await asyncio.sleep(.05)
        if self.cfg.mcp_port == 0:
            self.cfg.mcp_port = self._native_web.servers[0].sockets[0].getsockname()[1]
            self.rpc.environment['MCP_PORT'] = str(self.cfg.mcp_port)

    async def tool_specs(self):
        page = {'offset': {'type': 'integer', 'minimum': 1}, 'limit': {'type': 'integer', 'minimum': 1}}
        tools = [spec('read',
            'Read：读取容器内文件或列出目录。必读知识库必须使用本工具；文本带行号，offset为1起始行，limit为行数；'
            '目录按目录项分页，符号链接只列出、不跟随。'
            '支持文本、PNG/JPEG/GIF/WebP、PDF及Jupyter notebook；PDF pages为1起始页范围（最多20页）。'
            '正文或图像会直接送达当前轮次，不打印图像编码。仅读取/workspace、/kb、/sim，不执行程序、不消耗程序次数。',
            {'file_path': TEXT, 'path': {**TEXT, 'description': '旧版file_path别名，二者不能冲突'},
             **page, 'pages': TEXT}),
            spec('glob',
                 'Glob：按文件名模式查找文件（如 "**/*.md"、"game/*.json"），结果按修改时间从新到旧。'
                 'path 为起始目录，默认 /workspace；仅限 /workspace、/kb、/sim，不跟随符号链接，不运行程序、不消耗程序次数。',
                 {'pattern': TEXT, 'path': TEXT, **page}, ['pattern']),
            spec('grep',
                 'Grep：按 Python 正则搜索文件内容。path 为文件或目录（默认 /workspace），glob 过滤文件名（如 "*.md"）；'
                 'output_mode 为 files_with_matches（默认，文件列表）、content（匹配行，带行号与 context 上下文）或 count。'
                 '仅限 /workspace、/kb、/sim，不跟随符号链接，跳过二进制文件，不运行程序、不消耗程序次数；不计入知识库阅读进度。',
                 {'pattern': TEXT, 'path': TEXT, 'glob': TEXT,
                  'output_mode': {'type': 'string', 'enum': ['files_with_matches', 'content', 'count']},
                  'ignore_case': {'type': 'boolean'}, 'context': {'type': 'integer', 'minimum': 0, 'maximum': 20},
                  **page}, ['pattern'])]
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
        if method.startswith('rawResponse'):
            return  # Raw messages may include analysis; never publish them.
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
            if item.get("type") == "userMessage":
                self._input_recorded(item)
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
            elif item.get("type") in ("mcpToolCall", "collabToolCall"):
                self.fatal = f"独立游戏执行器出现未允许的宿主工具：{item['type']}，已停止"
                self.turn_done.set()
        elif method == "turn/completed":
            from .codex_native import finish_native_tools
            finish_native_tools(self)
            self.turn_result = params.get("turn") or {}
            self._settle_receipts(self.turn_result)
            self.turn_done.set()

    # ---------------- 经 turn/steer 送达的内容回执

    def _input_recorded(self, item):
        """Codex 把排队输入记入历史时发出带 clientId 的 userMessage：此后中断也不会再丢弃它。"""
        receipt = item.get('clientId')
        if receipt not in self._receipts:
            texts = [part.get('text') for part in item.get('content') or [] if isinstance(part, dict)]
            receipt = next((key for key in self._receipts
                            if any(isinstance(text, str) and key in text for text in texts)), None)
        if receipt is not None:
            self._deliver_receipt(receipt)

    def _deliver_receipt(self, receipt):
        entry = self._receipts.pop(receipt, None)
        if entry is None:
            return
        if entry['kind'] == 'kb' and self.study is not None:
            completed = self.study.note_result({'tool_use_result': {'file': entry['info']}})
            self.metrics.update(kb_progress=round(self.study.progress(), 4))
            if completed:
                self.metrics.kb_finished()
        elif entry['kind'] == 'reminder':
            self._inflight_notes.difference_update(key for key, _ in entry['notes'])
            self.note_reminders.acknowledge(entry['notes'])

    def _drop_receipt(self, receipt):
        entry = self._receipts.pop(receipt, None)
        if entry is not None and entry['kind'] == 'reminder':
            # 未送达的提醒保持待发；直播计时放回，下一次工具调用再提醒。
            self._inflight_notes.difference_update(key for key, _ in entry['notes'])
            if entry.get('waited') is not None:
                self.metrics.restore_live_reminder(entry['waited'])

    def _settle_receipts(self, turn):
        """轮次正常结束时，Codex 会把剩余排队输入记入历史；中断或失败时丢弃，对应回执作废。"""
        ident = turn.get('id') or self.turn_id
        completed = turn.get('status') == 'completed'
        for receipt, entry in list(self._receipts.items()):
            if ident is not None and entry['turn'] not in (ident, None):
                continue
            if completed and entry.get('acked'):
                self._deliver_receipt(receipt)
            else:
                self._drop_receipt(receipt)

    def _steerable(self, origin_turn):
        return bool(origin_turn and self.turn_id == origin_turn and self.turn_active
                    and not self.turn_done.is_set() and not self.stop_requested and not self.pausing)

    async def _steer_receipt(self, origin_turn, items, receipt):
        if not self._steerable(origin_turn):
            raise RuntimeError('发起读取的原轮次已结束或即将中断')
        response = await self.rpc.request('turn/steer', {'threadId': self.session_id,
            'expectedTurnId': origin_turn, 'clientUserMessageId': receipt, 'input': items}, timeout=15)
        if not isinstance(response, dict) or response.get('turnId') != origin_turn:
            raise RuntimeError('未确认内容送达原轮次')
        if receipt in self._receipts:
            self._receipts[receipt]['acked'] = True

    def _fresh_notes(self):
        return tuple(row for row in self.note_reminders.peek() if row[0] not in self._inflight_notes)

    def _notes_response(self, response):
        # 正在经 steer 投递、尚未确认记入历史的提醒不重复附加。
        offered = self._fresh_notes()
        if offered:
            self.note_reminders.acknowledge(offered)
            response['contentItems'].append({'type': 'inputText', 'text': '\n'.join(text for _, text in offered)})
        return response

    def _live_note(self):
        if self.cfg.live and self.cfg.live_remind_s and self.metrics.take_live_reminder(self.cfg.live_remind_s):
            return '\n' + LIVE_NOTE
        return ''

    def offer_native_reminders(self, turn):
        """原生 Shell／补丁的结果由 Codex 生成：提醒改经同轮 steer 投递，确认记入历史后才算送达。"""
        if not self._steerable(turn) or not self.note_reminders:
            return
        offered = self._fresh_notes()
        waited = (self.metrics.take_live_reminder(self.cfg.live_remind_s)
                  if self.cfg.live and self.cfg.live_remind_s else None)
        if not offered and waited is None:
            return
        text = '\n'.join([text for _, text in offered] + ([LIVE_NOTE] if waited is not None else []))
        receipt = 'celeste-reminder-' + secrets.token_hex(8)
        self._receipts[receipt] = {'kind': 'reminder', 'turn': turn, 'notes': offered, 'waited': waited,
                                   'acked': False}
        self._inflight_notes.update(key for key, _ in offered)
        task = asyncio.create_task(self._send_reminder(turn, receipt, text))
        self._receipt_tasks.add(task)
        task.add_done_callback(self._receipt_tasks.discard)

    async def _send_reminder(self, turn, receipt, text):
        try:
            await self._steer_receipt(turn, [{'type': 'text', 'text': '（系统提醒｜' + receipt + '）' + text}], receipt)
        except (RuntimeError, OSError):
            self._drop_receipt(receipt)

    async def _request(self, method, params):
        if method != "item/tool/call":
            # 拒绝任何宿主权限扩张/工具授权请求；不代替用户批准。
            raise RuntimeError(f"只接受客户端动态工具请求，拒绝 {method}")
        requested = params.get('tool', '')
        name = LEGACY_FILE_TOOLS.get(requested, requested)
        args = params.get('arguments') or {}
        if name in {'celeste_bash', 'celeste_write'}:
            return self._notes_response({'contentItems': [{'type': 'inputText', 'text':
                '此会话保留了旧编程工具登记，但该入口已停用，本次未执行。请用容器内原生 Shell 和 apply_patch；'
                f'只读聊天可用 {self._reader_name()} 读取文件；运行程序须先由用户开放程序能力。'}], 'success': False})
        reload_ticket = (getattr(self.session, 'reload_epoch', 0),
                         getattr(self.session, 'reload_observation_required', False))
        if not isinstance(args, dict):
            return self._notes_response({"contentItems": [{"type": "inputText", "text": "工具参数必须是 JSON 对象"}], "success": False})
        if name == 'celeste_cutscene' and 'frames' in args:
            # Existing Codex threads retain their original optional tool fields on resume.
            # Waiting now belongs to the host; the cached frame field has no effect.
            args = {key: value for key, value in args.items() if key != 'frames'}
        ident = params.get("callId") or name
        self.called_tools.append(requested)
        self.metrics.add(tool_calls=1)
        game = name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_narrate", "celeste_stop_run", "celeste_exit_level", "celeste_cutscene", "celeste_simulate")
        detail = (args.get("description") or args.get("file_path") or
                  (f"{args.get('pattern')} @ {args.get('path') or '/workspace'}" if name in FILE_TOOLS and args.get('pattern')
                   else args.get("path")) or "")
        self.metrics.tool_begin(ident, game_tool=game, label=requested + (" · " + detail if detail else ""))
        try:
            if name == 'celeste_set_effort' and self.manual_effort is not None:
                raise ValueError(f'用户已手动固定推理强度为 {self.manual_effort}，请保留用户设置；恢复自动后才可自行调整。')
            if self.session is not None and hasattr(self.session, "goals") and self.session.goals.stop_motion.is_set() and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene", "celeste_simulate"}:
                reason = self.session.goals.snapshot().get('stop_reason', '停止目标已触发')
                raise ValueError("停止目标已触发（" + reason + "）。正在保存录像，本次工具未执行，请结束本轮。")
            if self.chat_only and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene", "celeste_simulate", "celeste_stop_run"}:
                raise ValueError("本次试玩已经结束，当前只提供聊天，不连接或操作游戏。")
            if self.paused and name in {"celeste_act", "celeste_observe", "celeste_bash", "celeste_exit_level", "celeste_cutscene", "celeste_simulate"}:
                raise ValueError("用户已暂停试玩。请只回复用户，不推进游戏或运行规划；结束本轮并等待继续。")
            if game and not self.prepare_only and not self.chat_only and name.removeprefix('celeste_') not in game_tools(self.cfg):
                raise ValueError(f"当前试玩配置未启用 {name}，本次请求未执行；请使用当前已启用的工具。")
            if name in FILE_TOOLS:
                from .tool_gate import decide_tool
                gate = self.gate or getattr(self, '_native_gate', None)
                if gate is None:
                    from .pausegate import PauseGate
                    gate = self._native_gate = PauseGate(self.metrics, enabled=False)
                gate.note_tool_use(ident, ident)
                # 提醒由本次工具结果（_notes_response 与直播提醒）带回，不在决策时提前确认送达。
                decision = await decide_tool(gate, FILE_TOOLS[name], ident, session=self.native_session,
                                             reminders=False)
                if decision['decision'] != 'allow':
                    raise ValueError(decision['reason'])
            elif self.gate and self.gate.enabled and name != 'celeste_stop_run':
                # App Server has no Claude message ID: count each dynamic call.
                self.gate.note_tool_use(ident, ident)
                allowed, why = await self.gate.decide('mcp__celeste__' + name.removeprefix('celeste_'), ident)
                if not allowed:
                    raise ValueError(why)
            if name in FILE_TOOLS:
                origin_turn = self.turn_id
                worker_args = {**args, '_tool_names': {key: self._file_tool_name(key) for key in FILE_TOOLS}}
                try:
                    result = await (asyncio.to_thread(read_file, self.container, worker_args) if name == 'read' else
                                    asyncio.to_thread(search_files, self.container, name, worker_args))
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    result = {'kind': 'error', 'error': str(exc) or type(exc).__name__}
                return await self._file_response(result, requested, ident, origin_turn)
            if name == "celeste_narrate" and self.game_server is None:
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
                    raise ValueError(self.study.reminder(reader=self._reader_name()) if self.study else "没有准备阶段")
                text = "全部知识库已读完；结束本次准备，不启动游戏。"
            elif name in ("celeste_act", "celeste_observe", "celeste_set_effort", "celeste_exit_level", "celeste_cutscene", "celeste_simulate", "celeste_stop_run") and self.prepare_only:
                raise ValueError("本次只做知识库准备，不开放游戏操作，不会进入关卡。")
            elif game and self.game_server is not None and not self.prepare_only:
                origin_turn = self.turn_id
                if name in ("celeste_act", "celeste_observe") and self.study and not self.study.complete():
                    raise ValueError(self.study.reminder(reader=self._reader_name()))
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
                    text += "\n" + LIVE_NOTE
            return self._notes_response({"contentItems": [{"type": "inputText", "text": text}], "success": True})
        except Exception as exc:
            return self._notes_response({"contentItems": [{"type": "inputText", "text": str(exc)}], "success": False})
        finally:
            self.metrics.tool_end(ident)

    async def _file_response(self, result, tool, call_id, origin_turn):
        """read/glob/grep：正文与图像经同轮 steer 送达，工具结果只带确认摘要。

        Codex 会按截断策略缩短动态工具文本（实测 9.6 万字符只保留约 1.2–5 万），用户输入则原样保留；
        所以内容不放进工具结果。知识库行在 Codex 确认把这条输入记入历史后才计量（见 _input_recorded）。
        """
        live = self._live_note()
        if result.get('kind') == 'error':
            return self._notes_response({'contentItems': [{'type': 'inputText',
                'text': (result.get('error') or result.get('text') or '读取失败') + live}], 'success': False})
        info = result.get('file') or {}
        path = result.get('path') or info.get('filePath')
        kb_read = (self.study is not None and info.get('filePath') in self.study.container_paths()
                   and result.get('kind') == 'text')
        receipt = 'celeste-read-' + secrets.token_hex(8)
        label = json.dumps({'tool': tool, 'call_id': str(call_id), 'receipt': receipt,
                            'path': path, **info}, ensure_ascii=False)
        items = [{'type': 'text', 'text': f'【主控工具结果｜{tool}】以下是本次读取的文件内容，不是新任务。'
                  + label + '\n' + (result.get('text') or '')}]
        items += [{'type': 'image', 'url': f"data:{picture['mimeType']};base64,{picture['data']}"}
                  for picture in result.get('images') or []]
        if kb_read:
            self._receipts[receipt] = {'kind': 'kb', 'turn': origin_turn, 'info': info, 'acked': False}
        failure = None
        try:
            await self._steer_receipt(origin_turn, items, receipt)
        except TimeoutError as exc:
            # 可能已被接受：保留未确认的回执，只有记入历史的事件才会计量。
            failure = exc
        except (RuntimeError, OSError) as exc:
            self._receipts.pop(receipt, None)
            failure = exc
        if failure is not None:
            return self._notes_response({'contentItems': [{'type': 'inputText', 'text':
                f'文件内容交付失败：{failure}。本次未计入知识库阅读；可以重新调用 {tool}，'
                '不需要重复任何游戏操作。' + live}], 'success': False})
        kind = result.get('kind')
        if kind == 'text' and info.get('numLines'):
            last = info['startLine'] + info['numLines'] - 1
            text = (f"{path}：第 {info['startLine']}–{last} 行（本次 {info['numLines']} 行，共 {info['totalLines']} 行）"
                    "已作为本次工具结果完整送达当前轮次。")
        elif kind == 'directory':
            text = f"{path}：目录列表（共 {result.get('entries', 0)} 项）已作为本次工具结果送达当前轮次。"
        elif kind in ('glob', 'grep'):
            text = f"{path}：{tool} 结果（{result.get('matches', 0)} 条）已作为本次工具结果送达当前轮次。"
        else:
            text = f"{path}：文件内容／图像已作为本次工具结果直接送达当前轮次。"
        if kb_read:
            text += (f"\n知识库：本页完整行在模型实际接收后计入阅读进度（当前已确认 {self.study.progress():.1%}）。"
                     if info.get('numLines') else f"\n知识库：本次没有完整行（当前已确认 {self.study.progress():.1%}）。")
        if result.get('error'):
            text += '\n' + result['error']
        if result.get('notice'):
            text += '\n' + result['notice']
        elif result.get('truncated'):
            text += (f"\n内容未全部返回；下一次 offset={result['next_offset']} 继续读取。"
                     if result.get('next_offset') is not None else '\n内容超过本次输出预算，请缩小范围读取。')
        return self._notes_response({'contentItems': [{'type': 'inputText', 'text': text + live}], 'success': True})

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
            from .button_prompts import notification
            paths = "\n".join(f"- {p}" for p in self.study.container_paths())
            facts = "本次必读清单：\n" + paths + f"\n必读资料必须用 {self._reader_name()} 的 file_path 与 offset/limit 分页读取，按实际送达的完整行计量。"
            if self.cfg.sim:
                facts += '\n本次预置模拟器已开放，资料位于 /sim。'
            facts += ('\n本次只准备，不启动游戏；完成后调用 celeste_finish_preparation 提交完成信号。'
                      if self.prepare_only else '\n本次准备完成后开放试玩工具。')
            message = notification('prepare', facts=facts).replace('Read 工具', self._reader_name() + ' 工具')
            if self.prepare_only and self.cfg.sim:
                message += '\n' + notification('prepare_sim')
            return message
        from .prompting import first_play_prompt
        return self.resume_note or first_play_prompt(self.cfg).replace('observe', 'celeste_observe').replace('act ', 'celeste_act ')

    async def _resume_thread(self, params):
        # Resume the model's history without hydrating the complete transcript into
        # one JSON-RPC response. Long play sessions exceed the transport line limit.
        params = {**params, 'excludeTurns': True}
        try:
            return await self.rpc.request("thread/resume", params)
        except RuntimeError as exc:
            if 'not found' in str(exc).lower() or 'no rollout' in str(exc).lower():
                raise RuntimeError('容器中没有该 Codex 会话；请先用 python -m orchestrator.codex_container '
                                   '--container <容器名> import-session --file <原 rollout.jsonl> '
                                   '显式导入原会话后再重试，不会另建会话或退回宿主执行。') from exc
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
        from .codex_container import read_metadata
        meta = read_metadata(self.container, path)
        names = {tool['name'] for tool in meta.get('dynamic_tools', [])}
        from .modes import game_tools
        required = {'celeste_' + name for name in game_tools(self.cfg)}
        missing = required - names
        if missing:
            raise RuntimeError('旧会话缺少当前必需工具：' + ', '.join(sorted(missing))
                               + '。该容器记录的接口不完整，请新建准备会话；不会借用宿主工具绕过缺失接口。')
        self.registered_tools = names
        available = {name: self._file_tool_name(name) for name in FILE_TOOLS}
        absent = [name for name, actual in available.items() if actual is None]
        if 'read' in absent and self.study is not None and not self.study.complete():
            raise RuntimeError('旧会话没有登记 read（旧名 celeste_read），无法按实际送达的内容计量必读知识库；'
                               '本版本协议不能在恢复时补登记工具，请新建准备会话。')
        notes = []
        if any(actual is not None and actual != name for name, actual in available.items()):
            mapping = '、'.join(f'{name} → {actual}' for name, actual in available.items() if actual is not None)
            notes.append('（系统）本会话沿用持久化的文件工具登记，请按实际名称调用：' + mapping + '。'
                         '指南中的 read/glob/grep 是规范能力名；本会话应使用上述已登记名称，未补登记新工具。')
        if absent:
            print('[codex] 旧会话未登记 ' + '、'.join(absent) + '；继续恢复原会话，未登记的工具不可用', flush=True)
            notes.append('（系统）本会话未登记 ' + '、'.join(absent) + '，恢复后仍不可调用。')
            if available['read'] is None:
                notes.append('读取文件只能在开放程序能力时用原生 Shell（按 Bash 管理计次）；只读聊天中无法读取文件。')
            else:
                notes.append(f"列目录可用 {available['read']} 读取目录路径；未登记的查找或搜索工具需在开放程序时用原生 Shell。")
        self.tool_notice = ''.join(notes)

    async def run(self):
        if not self.container:
            raise RuntimeError('Codex 会话必须在智能体容器内运行；请先创建并登录该工作区的容器。')
        from .prompt_guide import startup_context
        guide = startup_context(self.cfg)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.run_dir / "codex_events.jsonl").open("a", encoding="utf-8")
        self.metrics.update(telemetry_source="codex_app_server", chat_thread_id=None,
                            tool_calls=self.metrics.state.tool_calls or 0)
        prompt = codex_prompt(self.cfg, guide=guide)
        if self.chat_only:
            from .button_prompts import notification
            prompt += (("\n\n# 试玩前聊天\n这是新会话，已完成入门资料准备，还没有开始试玩。" if getattr(self, "ready_to_play", False)
                        else "\n\n# 试玩结束后的聊天\n本次试玩已经结束。") +
                       ("可以按用户要求用 read/原生 Shell/apply_patch 整理工作区，在零网络沙箱里运行程序；"
                        "若已挂载 /sim，可从显式保存的快照做离线模拟。"
                        if getattr(self, 'allow_programs', False) else
                        "现在仅回答用户的消息、复盘试玩，可以用 read/glob/grep 查阅工作区，"
                        "并按要求用 apply_patch 读取、整理工作区笔记；不运行程序（原生 Shell 不开放）。") +
                       "不连接游戏，不调用游戏观察、动作或在线模拟工具，不自行开始下一章。用户没有发消息时安静等待。")
            prompt += '\n' + notification('wake_chat')
        (self.run_dir / ("chat_system_prompt.md" if self.chat_only else "system_prompt.md")).write_text(prompt, encoding="utf-8")
        started = time.monotonic()
        try:
            await self.rpc.start()
            # 只读聊天与 Claude 一样保留文件编辑（apply_patch → Write 门禁）；原生 Shell 仍由共享门禁拒绝，
            # 因此沿用可写工作区的权限配置，celeste-read 只作为旧镜像兼容保留。
            params = {"cwd": "/workspace", "permissions": "celeste-write",
                      "approvalPolicy": "never", "baseInstructions": prompt, "dynamicTools": await self.tool_specs(),
                      "config": {"model_reasoning_effort": self.manual_effort or self.cfg.effort}, "selectedCapabilityRoots": []}
            if self.cfg.model:
                params["model"] = self.cfg.model
            if self.resume and self.session_id:
                params["threadId"] = self.session_id
                # thread/resume 使用持久化的动态工具；这里不传该版本协议没有的字段。
                params.pop("dynamicTools", None)
                result = await self._resume_thread(params)
                await asyncio.to_thread(self._validate_resumed_tools, result['thread'])
            else:
                result = await self.rpc.request("thread/start", params)
            if (result.get('activePermissionProfile') or {}).get('id') != params['permissions']:
                raise RuntimeError('Codex 未采用主控指定的容器权限配置，拒绝运行')
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
                state.update(session_id=self.session_id, engine="codex", model=result["model"],
                             session_storage='container', session_container=self.container)
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
            opening = not self.chat_only
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
                # 旧会话缺少的文件工具说明只随恢复后的第一轮送达，聊天轮次也带上。
                message = self._turn_message(message, assigned, opening=opening, context=self.tool_notice)
                self.tool_notice = ''
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
                offered_notes = self._fresh_notes()
                turn_input = message + ('\n\n' + '\n'.join(text for _, text in offered_notes) if offered_notes else '')
                response = await self.rpc.request("turn/start", {"threadId": self.session_id, "effort": effort,
                    "serviceTierForTurn": tier,
                    "input": [{"type": "text", "text": turn_input}]})
                opening = False
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
                message = (controller_turn_prompt(f"继续准备，用 {self._reader_name()} 分页读取尚未读完的资料。")
                           if self.study and not self.study.complete() else continue_play_prompt(self.cfg))
            return "现实时间预算用完"
        finally:
            for task in list(self._receipt_tasks):
                task.cancel()
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
                await asyncio.gather(self.heartbeat_task, return_exceptions=True)
            if self.control_task:
                self.control_task.cancel()
                await asyncio.gather(self.control_task, return_exceptions=True)
            if self.control:
                self.control.heartbeat(paused=self.paused, stopped=True, session_id=self.session_id)
            self._end_model_turn()
            try:
                await self.rpc.close()
            finally:
                from .codex_native import finish_native_tools
                finish_native_tools(self)
                if self._native_web_task:
                    self._native_web.should_exit = True
                    try:
                        await asyncio.wait_for(self._native_web_task, 5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        self._native_web_task.cancel()
                        await asyncio.gather(self._native_web_task, return_exceptions=True)
                if self._events:
                    self._events.close()
