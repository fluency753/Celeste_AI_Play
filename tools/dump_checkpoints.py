"""从正在运行的游戏导出官方章节（A 面）的检查点表，写进 configs/checkpoints.json。

按段通关（--segments、控制台的分段选择）用这张表：段 k 从检查点 k（0 = 章节开头）到下一个检查点或通关。
表与游戏里选章面板的检查点一致（房间名与检查点名，名字有中英文）。需要游戏开着、桥接 1.0.6 或更新；
不接管游戏（不发 activate），游戏停在什么画面都可以。

    python tools/dump_checkpoints.py
    python tools/dump_checkpoints.py --out configs/checkpoints.json --port 47774
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from bridge_client import BridgeClient
except ImportError:  # 作为 tools 包导入时
    from tools.bridge_client import BridgeClient

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=47774)
    ap.add_argument("--out", default=str(REPO_ROOT / "configs" / "checkpoints.json"))
    args = ap.parse_args()
    client = BridgeClient(port=args.port, timeout=30)
    try:
        hello = client.call("hello")
        if not hello.get("checkpoints"):
            print(f"桥接 {hello.get('bridge')} 还不能导出检查点：请更新到 1.0.6 或更新版本", file=sys.stderr)
            return 1
        areas = client.call("areas")["areas"]
    finally:
        client.close()
    keep = ("index", "level", "name_en", "name_zh")
    data = {
        "note": "官方章节（A 面）的检查点：第 0 项是章节开头。由 tools/dump_checkpoints.py 从游戏导出，按段通关用。",
        "bridge": hello.get("bridge"),
        "areas": [{"id": a["id"], "sid": a["sid"], "name_en": a.get("name_en"), "name_zh": a.get("name_zh"),
                   "checkpoints": [{k: p.get(k) for k in keep} for p in a.get("checkpoints") or []]} for a in areas],
    }
    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for a in data["areas"]:
        names = " / ".join(f"{p['level']}（{p.get('name_zh') or p.get('name_en')}）" for p in a["checkpoints"])
        print(f"{a['id']:>2} {a.get('name_zh') or a['sid']}: {len(a['checkpoints'])} 段  {names}")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
