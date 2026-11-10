"""Codex's persistent container environment and explicit credential/session imports.

No host login is copied during ordinary startup. Imports are explicit maintenance
operations; credentials travel over stdin and never enter arguments or logs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from .config import find_docker_exe


CODEX_HOME = '/home/agent/.codex'
CODEX_VERSION = '0.165.0-alpha.1.2'
ACCOUNT_AGENT = 'codex-account'
LOGIN_BUSY = 73


class CodexLoginBusy(RuntimeError):
    def __init__(self):
        super().__init__('项目的 Codex player 登录正在使用，请先挂起试玩或断开当前聊天')


class CodexLoginRequired(RuntimeError):
    def __init__(self, container):
        self.container = container
        super().__init__(f'Codex 容器 {container} 需要登录或重新授权。请运行 '
                         f'.\\.venv\\Scripts\\python.exe -m orchestrator.codex_container --container {container} login')


def ensure_account_container(*, image=None, log=print) -> str:
    """Use the ordinary container lifecycle for the model catalog's account."""
    from .container import ensure_container
    from .config import RunConfig
    from .runtimes import runtime
    cfg = RunConfig(agent=ACCOUNT_AGENT, save='', model='', engine='codex',
                    mode='speedrun', knowledge_profile='full', sim=False)
    runtime('codex').configure(cfg)
    if image:
        cfg.image = image
    return ensure_container(cfg, log=log)


def exec_command(container: str, *command: str, workdir='/workspace', environment=None) -> list[str]:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container):
        raise ValueError('无效的 Codex 容器名称')
    args = [find_docker_exe(), 'exec', '-i', '-u', 'agent', '-w', workdir, '-e', f'CODEX_HOME={CODEX_HOME}']
    for key, value in (environment or {}).items():
        args += ['-e', f'{key}={value}']
    return [*args, container, *command]


def launch_command(container: str, owner: str, *command: str, environment=None) -> list[str]:
    if not re.fullmatch(r'[a-f0-9]{32}', owner):
        raise ValueError('无效的 App Server 所有者标识')
    return exec_command(container, 'python3', '/usr/local/lib/celeste/codex-app-server.py',
                        '--owner', owner, '--', *command, environment=environment)


def container_state(container: str) -> str:
    """One line of container state for disconnect evidence; never raises."""
    template = '{{.State.Status}} 退出码={{.State.ExitCode}} OOM={{.State.OOMKilled}} 启动于={{.State.StartedAt}}'
    try:
        result = subprocess.run([find_docker_exe(), 'inspect', '-f', template, container], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=10)
    except Exception as error:  # noqa: BLE001 - Docker itself may be what went away
        return f'状态未知（{type(error).__name__}: {error}）'
    if result.returncode:
        return f'状态未知（docker inspect：{(result.stderr or result.stdout).strip()[:300]}）'
    return result.stdout.strip()


def login_args(*args: str) -> list[str]:
    wrapper = ('if [ "$CELESTE_CODEX_PLAYER_LOGIN" = 1 ]; then '
               'exec python3 /usr/local/lib/celeste/codex-app-server.py --login-command -- "$@"; fi; exec "$@"')
    return ['sh', '-c', wrapper, 'codex-login', 'codex', 'login', *args]


def terminate_owned_server(container: str, owner: str) -> None:
    if not re.fullmatch(r'[a-f0-9]{32}', owner):
        raise ValueError('无效的 App Server 所有者标识')
    result = _run(container, 'python3', '/usr/local/lib/celeste/codex-app-server.py',
                  '--terminate', owner, timeout=20)
    if result.returncode or not json.loads(result.stdout).get('stopped'):
        raise RuntimeError('无法确认容器内本次 App Server 及其子进程已停止')


def _run(container, *command, input=None, timeout=30):
    return subprocess.run(exec_command(container, *command), input=input,
                          capture_output=True, text=True, encoding='utf-8', timeout=timeout)


def login_status(container: str) -> dict:
    result = _run(container, *login_args('status'))
    if result.returncode == LOGIN_BUSY:
        raise CodexLoginBusy()
    if result.returncode not in (0, 1):
        raise RuntimeError('Codex 登录检查失败，请检查容器镜像是否需要重建')
    text = result.stdout + result.stderr
    method = ('chatgpt' if 'ChatGPT' in text else 'api' if 'API key' in text else None)
    return {'authenticated': result.returncode == 0, 'method': method}


def ensure_runtime(container: str, *, require_login=True) -> None:
    result = _run(container, 'codex', '--version')
    if result.returncode or result.stdout.strip() != f'codex-cli {CODEX_VERSION}':
        raise RuntimeError(f'容器缺少匹配的 Codex {CODEX_VERSION}，请重新构建 agent-container 镜像')
    if require_login and not login_status(container)['authenticated']:
        raise CodexLoginRequired(container)


AUTH_IMPORT = r'''
import json,os,sys,tempfile
from pathlib import Path
if os.environ.get('CELESTE_CODEX_PLAYER_LOGIN') == '1':
 raise ValueError('共享 player 登录须使用设备授权')
root=Path(os.environ['CODEX_HOME'])
if root.is_symlink() or not root.is_dir():raise ValueError('Codex home unavailable')
data=sys.stdin.buffer.read(1048577)
if len(data)>1048576:raise ValueError('Credential file too large')
obj=json.loads(data)
if not isinstance(obj,dict) or not (isinstance(obj.get('tokens'),dict) or isinstance(obj.get('OPENAI_API_KEY'),str)):
 raise ValueError('Unsupported credential cache')
target=root/'auth.json'
if target.is_symlink():raise ValueError('Credential destination is a symlink')
temporary=None
try:
 fd,name=tempfile.mkstemp(prefix='.auth-import-',dir=root)
 temporary=Path(name)
 with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
 os.chmod(temporary,0o600)
 temporary.replace(target)
finally:
 if temporary is not None:temporary.unlink(missing_ok=True)
'''


def import_auth(container: str, source: Path) -> None:
    """Import only a file the caller explicitly selected; never discover host auth."""
    with source.open('rb') as stream:
        raw = stream.read(1048577)
    _install_auth(container, raw)


def _install_auth(container: str, raw: bytes) -> None:
    if len(raw) > 1048576:
        raise ValueError('登录缓存超过 1 MB')
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('登录缓存不是有效的 JSON') from exc
    if not isinstance(parsed, dict) or not (isinstance(parsed.get('tokens'), dict)
                                          or isinstance(parsed.get('OPENAI_API_KEY'), str)):
        raise ValueError('文件不是支持的 Codex 登录缓存')
    # Never put source data in the process arguments or include child output in errors.
    result = _run(container, 'python3', '-c', AUTH_IMPORT, input=raw.decode('utf-8-sig'))
    if result.returncode:
        raise RuntimeError('导入 Codex 登录缓存失败；原登录缓存未主动移除')


META_READ = r'''
import json,os,re,sys
from pathlib import Path
root=Path(os.environ['CODEX_HOME']).resolve()
p=Path(sys.argv[1]).resolve()
if not any(p.is_relative_to(root/part) for part in ('sessions','archived_sessions')):
 raise ValueError('Not a Codex session path')
with p.open(encoding='utf-8') as stream:line=stream.readline(4194305)
if len(line)>4194304:raise ValueError('Session metadata too large')
data=json.loads(line)
if data.get('type')!='session_meta':raise ValueError('Missing session metadata')
payload=data.get('payload') or {}
ident=payload.get('id')
if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident):
 raise ValueError('Invalid session identity')
journal=root/'simulation-schema-migrations'/ident/'journal.json'
try:
 pending=journal.is_symlink() or (journal.exists() and json.loads(journal.read_text()).get('state')!='complete')
except (OSError,ValueError,AttributeError):
 pending=True
if pending:raise SystemExit('SIMULATION_SCHEMA_MIGRATION_PENDING')
print(json.dumps({k:payload[k] for k in ('id','cwd','source','cli_version','dynamic_tools') if k in payload}))
'''


def read_metadata(container: str, path: str) -> dict:
    result = _run(container, 'python3', '-I', '-c', META_READ, path)
    if result.returncode:
        if 'SIMULATION_SCHEMA_MIGRATION_PENDING' in result.stderr:
            raise RuntimeError('该会话的模拟接口迁移尚未完成，拒绝开始模型轮次；'
                               '迁移工具已移除，请新建准备会话')
        raise RuntimeError('无法核验容器内 Codex 会话登记；确认会话属于当前工作区并已显式导入旧会话')
    try:
        metadata = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError('Codex 会话登记不是有效 JSON') from exc
    if not isinstance(metadata, dict):
        raise RuntimeError('Codex 会话登记格式错误')
    return metadata


SESSION_IMPORT = r'''
import json,os,sys,tempfile
from pathlib import Path
root=Path(os.environ['CODEX_HOME'])
relative=Path(sys.argv[1])
if relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in ('sessions','archived_sessions'):
 raise ValueError('Invalid session destination')
dest=root/relative
parent=dest.parent
if any(p.is_symlink() for p in [root,*parent.parents,parent,dest]):raise ValueError('Symlink destination')
parent.mkdir(parents=True,exist_ok=True)
temporary=None
try:
 fd,name=tempfile.mkstemp(prefix='.session-import-',dir=parent)
 temporary=Path(name)
 with os.fdopen(fd,'wb') as out:
  while chunk:=sys.stdin.buffer.read(65536):out.write(chunk)
  out.flush();os.fsync(out.fileno())
 os.chmod(temporary,0o600)
 os.link(temporary,dest)
finally:
 if temporary is not None:temporary.unlink(missing_ok=True)
'''


def import_session(container: str, source: Path) -> str:
    """Explicitly copy a rollout, keeping the original and refusing overwrites."""
    # Keep Codex's discovery layout; never copy the host config or credential directory.
    parts = source.absolute().parts
    matches = [i for i, part in enumerate(parts) if part in ('sessions', 'archived_sessions')]
    if not matches:
        raise ValueError('旧会话文件必须来自 sessions 或 archived_sessions 目录')
    relative = Path(*parts[matches[-1]:]).as_posix()
    with source.open('rb', buffering=0) as stream:
        line = stream.readline(4194305)
        try:
            metadata = json.loads(line)
        except ValueError as exc:
            raise ValueError('旧会话缺少有效登记信息') from exc
        if len(line) > 4194304 or not isinstance(metadata, dict) or metadata.get('type') != 'session_meta':
            raise ValueError('旧会话缺少有效登记信息')
        stream.seek(0)
        result = subprocess.run(exec_command(container, 'python3', '-c', SESSION_IMPORT, relative),
                                stdin=stream, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('旧会话导入失败，目标可能已存在；原文件未修改，未覆盖容器会话')
    return CODEX_HOME + '/' + relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--container')
    target.add_argument('--account', action='store_true', help='本仓库的模型目录账号环境')
    parser.add_argument('--image', help='创建共享账号环境时使用的容器镜像')
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('status')
    commands.add_parser('login')
    for name in ('import-auth', 'import-session'):
        command = commands.add_parser(name)
        command.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    name = ensure_account_container(image=args.image) if args.account else args.container
    ensure_runtime(name, require_login=False)
    if args.operation == 'status':
        print(json.dumps(login_status(name), ensure_ascii=False))
    elif args.operation == 'login':
        result = subprocess.call(exec_command(name, *login_args('--device-auth')))
        if result == LOGIN_BUSY:
            raise SystemExit(str(CodexLoginBusy()))
        raise SystemExit(result)
    elif args.operation == 'import-auth':
        import_auth(name, args.file)
        print('已导入；请用 status 核对容器登录状态。')
    elif args.operation == 'import-session':
        print(import_session(name, args.file))


if __name__ == '__main__':
    main()
