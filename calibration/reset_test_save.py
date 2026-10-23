"""把 test 存档重置成与某个正式存档相同的初始进度（只读正式存档，不改它），用于在 test 存档上彩排正式挑战的完整流程。
也可以把 test 存档还原成之前备份的样子。游戏必须先关闭（否则游戏退出时会用内存里的旧进度覆盖存档文件）。

    python calibration\\reset_test_save.py --saves-dir "D:\\...\\Celeste\\Saves" --like opus5.5
    python calibration\\reset_test_save.py --saves-dir "D:\\...\\Celeste\\Saves" --restore runs\\test_save_backups\\20261023-063000

每次改动之前都会把 test 存档的全部文件（N.celeste 与 Everest 的 N-*.celeste）备份到 runs\\test_save_backups\\<时间戳>\\。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"<Name>(.*?)</Name>")


def game_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Celeste.exe", "/NH"], capture_output=True, text=True).stdout
    return "celeste.exe" in out.lower()


def slots(saves: Path) -> dict[str, int]:
    """存档名 → 槽位号。"""
    found: dict[str, int] = {}
    for f in saves.glob("*.celeste"):
        if not f.stem.isdigit():
            continue
        m = NAME_RE.search(f.read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1) not in found:
            found[m.group(1)] = int(f.stem)
    return found


def slot_files(saves: Path, slot: int) -> list[Path]:
    main = saves / f"{slot}.celeste"
    return ([main] if main.is_file() else []) + sorted(saves.glob(f"{slot}-*.celeste"))


def backup(saves: Path, slot: int) -> Path:
    dest = REPO_ROOT / "runs" / "test_save_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    files = slot_files(saves, slot)
    for f in files:
        shutil.copy2(f, dest / f.name)
    print(f"已备份 test 存档（槽位 {slot}，{len(files)} 个文件）→ {dest}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saves-dir", required=True, help="游戏的 Saves 目录")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--like", help="照这个存档（如 opus5.5）的当前进度重置 test 存档")
    group.add_argument("--restore", help="把 test 存档还原成这个备份目录里的样子")
    args = ap.parse_args()

    saves = Path(args.saves_dir)
    if game_running():
        print("游戏正在运行：请先关闭游戏再重置存档", file=sys.stderr)
        return 2
    names = slots(saves)
    if "test" not in names:
        print(f"{saves} 里没有名为 test 的存档", file=sys.stderr)
        return 2
    test = names["test"]

    if args.restore:
        src = Path(args.restore)
        files = sorted(src.glob(f"{test}.celeste")) + sorted(src.glob(f"{test}-*.celeste"))
        if not files:
            print(f"{src} 里没有槽位 {test} 的备份", file=sys.stderr)
            return 2
        backup(saves, test)
        for f in slot_files(saves, test):
            f.unlink()
        for f in files:
            shutil.copy2(f, saves / f.name)
        print(f"已还原 test 存档（{len(files)} 个文件）")
        return 0

    if args.like not in names:
        print(f"{saves} 里没有名为 {args.like} 的存档", file=sys.stderr)
        return 2
    like = names[args.like]
    if like == test:
        print("--like 不能是 test 自己", file=sys.stderr)
        return 2
    backup(saves, test)
    for f in slot_files(saves, test):
        f.unlink()
    copied = 0
    for f in slot_files(saves, like):
        target = saves / (f"{test}" + f.name[len(str(like)):])
        if f.name == f"{like}.celeste":
            data = re.sub(rb"<Name>.*?</Name>", b"<Name>test</Name>", f.read_bytes(), count=1)
            target.write_bytes(data)
        else:
            shutil.copy2(f, target)
        copied += 1
    print(f"已把 test 存档（槽位 {test}）重置成 {args.like}（槽位 {like}）当前的进度：{copied} 个文件；{args.like} 本身没有改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
