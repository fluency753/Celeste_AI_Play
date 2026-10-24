"""构建控制台的启动器 CelesteAIConsole.exe（Windows 64 位，放在仓库根目录）。

不需要 Visual Studio：用 pip 安装的 zig 交叉编译 tools/launcher/launcher.c，图标由 Pillow 画出（夜色底、一条往上爬的
红色路线，与控制台的章节路线一致）。启动器只负责用 .venv 的 Python 打开 `python -m overlay.console`，不含控制台代码，
所以只有改了 launcher.c / launcher.rc 或图标时才需要重新构建：

    python tools/build_launcher.py            # 输出 CelesteAIConsole.exe
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "tools" / "launcher"
BUILD = REPO_ROOT / "build" / "launcher"
OUTPUT = REPO_ROOT / "CelesteAIConsole.exe"

NIGHT = (20, 23, 41, 255)        # 控制台底色 #141729
RED = (224, 71, 91, 255)         # #E0475B
LINE = (74, 81, 112, 255)       # 还没点亮的路线 #4A5170
GOLD = (242, 193, 78, 255)       # #F2C14E


def draw_icon(path: Path) -> None:
    from PIL import Image, ImageDraw
    big = 256
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, big - 8, big - 8), radius=52, fill=NIGHT)
    # 一条往上爬的路线：前半段已点亮（红），后半段暗线，山顶一颗金色的点
    points = [(46, 196), (86, 170), (118, 178), (150, 132), (178, 140), (210, 70)]
    draw.line(points[3:], fill=LINE, width=12, joint="curve")
    draw.line(points[:4], fill=RED, width=16, joint="curve")
    for x, y in points[:4]:
        draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=RED)
    for x, y in points[4:5]:
        draw.ellipse((x - 11, y - 11, x + 11, y + 11), outline=LINE, width=6)
    x, y = points[-1]
    draw.ellipse((x - 19, y - 19, x + 19, y + 19), fill=GOLD)
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(path, format="ICO", sizes=sizes)


def zig(*args: str) -> None:
    try:
        import ziglang  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "ziglang"], check=True)
    subprocess.run([sys.executable, "-m", "ziglang", *args], check=True, cwd=BUILD)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUTPUT), help="输出路径（默认仓库根目录的 CelesteAIConsole.exe）")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    BUILD.mkdir(parents=True, exist_ok=True)
    for name in ("launcher.c", "launcher.rc", "launcher.manifest"):
        shutil.copy2(SRC / name, BUILD / name)
    draw_icon(BUILD / "icon.ico")
    exe = BUILD / "CelesteAIConsole.exe"
    zig("build-exe", "launcher.c", "launcher.rc", "-lc", "-luser32", "-target", "x86_64-windows-gnu",
        "-O", "ReleaseSmall", "--subsystem", "windows", "-fstrip", "--name", "CelesteAIConsole")
    out = Path(args.out)
    shutil.copy2(exe, out)
    print(f"已构建 {out}（{out.stat().st_size // 1024} KB）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
