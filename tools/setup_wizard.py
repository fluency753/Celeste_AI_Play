"""一键部署向导：在一台新的 Windows 电脑上把整套系统装好、配好，最后生成启动脚本 start_local.ps1。

通常由仓库根目录的 setup.ps1 启动（它先装 Python、建虚拟环境、装依赖）。也可以直接运行：
    .\\.venv\\Scripts\\python.exe tools\\setup_wizard.py            # 交互式部署（每一步先检查，已就绪的可以跳过）
    .\\.venv\\Scripts\\python.exe tools\\setup_wizard.py --check    # 只检查各项是否就绪，不改动任何东西
    .\\.venv\\Scripts\\python.exe tools\\setup_wizard.py --only platform,switches   # 只做其中几步

步骤：system 系统与包管理器 → game 游戏与 Everest → dotnet .NET 8 SDK → bridge 桥接 Mod → docker Docker 与智能体镜像
→ platform 智能体平台与密钥 → recording 录制（OBS、ffmpeg、锁步插件）→ sim 预置模拟器 → kb 知识库参考资料
→ saves 存档 → switches 开关与启动脚本 → checks 自检。

回答记在 configs/local.deploy.json（不入库），下次运行时作为默认值。密钥与令牌不写进这个文件：
Claude 的令牌写进仓库根目录的 .env，第三方平台的密钥存成仓库目录之外的文件（configs/local.providers.json 只记文件位置）。
详见 docs/DEPLOY.md。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ANSWERS = REPO / "configs" / "local.deploy.json"
LOCAL_PROVIDERS = REPO / "configs" / "local.providers.json"
LOCAL_RECORDING = REPO / "configs" / "local.recording.json"
ENV_FILE = REPO / ".env"
LAUNCHER = REPO / "start_local.ps1"
SECRETS = Path.home() / ".celeste-ai-play"
IMAGE = "celeste-agent:0.1"
STEPS = ["system", "game", "dotnet", "bridge", "docker", "platform", "recording", "sim", "kb", "saves", "switches", "checks"]
TITLES = {"system": "系统与包管理器", "game": "游戏与 Everest", "dotnet": ".NET 8 SDK", "bridge": "桥接 Mod",
          "docker": "Docker Desktop 与智能体镜像", "platform": "智能体平台与密钥", "recording": "录制：OBS、ffmpeg 与锁步插件",
          "sim": "预置模拟器", "kb": "知识库参考资料", "saves": "存档", "switches": "开关与启动脚本", "checks": "自检"}

CHECK_ONLY = False


# ---------------------------------------------------------------- 基本工具

def say(text: str = "") -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    say(f"  [就绪] {text}")


def bad(text: str) -> None:
    say(f"  [缺少] {text}")


def note(text: str) -> None:
    say(f"  · {text}")


def ask(prompt: str, default: str | None = None) -> str:
    if CHECK_ONLY:
        return default or ""
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"  {prompt}{suffix}：").strip().strip('"')
        if value:
            return value
        if default is not None:
            return default
        say("  （不能为空）")


def ask_yes(prompt: str, default: bool = True) -> bool:
    if CHECK_ONLY:
        return False
    hint = "Y/n" if default else "y/N"
    while True:
        value = input(f"  {prompt} ({hint})：").strip().lower()
        if not value:
            return default
        if value in ("y", "yes", "是", "好", "1"):
            return True
        if value in ("n", "no", "否", "不", "0"):
            return False


def choose(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    """options：[(值, 说明)]。"""
    if CHECK_ONLY:
        return default
    for i, (value, label) in enumerate(options, 1):
        say(f"    {i}. {label}{'（默认）' if value == default else ''}")
    while True:
        value = input(f"  {prompt}（填序号）：").strip()
        if not value:
            return default
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1][0]


def run(cmd: list[str] | str, check: bool = False, capture: bool = False, env: dict | None = None,
        cwd: Path | None = None, shell: bool = False) -> subprocess.CompletedProcess:
    kwargs = dict(cwd=str(cwd or REPO), env=env, shell=shell)
    if capture:
        kwargs.update(capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        result = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(cmd, 127, "", str(exc))
    if check and result.returncode != 0:
        raise SystemExit(f"命令失败（{result.returncode}）：{cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    return result


def powershell(script: str, capture: bool = False) -> subprocess.CompletedProcess:
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], capture=capture)


def winget_install(package: str, label: str) -> bool:
    if not shutil.which("winget"):
        bad("没有 winget（应用安装程序）：请从 Microsoft Store 安装\"应用安装程序\"，或手动安装 " + label)
        return False
    if not ask_yes(f"用 winget 安装 {label}（{package}）？"):
        return False
    result = run(["winget", "install", "--id", package, "-e", "--accept-source-agreements", "--accept-package-agreements"])
    if result.returncode != 0:
        bad(f"winget 安装 {label} 没有成功（退出码 {result.returncode}），请手动安装后重新运行向导")
        return False
    note("安装完成。部分程序要新开一个终端才能在 PATH 里找到；向导会再到默认安装位置找一次")
    return True


def process_running(image: str) -> bool:
    out = run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture=True).stdout or ""
    return image.lower() in out.lower()


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_json(path: Path, updates: dict) -> None:
    """把 updates 合并写进本机配置（嵌套一层的 dict 按键合并）。"""
    data = load_json(path)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Answers(dict):
    def save(self) -> None:
        if CHECK_ONLY:
            return
        ANSWERS.parent.mkdir(parents=True, exist_ok=True)
        ANSWERS.write_text(json.dumps(self, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 各步

def step_system(a: Answers) -> None:
    say(f"  Windows：{sys.getwindowsversion().major}.{sys.getwindowsversion().minor} build {sys.getwindowsversion().build}"
        if os.name == "nt" else "  （不是 Windows：游戏、OBS 插件与桥接都只支持 Windows）")
    if os.name == "nt" and sys.getwindowsversion().build < 19041:
        bad("需要 Windows 10 2004（build 19041）或更新：OBS 的应用音频捕获与 WSL2 都需要")
    ok(f"Python {sys.version.split()[0]}（{sys.executable}）")
    (ok if shutil.which("winget") else bad)("winget（用来自动安装 .NET、Docker、OBS、ffmpeg）")
    (ok if shutil.which("git") else note)("git（更新仓库用，没有也能运行）" if shutil.which("git") else "没有 git：更新仓库时要重新下载压缩包")


def find_celeste_candidates() -> list[Path]:
    found: list[Path] = []
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg  # noqa: WPS433
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            roots.append(steam)
            vdf = steam / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                for match in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
                    roots.append(Path(match.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
        roots += [Path(r"C:\Program Files (x86)\Steam")]
    for root in roots:
        candidate = root / "steamapps" / "common" / "Celeste"
        if (candidate / "Celeste.exe").exists() and candidate not in found:
            found.append(candidate)
    return found


def step_game(a: Answers) -> None:
    current = a.get("celeste_dir")
    candidates = find_celeste_candidates()
    default = current or (str(candidates[0]) if candidates else None)
    if CHECK_ONLY:
        if not default:
            bad("找不到《蔚蓝》安装目录（Steam 版）")
            return
    else:
        if candidates:
            note("找到的《蔚蓝》安装目录：" + "；".join(map(str, candidates)))
        default = ask("《蔚蓝》的安装目录（含 Celeste.exe）", default)
    game = Path(default)
    if not (game / "Celeste.exe").exists():
        bad(f"{game} 里没有 Celeste.exe")
        return
    a["celeste_dir"] = str(game)
    ok(f"游戏目录 {game}")
    if (game / "Celeste.dll").exists():
        ok("Everest（.NET 8 版本）已安装")
    else:
        bad("没有安装 Everest：用 Olympus（https://everestapi.github.io/）给这个目录装上最新稳定版 Everest，"
            "装完至少启动一次游戏，再重新运行向导")
        if not CHECK_ONLY and ask_yes("现在打开 Everest 的下载页？"):
            os.startfile("https://everestapi.github.io/")  # noqa: S606
    note("游戏设置：窗口化或无边框，1920×1080；辅助模式与变体关闭；在 Steam 里关闭这个游戏的云同步")
    note("建议在 Olympus / CeleMod 里建一个只启用 CelesteAIBridge 的 Mod 配置，其他 Mod 会改变手感或抢占按键")


def find_dotnet(a: Answers) -> str | None:
    custom = a.get("dotnet_dir")
    candidates = [shutil.which("dotnet")]
    if custom:
        candidates.insert(0, str(Path(custom) / "dotnet.exe"))
    if os.environ.get("DOTNET_ROOT"):
        candidates.append(str(Path(os.environ["DOTNET_ROOT"]) / "dotnet.exe"))
    candidates.append(str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "dotnet" / "dotnet.exe"))
    candidates.append(str(Path.home() / ".dotnet" / "dotnet.exe"))
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "dotnet" / "dotnet.exe"))
    for exe in candidates:
        if exe and Path(exe).exists():
            sdks = run([exe, "--list-sdks"], capture=True).stdout or ""
            if re.search(r"^8\.", sdks, re.M):
                return exe
    return None


def step_dotnet(a: Answers) -> None:
    exe = find_dotnet(a)
    if exe:
        ok(f".NET 8 SDK（{exe}）")
        a["dotnet_dir"] = str(Path(exe).parent)
        return
    bad(".NET 8 SDK（编译桥接 Mod 用）")
    if CHECK_ONLY:
        say("  装在别处（不在 PATH 里）时运行 setup.ps1 -Only dotnet，填 dotnet.exe 所在目录")
        return
    if winget_install("Microsoft.DotNet.SDK.8", ".NET 8 SDK") and (exe := find_dotnet(a)):
        ok(f".NET 8 SDK（{exe}）")
        a["dotnet_dir"] = str(Path(exe).parent)
        return
    custom = ask("已经装在别处的话，填 dotnet.exe 所在目录（留空跳过）", "")
    if custom:
        a["dotnet_dir"] = custom
        if find_dotnet(a):
            ok(".NET 8 SDK")


def dotnet_env(a: Answers) -> dict:
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1")
    if a.get("dotnet_dir"):
        env["PATH"] = a["dotnet_dir"] + os.pathsep + env.get("PATH", "")
    return env


def step_bridge(a: Answers) -> None:
    game = Path(a.get("celeste_dir") or "")
    installed = game / "Mods" / "CelesteAIBridge" / "bin" / "CelesteAIBridge.dll"
    version = re.search(r'Version = "([\d.]+)"', (REPO / "game-bridge" / "Source" / "Bridge.cs").read_text(encoding="utf-8"))
    want = version.group(1) if version else "?"
    if installed.exists():
        ok(f"桥接 Mod 已安装（仓库版本 {want}；更新仓库后要重新部署）")
    else:
        bad("桥接 Mod（CelesteAIBridge）还没装进游戏")
    if CHECK_ONLY or not game.exists():
        return
    if not ask_yes("现在编译并安装桥接 Mod？", default=not installed.exists()):
        return
    if not find_dotnet(a):
        bad("先装好 .NET 8 SDK（dotnet 步骤）")
        return
    if process_running("Celeste.exe"):
        say("  游戏正在运行，要先关掉（DLL 被占用时无法覆盖）。")
        if not ask_yes("关掉游戏后继续？"):
            return
    result = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "tools" / "deploy_bridge.ps1"),
                  "-CelesteDir", str(game)], env=dotnet_env(a))
    (ok if result.returncode == 0 else bad)("桥接 Mod 编译安装" + ("完成；启动游戏后 log.txt 里应有 [CelesteAIBridge] loaded"
                                                                 if result.returncode == 0 else "失败，见上面的输出"))


def docker_exe() -> str | None:
    try:
        from orchestrator.config import find_docker_exe  # noqa: WPS433
        return find_docker_exe()
    except FileNotFoundError:
        return None


def step_docker(a: Answers) -> None:
    exe = docker_exe()
    if not exe:
        bad("Docker Desktop（智能体在容器里运行）")
        if CHECK_ONLY:
            return
        say("  Docker Desktop 需要 WSL2：在\"终端（管理员）\"里执行 wsl --install --no-distribution，然后重启电脑。")
        if winget_install("Docker.DockerDesktop", "Docker Desktop"):
            say("  装好后从开始菜单打开 Docker Desktop，接受协议，等左下角显示 Engine running，再重新运行向导。")
        return
    server = run([exe, "version", "--format", "{{.Server.Version}}"], capture=True)
    if server.returncode != 0:
        bad("Docker Desktop 已安装但引擎没有运行：从开始菜单打开 Docker Desktop，等 Engine running 后重新运行向导")
        return
    ok(f"Docker 引擎 {server.stdout.strip()}")
    image = run([exe, "image", "inspect", "-f", "{{.Created}}", IMAGE], capture=True)
    if image.returncode == 0:
        ok(f"智能体镜像 {IMAGE}（构建于 {image.stdout.strip()[:19]}）")
    else:
        bad(f"智能体镜像 {IMAGE}")
    if CHECK_ONLY:
        return
    if ask_yes(f"现在构建（或重新构建）智能体镜像 {IMAGE}？约 1～2 分钟", default=image.returncode != 0):
        result = run([exe, "build", "-t", IMAGE, "agent-container"])
        (ok if result.returncode == 0 else bad)("镜像构建" + ("完成" if result.returncode == 0 else "失败"))


def read_env_token() -> str:
    try:
        from orchestrator.config import load_dotenv  # noqa: WPS433
        return load_dotenv(ENV_FILE).get("CLAUDE_CODE_OAUTH_TOKEN", "")
    except Exception:  # noqa: BLE001
        return ""


def write_env_token(token: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.startswith("CLAUDE_CODE_OAUTH_TOKEN=")]
    lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_secret(name: str, value: str) -> Path:
    SECRETS.mkdir(parents=True, exist_ok=True)
    path = SECRETS / f"{name}.key"
    path.write_text(value.strip() + "\n", encoding="utf-8")
    return path


def providers() -> dict:
    data = load_json(REPO / "configs" / "providers.json")
    local = load_json(LOCAL_PROVIDERS)
    for key, value in local.items():
        if isinstance(value, dict):
            data[key] = {**data.get(key, {}), **value}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def step_platform(a: Answers) -> None:
    known = providers()
    current = a.get("provider", "anthropic")
    if CHECK_ONLY:
        info = known.get(current, {})
        say(f"  平台：{info.get('label', current)}")
        if current == "anthropic":
            (ok if read_env_token() else bad)("Claude Code 令牌（.env 的 CLAUDE_CODE_OAUTH_TOKEN）")
        else:
            key_file = info.get("key_file") or ""
            env_name = info.get("key_env") or ""
            have = (key_file and Path(os.path.expandvars(key_file)).is_file()) or (env_name and os.environ.get(env_name))
            (ok if have else bad)(f"{current} 的密钥（configs/local.providers.json 的 key_file，或环境变量 {env_name}）")
        return
    options = [(name, f"{info.get('label', name)}" + (f"——{info['status']}" if info.get("status") not in (None, "ready") else ""))
               for name, info in known.items()]
    options.append(("custom", "其他平台（提供 Anthropic 兼容接口）"))
    provider = choose("智能体接入哪个平台", options, current if current in known else "anthropic")
    if provider == "custom":
        provider = setup_custom_provider()
        known = providers()
    a["provider"] = provider
    info = known.get(provider, {})
    if provider == "anthropic":
        setup_anthropic(a)
    elif provider == "openai":
        setup_openai(a, info)
    else:
        setup_api_key(provider, info)
    defaults = {"anthropic": "opus", "deepseek": "deepseek-flash"}
    previous = a.get("model") if a.get("provider_prev") == provider else None
    a["model"] = ask("模型名（Anthropic 用别名 opus、fable、haiku；第三方用它自己的模型名；GPT 填网关 config.yaml 里的 model_name）",
                     previous or defaults.get(provider))
    a["provider_prev"] = provider


def setup_anthropic(a: Answers) -> None:
    say("  Anthropic：容器里的 Claude Code 用 Max（或 Pro）订阅的长期令牌登录，不需要 API 密钥。")
    if read_env_token():
        ok("已有令牌（.env）")
        if not ask_yes("重新生成并替换？", default=False):
            return
    try:
        from orchestrator.config import find_claude_exe  # noqa: WPS433
        claude = find_claude_exe()
    except FileNotFoundError:
        claude = None
    if not claude:
        bad("本机没有 Claude Code（只用来生成令牌；智能体用的是镜像里固定版本的 Claude Code）")
        if ask_yes("用官方脚本安装 Claude Code？（irm https://claude.ai/install.ps1 | iex）"):
            powershell("irm https://claude.ai/install.ps1 | iex")
            candidate = Path.home() / ".local" / "bin" / "claude.exe"
            claude = str(candidate) if candidate.exists() else shutil.which("claude")
    if claude and ask_yes("现在运行 claude setup-token？浏览器会打开授权页，用订阅账号登录并授权"):
        run([claude, "setup-token"])
    token = getpass.getpass("  把终端里打印的整串令牌粘贴到这里（输入不显示），回车：").strip()
    if token:
        write_env_token(token)
        ok("令牌已写进 .env（.gitignore 已排除，不会提交）")


def setup_api_key(provider: str, info: dict) -> None:
    env_name = info.get("key_env") or ""
    key_file = info.get("key_file") or ""
    if key_file and Path(os.path.expandvars(key_file)).is_file():
        ok(f"已配置密钥文件 {key_file}")
        if not ask_yes("换一个？", default=False):
            return
    say(f"  {info.get('label', provider)} 的 API 密钥只放在本机：可以给一个已有的密钥文件（只有密钥一行），"
        f"或者直接粘贴，向导把它存到 {SECRETS}（仓库目录之外）。也可以改设环境变量 {env_name}。")
    path = ask("密钥文件路径（直接回车 = 粘贴密钥）", "")
    if not path:
        value = getpass.getpass("  粘贴 API 密钥（输入不显示），回车：").strip()
        if not value:
            note("没有填密钥，跳过")
            return
        path = str(save_secret(provider, value))
    elif not Path(path).is_file():
        bad(f"找不到文件 {path}")
        return
    merge_json(LOCAL_PROVIDERS, {provider: {"key_file": path}})
    ok(f"密钥文件 {path} 已写进 configs/local.providers.json（不入库）")


def setup_openai(a: Answers, info: dict) -> None:
    say("  GPT：OpenAI 的接口不是 Anthropic 格式，容器里的 Claude Code 不能直接调用。需要在本机运行一个把 Anthropic "
        "接口转成 OpenAI 接口的网关（例如 LiteLLM 代理，见 docs/DEPLOY.md 的 GPT 一节），网关持有 OpenAI 的密钥，"
        "容器只连宿主机上网关的端口。这条路径目前只有接口，尚未实测。")
    port = ask("网关在本机监听的端口", str((info.get("allowed_host_ports") or [4000])[0]))
    merge_json(LOCAL_PROVIDERS, {"openai": {
        "base_url": f"http://host.docker.internal:{port}", "allowed_host_ports": [int(port)],
        "gateway_check": f"http://127.0.0.1:{port}/health/liveliness"}})
    setup_api_key("openai", {**info, "label": "网关的访问密钥（LiteLLM 的 master key）"})
    note("智能体镜像要用 2026-10-24 之后的版本构建（防火墙才放行宿主机上的网关端口）：在 docker 步骤里重新构建镜像")


def setup_custom_provider() -> str:
    say("  其他平台：需要它提供兼容 Anthropic Messages 的接口（例如 https://api.example.com/anthropic）。")
    name = ask("给这个平台起个名字（英文小写，用作 --provider）", "myprovider")
    base = ask("接口地址 base_url")
    domain = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    entry = {"label": name, "status": "未实测", "auth": "api_key", "base_url": base, "allowed_domains": [domain],
             "key_env": f"{name.upper()}_API_KEY", "key_file": "", "model_suffix": "", "images": True,
             "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}", "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model_base}", "CLAUDE_CODE_SUBAGENT_MODEL": "{model_base}",
                     "API_TIMEOUT_MS": "600000"}}
    merge_json(LOCAL_PROVIDERS, {name: entry})
    ok(f"已把 {name} 写进 configs/local.providers.json（防火墙只放行 {domain}）")
    return name


def find_obs_dir(a: Answers) -> Path | None:
    candidates = [a.get("obs_dir"), load_json(LOCAL_RECORDING).get("obs_dir")]
    for game_root in find_celeste_candidates():
        candidates.append(str(game_root.parent / "OBS Studio"))
    candidates += [r"C:\Program Files\obs-studio", r"C:\Program Files (x86)\Steam\steamapps\common\OBS Studio"]
    for c in candidates:
        if c and (Path(c) / "bin" / "64bit" / "obs64.exe").exists():
            return Path(c)
    return None


def step_recording(a: Answers) -> None:
    obs = find_obs_dir(a)
    (ok if obs else bad)(f"OBS Studio（{obs}）" if obs else "OBS Studio（录制用；不录制可以不装）")
    try:
        from orchestrator.recording import find_ffmpeg  # noqa: WPS433
        ffmpeg = find_ffmpeg()
    except Exception:  # noqa: BLE001
        ffmpeg = None
    (ok if ffmpeg else bad)(f"ffmpeg（{ffmpeg}）" if ffmpeg else "ffmpeg（录制用）")
    plugin = obs / "obs-plugins" / "64bit" / "celeste-lockstep.dll" if obs else None
    (ok if plugin and plugin.exists() else bad)("OBS 锁步插件 celeste-lockstep")
    if CHECK_ONLY:
        return
    a["record"] = ask_yes("要用 OBS 录屏吗（不录屏时运行加 --no-record，不需要装 OBS）？", default=a.get("record", True))
    if not a["record"]:
        return
    if not obs:
        if winget_install("OBSProject.OBSStudio", "OBS Studio"):
            obs = find_obs_dir(a)
        if not obs:
            path = ask("OBS 的安装目录（含 bin\\64bit\\obs64.exe；留空跳过）", "")
            obs = Path(path) if path and (Path(path) / "bin" / "64bit" / "obs64.exe").exists() else None
    if not ffmpeg and winget_install("Gyan.FFmpeg", "ffmpeg"):
        try:
            ffmpeg = find_ffmpeg()
        except Exception:  # noqa: BLE001
            ffmpeg = None
    if not obs or not ffmpeg:
        bad("OBS 或 ffmpeg 没装好，录制先跳过")
        return
    a["obs_dir"] = str(obs)
    nvenc = bool(shutil.which("nvidia-smi"))
    video_args = ("-c:v h264_nvenc -preset p5 -tune hq -rc constqp -qp 16 -g 120" if nvenc
                  else "-c:v libx264 -preset veryfast -crf 16 -g 120")
    merge_json(LOCAL_RECORDING, {"obs_dir": str(obs), "ffmpeg": ffmpeg,
                                 "video_args": load_json(LOCAL_RECORDING).get("video_args") or video_args})
    ok(f"configs/local.recording.json 已写好（编码：{'NVIDIA NVENC' if nvenc else 'libx264'}）")
    if ask_yes("编译并安装 OBS 锁步插件？（先关闭 OBS；会用 pip 装 zig 交叉编译，并从 GitHub 取 libobs 头文件）",
               default=not (plugin and plugin.exists())):
        if process_running("obs64.exe"):
            run([sys.executable, "tools/obs_setup.py", "stop"])
        result = run([sys.executable, "tools/build_obs_plugin.py", "--obs-dir", str(obs)])
        (ok if result.returncode == 0 else bad)("锁步插件" + ("已安装" if result.returncode == 0 else "构建失败，见上面的输出"))
    note("声音延迟的校准（audio_offset_ms）在 checks 步骤里做：要游戏开着、停在 test 存档的关卡里")


def step_sim(a: Answers) -> None:
    dist = REPO / "sim" / "dist"
    if (dist / "celeste-sim").exists() and (dist / "sim.py").exists():
        ok(f"预置模拟器（{dist}）")
        a["sim_available"] = True
        return
    bad("预置模拟器（sim\\dist）：它由游戏代码生成，只在开发者本机，不随仓库分发")
    a["sim_available"] = False
    if CHECK_ONLY:
        return
    say("  没有它时只能用关模拟器的版本（--no-sim）。如果你从开发机拷来了 sim\\dist 整个目录（celeste-sim、sim.py、"
        "README.md、sim_assets.json、statemap.json），在这里给出它的位置，向导复制进来。")
    src = ask("拷来的 sim\\dist 目录（留空跳过）", "")
    if src and (Path(src) / "celeste-sim").exists():
        dist.parent.mkdir(exist_ok=True)
        shutil.copytree(src, dist, dirs_exist_ok=True)
        ok("预置模拟器已复制")
        a["sim_available"] = True


def step_kb(a: Answers) -> None:
    ref = REPO / "kb" / "reference"
    if (ref / "README.md").exists():
        ok("知识库参考资料原文（kb\\reference）")
        return
    bad("知识库参考资料原文（kb\\reference；没有时准备阶段只读知识库本身）")
    if CHECK_ONLY or not a.get("celeste_dir"):
        return
    if not ask_yes("现在生成？要能访问 celeste.ink；装了草莓酱 Mod（StrawberryJam2021.zip）时一并提取它的训练场与机制库"):
        return
    cmd = [sys.executable, "tools/fetch_reference.py", "--celeste-dir", a["celeste_dir"]]
    if not (Path(a["celeste_dir"]) / "Mods" / "StrawberryJam2021.zip").exists():
        cmd.append("--skip-sj")
        note("没有草莓酱 Mod，只取 Celeste Wiki 的技巧页面")
    result = run(cmd)
    (ok if result.returncode == 0 else bad)("参考资料" + ("已生成" if result.returncode == 0 else "生成失败"))


def save_names(game: Path) -> dict[int, str]:
    """读游戏存档里的名字（只读）。"""
    names = {}
    for f in (game / "Saves").glob("*.celeste"):
        if not f.stem.isdigit():
            continue
        match = re.search(r"<Name>(.*?)</Name>", f.read_text(encoding="utf-8", errors="ignore"))
        if match:
            names[int(f.stem)] = match.group(1)
    return dict(sorted(names.items()))


def step_saves(a: Answers) -> None:
    game = Path(a.get("celeste_dir") or "")
    names = save_names(game) if game.exists() else {}
    if names:
        say("  游戏里的存档：" + "，".join(f"槽位 {k}：{v}" for k, v in names.items()))
    else:
        bad("还没有存档（或者找不到游戏目录）")
    if CHECK_ONLY:
        return
    say("  调试用一个 test 存档，正式挑战每个智能体用一个新存档（在游戏里手动新建，名字自取，例如 opus5.5）。"
        "主控只会进入你指定的存档，不会碰别的存档。")
    a["test_save"] = ask("调试用存档的名字", a.get("test_save", "test"))
    a["save"] = ask("正式挑战用的存档名字", a.get("save", "opus5.5"))
    for name in (a["test_save"], a["save"]):
        if names and name not in names.values():
            bad(f"游戏里还没有叫 {name} 的存档：进游戏新建一个（新存档会先播开场，建好后退回标题即可）")


def step_switches(a: Answers) -> None:
    if CHECK_ONLY:
        say(f"  启动脚本：{LAUNCHER if LAUNCHER.exists() else '（还没生成）'}")
        return
    sim_ok = bool(a.get("sim_available"))
    a["agent"] = ask("智能体标签（决定容器、工作区与日志目录；同一模型准备与正式用同一个标签）",
                     a.get("agent") or (a.get("model") or "agent").replace("/", "-"))
    a["chapters"] = ask("依次通关的章节（逗号分隔：0 序章、1 1A、2 2A……）", a.get("chapters", "0,1,2"))
    a["max_minutes"] = ask("现实时间上限（分钟；只是安全上限，不显示给智能体）", str(a.get("max_minutes", 600)))
    a["effort"] = choose("开局的推理强度", [(x, x) for x in ("low", "medium", "high", "xhigh", "max")], a.get("effort", "high"))
    if sim_ok:
        a["sim"] = ask_yes("开模拟器的版本（智能体可以随意调用预置模拟器）？否 = 关模拟器的版本", a.get("sim", True))
    else:
        a["sim"] = False
        note("没有预置模拟器：用关模拟器的版本（--no-sim）")
    a["live"] = ask_yes("实时展示（智能体思考期间随时更新 HUD 上的思考，运行程序时报告进度）？", a.get("live", True))
    a["playback"] = ask_yes("规划回放（把推演过的路线画进游戏给观众看）？", a.get("playback", True))
    a["record"] = ask_yes("OBS 录屏（主控自己启动 OBS 锁步录制，生成完整版与纯享版；结束时关掉它）？", a.get("record", True))
    a["study"] = ask_yes("开局先读完知识库（准备阶段）？", a.get("study", True))
    a["limit_planning"] = sim_ok and a["sim"] and ask_yes("限制规划轮次（一般不开）？", a.get("limit_planning", False))
    a["hud_window"] = ask_yes("在屏幕上打开置顶 HUD 窗口？", a.get("hud_window", True))
    write_launcher(a)


def launcher_args(a: Answers, save: str, chapters: str, minutes: str) -> list[str]:
    args = ["--agent", a["agent"], "--save", save, "--provider", a.get("provider", "anthropic"),
            "--model", a.get("model", "opus"), "--effort", a.get("effort", "high"), "--chapters", chapters,
            "--max-minutes", str(minutes)]
    args += [] if a.get("sim") else ["--no-sim"]
    args += [] if a.get("live", True) else ["--no-live"]
    args += [] if a.get("playback", True) else ["--no-playback"]
    args += [] if a.get("record", True) else ["--no-record"]
    args += [] if a.get("study", True) else ["--no-study"]
    args += ["--limit-planning"] if a.get("limit_planning") else []
    args += [] if a.get("hud_window", True) else ["--no-hud-window"]
    return args


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def write_launcher(a: Answers) -> None:
    formal = launcher_args(a, a.get("save", "opus5.5"), a.get("chapters", "0,1,2"), a.get("max_minutes", 600))
    # 试跑用单独的标签（dev-<标签>）：正式挑战的工作区保持干净；想把试跑写的程序带进正式挑战，就把两个标签改成一样
    trial = launcher_args({**a, "agent": "dev-" + a["agent"]}, a.get("test_save", "test"), a.get("chapters", "0,1,2").split(",")[0], 30)
    text = f"""\ufeff# 由 tools/setup_wizard.py 生成（本机文件，不入库）。重新运行向导会覆盖它；也可以直接改下面的参数。
# 用法：.\\start_local.ps1          正式挑战（存档 {a.get('save')}）
#       .\\start_local.ps1 -Trial   在调试存档 {a.get('test_save')} 上试跑 30 分钟
#       .\\start_local.ps1 -Resume runs\\<标签>\\<时间戳>   续跑一次中断的运行
param([switch]$Trial, [string]$Resume = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"
$env:PYTHONIOENCODING = "utf-8"
$formal = @({', '.join(ps_quote(x) for x in formal)})
$trial = @({', '.join(ps_quote(x) for x in trial)})
$args2 = if ($Trial) {{ $trial }} else {{ $formal }}
if ($Resume) {{ $args2 = @('--agent', {ps_quote(a['agent'])}, '--save', {ps_quote(a.get('save', ''))}, '--resume', $Resume) }}
# OBS 录屏由主控自己处理（开着时启动 OBS、结束时关掉）；续跑沿用原运行的开关
& $py -X utf8 -m orchestrator.run @args2
"""
    LAUNCHER.write_text(text, encoding="utf-8")
    ok(f"启动脚本已生成：{LAUNCHER}")
    say("    正式挑战：.\\start_local.ps1     试跑：.\\start_local.ps1 -Trial     续跑：.\\start_local.ps1 -Resume <运行目录>")


def step_checks(a: Answers) -> None:
    py = sys.executable
    bridge_up = run([py, "tools/bridge_client.py", "hello"], capture=True).returncode == 0
    (ok if bridge_up else note)("游戏开着、桥接已加载" if bridge_up else "游戏没有开（或桥接没加载）：下面几项自检要先启动游戏")
    if CHECK_ONLY:
        return
    if a.get("provider", "anthropic") == "anthropic" and ask_yes("运行容器隔离验收（orchestrator.verify，约 2 分钟）？"):
        run([py, "-m", "orchestrator.verify", "--agent", a.get("agent", "opus")])
    if not bridge_up:
        return
    test = a.get("test_save", "test")
    if ask_yes(f"冒烟测试：进入存档 {test} 的关卡，测试冻结、单帧推进与截图？"):
        run([py, "tools/enter_save.py", test, "--leave-active"])
        run([py, "tools/bridge_client.py", "smoke", "--out-dir", "smoke"])
    if a.get("sim_available") and ask_yes("预置模拟器自检（在关卡里推进几段，与模拟器的预测逐帧对照）？"):
        run([py, "tools/enter_save.py", test, "--chapter", "1", "--leave-active"])
        run([py, "tools/sim_check.py"])
    if a.get("record") and ask_yes("录制校准（启动 OBS，在 1A 录 18 段动作，检查帧数与声音对齐，给出 audio_offset_ms）？"):
        run([py, "tools/enter_save.py", test, "--chapter", "1", "--leave-active"])
        run([py, "tools/obs_setup.py", "launch"])
        result = run([py, "calibration/m6_recording_test.py", "--rounds", "18", "--think", "1.5"], capture=True)
        say(result.stdout[-3000:] if result.stdout else "")
        match = re.search(r"audio_offset_ms 设为 (\d+)", result.stdout or "")
        if match:
            merge_json(LOCAL_RECORDING, {"audio_offset_ms": int(match.group(1))})
            ok(f"audio_offset_ms = {match.group(1)} 已写进 configs/local.recording.json")
        run([py, "tools/obs_setup.py", "stop"])
        run([py, "tools/bridge_client.py", "deactivate"])


# ---------------------------------------------------------------- 主程序

def main() -> int:
    global CHECK_ONLY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只检查各项是否就绪，不改动任何东西")
    ap.add_argument("--only", help="只做这几步（逗号分隔）：" + ",".join(STEPS))
    args = ap.parse_args()
    CHECK_ONLY = args.check
    if os.name == "nt":
        os.system("")   # 打开终端的 ANSI 支持
    a = Answers(load_json(ANSWERS))
    steps = [s.strip() for s in args.only.split(",")] if args.only else STEPS
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise SystemExit(f"未知的步骤：{unknown}；可选 {STEPS}")
    say("Celeste_AI_Play 部署向导" + ("（只检查，不改动）" if CHECK_ONLY else "") + f"　仓库：{REPO}")
    for i, step in enumerate(steps, 1):
        say(f"\n== {i}/{len(steps)} {TITLES[step]} ==")
        try:
            globals()[f"step_{step}"](a)
        except KeyboardInterrupt:
            say("\n已中断；已经填好的回答保存在 configs/local.deploy.json，下次接着用")
            a.save()
            return 1
        a.save()
    say("\n完成。" + ("" if CHECK_ONLY else "之后用 .\\start_local.ps1 启动；详见 docs/DEPLOY.md。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
