"""生产会话通过容器 stdio；宿主 transport 仅用于显式维护迁移前记录。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tomllib
import uuid
from pathlib import Path


def codex_executable() -> str:
    candidate = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH") or shutil.which("codex")
    if not candidate and os.name == "nt":
        installed = list((Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin").glob("*/codex.exe"))
        if installed:
            candidate = str(max(installed, key=lambda path: path.stat().st_mtime))
    if not candidate or not Path(candidate).is_file():
        raise RuntimeError("找不到 Codex CLI；请先安装并运行 codex login")
    return str(candidate)


def process_options(*, container=False) -> list[str]:
    # Native tools stay inside Docker. Legacy maintenance disables host tools.
    options = []
    for feature in ("shell_tool", "unified_exec", "view_image", "apps", "browser_use", "computer_use",
                    "in_app_browser", "skill_search", "multi_agent", "workspace_dependencies", "goals",
                    "sleep_tool", "image_generation", "code_mode"):
        if container and feature in {'shell_tool', 'view_image'}:
            options += ['--enable', feature]
        else:
            options += ["--disable", feature]
    options += ["-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                "-c", "features.skip_host_skill_discovery=true"]
    if container:
        from .program_sandbox import PATH
        return options + ['--enable', 'hooks', '-c', 'shell_environment_policy.inherit="none"',
                          '-c', 'shell_environment_policy.set.PATH=' + json.dumps(PATH),
                          '-c', 'shell_environment_policy.set.HOME="/home/agent"',
                          '-c', 'approval_policy="never"',
                          '-c', 'projects={"/workspace"={trust_level="untrusted"}}']
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    for name in config.get("mcp_servers", {}):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RuntimeError(f"无法安全覆盖 MCP 配置名：{name!r}")
        options += ["-c", f"mcp_servers.{name}.enabled=false"]
    return options


class CodexRPC:
    def __init__(self, directory: Path, on_event=None, on_request=None, *, container=None,
                 before_start=None, environment=None):
        self.directory = directory
        self.on_event, self.on_request = on_event, on_request
        self.proc = None
        self.reader = None
        self.next_id = 0
        self.pending = {}
        self.tasks = set()
        self.write_lock = asyncio.Lock()
        self.stderr = None
        self.container = container
        self.before_start = before_start
        self.environment = environment or {}
        self.owner = uuid.uuid4().hex

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stderr = (self.directory / "codex_stderr.log").open("ab")
        env = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_TOOL_CALL_ID",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
            env.pop(key, None)
        if self.container:
            from .codex_container import ensure_runtime, launch_command
            await asyncio.to_thread(ensure_runtime, self.container)
            if self.before_start:
                await self.before_start()
            command = launch_command(self.container, self.owner, 'env',
                *[f'{key}={value}' for key, value in self.environment.items()],
                'codex', 'app-server', '--listen', 'stdio://', *process_options(container=True))
        else:
            command = [codex_executable(), 'app-server', '--listen', 'stdio://', *process_options()]
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.directory.resolve()), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self.stderr, limit=32 * 1024 * 1024,
            creationflags=0x08000000 if os.name == "nt" else 0)
        self.reader = asyncio.create_task(self._read())
        result = await self.request("initialize", {"clientInfo": {
            "name": "celeste_ai_play", "title": "Celeste AI Play", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}, timeout=60)
        await self.send({"method": "initialized"})
        return result

    async def send(self, data):
        async with self.write_lock:
            self.proc.stdin.write((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def request(self, method, params, timeout=60):
        self.next_id += 1
        ident = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def _answer(self, message):
        try:
            if self.on_request is None:
                raise RuntimeError(f"未允许的服务端请求：{message['method']}")
            result = await self.on_request(message["method"], message.get("params") or {})
            await self.send({"id": message["id"], "result": result})
        except Exception as exc:
            await self.send({"id": message["id"], "error": {"code": -32000, "message": str(exc)}})

    async def _read(self):
        failure = None
        try:
            while line := await self.proc.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        task = asyncio.create_task(self._answer(message))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    elif self.on_event:
                        self.on_event(message["method"], message.get("params") or {})
                elif (future := self.pending.get(message.get("id"))) is not None and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
        except Exception as exc:
            failure = ConnectionError(f'Codex app-server 协议读取失败：{type(exc).__name__}: {exc}')
            raise
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure or ConnectionError("Codex app-server 已断开；检查 codex_stderr.log"))

    async def close(self):
        failure = None
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.stdin.close()
                try:
                    await asyncio.wait_for(self.proc.wait(), 5)
                except asyncio.TimeoutError:
                    if self.container:
                        from .codex_container import terminate_owned_server
                        try:
                            await asyncio.to_thread(terminate_owned_server, self.container, self.owner)
                        except Exception as exc:
                            failure = exc
                    if self.proc.returncode is None:
                        self.proc.terminate()
                        await self.proc.wait()
            if self.container and self.proc is not None:
                from .codex_container import terminate_owned_server
                try:
                    await asyncio.to_thread(terminate_owned_server, self.container, self.owner)
                except Exception as exc:
                    failure = failure or exc
        finally:
            if self.reader:
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.stderr:
                self.stderr.close()
        if failure:
            raise RuntimeError('Codex 容器进程收尾未确认，请保留容器检查，不要另起同一会话') from failure
