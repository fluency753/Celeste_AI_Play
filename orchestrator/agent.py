"""启动并驱动 Claude Code（无头模式）。

两种运行方式：
- container（默认）：在 Docker 容器里用 docker exec 启动 Claude Code。开放 Bash、Read、Write、Edit、Glob、Grep
  与游戏工具；容器出站只放行 Anthropic 与主控端口，Bash 命令在零网络的沙箱里运行（见 agent-container/）。
- host（仅游戏工具模式）：在本机启动，--tools "" 关闭全部内置工具，只能调用游戏工具。

两种方式共同的措施：--strict-mcp-config 只加载本项目的游戏工具服务；--permission-mode dontAsk 拒绝一切未预先
允许的调用；不读取使用者自己的 Claude Code 配置、CLAUDE.md 与自动记忆；启动时核对工具列表，多出任何工具立即停止。

推理强度：以 --effort 启动（默认 high）。智能体用游戏工具 set_effort 申请调整，本轮结束后主控在同一个会话里发送
/effort <强度>（Claude Code 的斜杠命令，回复 "Set effort level to …" 即为生效），再发消息让它接着玩；
一轮进行中发出的 /effort 要等这一轮结束才会执行，所以只在两轮之间切换。Claude Code 进程重启（额度等待后接回会话）
时用当前强度启动。

Max 额度：默认一直用到额度真正用完（Claude Code 报告限额拒绝）为止；用完时结束 Claude Code 进程，游戏保持冻结，
等到额度重置后用 --resume 接着同一个会话继续（上下文保留），等待时间不计入现实用时；要等超过
--quota-wait-max-hours 才重置时结束本次运行。可选的 --max-usage 让主控在用量到某个比例时提前这样暂停。
"""
from __future__ import annotations

import asyncio
import time
import json
import os
import re
import uuid
from pathlib import Path

from .config import REPO_ROOT, RunConfig, find_claude_exe, find_docker_exe
from .game_session import GameSession
from .kb_study import KbStudy
from .metrics import Metrics
from .session_controls import RuntimeCapabilities, SessionControls
from .prompting import play_prompt, study_prompt, resume_prompt, FIRST_PROMPT, CONTINUE_PROMPT

GAME_TOOLS = ['mcp__celeste__' + name for name in
              ('observe', 'act', 'set_effort', 'cutscene', 'exit_level', 'simulate')]
NARRATE_TOOL = "mcp__celeste__narrate"   # 所有模式都有：更新 HUD 上的思考语句（算思考时间，不算工具）
# /effort 命令成功时 Claude Code 的回复，例如 "Set effort level to low (this session only): ..."
EFFORT_OK = re.compile(r"^Set effort level to ([A-Za-z0-9][A-Za-z0-9_.-]*)(?=\s|$)")
# 额度用完时 Claude Code 给出的提示（没收到 rejected 限额事件时的后备判断），例如
# "Claude AI usage limit reached|1792766400"、"You've hit your limit · resets 3pm"
LIMIT_TEXT = re.compile(r"usage limit|hit your (?:usage )?limit|rate[ _]limit", re.I)
CONTAINER_BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
def tool_label(name: str, tool_input) -> str:
    """HUD"调用工具"一栏：工具名（游戏工具去掉前缀）加一句说明——Bash 用智能体写的 description，文件工具用路径，查找用模式。"""
    short = name.removeprefix("mcp__celeste__")
    args = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        detail = str(args.get("description") or "").strip() or " ".join(str(args.get("command") or "").split())[:60]
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = str(args.get("file_path") or args.get("notebook_path") or "")
    elif name in ("Glob", "Grep"):
        detail = str(args.get("pattern") or "")
    elif short == "set_effort":
        detail = str(args.get("effort") or args.get("level") or "")
    else:
        detail = ""
    detail = " ".join(detail.split())
    return f"{short} · {detail}" if detail else short


def _strip_images(value):
    """写日志时去掉图片的 base64，避免日志膨胀。"""
    if isinstance(value, dict):
        return {k: ("<image omitted>" if k == "data" and isinstance(v, str) and len(v) > 512 else _strip_images(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_images(v) for v in value]
    return value



def bash_limits_ms(cfg) -> tuple[int, int]:
    """Claude Code 的 Bash 时长（毫秒）：(不写 timeout 时的上限, 写了 timeout 时的最长)。"""
    if getattr(cfg, "mode", "speedrun") == "explore":
        cap = int(cfg.explore_program_s * 1000)
        return cap, cap
    return int(cfg.bash_default_s * 1000), int(cfg.bash_max_s * 1000)

class ClaudeRunner(SessionControls):
    capabilities = RuntimeCapabilities()
    supports_interrupt = True        # 控制台"中断"：结束运行、游戏保持冻结、可续跑
    def __init__(self, cfg: RunConfig, run_dir: Path, session: GameSession, metrics: Metrics,
                 token: str, container: str | None = None, study: KbStudy | None = None,
                 session_id: str | None = None, resume: bool = False, resume_note: str | None = None,
                 gate=None, playback=None, start_paused=False, prepare_only=False, chat_only=False, naming_only=False):
        """resume=True：用 --resume 接着 session_id 这个已有的会话（续跑）；resume_note 不为空时代替开局的第一条消息。"""
        self.cfg = cfg
        from .efforts import normalize_efforts
        selected = next((m for m in (cfg.provider_info or {}).get('models', []) if m.get('id') == cfg.model), None)
        self.capabilities = RuntimeCapabilities(effort_levels=tuple(normalize_efforts((selected or {}).get('efforts'))))
        self.study = study
        self.gate = gate                         # 规划轮次（pausegate.PauseGate），按输出流里的消息分组
        self.playback = playback                 # 规划回放（playback.Playback）：按输出流里的消息把模拟器推演分轮
        self.container = container
        self.run_dir = run_dir
        self.session = session
        self.metrics = metrics
        self.token = token
        self.session_id = session_id or str(uuid.uuid4())
        self.resume = resume                     # True：用 --resume 接着已有的会话
        self.resume_note = resume_note
        self.quota_until: float | None = None    # 额度到上限：预计重置的时刻（墙钟，秒）
        self.quota_reason = ""
        self._windows: dict[str, dict] = {}      # 最近一次限额事件里各窗口的用量与重置时刻
        self.proc: asyncio.subprocess.Process | None = None
        self.turns = 0
        self.idle_turns = 0
        self._tool_calls_this_turn = 0
        self._events = open(run_dir / "agent_events.jsonl", "a", encoding="utf-8")
        self._turn_done = asyncio.Event()
        self._init_checked = False
        self.stop_reason = ""
        self._command_active = False             # 正在执行主控自己发出的斜杠命令（/effort）
        self._command_reply = ""
        self.prepare_only, self.chat_only, self.naming_only = prepare_only, chat_only, naming_only
        self.name_result, self.naming_reply = None, None
        self.turn_done = self._turn_done
        self.turn_id = None
        self._interrupt_responses = {}
        self._init_controls(disabled=prepare_only, start_paused=start_paused)
        self.action_lock = getattr(session, 'tool_lock', self.action_lock)

    async def _validate_effort(self, value):
        if value not in self.capabilities.effort_levels:
            raise ValueError('当前模型可选推理强度：' + ('、'.join(self.capabilities.effort_levels) or '能力列表尚未配置'))

    async def _interrupt_user(self):
        if not self.turn_active or self.user_interrupted or self.turn_done.is_set():
            return
        self.user_interrupted = True
        ident = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._interrupt_responses[ident] = future
        try:
            # Claude Agent SDK control protocol (query.py::_send_control_request).
            packet = {'type': 'control_request', 'request_id': ident,
                      'request': {'subtype': 'interrupt'}}
            self.proc.stdin.write((json.dumps(packet) + '\n').encode('utf-8'))
            await self.proc.stdin.drain()
            await asyncio.wait_for(future, 10)
        except Exception:
            self.user_interrupted = False
            raise
        finally:
            self._interrupt_responses.pop(ident, None)

    async def _next_user_turn(self, fallback):
        while self.paused and not self.pending_messages:
            if self.metrics.state.status != 'paused':
                self.metrics.update(status='paused')
            if self.session.done or self.stop_requested:
                return False
            if self.control_task and self.control_task.done():
                await self.control_task
            await asyncio.sleep(.1)
        if self.session.done or self.stop_requested:
            return False
        rows, self.pending_messages = self.pending_messages, []
        text = '\n'.join(row['text'] for row in rows) if rows else fallback
        if self.paused:
            text += '\n试玩仍处于暂停状态。只回复用户，不调用游戏操作，回复后结束本轮。'
        await self._send(text)
        for row in rows:
            self.control.mark(row['id'], 'submitted', '已提交给当前 agent 会话')
        return True

    @property
    def effort(self) -> str:
        """当前生效的推理强度（启动参数与 /effort 切换都以它为准）。"""
        return self.metrics.state.effort or self.cfg.effort

    def _model_name(self) -> str:
        """传给 Claude Code 的模型名；第三方提供方可以加后缀（例如 [1m]：按 100 万 token 上下文压缩）。"""
        suffix = (self.cfg.provider_info or {}).get("model_suffix") or "" if self.cfg.third_party else ""
        return self.cfg.model + suffix

    def _provider_env(self) -> dict[str, str]:
        """第三方提供方：接口地址、API 密钥与模型槽位。都经 docker exec -e 名字 从环境传进容器，值不进命令行；
        不传 Max 订阅令牌，所以订阅令牌不会发给第三方。"""
        info = self.cfg.provider_info or {}
        env = {"ANTHROPIC_BASE_URL": info["base_url"], "ANTHROPIC_AUTH_TOKEN": self.token}
        for key, value in (info.get("env") or {}).items():
            env[key] = str(value).replace("{model_base}", self.cfg.model).replace("{model}", self._model_name())
        return env

    def _credential_names(self) -> list[str]:
        return list(self._provider_env()) if self.cfg.third_party else ["CLAUDE_CODE_OAUTH_TOKEN"]

    def _common_args(self) -> list[str]:
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", self._model_name(), "--effort", self.effort,
                "--strict-mcp-config", "--permission-mode", "dontAsk",
                *(["--resume", self.session_id] if self.resume else ["--session-id", self.session_id])]
        autocompact = self.cfg.autocompact or (self.cfg.provider_info or {}).get("autocompact")
        if autocompact:
            args += ["--autocompact", str(autocompact)]
        return args

    def _command(self) -> list[str]:
        if self.cfg.runtime == "container":
            mcp = json.dumps({"mcpServers": {"celeste": {
                "type": "http", "url": f"http://host.docker.internal:{self.cfg.mcp_port}/mcp"}}})
            prompt = play_prompt(self.cfg)
            prompt += self._lifecycle_prompt()
            (self.run_dir / "system_prompt.md").write_text(prompt, encoding="utf-8")
            return [
                find_docker_exe(), "exec", "-i", "-u", "agent", "-w", "/workspace",
                *[arg for name in self._credential_names() for arg in ("-e", name)],
                "-e", "MCP_TOOL_TIMEOUT=900000", "-e", "MAX_MCP_OUTPUT_TOKENS=40000",
                # 不允许后台任务：游戏只在 act 时推进，后台跑的搜索会绕过每次暂停一轮工具的限制，也让计时失真。
                # 前台命令默认最多等 5 分钟（可以用 timeout 参数加到 10 分钟），超时就结束，不会转入后台
                "-e", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1",
                # Bash 等子进程里清掉凭据变量（ANTHROPIC_AUTH_TOKEN 等）：第三方的 API 密钥不能让智能体的程序看到。
                # 不设时 Claude Code 只清订阅令牌，实测 Bash 里能读到 ANTHROPIC_AUTH_TOKEN
                "-e", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1",
                # Bash 的时长上限（可调参数）：速通模式默认 5 分钟、最长 10 分钟；探索模式两者都是单次规划程序的上限（默认 15 秒）
                "-e", f"BASH_DEFAULT_TIMEOUT_MS={bash_limits_ms(self.cfg)[0]}",
                "-e", f"BASH_MAX_TIMEOUT_MS={bash_limits_ms(self.cfg)[1]}",
                self.container, "claude", *self._common_args(),
                # 受限模式：不读取用户、项目与本地设置文件（智能体改不了自己的配置），文件工具只能访问工作区与知识库
                "--restricted", "--add-dir", "/kb", *(["--add-dir", "/sim"] if self.cfg.sim else []),
                "--tools", ",".join(CONTAINER_BUILTIN_TOOLS),
                "--mcp-config", mcp,
                "--allowedTools", *CONTAINER_BUILTIN_TOOLS, *self._game_tools(),
                "--append-system-prompt", prompt,
            ]
        mcp_config = self.run_dir / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"celeste": {
            "type": "http", "url": f"http://127.0.0.1:{self.cfg.mcp_port}/mcp"}}}), encoding="utf-8")
        prompt_file = self.run_dir / "system_prompt.md"
        prompt_file.write_text(play_prompt(self.cfg) + self._lifecycle_prompt(), encoding="utf-8")
        return [
            find_claude_exe(), *self._common_args(),
            "--tools", "",
            "--mcp-config", str(mcp_config),
            "--allowedTools", *self._game_tools(),
            "--system-prompt-file", str(prompt_file),
        ]

    def _game_tools(self) -> list[str]:
        if self.prepare_only or self.chat_only:
            return ['mcp__celeste__set_effort'] + [NARRATE_TOOL]
        return GAME_TOOLS + [NARRATE_TOOL]

    def _lifecycle_prompt(self):
        if self.prepare_only:
            return '\n本次仅准备：按要求完整读取知识库、整理工作区 notes.md 后结束本轮；不调用游戏工具，不开始试玩。'
        if self.chat_only:
            return '\n本次仅聊天：回复用户、读取笔记，不观察或操作游戏，不运行规划程序，不自行开始试玩。没有用户消息时等待。'
        return ''

    def _allowed_tool(self, name: str) -> bool:
        if name.startswith("mcp__celeste__"):
            return True
        return self.cfg.runtime == "container" and name in CONTAINER_BUILTIN_TOOLS

    def _env(self) -> dict[str, str]:
        # 去掉所有 Claude / Anthropic / MCP 相关的继承变量（例如外层 Claude Code 会话留下的推理强度、
        # 入口标记、API 密钥），只保留系统变量，再设置本项目需要的那几项。
        drop_prefixes = ("CLAUDE", "ANTHROPIC", "MCP_", "MAX_THINKING", "MAX_MCP", "DISABLE_", "OTEL_")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(drop_prefixes)}
        if self.cfg.runtime == "container":
            # docker exec -e 名字 从这里取值传进容器，令牌与密钥不会出现在命令行里
            if self.cfg.third_party:
                env.update(self._provider_env())
            else:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
            return env
        config_dir = REPO_ROOT / "agent-home" / self.cfg.agent
        config_dir.mkdir(parents=True, exist_ok=True)
        env.update({
            "CLAUDE_CODE_OAUTH_TOKEN": self.token,
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "MCP_TOOL_TIMEOUT": "900000",
            "MAX_MCP_OUTPUT_TOKENS": "40000",
        })
        return env

    async def run(self) -> str:
        workdir = self.run_dir / "agent_cwd"
        workdir.mkdir(exist_ok=True)
        if self.resume_note:
            first = self.resume_note
        else:
            first = study_prompt(self.study, self.cfg.sim) if self.study else FIRST_PROMPT
        if self.prepare_only:
            first += self._lifecycle_prompt()
        self.metrics.update(telemetry_source='claude_stream_json', chat_thread_id=self.session_id)
        if self.paused:
            self.pause_at = time.monotonic()
            self.metrics.pause()
            self.metrics.update(status='paused')
        if self.control:
            self.control_task = asyncio.create_task(self._controls())
            self.heartbeat_task = asyncio.create_task(self._heartbeat())
        try:
            while True:
                if (note := self._effort_at_start()):
                    first = note + first
                stopped = await self._run_process(workdir, first)
                if stopped or self.quota_until is None:
                    break
                # 额度到上限：游戏保持冻结，等额度重置后接着同一个会话
                minutes = await self._wait_for_quota()
                if self.session.done or self.stop_requested:
                    self.stop_reason = self.session.done_reason if self.session.done else '用户停止'
                    break
                self.resume = True
                self.idle_turns = 0
                study_note = None
                if not self.metrics.playing and self.study is not None and not self.study.ready():
                    study_note = self.study.reminder()
                elif not self.metrics.playing and self.study is not None:
                    study_note = "知识库已经读完；整理好笔记后调用 observe 开始游玩。"
                first = resume_prompt(self.quota_reason, minutes, study_note)
        finally:
            tasks = [task for task in (self.control_task, self.heartbeat_task) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.control:
                self.control.heartbeat(stopped=True, phase='stopped', session_id=self.session_id)
            self._events.close()
        return self.stop_reason

    async def _run_process(self, workdir: Path, first: str) -> bool:
        """启动一个 Claude Code 进程并驱动到结束。返回 True 表示本次运行结束；False 表示因额度暂停，稍后续上。"""
        stderr_log = open(self.run_dir / "claude_stderr.log", "ab")
        self.metrics.turn_end()  # 新的 Claude Code 进程：之前没收到结果的工具调用都不算了
        self._init_checked = False
        self._turn_done.clear()
        self.proc = await asyncio.create_subprocess_exec(
            *self._command(), cwd=str(workdir), env=self._env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr_log,
            limit=64 * 1024 * 1024)
        reader = asyncio.create_task(self._read_stdout())
        started = time.monotonic()
        try:
            if not await self._next_user_turn(first):
                self.stop_reason = self.session.done_reason if self.session.done else (self.stop_reason or '用户停止')
                return True
            while True:
                if reader.done():
                    await reader
                    self.stop_reason = self.stop_reason or 'Claude Code 输出流已结束'
                    return True
                if not self.metrics.playing and self.study is not None:
                    # 准备阶段：不消耗现实用时预算，只受准备阶段上限约束
                    remaining = self.study.deadline() - time.monotonic()
                    if remaining <= 0:
                        if self.prepare_only:
                            self.stop_reason = '准备超时，知识库阅读未完成'
                            return True
                        self.study.force_open()
                        self.metrics.start_play()
                        print(f"[agent] 准备阶段到达上限 {self.study.minutes:g} 分钟，开始计时"
                              f"（知识库已读 {self.study.progress():.0%}）", flush=True)
                        continue
                    # 每秒复查一次：智能体在这一轮中途开始游玩时，要立即改用现实用时预算
                    if not await self._wait_turn(min(remaining, 1.0)):
                        continue
                else:
                    remaining = self.cfg.budget_seconds - (time.monotonic()-started if self.naming_only else self.metrics.elapsed())
                    if remaining <= 0:
                        self.stop_reason = "时间预算用完"
                        return True
                    if not await self._wait_turn(min(remaining, 1.0)):
                        if self.control_task and self.control_task.done():
                            await self.control_task
                        if self.session.done:
                            self.stop_reason = self.session.done_reason
                            return True
                        if self.stop_requested and self.stop_reason and not self.turn_active:
                            return True       # 控制台中断：这一轮已经停下
                        continue
                if self.quota_until is not None and not self.stop_reason:
                    return False
                if reader.done():
                    self.stop_reason = self.stop_reason or "Claude Code 进程已退出"
                    return True
                self._turn_done.clear()
                self.turn_active = False
                if self.session.done:
                    self.stop_reason = self.session.done_reason
                    return True
                if self.stop_reason:
                    return True
                if self.naming_only:
                    from .save_naming import parse_name
                    self.name_result = parse_name(self.naming_reply)
                    self.stop_reason = '存档命名完成'
                    return True
                if self.prepare_only and self.study.complete():
                    self.completed_preparation = True
                    self.stop_reason = '知识库准备完成'
                    return True
                self.idle_turns = (self.idle_turns + 1 if self._tool_calls_this_turn == 0
                                   and not self.user_interrupted and not self.paused and not self.chat_only else 0)
                if self.idle_turns >= 3:
                    self.stop_reason = "连续 3 轮没有调用工具"
                    return True
                note = await self._switch_effort() if self.metrics.state.effort_pending else ""
                next_prompt = self.study.reminder() if self.study is not None and not self.study.ready() else CONTINUE_PROMPT
                if not await self._next_user_turn(note + next_prompt):
                    self.stop_reason = self.session.done_reason if self.session.done else (self.stop_reason or '用户停止')
                    return True
        finally:
            await self._shutdown(reader)
            self.metrics.turn_end()
            stderr_log.close()

    def _effort_at_start(self) -> str:
        """启动（或额度等待后重启）Claude Code 之前：有待生效的推理强度就直接用它作启动参数。"""
        level = self.metrics.state.effort_pending
        if not level:
            return ""
        before = self.effort
        if level == before:
            self.metrics.request_effort(None)
            return ""
        self.metrics.effort_applied(level, via="启动参数")
        print(f"[agent] 推理强度 {before} → {level}（重启 Claude Code 时生效）", flush=True)
        return f"（系统）推理强度已从 {before} 切换为 {level}。"

    async def _switch_effort(self) -> str:
        """两轮之间切换推理强度：在同一个会话里发送 /effort <强度>，等 Claude Code 确认。返回给智能体的说明。"""
        level = self.metrics.state.effort_pending
        before = self.effort
        if level == before:
            self.metrics.request_effort(None)
            return ""
        self._command_reply = ""
        self._command_active = True
        self._turn_done.clear()
        try:
            await self._write(f"/effort {level}")
            finished = await self._wait_turn(60)
        finally:
            self._command_active = False
            self._turn_done.clear()
        reply = self._command_reply or ""
        m = EFFORT_OK.match(reply)
        if finished and m and m.group(1) == level:
            queued = self.metrics.state.effort_pending
            self.metrics.effort_applied(level, via="/effort")
            next_effort = self.manual_effort or (queued if queued != level else None)
            if next_effort and next_effort != level:
                self.metrics.request_effort(next_effort, '用户设置等待下一轮生效')
            print(f"[agent] 推理强度 {before} → {level}", flush=True)
            return f"（系统）推理强度已从 {before} 切换为 {level}。"
        self.metrics.request_effort(None)
        print(f"[agent] 推理强度切换失败（{reply[:120]!r}），保持 {before}", flush=True)
        return f"（系统）推理强度切换没有成功，仍为 {before}。"

    async def _wait_for_quota(self) -> float:
        """等到额度重置（再多等 2 分钟）；返回等待的分钟数。等待期间游戏冻结，现实用时不走。"""
        now = time.time()
        until = max(self.quota_until or 0.0, now + 60.0) + 120.0
        if self.cfg.quota_wait_cap_s is not None:  # 只用于测试
            until = min(until, now + self.cfg.quota_wait_cap_s)
        self.metrics.pause(until)
        self.metrics.update(thought="")
        print(f"[agent] 等待 Max 额度重置：约 {(until - now) / 60:.0f} 分钟后（{time.strftime('%H:%M', time.localtime(until))}）"
              "接着同一个会话继续；游戏保持冻结", flush=True)
        while time.time() < until:
            if self.session.done or self.stop_requested:
                break
            if self.control_task and self.control_task.done():
                await self.control_task
            await asyncio.sleep(min(.5, max(.01, until - time.time())))
        waited = self.metrics.resume()
        if self.study is not None and not self.metrics.playing:
            self.study.started += waited  # 准备阶段的上限不算等待的时间
        self.quota_until = None
        print(f"[agent] 额度等待结束（{waited / 60:.0f} 分钟），恢复智能体", flush=True)
        return waited / 60.0

    async def _wait_turn(self, timeout: float) -> bool:
        """等这一轮结束；超时返回 False（这一轮仍在进行）。"""
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _idle_status(self) -> str:
        return "thinking" if (self.metrics.playing or self.metrics.kb_done) else "studying"

    async def _write(self, text: str) -> None:
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _send(self, text: str) -> None:
        self.turns += 1
        self.turn_id = str(self.turns)
        self.user_interrupted = False
        self.turn_active = True
        self.turn_done.clear()
        self._tool_calls_this_turn = 0
        await self._write(text)
        self.metrics.turn_begin(runtime="claude")
        self.metrics.update(status=self._idle_status())

    async def _shutdown(self, reader: asyncio.Task) -> None:
        if self.proc is None:
            return
        try:
            await self._shutdown_process()
        finally:
            if self.container:
                # docker exec 的客户端退出不一定会结束容器里的进程：把 agent 用户的进程全部结束
                kill = await asyncio.create_subprocess_exec(
                    find_docker_exe(), "exec", self.container, "pkill", "-KILL", "-u", "agent",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await kill.wait()
            reader.cancel()

    async def _shutdown_process(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), timeout=30)
        except (asyncio.TimeoutError, ProcessLookupError):
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                self._turn_done.set()
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 本机运行目录里的完整事件记录保留思考正文，供复盘（runs\evscan.py 等）；它不进 HUD 的会话列表
            self._events.write(json.dumps(_strip_images(event), ensure_ascii=False) + "\n")
            self._events.flush()
            self._handle(event)

    def _quota_hit(self, names: str, until: float | None, text: str) -> None:
        """额度用完（或到了 --max-usage）：等得起就暂停等重置，否则结束本次运行。"""
        if self.stop_reason or self.quota_until is not None:
            return
        if self.cfg.quota_wait and until and until - time.time() <= self.cfg.quota_wait_max_h * 3600:
            # 结束这个进程，游戏保持冻结，等额度重置后接着同一个会话（见 run）
            self.quota_until = until
            self.quota_reason = f"Max 额度的{names}用到了上限"
            print(f"[agent] {text}，暂停智能体，等{names}重置"
                  f"（{time.strftime('%m-%d %H:%M', time.localtime(until))}）", flush=True)
        else:
            # 关闭了等待、不知道何时重置，或要等太久：结束本次运行（之后可以用 --resume 续跑）
            self.stop_reason = text
            print(f"[agent] {self.stop_reason}，停止运行", flush=True)
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()

    def _quota_from_text(self, text: str) -> None:
        """没收到 rejected 限额事件、只看到"额度用完"提示时的后备：重置时刻取提示里的时间戳，
        否则取最近一次限额事件里用量最高的窗口的重置时刻，都没有就 30 分钟后再试。"""
        if self.stop_reason or self.quota_until is not None:
            return
        now = time.time()
        m = re.search(r"\|(\d{10})", text)
        until = float(m.group(1)) if m else None
        if until is None and self._windows:
            name, win = max(self._windows.items(), key=lambda kv: kv[1].get("utilization") or 0)
            if win.get("resetsAt") and float(win["resetsAt"]) > now:
                until = float(win["resetsAt"])
        until = max(until or now + 1800, now + 300)
        self._quota_hit("用量窗口", until, f"Max 额度用完（{text.strip()[:80]}）")

    def _handle(self, event: dict) -> None:
        etype = event.get("type")
        subtype = event.get("subtype")
        if etype == 'control_response':
            response = event.get('response') or {}
            future = self._interrupt_responses.get(response.get('request_id'))
            if future and not future.done():
                if response.get('subtype') == 'error':
                    future.set_exception(RuntimeError(response.get('error', 'interrupt failed')))
                else:
                    future.set_result(response)
        elif etype == "system" and subtype == "init":
            if event.get("model"):
                self.metrics.update(model=event["model"])
            # 每轮开始都会输出一次 init：每次都核对工具列表，只在第一次打印
            tools = event.get("tools") or []
            unexpected = [t for t in tools if not self._allowed_tool(t)]
            servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []}
            if not self._init_checked:
                print(f"[agent] model={event.get('model')} tools={tools} mcp={servers}", flush=True)
            self._init_checked = True
            if unexpected:
                print(f"[agent] 发现不应开放的工具 {unexpected}，立即停止", flush=True)
                self.stop_reason = f"隔离检查失败：{unexpected}"
                self.proc.terminate()
        elif etype == "system" and subtype == "status":
            if event.get("status") == "compacting":
                self._public_compaction(self.metrics.state.compactions)
                self.metrics.update(status="compacting")
                print("[agent] 上下文压缩中……", flush=True)
            elif event.get("compact_result"):
                self.metrics.update(status=self._idle_status())
        elif etype == "system" and subtype == "api_retry":
            # 认证失败不会自己好：Claude Code 会按退避重试 10 次（约 3 分钟），连续两次 401 就直接停下
            if event.get("error_status") == 401 and (event.get("attempt") or 0) >= 2 and not self.stop_reason:
                who = (self.cfg.provider_info or {}).get("label") or self.cfg.provider
                self.stop_reason = (f"{who} 认证失败（401）：检查 API 密钥" if self.cfg.third_party
                                    else "认证失败（401）：检查 .env 里的 CLAUDE_CODE_OAUTH_TOKEN")
                print(f"[agent] {self.stop_reason}，停止运行", flush=True)
                if self.proc and self.proc.returncode is None:
                    self.proc.terminate()
        elif etype == "system" and subtype == "compact_boundary":
            self._public_compaction(self.metrics.state.compactions, completed=True)
            meta = event.get("compact_metadata") or {}
            self.metrics.add(compactions=1, compact_s=(meta.get("duration_ms") or 0) / 1000)
            print(f"[agent] 压缩完成（{meta.get('trigger')}）：{meta.get('pre_tokens')} → {meta.get('post_tokens')} token，"
                  f"用时 {(meta.get('duration_ms') or 0) / 1000:.1f} 秒", flush=True)
        elif etype == "assistant":
            message = event.get("message") or {}
            if self._command_active:
                return  # /effort 等主控命令的回复，不是模型输出
            if event.get("error") == "rate_limit" or message.get("model") == "<synthetic>":
                text = " ".join(b.get("text", "") for b in message.get("content") or [] if b.get("type") == "text")
                if event.get("error") == "rate_limit" or LIMIT_TEXT.search(text):
                    self._quota_from_text(text)
            usage = message.get("usage") or {}
            context = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0))
            if context:
                self.metrics.update(context_tokens=context)
            for index, block in enumerate(message.get("content") or []):
                if block.get("type") == "tool_use":
                    self.metrics.add(tool_calls=1)
                if block.get('type') == 'text' and block.get('text'):
                    if self.naming_only:
                        self.naming_reply = block['text']
                    self._public_message(str(message.get('id') or uuid.uuid4()) + ":" + str(index), block['text'])
                if block.get("type") == "tool_use" and block.get("name") == NARRATE_TOOL:
                    self._tool_calls_this_turn += 1     # 只换 HUD 上的思考语句：算思考时间，不显示在"调用工具"一栏
                    continue
                if block.get("type") == "tool_use":
                    self._tool_calls_this_turn += 1
                    # 工具时间从这里开始，到对应的 tool_result 为止（见 metrics.py）
                    self.metrics.tool_begin(str(block.get("id")), str(block.get("name", "")).startswith("mcp__celeste__"),
                                            tool_label(str(block.get("name", "")), block.get("input")))
                    if self.gate is not None:
                        self.gate.note_tool_use(str(block.get("id")), message.get("id"))
                    if self.playback is not None:
                        self.playback.note_tool_use(message.get("id"), str(block.get("id")), str(block.get("name", "")),
                                                    block.get("input"))
        elif etype == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.metrics.tool_end(str(block.get("tool_use_id")))
                    if self.playback is not None:
                        self.playback.note_tool_result(str(block.get("tool_use_id")))
            if self.study is None or self.study.finished is not None:
                return
            if self.study.note_result(event):
                print(f"[agent] 知识库已读完：{len(self.study.files)} 个文件，"
                      f"用时 {self.study.finished - self.study.started:.0f} 秒", flush=True)
                self.metrics.update(kb_progress=round(self.study.progress(), 4))
                self.metrics.kb_finished()
                return
            self.metrics.update(kb_progress=round(self.study.progress(), 4))
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            self._windows.update({k: v for k, v in windows.items() if isinstance(v, dict)})
            five = (windows.get("five_hour") or {}).get("utilization")
            seven = (windows.get("seven_day") or {}).get("utilization")
            self.metrics.update(usage_5h=five, usage_7d=seven,
                                usage_resets_at=(windows.get("five_hour") or {}).get("resetsAt"))
            limit = self.cfg.max_usage  # 默认 None：不提前暂停，用到额度真正用完
            over = []  # 到了上限的窗口：(名称, 重置时刻)
            if limit and five is not None and five >= limit:
                over.append(("5 小时窗口", (windows.get("five_hour") or {}).get("resetsAt")))
            if limit and seven is not None and seven >= limit:
                over.append(("7 天窗口", (windows.get("seven_day") or {}).get("resetsAt")))
            if not over and info.get("status") == "rejected":
                kind = {"five_hour": "5 小时窗口", "seven_day": "7 天窗口"}.get(info.get("rateLimitType"), "用量窗口")
                over.append((kind, info.get("resetsAt")))
            if over:
                names = "、".join(name for name, _ in over)
                resets = [float(r) for _, r in over if r]
                until = max(resets) if len(resets) == len(over) else None
                usage = f"5 小时 {five}，7 天 {seven}"
                text = (f"Max 用量达到设定的上限 {limit:.0%}（{usage}）" if limit and info.get("status") != "rejected"
                        else f"Max 额度用完（{names}；{usage}）")
                self._quota_hit(names, until, text)
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                print(f"[agent] 用量限制：{info}", flush=True)
        elif etype == "system" and subtype == "api_retry":
            print(f"[agent] API 重试：{event.get('error')} attempt={event.get('attempt')}", flush=True)
        elif etype == "result" and (self._command_active or EFFORT_OK.match(str(event.get("result") or ""))):
            # 主控发出的 /effort 命令执行完（超时之后才到的确认也在这里吃掉，不当作智能体的一轮）
            self._command_reply = str(event.get("result") or "")
            if self._command_active:
                self._turn_done.set()
        elif etype == "result":
            if self.naming_only and event.get('result'):
                self.naming_reply = event['result']
            if event.get("is_error") and LIMIT_TEXT.search(str(event.get("result") or "")):
                self._quota_from_text(str(event.get("result")))
            usage = event.get("usage") or {}
            self.metrics.add(output_tokens=usage.get("output_tokens", 0),
                             thinking_tokens=(usage.get("output_tokens_details") or {}).get("thinking_tokens", 0))
            print(f"[agent] 第 {self.turns} 轮结束：{subtype} is_error={event.get('is_error')} "
                  f"输出 {usage.get('output_tokens', 0)} token", flush=True)
            self.metrics.turn_end()
            self._turn_done.set()
