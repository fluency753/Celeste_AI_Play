"""Reload the operator's stop_run category table for each report."""
from dataclasses import dataclass
from pathlib import Path
import re

from .session_store import load_json


CONFIG_DIR = Path(__file__).resolve().parents[1] / 'configs'
DEFAULT_CATEGORIES = CONFIG_DIR / 'stop_categories.json'
LOCAL_CATEGORIES = CONFIG_DIR / 'local.stop_categories.json'
STEPS = {'controller_defect': 2, 'user_help': 3}


@dataclass(frozen=True)
class StopCategory:
    label: str
    criterion: str
    required_steps: int


def load_stop_categories() -> dict[str, StopCategory]:
    categories = {}
    for path in (DEFAULT_CATEGORIES, LOCAL_CATEGORIES):
        try:
            rows = load_json(path, encoding='utf-8-sig')
        except FileNotFoundError:
            if path == LOCAL_CATEGORIES:
                continue
            raise
        except ValueError as exc:
            raise ValueError(f'{path.name} 无法解析：{exc}') from exc
        if not isinstance(rows, dict):
            raise ValueError(f'{path.name} 须为按类别 ID 索引的对象')
        for key, row in rows.items():
            if not re.fullmatch(r'[a-z][a-z0-9_]*', key) or not isinstance(row, dict):
                raise ValueError(f'{path.name} 类别定义无效：{key}')
            kind, label, criterion = (row.get(field) for field in ('kind', 'label', 'criterion'))
            if (not isinstance(kind, str) or kind not in STEPS
                    or not all(isinstance(value, str) and value.strip() for value in (label, criterion))):
                raise ValueError(f'{path.name} 类别 {key} 须填写有效的 kind、label 和 criterion')
            categories[key] = StopCategory(label.strip(), criterion.strip(), STEPS[kind])
    if not categories:
        raise ValueError('阻塞分类表为空')
    return categories
