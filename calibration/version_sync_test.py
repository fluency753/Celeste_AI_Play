"""项目版本只有一个来源（docs/开发/发布流程.md）：overlay/console_api.py 的 CONSOLE_VERSION。
控制台启动器的文件版本（launcher.rc、launcher.manifest）与 README 里写的版本必须跟它一致——
README 这一处曾两次在升版本时漏改。版本摘要里的最新一节不能比它新（摘要写的版本要已经在主线上）。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def console_version() -> str:
    text = (ROOT / "overlay" / "console_api.py").read_text(encoding="utf-8")
    return re.search(r'^CONSOLE_VERSION = "(\d+\.\d+\.\d+)"', text, re.M).group(1)


def as_tuple(version: str):
    return tuple(int(part) for part in version.split("."))


class VersionSyncTests(unittest.TestCase):
    def test_launcher_and_readme_follow_the_console_version(self):
        version = console_version()
        rc = (ROOT / "tools" / "launcher" / "launcher.rc").read_text(encoding="utf-8")
        start = rc.index('"CelesteAIConsole.exe"')
        console_block = rc[start:rc.index("#endif", start)]                 # 控制台启动器那一段（HUD 启动器另有版本）
        self.assertIn(f'#define APP_VERSION_TEXT "{version}"', console_block)
        self.assertIn(f"#define APP_VERSION {version.replace('.', ',')},0\n", console_block)
        manifest = (ROOT / "tools" / "launcher" / "launcher.manifest").read_text(encoding="utf-8")
        self.assertIn(f'name="CelesteAIPlay.Console" version="{version}.0"', manifest)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"`CelesteAIConsole.exe`，{version}）", readme)

    def test_release_summary_is_not_ahead_of_the_code(self):
        summary = (ROOT / "版本摘要.md").read_text(encoding="utf-8")
        latest = re.search(r"^## (\d+\.\d+\.\d+)(?:-[\w.]+)?（", summary, re.M).group(1)
        self.assertLessEqual(as_tuple(latest), as_tuple(console_version()))


if __name__ == "__main__":
    unittest.main()
