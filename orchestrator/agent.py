"""启动并驱动 Claude Code（无头模式）。

两种运行方式：
- container（默认）：在 Docker 容器里用 docker exec 启动 Claude Code。开放 Bash、Read、Write、Edit、Glob、Grep
  与游戏工具；容器出站只放行 Anthropic 与主控端口，Bash 命令在零网络的沙箱里运行（见 agent-container/）。
- host（M2 的过渡方式）：在本机启动，--tools "" 关闭全部内置工具，只能调用游戏工具。

两种方式共同的措施：--strict-mcp-config 只加载本项目的游戏工具服务；--permission-mode dontAsk 拒绝一切未预先
允许的调用；不读取使用者自己的 Claude Code 配置、CLAUDE.md 与自动记忆；启动时核对工具列表，多出任何工具立即停止。
"""
from __future__ import annotations

import asyncio
import time
import json
import os
import uuid
from pathlib import Path

from .config import REPO_ROOT, RunConfig, chapter_name_by_id, find_claude_exe, find_docker_exe
from .game_session import GameSession
from .kb_study import KbStudy
from .metrics import Metrics

GAME_TOOLS = ["mcp__celeste__observe", "mcp__celeste__act"]
CONTAINER_BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
PROMPTS = REPO_ROOT / "orchestrator" / "prompts"


def compose_prompt(runtime: str, sim: bool = False, chapters: list[int] | None = None) -> str:
    extra = "container_zh.md" if runtime == "container" else "host_zh.md"
    text = (PROMPTS / "system_zh.md").read_text(encoding="utf-8") + "\n\n" + (PROMPTS / extra).read_text(encoding="utf-8")
    if sim and runtime == "container":
        text += "\n\n" + (PROMPTS / "sim_zh.md").read_text(encoding="utf-8")
    if chapters and len(chapters) > 1:
        names = " → ".join(chapter_name_by_id(c) for c in chapters)
        text += ("\n\n# 本次的章节\n\n本次要依次通关：" + names + "。评价指标是各章章节计时之和。"
                 "一章通关后系统自动进入下一章，你接着玩；之前写的程序与笔记都在工作区里，可以继续用。")
    return text


FIRST_PROMPT = "开始游玩。先调用 observe 看清当前画面，然后用 act 推进，直到通关。"


def study_prompt(study: KbStudy) -> str:
    files = "\n".join(f"- {path}" for path in study.container_paths())
    return (
        "开始游玩之前，先用 Read 工具把知识库从头到尾完整读一遍，按下面的顺序：\n"
        f"{files}\n\n"
        "要求：每个文件都读到最后一行。文件长时 Read 会按长度截断，看到截断就用 offset 与 limit 接着读，"
        "不要只读开头，也不要用 Grep 或 Bash 代替阅读。草莓酱文本的中文版（*_zh.md）与英文版是同一内容的翻译，可以不读。"
        "主控会核对你读过的行，读完之前 observe 与 act 不会执行。\n\n"
        "读完后，结合知识库想清楚：哪些技巧能在本章用来缩短游戏内计时、它们的输入写法与帧窗口，以及你要写哪些程序"
        "（模拟器、搜索）。把要点整理进 /workspace/notes.md（工作区里已有的笔记和程序先看一遍），然后调用 observe 开始游玩。\n"
        f"这个准备阶段（读知识库、写笔记、写程序）不计入现实用时，上限 {study.minutes:g} 分钟，到时自动开始计时；"
        "从你第一次成功调用 observe 或 act 起开始计时。游戏在你思考期间一直暂停，章节计时只算游戏真正推进的帧。"
    )
CONTINUE_PROMPT = "本章还没有通关，请继续调用 act 游玩。"


def _strip_images(value):
    """写日志时去掉图片的 base64，避免日志膨胀。"""
    if isinstance(value, dict):
        return {k: ("<image omitted>" if k == "data" and isinstance(v, str) and len(v) > 512 else _strip_images(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_images(v) for v in value]
    return value


class ClaudeRunner:
    def __init__(self, cfg: RunConfig, run_dir: Path, session: GameSession, metrics: Metrics,
                 token: str, reset_clock, container: str | None = None, study: KbStudy | None = None):
        self.cfg = cfg
        self.study = study
        self.container = container
        self.run_dir = run_dir
        self.session = session
        self.metrics = metrics
        self.token = token
        self.reset_clock = reset_clock
        self.session_id = str(uuid.uuid4())
        self.proc: asyncio.subprocess.Process | None = None
        self.turns = 0
        self.idle_turns = 0
        self._tool_calls_this_turn = 0
        self._events = open(run_dir / "agent_events.jsonl", "a", encoding="utf-8")
        self._turn_done = asyncio.Event()
        self._init_checked = False
        self.stop_reason = ""

    def _common_args(self) -> list[str]:
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", self.cfg.model, "--effort", self.cfg.effort,
                "--strict-mcp-config", "--permission-mode", "dontAsk", "--session-id", self.session_id]
        if self.cfg.autocompact:
            args += ["--autocompact", self.cfg.autocompact]
        return args

    def _command(self) -> list[str]:
        if self.cfg.runtime == "container":
            mcp = json.dumps({"mcpServers": {"celeste": {
                "type": "http", "url": f"http://host.docker.internal:{self.cfg.mcp_port}/mcp"}}})
            prompt = compose_prompt("container", self.cfg.sim, self.cfg.chapters)
            (self.run_dir / "system_prompt.md").write_text(prompt, encoding="utf-8")
            return [
                find_docker_exe(), "exec", "-i", "-u", "agent", "-w", "/workspace",
                "-e", "CLAUDE_CODE_OAUTH_TOKEN", "-e", "MCP_TOOL_TIMEOUT=900000", "-e", "MAX_MCP_OUTPUT_TOKENS=40000",
                self.container, "claude", *self._common_args(),
                # 受限模式：不读取用户、项目与本地设置文件（智能体改不了自己的配置），文件工具只能访问工作区与知识库
                "--restricted", "--add-dir", "/kb", *(["--add-dir", "/sim"] if self.cfg.sim else []),
                "--tools", ",".join(CONTAINER_BUILTIN_TOOLS),
                "--mcp-config", mcp,
                "--allowedTools", *CONTAINER_BUILTIN_TOOLS, *GAME_TOOLS,
                "--append-system-prompt", prompt,
            ]
        mcp_config = self.run_dir / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"celeste": {
            "type": "http", "url": f"http://127.0.0.1:{self.cfg.mcp_port}/mcp"}}}), encoding="utf-8")
        prompt_file = self.run_dir / "system_prompt.md"
        prompt_file.write_text(compose_prompt("host", chapters=self.cfg.chapters), encoding="utf-8")
        return [
            find_claude_exe(), *self._common_args(),
            "--tools", "",
            "--mcp-config", str(mcp_config),
            "--allowedTools", *GAME_TOOLS,
            "--system-prompt-file", str(prompt_file),
        ]

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
            # docker exec -e CLAUDE_CODE_OAUTH_TOKEN 从这里取值传进容器，令牌不会出现在命令行里
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
        stderr_log = open(self.run_dir / "claude_stderr.log", "ab")
        self.proc = await asyncio.create_subprocess_exec(
            *self._command(), cwd=str(workdir), env=self._env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr_log,
            limit=64 * 1024 * 1024)
        reader = asyncio.create_task(self._read_stdout())
        try:
            await self._send(study_prompt(self.study) if self.study else FIRST_PROMPT)
            while True:
                if not self.metrics.playing and self.study is not None:
                    # 准备阶段：不消耗现实用时预算，只受准备阶段上限约束
                    remaining = self.study.deadline() - time.monotonic()
                    if remaining <= 0:
                        self.study.force_open()
                        self.metrics.start_play()
                        self.reset_clock()
                        print(f"[agent] 准备阶段到达上限 {self.study.minutes:g} 分钟，开始计时"
                              f"（知识库已读 {self.study.progress():.0%}）", flush=True)
                        continue
                    # 每秒复查一次：智能体在这一轮中途开始游玩时，要立即改用现实用时预算
                    if not await self._wait_turn(min(remaining, 1.0)):
                        continue
                else:
                    remaining = self.cfg.budget_seconds - self.metrics.elapsed()
                    if remaining <= 0 or not await self._wait_turn(remaining):
                        self.stop_reason = "时间预算用完"
                        break
                if reader.done():
                    self.stop_reason = self.stop_reason or "Claude Code 进程已退出"
                    break
                self._turn_done.clear()
                if self.session.done:
                    self.stop_reason = self.session.done_reason
                    break
                self.idle_turns = self.idle_turns + 1 if self._tool_calls_this_turn == 0 else 0
                if self.idle_turns >= 3:
                    self.stop_reason = "连续 3 轮没有调用工具"
                    break
                if self.study is not None and not self.study.ready():
                    await self._send(self.study.reminder())
                else:
                    await self._send(CONTINUE_PROMPT)
        finally:
            await self._shutdown(reader)
            stderr_log.close()
            self._events.close()
        return self.stop_reason

    async def _wait_turn(self, timeout: float) -> bool:
        """等这一轮结束；超时返回 False（这一轮仍在进行）。"""
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _idle_status(self) -> str:
        return "thinking" if self.metrics.playing else "studying"

    async def _send(self, text: str) -> None:
        self.turns += 1
        self._tool_calls_this_turn = 0
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        self.reset_clock()
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
            self._events.write(json.dumps(_strip_images(event), ensure_ascii=False) + "\n")
            self._events.flush()
            self._handle(event)

    def _handle(self, event: dict) -> None:
        etype = event.get("type")
        subtype = event.get("subtype")
        if etype == "system" and subtype == "init":
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
                self.metrics.update(status="compacting")
                print("[agent] 上下文压缩中……", flush=True)
            elif event.get("compact_result"):
                self.metrics.update(status=self._idle_status())
        elif etype == "system" and subtype == "compact_boundary":
            meta = event.get("compact_metadata") or {}
            self.metrics.add(compactions=1, compact_s=(meta.get("duration_ms") or 0) / 1000)
            print(f"[agent] 压缩完成（{meta.get('trigger')}）：{meta.get('pre_tokens')} → {meta.get('post_tokens')} token，"
                  f"用时 {(meta.get('duration_ms') or 0) / 1000:.1f} 秒", flush=True)
        elif etype == "assistant":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            context = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0))
            if context:
                self.metrics.update(context_tokens=context)
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    self._tool_calls_this_turn += 1
        elif etype == "user" and self.study is not None and self.study.finished is None:
            if self.study.note_result(event):
                print(f"[agent] 知识库已读完：{len(self.study.files)} 个文件，"
                      f"用时 {self.study.finished - self.study.started:.0f} 秒", flush=True)
            self.metrics.update(kb_progress=round(self.study.progress(), 4))
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            five = (windows.get("five_hour") or {}).get("utilization")
            seven = (windows.get("seven_day") or {}).get("utilization")
            self.metrics.update(usage_5h=five, usage_7d=seven,
                                usage_resets_at=(windows.get("five_hour") or {}).get("resetsAt"))
            limit = self.cfg.max_usage
            if limit and any(u is not None and u >= limit for u in (five, seven)) and not self.stop_reason:
                # 同一账号还有其他会话（包括开发会话）要用：到上限就结束本次运行
                self.stop_reason = f"Max 用量达到上限 {limit:.0%}（5 小时 {five}，7 天 {seven}）"
                print(f"[agent] {self.stop_reason}，停止运行", flush=True)
                if self.proc and self.proc.returncode is None:
                    self.proc.terminate()
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                print(f"[agent] 用量限制：{info}", flush=True)
        elif etype == "system" and subtype == "api_retry":
            print(f"[agent] API 重试：{event.get('error')} attempt={event.get('attempt')}", flush=True)
        elif etype == "result":
            usage = event.get("usage") or {}
            self.metrics.add(output_tokens=usage.get("output_tokens", 0),
                             thinking_tokens=(usage.get("output_tokens_details") or {}).get("thinking_tokens", 0))
            print(f"[agent] 第 {self.turns} 轮结束：{subtype} is_error={event.get('is_error')} "
                  f"输出 {usage.get('output_tokens', 0)} token", flush=True)
            self._turn_done.set()
