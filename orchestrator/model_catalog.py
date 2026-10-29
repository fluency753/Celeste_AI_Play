"""Read Codex's public model catalog without creating threads or accessing credentials."""
import asyncio
import copy
import re
import threading
import time

from .config import REPO_ROOT
from .codex_rpc import CodexRPC
from .codex_container import ensure_account_container
from .efforts import normalize_efforts

_lock = threading.Lock()
_cached = None
_loaded = 0.0


def normalize_models(rows):
    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = row.get('model') or row.get('id')
        if (row.get('hidden') or not isinstance(ident, str) or ident in seen
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', ident)):
            continue
        if row.get('inputModalities') and 'image' not in row['inputModalities']:
            continue
        efforts = normalize_efforts([e.get('reasoningEffort') for e in (row.get('supportedReasoningEfforts') or [])
                                     if isinstance(e, dict)])
        if not efforts:
            continue
        default_effort = row.get('defaultReasoningEffort')
        models.append({'id': ident, 'label': row.get('displayName') or ident,
                       'description': row.get('description') or '', 'efforts': efforts,
                       'default_effort': default_effort if default_effort in efforts else efforts[0],
                       'default': bool(row.get('isDefault'))})
        seen.add(ident)
    if not models:
        raise RuntimeError('Codex 没有返回适合当前试玩执行器的模型，请刷新列表或检查登录状态')
    default = next((m['id'] for m in models if m['default']), models[0]['id'])
    return {'models': models, 'default_model': default, 'source': 'codex_model_list'}


async def fetch_models():
    container = await asyncio.to_thread(ensure_account_container)
    rpc = CodexRPC(REPO_ROOT / 'local/model-catalog', container=container)
    rows, cursor, cursors = [], None, set()
    try:
        await rpc.start()
        for _ in range(20):
            params = {'includeHidden': False, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            result = await rpc.request('model/list', params, timeout=15)
            if not isinstance(result, dict) or not isinstance(result.get('data'), list):
                raise RuntimeError('模型列表响应格式无效，请刷新后重试')
            rows.extend(result.get('data') or [])
            cursor = result.get('nextCursor')
            if not cursor:
                return normalize_models(rows)
            if cursor in cursors:
                raise RuntimeError('模型列表分页重复，请刷新后重试')
            cursors.add(cursor)
        raise RuntimeError('模型列表分页过多，请刷新后重试')
    finally:
        await rpc.close()


def load_models(*, refresh=False):
    global _cached, _loaded
    with _lock:
        if refresh or _cached is None or time.monotonic() - _loaded > 300:
            _cached = asyncio.run(fetch_models())
            _loaded = time.monotonic()
        return copy.deepcopy(_cached)


def resolve_selection(model, effort):
    catalog = load_models()
    ident = model or catalog['default_model']
    selected = next((item for item in catalog['models'] if item['id'] == ident), None)
    if selected is None:
        raise ValueError('模型不在当前列表中，请刷新模型列表后重新选择')
    if effort not in selected['efforts']:
        raise ValueError('所选模型不支持该推理强度，请重新选择')
    return ident, effort
