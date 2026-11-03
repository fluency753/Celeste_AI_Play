"""Optional, host-owned predictions. Agent files contain inputs/results, never a simulator."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .sim_assist import AssistRules, validate_request

PRESET_RULES = AssistRules(max_candidates=256, max_frames=6000, max_total=60000)
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def available(cfg):
    return (cfg.sim_dir / 'celeste-sim').is_file()


def enabled(cfg):
    return bool(cfg.sim_assist or (cfg.sim and cfg.pause_limit))


def _request_file(workspace, value):
    if workspace is None or not isinstance(value, str) or not value:
        raise ValueError('request_file 必须是工作区内的 JSON 文件')
    relative = value.removeprefix('/workspace/').replace('\\', '/')
    parts = relative.split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts) or relative.startswith('/'):
        raise ValueError('request_file 必须在 /workspace 内，不允许链接或上级路径')
    root = Path(workspace).resolve()
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or path.is_junction():
            raise ValueError('request_file 不允许符号链接或目录联接')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
            raise ValueError('模拟请求必须是最多2 MiB的普通 JSON 文件')
        if not path.resolve().is_relative_to(root) or path.stat().st_ino != info.st_ino:
            raise ValueError('模拟请求文件读取期间发生替换')
        content = stream.read(MAX_REQUEST_BYTES + 1)
    if len(content) > MAX_REQUEST_BYTES:
        raise ValueError('模拟请求文件超过2 MiB')
    return json.loads(content)


def _normalize(session, request):
    if not isinstance(request, dict) or set(request) - {'candidates', 'input_format', 'request_file', 'request_id'}:
        raise ValueError('模拟请求仅允许 candidates、input_format、request_file、request_id')
    request = dict(request)
    ident = request.pop('request_id', None)
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', ident):
        raise ValueError('实际预测必须显式提供 request_id（1到80个字母、数字、下划线或连字符），重试沿用同一编号；旧会话未登记此参数时须先迁移工具或新建会话。本次未执行且未扣费。')
    filename = request.pop('request_file', None)
    if filename is not None:
        if 'candidates' in request:
            raise ValueError('candidates 与 request_file 只能提供一个')
        body = _request_file(session.workspace, filename)
        if not isinstance(body, dict) or set(body) - {'candidates', 'input_format'}:
            raise ValueError('请求文件仅允许 candidates 和 input_format')
        request = {**request, **body}
    requested_format = request.pop('input_format', 'celeste_ai')
    from .workspace_files import tas
    tas.check_format(requested_format)
    rules = PRESET_RULES if session.cfg.sim else session.assist_rules
    if requested_format == 'celeste_tas':
        candidates = request.get('candidates')
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= rules.max_candidates:
            raise ValueError('TAS模拟候选数量超出限制')
        request = {'candidates': [tas.to_sim(plan, rules.max_frames) for plan in candidates]}
    candidates, cost = validate_request(request, rules)
    digest = hashlib.sha256(json.dumps({'candidates': candidates, 'format': requested_format},
                                      sort_keys=True).encode()).hexdigest()
    return ident, digest, candidates, cost, requested_format


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.simulation-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _cached(session, ident, digest):
    directory = session.run_dir
    seen = set()
    while directory not in seen:
        seen.add(directory)
        path = directory / 'simulation' / (ident + '.json')
        if path.exists():
            result = json.loads(path.read_text(encoding='utf-8'))
            if result.get('request_digest') != digest:
                raise ValueError('模拟请求编号已用于不同内容，本次未执行')
            return result
        try:
            previous = json.loads((directory / 'run_state.json').read_text(encoding='utf-8')).get('resumed_from')
        except FileNotFoundError:
            previous = None
        if not previous:
            return None
        directory = Path(previous)
    raise ValueError('运行恢复链包含循环，不能读取模拟回执')


def _present(session, result):
    # Re-publication is repeatable. Workspace failure never changes a prediction's outcome.
    result = dict(result)
    budget = getattr(session, 'planning_budget', None) if session.cfg.sim else session.sim_energy
    result['planning' if session.cfg.sim else 'energy'] = budget.snapshot()
    if session.workspace:
        rel = 'game/simulation/' + result['receipt'] + '.json'
        try:
            _write(session.workspace / rel, result)
            result['trajectory_file'] = '/workspace/' + rel
        except OSError:
            result['publication_warning'] = '预测已执行；工作区结果发布失败。用同一 request_id 重试可补发布，不会重新预测或扣费。'
    summary = {key: value for key, value in result.items() if key not in {'results', 'request_digest', 'candidates'}}
    summary['results_total'] = len(result['results'])
    summary['results'] = [{'stop': r.get('stop'), 'detail': r.get('detail'),
                           'frames': len(r.get('frames', [])),
                           'last_frame': (r.get('frames') or [None])[-1],
                           'events': r.get('events', [])[:16], 'warnings': r.get('warnings', [])[:8]}
                          for r in result['results'][:24]]
    return summary


def predict(session, request):
    """Called by both adapters through the real game tool; no agent code is executed."""
    from . import sim_assist
    with session.lock:
        tool_guard = getattr(session, 'tool_guard', None)
        guard = (lambda: tool_guard('simulate')) if tool_guard else getattr(session, 'control_guard', None)
        if guard and (reason := guard()):
            raise ValueError(reason)
        if session.done or session.goals.stop_motion.is_set():
            raise ValueError('试玩已结束或正在收尾，本次未执行模拟')
        if not enabled(session.cfg):
            raise ValueError('未开启限额模拟能力，本次未执行；工作区程序仍可使用')
        if not available(session.cfg):
            raise ValueError('未配置可用的主控模拟器，本次未执行且未扣费')
        ident, digest, candidates, cost, requested_format = _normalize(session, request)
        budget = getattr(session, 'planning_budget', None) if session.cfg.sim else session.sim_energy
        if budget is None:
            raise ValueError('主控模拟账本尚未就绪，本次未执行')
        # A result is reusable only with a matching authoritative reservation.
        try:
            if session.cfg.sim:
                receipt, fresh = budget.reserve(ident, digest)
            else:
                receipt, fresh = budget.reserve_request(ident, digest, cost)
        except OSError as exc:
            raise ValueError('模拟授权落盘失败，本次未提交、未执行且未扣费；保留同一 request_id 重试。') from exc
        if not session.cfg.sim:
            try:
                session.metrics.update(sim_energy=budget.snapshot())
            except OSError:
                pass
        cached = _cached(session, ident, digest) if not fresh else None
        if not fresh:
            if cached is None:
                raise ValueError(f'模拟请求 {ident} 已受理，执行结果未确认或执行失败；不会自动重跑或重复扣费。请依据已有状态继续。')
            return _present(session, cached)
        directory = session.run_dir / 'sim_private'
        try:
            directory.mkdir(exist_ok=True)
            state = session.client.call('sim_state', margin=4)['state']
            _write(directory / 'state.json', state)
        except Exception as exc:
            # No worker has been submitted, so an energy reservation is safely refundable.
            try:
                if not session.cfg.sim:
                    budget.finish(ident, refund=True)
                    session.metrics.update(sim_energy=budget.snapshot())
                else:
                    budget.refund(ident)
            except OSError as refund_error:
                raise RuntimeError(f'模拟请求 {ident} 尚未提交、未执行；额度返还落盘失败，保留同一编号核验，不重新预测：{refund_error}') from exc
            raise ValueError(f'模拟请求 {ident} 尚未提交、未执行，预留额度已返还：{exc}') from exc
        try:
            result = sim_assist.run_predictions(session.cfg, directory, candidates)
        except Exception as exc:
            # Docker/transport failure can happen after execution. Never issue a free retry.
            try:
                if session.cfg.sim:
                    budget.finish(ident, status='failed')
                else:
                    budget.finish(ident)
            except OSError:
                pass  # Reserved is already durable; an unknown worker must not be replayed.
            raise RuntimeError(f'模拟请求 {ident} 已提交但未获得完整结果；额度保留扣除，不要重放该请求：{exc}') from exc
        result = dict(result, receipt=ident, request_digest=digest, cost=1 if session.cfg.sim else cost,
                      candidates=candidates, input_format='celeste_ai', requested_input_format=requested_format)
        result['planning' if session.cfg.sim else 'energy'] = budget.snapshot()
        try:
            session.metrics.add(sim_calls=1, sim_rollouts=len(candidates))
        except OSError:
            result['metrics_warning'] = '预测已执行，统计持久化失败；不要重新执行预测。'
        try:
            _write(session.run_dir / 'simulation' / (ident + '.json'), result)
        except OSError as exc:
            raise RuntimeError(f'模拟请求 {ident} 已执行但私有结果落盘失败，已扣额度；不要重复执行：{exc}') from exc
        try:
            budget.finish(ident)
        except OSError:
            result['receipt_warning'] = '预测已执行且结果已保存，账本终态写入失败；同一 request_id 只补取结果，不重跑。'
        if not session.cfg.sim:
            try:
                session.metrics.update(sim_energy=budget.snapshot())
            except OSError:
                result['metrics_warning'] = '预测已执行，统计持久化失败；不要重新执行预测。'
        if session.playback:
            try:
                session.playback.collect_prediction(result['results'], result['info'].get('frame', 0))
            except Exception:
                result['replay_warning'] = '本次预测已完成，但轨迹回放记录失败'
        return _present(session, result)
