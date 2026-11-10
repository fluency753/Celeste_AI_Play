"""生产会话通过容器 stdio；宿主 transport 仅用于显式维护迁移前记录。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import tomllib
import uuid
from pathlib import Path

DISCONNECT_LOG = 'app_server_exits.jsonl'
STDERR_TAIL_BYTES = 4000
STDERR_TAIL_LINES = 12


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


class CodexRPCError(RuntimeError):
    """A definite JSON-RPC refusal, retaining the server's error fields."""
    def __init__(self, error, *, context=None):
        self.error = error
        super().__init__(f'{context}：{error}' if context else str(error))


class CodexRPC:
    def __init__(self, directory: Path, on_event=None, on_request=None, *, container=None,
                 before_start=None, environment=None, require_login=True):
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
        self.require_login = require_login
        self.before_start = before_start
        self.environment = environment or {}
        self.owner = uuid.uuid4().hex
        self.closing = False
        self.disconnect = None       # What was observed when the stream ended unexpectedly.
        self._stderr_start = 0

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        stderr_path = self.directory / "codex_stderr.log"
        self._stderr_start = stderr_path.stat().st_size if stderr_path.exists() else 0
        self.stderr = stderr_path.open("ab")
        env = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_TOOL_CALL_ID",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
            env.pop(key, None)
        if self.container:
            from .codex_container import ensure_runtime, launch_command
            await asyncio.to_thread(ensure_runtime, self.container, require_login=self.require_login)
            if self.before_start:
                await self.before_start()
            command = launch_command(self.container, self.owner, 'codex', 'app-server',
                                     '--listen', 'stdio://', *process_options(container=True),
                                     environment=self.environment)
        else:
            command = [codex_executable(), 'app-server', '--listen', 'stdio://', *process_options()]
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.directory.resolve()), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self.stderr, limit=32 * 1024 * 1024,
            creationflags=0x08000000 if os.name == "nt" else 0)
        self.reader = asyncio.create_task(self._read())
        try:
            result = await self.request("initialize", {"clientInfo": {
                "name": "celeste_ai_play", "title": "Celeste AI Play", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True}}, timeout=60)
        except ConnectionError:
            if self.container:
                from .codex_container import LOGIN_BUSY, CodexLoginBusy
                try:
                    code = await asyncio.wait_for(self.proc.wait(), 1)
                except asyncio.TimeoutError:
                    code = None
                if code == LOGIN_BUSY:
                    raise CodexLoginBusy() from None
            raise
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

    def _stderr_tail(self) -> str:
        """Last lines this server wrote to stderr (earlier servers in the same file excluded)."""
        try:
            with open(self.directory / "codex_stderr.log", "rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(self._stderr_start, size - STDERR_TAIL_BYTES))
                data = stream.read()
        except OSError as error:
            return f'（读取失败：{error}）'
        lines = data.decode('utf-8', errors='replace').splitlines()
        return '\n'.join(lines[-STDERR_TAIL_LINES:]).strip()

    async def _note_disconnect(self, failure):
        """Record exit code, stderr tail and container state before callers see the disconnect."""
        code = None
        if self.proc is not None and hasattr(self.proc, 'wait'):
            try:
                code = await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:  # noqa: BLE001 - still running (timeout) or not waitable
                code = getattr(self.proc, 'returncode', None)
        info = {'wall': time.time(), 'exit_code': code, 'protocol_error': str(failure) if failure else None,
                'stderr_tail': self._stderr_tail(), 'container': self.container}
        if self.container:
            from .codex_container import container_state
            info['container_state'] = await asyncio.to_thread(container_state, self.container)
        self.disconnect = info
        try:
            with open(self.directory / DISCONNECT_LOG, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(info, ensure_ascii=False) + '\n')
        except OSError:
            pass
        tail = info['stderr_tail'].replace('\n', ' | ') if info['stderr_tail'] else '（stderr 无输出）'
        print(f'[codex] App Server 连接意外结束：{self.disconnect_detail()}；stderr：{tail}', flush=True)

    def disconnect_detail(self) -> str:
        info = self.disconnect
        if not info:
            return '未取得退出信息'
        code = info.get('exit_code')
        parts = ['进程仍在运行，输出流已断开' if code is None else f'进程退出码 {code}']
        if info.get('container_state'):
            parts.append(f"容器 {info['container_state']}")
        if info.get('protocol_error'):
            parts.append(info['protocol_error'])
        return '，'.join(parts)

    def disconnect_message(self, prefix='Codex app-server 已断开') -> str:
        return (f'{prefix}（{self.disconnect_detail()}）；详见 {self.directory / DISCONNECT_LOG} '
                f'与 {self.directory / "codex_stderr.log"}')

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
                        future.set_exception(CodexRPCError(message["error"]))
                    else:
                        future.set_result(message.get("result"))
        except Exception as exc:
            failure = ConnectionError(f'Codex app-server 协议读取失败：{type(exc).__name__}: {exc}')
            raise
        finally:
            # Our own close() and task cancellation are not disconnects.
            if not self.closing and not isinstance(sys.exception(), asyncio.CancelledError):
                try:
                    await self._note_disconnect(failure)
                except Exception as error:  # noqa: BLE001 - evidence must not hide the disconnect
                    print(f'[codex] 记录 App Server 断开信息失败：{error}', flush=True)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure or ConnectionError(self.disconnect_message()))

    async def close(self):
        self.closing = True   # The stream ending from here on is our own shutdown.
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
