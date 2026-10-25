"""Stopped Codex sessions: synchronize client tools and sandbox without playing."""
import asyncio
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from .config import REPO_ROOT, RunConfig
from .control import RunControl
from .runtimes import config_from_state


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(path):
    text = str(path)
    if os.name == 'nt' and text.startswith('\\\\?\\'):
        text = '\\\\'+text[8:] if text.startswith('\\\\?\\UNC\\') else text[4:]
    return Path(text).resolve()


def configuration(state):
    cfg = config_from_state(state)
    # Match persisted play settings, without introducing CLI launch defaults.
    for field in fields(RunConfig):
        if field.name in state and field.name not in {'model', 'effort', 'provider', 'engine'}:
            setattr(cfg, field.name, state[field.name])
    from .modes import MODE_OF_KNOWLEDGE
    cfg.mode = state.get('mode') or MODE_OF_KNOWLEDGE[cfg.knowledge_profile]
    cfg.live = bool(state.get('live_mode', False))
    from .tuning import apply
    apply(cfg, state.get('tuning') or {})
    return cfg


def targets(paths=None, root=REPO_ROOT):
    runs = (root/'runs').resolve()
    candidates = [Path(p).resolve() for p in paths] if paths else sorted(runs.rglob('run_state.json'), reverse=True)
    selected = {}
    for path in candidates:
        directory = path.parent if path.name == 'run_state.json' else path
        if runs not in directory.parents:
            raise ValueError('只能同步本仓库 runs 下的会话')
        state = read_json(directory/'run_state.json')
        if not state.get('session_id') or not state.get('engine'):
            continue
        if state['engine'] != 'codex':
            if paths:
                raise ValueError('此脚本仅同步 Codex 会话')
            continue
        key = (state['engine'], state['session_id'])
        if key not in selected or target_order(directory, runs) > target_order(selected[key][0], runs):
            selected[key] = (directory, state)
    return list(selected.values())


def target_order(directory, runs):
    # chats/<hash> 不是运行时间；先比较所属运行，再比较同一运行内的元数据更新时间。
    parts = directory.relative_to(runs).parts
    return (parts[1] if len(parts) > 1 else directory.name,
            (directory/'run_state.json').stat().st_mtime_ns, str(directory))


def active_agents(root=REPO_ROOT):
    active = set()
    for path in (root/'runs').rglob('run_state.json'):
        if (path.parent/'control.sqlite3').exists() and RunControl(path.parent).snapshot().get('connected'):
            active.add(read_json(path).get('agent'))
    return active


def patched_rollout(original, ident, tools):
    first, separator, history = original.partition(b'\n')
    if not separator:
        raise ValueError('会话记录缺少首行边界')
    meta = json.loads(first)
    if meta.get('type') != 'session_meta' or meta.get('payload', {}).get('id') != ident:
        raise ValueError('会话元数据身份不匹配')
    existing = meta['payload'].get('dynamic_tools', [])
    if any(not tool.get('name', '').startswith('celeste_') for tool in existing):
        raise ValueError('包含非本项目工具，拒绝覆盖')
    if existing == tools:
        return original
    meta['payload']['dynamic_tools'] = tools
    ending = b'\r\n' if first.endswith(b'\r') else b'\n'
    return json.dumps(meta, ensure_ascii=False, separators=(',', ':')).encode()+ending+history


def replace_checked(path, expected, replacement):
    """Atomic write; never overwrite history appended after the snapshot."""
    fd, name = tempfile.mkstemp(prefix=path.name+'.sync-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != expected:
            raise ValueError('会话记录在检查后发生变化，未覆盖')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def tool_specs(cfg, scratch):
    from .codex_agent import CodexRunner
    metrics = SimpleNamespace(state=SimpleNamespace(tool_calls=0))
    runner = CodexRunner(cfg, scratch, None, metrics, None, prepare_only=True)
    return await runner.tool_specs()


async def thread_metadata(ident, scratch, *, resume=False):
    from .codex_rpc import CodexRPC
    rpc = CodexRPC(scratch)
    try:
        await rpc.start()
        params = {'threadId': ident}
        if resume:
            params.update(excludeTurns=True, sandbox='read-only', approvalPolicy='never', selectedCapabilityRoots=[])
        else:
            params['includeTurns'] = False
        result = await rpc.request('thread/resume' if resume else 'thread/read', params)
        if result['thread']['id'] != ident:
            raise ValueError('会话身份发生变化，拒绝同步')
        return result['thread']
    finally:
        await rpc.close()


async def synchronize(directory, state, output, *, apply=False, root=REPO_ROOT):
    if state['engine'] != 'codex':
        raise ValueError('此脚本仅同步 Codex 会话')
    ident = state['session_id']
    report = {'run': str(directory), 'engine': state['engine'], 'session_id': ident}
    if state['agent'] in active_agents(root):
        return {**report, 'status': 'busy', 'reason': '同一工作区仍有试玩或聊天宿主，已跳过'}
    cfg = configuration(state)
    from .prompting import play_prompt
    report['prompt_sha256'] = digest(play_prompt(cfg).encode())
    # Prompts and KB are loaded from repository files on every launch. No copies of
    # stale prompts, preparation completion flags, metrics or user preferences are written.
    report['prompt_and_kb'] = 'loaded_from_repository_on_next_launch'
    report['status'] = 'current'
    scratch = output/ident
    tools = await tool_specs(cfg, scratch)
    thread = await thread_metadata(ident, scratch/'read')
    path = canonical(thread['path'])
    home = canonical(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    if not any((home/sub).resolve() in path.parents for sub in ('sessions', 'archived_sessions')):
        raise ValueError('会话文件不在 Codex 的会话目录，拒绝修改')
    if (home/'archived_sessions') in path.parents:
        return {**report, 'status': 'archived', 'reason': '已归档会话不修改、不唤起'}
    original = path.read_bytes()
    updated = patched_rollout(original, ident, tools)
    before = {t['name']: t for t in json.loads(original.split(b'\n', 1)[0])['payload'].get('dynamic_tools', [])}
    after = {t['name']: t for t in tools}
    report.update(added=sorted(after.keys()-before.keys()), removed=sorted(before.keys()-after.keys()),
                  changed=sorted(k for k in before.keys() & after.keys() if before[k] != after[k]))
    needs_update = updated != original
    if needs_update:
        report['status'] = 'outdated'
        if apply:
            # Acquire/release the real writer first. Never fork or silently edit a
            # thread held by another app-server, even when its UI looks idle.
            await thread_metadata(ident, scratch/'writer-check', resume=True)
            if state['agent'] in active_agents(root):
                raise ValueError('检查期间已有宿主接入，未修改')
            # Some CLI versions append a thread_settings_applied audit event on
            # resume. Snapshot after our writer probe, preserving those records.
            original = path.read_bytes()
            updated = patched_rollout(original, ident, tools)
            backup = scratch/path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(original)
            replace_checked(path, original, updated)
            try:
                verified = await thread_metadata(ident, scratch/'verify', resume=True)
                actual = Path(verified['path']).read_bytes()
                if patched_rollout(actual, ident, tools) != actual:
                    raise ValueError('恢复后工具登记校验失败')
                history = original.partition(b'\n')[2]
                actual_history = actual.partition(b'\n')[2]
                if not actual_history.startswith(history):
                    raise ValueError('校验期间历史发生变化')
                for line in actual_history[len(history):].splitlines():
                    record = json.loads(line)
                    if record.get('type') != 'event_msg' or record.get('payload', {}).get('type') != 'thread_settings_applied':
                        raise ValueError('校验期间出现新的会话内容')
            except Exception:
                # Restore only our metadata, retaining protocol audit records and
                # any history another writer may have appended in the meantime.
                current = path.read_bytes()
                if current.partition(b'\n')[0] == updated.partition(b'\n')[0]:
                    restored = original.partition(b'\n')[0]+b'\n'+current.partition(b'\n')[2]
                    replace_checked(path, current, restored)
                raise
            report.update(status='updated', backup=str(backup), history_unchanged=True,
                          before_sha256=digest(original), after_sha256=digest(updated))
    if apply and cfg.runtime == 'container':
        from .kb_access import prepare_view
        from .container import ensure_container
        # 容器同步有独立副作用；不能依赖“工具需要修改”分支里的所有权检查。
        if state['agent'] in active_agents(root):
            return {**report, 'status': 'busy', 'container': 'not_changed',
                    'reason': '容器同步前已有宿主接入，未修改容器'}
        if not needs_update:
            await thread_metadata(ident, scratch/'container-writer-check', resume=True)
        if state['agent'] in active_agents(root):
            return {**report, 'status': 'busy', 'container': 'not_changed',
                    'reason': '容器检查期间已有宿主接入，未修改容器'}
        prepare_view(cfg)
        report['container'] = await asyncio.to_thread(ensure_container, cfg, lambda text: print(text, flush=True))
    else:
        report['container'] = 'not_changed'
    return report
