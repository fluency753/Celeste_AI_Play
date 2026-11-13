"""静态检查：只拦会在运行时出错的写法（未定义的名字、语法错误），其余只报数量。

用法：python tools/static_check.py [目录或文件 …]（默认检查 orchestrator、overlay、tools、calibration、agent-container）
需要 pyflakes（python -m pip install pyflakes；不在 requirements.txt 里，运行程序用不到）。
未使用的导入、未使用的局部变量等不算失败：仓库没有统一的格式与静态检查规范，这里不借机引入。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT = ('orchestrator', 'overlay', 'tools', 'calibration', 'agent-container')
BLOCKING = ('UndefinedName', 'UndefinedExport', 'UndefinedLocal', 'DoctestSyntaxError')


def check(paths) -> tuple[list[str], Counter]:
    from pyflakes import api
    from pyflakes.reporter import Reporter

    class Collect(Reporter):
        def __init__(self):
            self.blocking, self.other = [], Counter()

        def unexpectedError(self, filename, message):
            self.blocking.append(f'{filename}: {message}')

        def syntaxError(self, filename, message, lineno, offset, text):
            self.blocking.append(f'{filename}:{lineno}: 语法错误：{message}')

        def flake(self, message):
            kind = type(message).__name__
            if kind in BLOCKING:
                self.blocking.append(str(message))
            else:
                self.other[kind] += 1

    collect = Collect()
    api.checkRecursive([str(path) for path in paths], collect)
    return collect.blocking, collect.other


def main(argv=None) -> int:
    args = (sys.argv[1:] if argv is None else argv) or [str(REPO / name) for name in DEFAULT]
    try:
        blocking, other = check(args)
    except ImportError:
        print('缺少 pyflakes：python -m pip install pyflakes', file=sys.stderr)
        return 2
    for line in blocking:
        print(line)
    rest = '，'.join(f'{kind} {count}' for kind, count in sorted(other.items())) or '无'
    print(f'[static] 会在运行时出错的问题 {len(blocking)} 处；其余提示（不算失败）：{rest}')
    return 1 if blocking else 0


if __name__ == '__main__':
    raise SystemExit(main())
