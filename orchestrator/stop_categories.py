"""Reload the operator's stop_run category table for each report."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from .session_store import load_json, locked_write, write_json


CONFIG_DIR = Path(__file__).resolve().parents[1] / 'configs'
DEFAULT_CATEGORIES = CONFIG_DIR / 'stop_categories.json'
LOCAL_CATEGORIES = CONFIG_DIR / 'local.stop_categories.json'
STEPS = {'controller_defect': 2, 'user_help': 3}


@dataclass(frozen=True)
class StopCategory:
    label: str
    criterion: str
    required_steps: int


def _category(key, row):
    if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', key) or not isinstance(row, dict):
        raise ValueError('类别 ID 须以小写字母开头，后接小写字母、数字或下划线')
    kind, label, criterion = (row.get(field) for field in ('kind', 'label', 'criterion'))
    if (not isinstance(kind, str) or kind not in STEPS
            or not all(isinstance(value, str) and value.strip() for value in (label, criterion))):
        raise ValueError(f'类别 {key} 须填写有效的类型、显示名和复核要求')
    return {'kind': kind, 'label': label.strip(), 'criterion': criterion.strip()}


def _rows(path, *, optional=False):
    try:
        rows = load_json(path, encoding='utf-8-sig')
    except FileNotFoundError:
        if optional:
            return {}
        raise
    except ValueError as exc:
        raise ValueError(f'{path.name} 无法解析：{exc}') from exc
    if not isinstance(rows, dict):
        raise ValueError(f'{path.name} 须为按类别 ID 索引的对象')
    for key, row in rows.items():
        try:
            _category(key, row)
        except ValueError as exc:
            raise ValueError(f'{path.name}：{exc}') from exc
    return rows


def load_stop_categories() -> dict[str, StopCategory]:
    categories = {}
    for key, row in (_rows(DEFAULT_CATEGORIES) | _rows(LOCAL_CATEGORIES, optional=True)).items():
        item = _category(key, row)
        categories[key] = StopCategory(item['label'], item['criterion'], STEPS[item['kind']])
    if not categories:
        raise ValueError('阻塞分类表为空')
    return categories


def _editor_document(defaults, local):
    version = sha256(json.dumps([defaults, local], sort_keys=True).encode()).hexdigest()
    return {'version': version, 'categories': [
        {'id': key, **_category(key, row), 'builtin': key in defaults, 'custom': key in local}
        for key, row in (defaults | local).items()], 'steps': STEPS}


def editor_read():
    return _editor_document(_rows(DEFAULT_CATEGORIES), _rows(LOCAL_CATEGORIES, optional=True))


def editor_save(key, action, fields, version):
    with locked_write(LOCAL_CATEGORIES.with_suffix('.json.lock')):
        defaults = _rows(DEFAULT_CATEGORIES)
        local = _rows(LOCAL_CATEGORIES, optional=True)
        if version != _editor_document(defaults, local)['version']:
            raise ValueError('分类配置已被修改，请重新读取后保存')
        if not isinstance(key, str):
            raise ValueError('请选择分类')
        if action == 'reset':
            if key not in local:
                raise ValueError('所选类别没有本机设置')
            del local[key]
        elif action in ('save', 'add'):
            if (key in defaults or key in local) != (action == 'save'):
                raise ValueError('类别已存在或已删除，请检查类别 ID')
            item = _category(key, fields)
            for value in item.values():
                if len(value) > 4000 or '\x00' in value or any('\ud800' <= char <= '\udfff' for char in value):
                    raise ValueError('分类文字最多 4000 字，须为有效文本')
            local[key] = {**local.get(key, {}), **item}
        else:
            raise ValueError('未知分类编辑操作')
        if not (defaults or local):
            raise ValueError('阻塞分类表不能为空')
        write_json(LOCAL_CATEGORIES, local, indent=2)
        return _editor_document(defaults, local)
