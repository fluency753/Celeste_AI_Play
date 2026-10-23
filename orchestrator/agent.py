"""启动并驱动 Claude Code（无头模式）。

隔离措施（开发阶段在宿主机上运行时）：
- --tools "" 关闭全部内置工具（命令、文件、联网都没有），只能调用本项目的游戏工具；
- --strict-mcp-config 只加载本项目的 MCP 服务；--permission-mode dontAsk 拒绝一切未预先允许的调用；
- 独立的 CLAUDE_CONFIG_DIR，并关闭 CLAUDE.md 与自动记忆，不读取使用者自己的 Claude Code 配置；
- --system-prompt-file 用游戏智能体的提示词替换默认的编程助手提示词。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from .config import REPO_ROOT, RunConfig, find_claude_exe
from .game_session import GameSession
from .metrics import Metrics

ALLOWED_TOOLS = ["mcp__celeste__observe", "mcp__celeste__act"]
FIRST_PROMPT = "开始游玩。先调用 observe 看清当前画面，然后用 act 推进，直到通关本章。"
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
                 token: str, reset_clock):
        self.cfg = cfg
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

    def _command(self) -> list[str]:
        mcp_config = self.run_dir / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"celeste": {
            "type": "http", "url": f"http://127.0.0.1:{self.cfg.mcp_port}/mcp"}}}), encoding="utf-8")
        prompt_file = REPO_ROOT / "orchestrator" / "prompts" / "system_zh.md"
        return [
            find_claude_exe(), "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", self.cfg.model, "--effort", self.cfg.effort,
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", str(mcp_config),
            "--permission-mode", "dontAsk",
            "--allowedTools", *ALLOWED_TOOLS,
            "--system-prompt-file", str(prompt_file),
            "--session-id", self.session_id,
        ]

    def _env(self) -> dict[str, str]:
        # 去掉所有 Claude / Anthropic / MCP 相关的继承变量（例如外层 Claude Code 会话留下的推理强度、
        # 入口标记、API 密钥），只保留系统变量，再设置本项目需要的那几项。
        drop_prefixes = ("CLAUDE", "ANTHROPIC", "MCP_", "MAX_THINKING", "MAX_MCP", "DISABLE_", "OTEL_")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(drop_prefixes)}
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
            await self._send(FIRST_PROMPT)
            while True:
                remaining = self.cfg.budget_seconds - self.metrics.elapsed()
                if remaining <= 0:
                    self.stop_reason = "时间预算用完"
                    break
                try:
                    await asyncio.wait_for(self._turn_done.wait(), timeout=remaining)
                except asyncio.TimeoutError:
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
                await self._send(CONTINUE_PROMPT)
        finally:
            await self._shutdown(reader)
            stderr_log.close()
            self._events.close()
        return self.stop_reason

    async def _send(self, text: str) -> None:
        self.turns += 1
        self._tool_calls_this_turn = 0
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        self.reset_clock()
        self.metrics.update(status="thinking")

    async def _shutdown(self, reader: asyncio.Task) -> None:
        if self.proc is None:
            return
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
        reader.cancel()

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
        if etype == "system" and subtype == "init" and not self._init_checked:
            self._init_checked = True
            tools = event.get("tools") or []
            unexpected = [t for t in tools if not t.startswith("mcp__celeste__") and t != "EndConversation"]
            servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []}
            print(f"[agent] model={event.get('model')} tools={tools} mcp={servers}", flush=True)
            if unexpected:
                print(f"[agent] 发现不应开放的工具 {unexpected}，立即停止", flush=True)
                self.stop_reason = f"隔离检查失败：{unexpected}"
                self.proc.terminate()
        elif etype == "system" and subtype == "compact_boundary":
            self.metrics.add(compactions=1)
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
        elif etype == "system" and subtype == "api_retry":
            print(f"[agent] API 重试：{event.get('error')} attempt={event.get('attempt')}", flush=True)
        elif etype == "result":
            print(f"[agent] 第 {self.turns} 轮结束：{subtype} is_error={event.get('is_error')}", flush=True)
            self._turn_done.set()
