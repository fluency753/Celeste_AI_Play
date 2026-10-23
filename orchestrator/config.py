"""运行配置与本机路径。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CHAPTER_NAMES = {
    "Celeste/0-Intro": "序章",
    "Celeste/1-ForsakenCity": "1 被遗弃之城",
    "Celeste/2-OldSite": "2 旧址",
}


def load_dotenv(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 .env；忽略注释与空行，兼容带 BOM 的 UTF-8。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def find_claude_exe() -> str:
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("找不到 claude 可执行文件，请先安装 Claude Code（见使用说明 2.1 节）")


@dataclass
class RunConfig:
    agent: str                      # 智能体标签，如 opus5.5
    save: str                       # 存档名，如 test
    model: str                      # Claude Code 模型别名：opus / fable
    effort: str = "high"
    bridge_port: int = 47774
    mcp_port: int = 47780
    max_minutes: float = 120.0      # 现实时间预算
    max_frames_per_act: int = 600   # 单次 act 最多推进的帧数
    play_speed: int = 1             # 智能体动作块的推进速度（1 = 实时 60 帧/秒）
    auto_speed: int = 1             # 转场、剧情、重生等自动推进段的速度（1 = 与原版一致；大于 1 只用于调试）
    skip_enter: bool = False        # 已在关卡内时跳过菜单宏

    @property
    def budget_seconds(self) -> float:
        return self.max_minutes * 60.0
