"""运行配置与本机路径。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CHAPTER_NAMES = {
    "Celeste/0-Intro": "序章",
    "Celeste/1-ForsakenCity": "1 被遗弃之城",
    "Celeste/2-OldSite": "2 旧址",
    "Celeste/3-CelestialResort": "3 天空度假村",
    "Celeste/4-GoldenRidge": "4 黄金山脊",
    "Celeste/5-MirrorTemple": "5 镜之神殿",
    "Celeste/6-Reflection": "6 映像",
    "Celeste/7-Summit": "7 顶峰",
    "Celeste/8-Epilogue": "终章",
    "Celeste/9-Core": "8 核心",
    "Celeste/LostLevels": "9 告别",
}

# 章节编号（菜单宏与 --chapters 用的地图编号）→ 章节 SID
CHAPTER_SIDS = {i:'Celeste/'+name for i,name in enumerate([
    '0-Intro','1-ForsakenCity','2-OldSite','3-CelestialResort','4-GoldenRidge',
    '5-MirrorTemple','6-Reflection','7-Summit','8-Epilogue','9-Core','LostLevels'])}


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


PROVIDERS = REPO_ROOT / "configs" / "providers.json"
LOCAL_PROVIDERS = REPO_ROOT / "configs" / "local.providers.json"   # 本机覆盖（不入库）：密钥文件的位置等


def load_provider(name: str) -> dict:
    """模型提供方的设置（configs/providers.json，本机的 configs/local.providers.json 覆盖同名项）。"""
    import json  # noqa: WPS433

    merged: dict = {}
    for path in (PROVIDERS, LOCAL_PROVIDERS):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data.get(name), dict):
            merged.update(data[name])
    if not merged:
        raise SystemExit(f"未知的模型提供方 {name!r}：见 configs/providers.json")
    merged["name"] = name
    return merged


def load_api_key(provider: dict) -> str:
    """第三方提供方的 API 密钥：先看环境变量 key_env，再读本机 key_file（只含密钥，或 KEY=VALUE 形式的 .env）。
    密钥只在内存里传给容器，不写进任何文件、日志或命令行。"""
    env_name = provider.get("key_env") or ""
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    key_file = provider.get("key_file") or ""
    if key_file:
        path = Path(os.path.expandvars(os.path.expanduser(key_file)))
        if not path.is_file():
            raise SystemExit(f"找不到 {provider['name']} 的密钥文件（configs/local.providers.json 的 key_file）")
        text = path.read_text(encoding="utf-8-sig").strip()
        if "=" in text:
            values = load_dotenv(path)
            text = values.get(env_name) or next(iter(values.values()), "")
        if text:
            return text.strip()
    raise SystemExit(f"缺少 {provider['name']} 的 API 密钥：在 configs/local.providers.json 里把 "
                     f'{{"{provider["name"]}": {{"key_file": "密钥文件的路径"}}}} 写好，或设置环境变量 {env_name}')


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
    model: str                      # Claude Code 模型别名：opus / fable；第三方提供方写它的模型名
    provider: str = "anthropic"     # 模型提供方（configs/providers.json）：anthropic 用 Max 订阅，其他用本机保存的 API 密钥
    provider_info: dict | None = None  # load_provider(provider) 的结果
    engine: str = "claude"          # claude：原有执行器；codex：原生 app-server + 客户端沙箱工具
    effort: str = "high"
    bridge_port: int = 47774
    mcp_port: int = 47780
    max_minutes: float = 240.0      # 现实时间上限（宽松；主要指标是游戏内计时）
    max_frames_per_act: int = 600   # 单次 act 最多推进的帧数
    play_speed: int = 1             # 智能体动作块的推进速度（1 = 实时 60 帧/秒）
    auto_speed: int = 1             # 转场、剧情、重生等自动推进段的速度（1 = 与原版一致；大于 1 只用于调试）
    skip_enter: bool = False        # 已在关卡内时跳过菜单宏
    create_save: bool = False       # 显式新建空存档；只用于首次进档，不用于续跑或换章
    save_slot: int | None = None    # 存档身份由槽位决定，名称可以重复
    cutscene_mode: str = 'auto'     # auto 自动跳过；agent 自行决定观看、推进或跳过
    play_scope: str = 'chapter'     # independent of the user's dynamic stop goal
    chapter: int | None = None      # 菜单宏进入的章节编号；None = 选章页当前章节
    chapters: list[int] | None = None  # 依次通关的章节（如 [0, 1, 2]）：一章完成后主控自动进入下一章，智能体接着玩
    runtime: str = "container"      # container：智能体在 Docker 容器里运行；host：在本机运行且关闭全部内置工具
    image: str = "celeste-agent:0.1"
    autocompact: str | None = None   # Claude Code 自动压缩的窗口：None 用默认，"auto" 或 100000~1000000 的 token 数
    seccomp_unconfined: bool = True  # 容器内 Bash 沙箱（bubblewrap）需要创建命名空间，Docker 默认的 seccomp 配置不允许
    study: bool = True               # 开始游玩前先读完知识库（只在容器运行方式下生效：本机方式没有 Read 工具）
    study_minutes: float = 60.0      # 准备阶段上限；到时自动开始计时并开放游戏工具
    max_usage: float | None = None   # 可选：5 小时或 7 天窗口的用量到这个比例就提前暂停；None = 用到额度真正用完
    quota_wait: bool = True          # 额度用完（或到 max_usage）时等重置后接着同一个会话；False 则直接停止
    quota_wait_max_h: float = 24.0   # 重置还要等更久时直接停止
    quota_wait_cap_s: float | None = None  # 只用于测试：等待额度的最长秒数
    pause_limit: bool = False        # 限制规划轮次（--limit-planning；按章节记账，见 pausegate.py，自动重试关闭）；默认不限制思考与调用
    rounds_per_room: int = 3         # 一屏大小（320×180，1A 第一个房间）的新房间存入的规划轮次；大房间按 room_scale 放大
    room_scale: float = 2.0          # 房间的长、高每变成这么多倍，存入的轮次乘这么多（2 = 与面积成正比），向上取整
    death_bonus: int = 1             # 在游戏里死亡一次补的规划轮次
    playback: bool = True            # 执行前把这次规划里模拟器推演过的路线画进游戏（规划回放，见 playback.py）
    playback_ratio: int = 100        # 每多少条推演画一条路线（线条数随推演条数等比增长）
    playback_min_tracks: int = 40    # 每一轮至少画几条（推演少于这个数时全画）
    playback_speed: float = 3.0      # 回放倍速：每个画面帧推进几帧游戏时间（所有轮次相同）
    playback_recolor: float = 8.0    # 线走到头定下结果后（死亡变红、金色变金）从终点往起点变色的传导倍速
    playback_stagger_s: float = 0.8  # 一轮里各条路线按推演先后陆续出场的时间跨度（秒）
    playback_round_s: float = 1.5    # 一轮回放的最长时长（以 1A 第一间、一屏 320×180 的房间为准；房间面积是它的几倍就乘几倍）；
                                     # 按 playback_speed 画会超过时，这一轮整体按比例加速，在这个时长内画完
    thought_lead_s: float = 0.4      # 每段动作推进之前等 HUD 换上新的思考语句（游戏冻结着，录像里不占时间）
    sim: bool = False                # 预置模拟器：把本机的 sim/dist 只读挂到容器的 /sim，每次停下都把当前帧的完整场景写进工作区
    sim_assist: bool = False         # 新手模式的限额模拟：后台持有模拟器，真实试玩赚取体力
    knowledge_profile: str = "full"  # explore：精简入门资料和独立工作区；full：原有速通资料（由 mode 决定，见 modes.py）
    mode: str = "speedrun"           # 速通模式 speedrun / 探索模式 explore（互斥）；直播模式是 live，可以和任一个叠加
    record: bool = True              # OBS 录屏：主控自己启动 OBS（没在运行时）并锁步录制，结束时关掉它启动的 OBS
    continuous_music: bool = False   # 直播：冻结逻辑时继续播放音乐，仅暂停音效
    live: bool = False               # 直播模式（--live）：网页 HUD、解说计时 10 秒、可按用户要求退出关卡；narrate 在所有模式都有
    hud_mod: bool = False            # HUD 以 MOD 形式画在游戏画面里（overlay/modhud.py；不开直播时默认开）
    live_remind_s: float = 30.0      # 解说计时：思考累计这么多秒没有公开解说，在下一次工具调用时提醒（由可调参数 remind_s / remind_live_s 决定）
    # 以下是可调参数（orchestrator/tuning.py，命令行 --tune 名称=值）；默认值就是一直以来的数值
    thought_max_chars: int = 120     # act 的 thought 与 narrate 的长度上限
    remind_s: float = 30.0           # 解说提醒间隔（不开直播）
    remind_live_s: float = 10.0      # 解说提醒间隔（直播）
    bash_default_s: int = 300        # Claude Code 的 Bash 不写 timeout 时的上限（速通模式）
    bash_max_s: int = 600            # Bash 写了 timeout 时最多能到多久（速通模式）
    retry_max: int = 5               # 计划文件 retry.max 的上限
    retry_timeout_max_s: float = 60.0  # 计划文件 retry.timeout_s 的上限
    explore_program_s: int | None = 15   # 探索模式：单次规划程序（Bash）最长秒数；None = 不限时
    explore_program_runs: int | None = None  # 探索模式：两次 act 之间最多运行几次规划程序；None = 不限
    assist_capacity: int = 100       # 探索模式的限额预测：体力上限
    assist_initial: int = 40         # 初始体力
    assist_regen_frames: int = 60    # 真实操作每多少个移动帧恢复 1 点
    assist_regen_per_min: int = 20   # 每分钟最多恢复几点
    assist_room_bonus: int = 15      # 首次进入新房间奖励（起始房间不奖励）
    assist_cost_base: int = 8        # 每次预测的花费 = 基础 + 向上取整(总帧数 / assist_cost_frames) + 每条额外候选 × assist_cost_extra
    assist_cost_frames: int = 12
    assist_cost_extra: int = 4
    assist_max_candidates: int = 6   # 每次最多几条候选
    assist_max_frames: int = 180     # 每条最多预测几帧
    assist_max_total: int = 600      # 每次合计最多预测几帧
    tuning: dict = field(default_factory=dict)   # 与默认值不同的可调参数（记进 run_state，续跑沿用）
    kb_deny: list = field(default_factory=list)  # 不给智能体看的知识库文件（知识库内的相对路径；控制台的"知识库"页）

    @property
    def third_party(self) -> bool:
        """非 Anthropic 的提供方：用 API 密钥，经它兼容 Anthropic 的接口运行同一个 Claude Code。"""
        return self.provider != "anthropic"

    @property
    def provider_domains(self) -> list[str]:
        return list((self.provider_info or {}).get("allowed_domains") or [])

    @property
    def provider_host_ports(self) -> list[int]:
        """第三方提供方要连的宿主机端口（例如本机的接口转换网关）；防火墙除了主控端口另外放行这些。"""
        return [int(p) for p in (self.provider_info or {}).get("allowed_host_ports") or []]

    @property
    def images(self) -> bool:
        """观测里附不附截图（只支持文字的模型关掉，观测里另有字符地图与精确数据）。"""
        return bool((self.provider_info or {}).get("images", True))

    @property
    def workspace_dir(self) -> Path:
        """智能体的工作区：按智能体标签区分，跨存档、跨运行保留（test 存档上准备的程序可以带进正式挑战）。"""
        tag = self.agent + ("-explore" if self.knowledge_profile == "explore" else "")
        return REPO_ROOT / "workspaces" / tag

    @property
    def kb_source(self) -> Path:
        """这个模式的知识库：速通模式是整个 kb/，探索模式只有 kb/explore/。"""
        return REPO_ROOT / "kb" / "explore" if self.knowledge_profile == "explore" else REPO_ROOT / "kb"

    @property
    def kb_dir(self) -> Path:
        """挂进容器 /kb 的目录：全部可读时就是知识库本身；有不给看的文件（kb_deny）时是只含允许文件的副本
        （orchestrator/kb_access.prepare_view 同步）。"""
        if self.kb_deny:
            return REPO_ROOT / "local" / "kb_views" / self.workspace_dir.name
        return self.kb_source

    @property
    def sim_dir(self) -> Path:
        """预置模拟器的发布目录（只在本机，不入库；内容见 /sim/README.md）。"""
        return REPO_ROOT / "sim" / "dist"

    @property
    def budget_seconds(self) -> float:
        return self.max_minutes * 60.0
