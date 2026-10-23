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

# 章节编号（菜单宏与 --chapters 用的地图编号）→ 章节 SID
CHAPTER_SIDS = {0: "Celeste/0-Intro", 1: "Celeste/1-ForsakenCity", 2: "Celeste/2-OldSite"}


def chapter_name(sid: str) -> str:
    return CHAPTER_NAMES.get(sid, sid or "?")


def chapter_name_by_id(chapter: int) -> str:
    return chapter_name(CHAPTER_SIDS.get(chapter, f"第 {chapter} 号章节"))


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


def find_docker_exe() -> str:
    """Docker Desktop 装好后，已打开的终端里 PATH 可能还没更新，所以再找几个默认安装位置。"""
    found = shutil.which("docker")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        candidates += [Path(local) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
                       Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("找不到 docker 可执行文件，请先安装 Docker Desktop（见使用说明 2.3 节）")


@dataclass
class RunConfig:
    agent: str                      # 智能体标签，如 opus5.5
    save: str                       # 存档名，如 test
    model: str                      # Claude Code 模型别名：opus / fable
    effort: str = "high"
    bridge_port: int = 47774
    mcp_port: int = 47780
    max_minutes: float = 240.0      # 现实时间上限（宽松；主要指标是游戏内计时）
    max_frames_per_act: int = 600   # 单次 act 最多推进的帧数
    play_speed: int = 1             # 智能体动作块的推进速度（1 = 实时 60 帧/秒）
    auto_speed: int = 1             # 转场、剧情、重生等自动推进段的速度（1 = 与原版一致；大于 1 只用于调试）
    skip_enter: bool = False        # 已在关卡内时跳过菜单宏
    chapter: int | None = None      # 菜单宏进入的章节编号；None = 选章页当前章节
    chapters: list[int] | None = None  # 依次通关的章节（如 [0, 1, 2]）：一章完成后主控自动进入下一章，智能体接着玩
    runtime: str = "container"      # container：智能体在 Docker 容器里运行；host：在本机运行且关闭全部内置工具（M2 的过渡方式）
    image: str = "celeste-agent:0.1"
    autocompact: str | None = None   # Claude Code 自动压缩的窗口：None 用默认，"auto" 或 100000~1000000 的 token 数
    seccomp_unconfined: bool = True  # 容器内 Bash 沙箱（bubblewrap）需要创建命名空间，Docker 默认的 seccomp 配置不允许
    study: bool = True               # 开始游玩前先读完知识库（只在容器运行方式下生效：本机方式没有 Read 工具）
    study_minutes: float = 60.0      # 准备阶段上限；到时自动开始计时并开放游戏工具
    max_usage: float = 0.9           # Max 套餐用量到这个比例：5 小时窗口暂停等重置（quota_wait），7 天窗口停止本次运行
    quota_wait: bool = True          # 5 小时窗口到上限时等额度重置后接着同一个会话；False 则直接停止
    quota_wait_cap_s: float | None = None  # 只用于测试：等待额度的最长秒数
    sim: bool = False                # 预置模拟器：把本机的 sim/dist 只读挂到容器的 /sim，每次停下都把当前帧的完整场景写进工作区

    @property
    def workspace_dir(self) -> Path:
        """智能体的工作区：按智能体标签区分，跨存档、跨运行保留（test 存档上准备的程序可以带进正式挑战）。"""
        return REPO_ROOT / "workspaces" / self.agent

    @property
    def sim_dir(self) -> Path:
        """预置模拟器的发布目录（只在本机，不入库；内容见 /sim/README.md）。"""
        return REPO_ROOT / "sim" / "dist"

    @property
    def budget_seconds(self) -> float:
        return self.max_minutes * 60.0
