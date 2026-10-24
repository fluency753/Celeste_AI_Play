# 部署说明：在一台新电脑上部署 Celeste_AI_Play

这份文档讲怎样把整套系统部署到一台 Windows 电脑上：需要什么环境、怎样一键部署、怎样逐步手动部署、要运行哪些命令、有哪些开关与配置文件。日常使用的细节（每个功能的原理与实测）见 [USAGE.md](USAGE.md)，设计见 [DESIGN.md](DESIGN.md)，原生 GPT 与直播见 [LIVE_ADAPTATION.md](LIVE_ADAPTATION.md)。

> 项目尚未完成，部署流程会随开发更新。本文最后更新：2026-10-24。

文中的 `<Celeste 安装目录>`、`<OBS 安装目录>` 等尖括号内容换成你电脑上的实际路径。存档名 `test`、`opus5.5`、`fable5.1`、`dsflashv4.1` 是本项目调试与正式挑战用的名字，你可以用自己的存档名。

## 目录

1. [系统由哪些部分组成](#1-系统由哪些部分组成)
2. [环境要求](#2-环境要求)
3. [一键部署（推荐）](#3-一键部署推荐)
4. [手动部署（逐步）](#4-手动部署逐步)
5. [运行](#5-运行)
6. [全部开关](#6-全部开关)
7. [配置文件](#7-配置文件)
8. [端口、网络与隔离](#8-端口网络与隔离)
9. [接入其他平台的模型（DeepSeek、原生 GPT 与自定义）](#9-接入其他平台的模型)
10. [更新、迁移与卸载](#10-更新迁移与卸载)
11. [常见问题](#11-常见问题)

## 1. 系统由哪些部分组成

| 部分 | 在哪里运行 | 从哪里来 | 作用 |
| --- | --- | --- | --- |
| 《蔚蓝》+ Everest | 本机 | Steam 购买；Everest 用 Olympus 安装 | 游戏本体与 Mod 加载器 |
| 桥接 Mod（CelesteAIBridge） | 游戏进程内 | 仓库 `game-bridge/`，本机编译安装 | 逐帧暂停与放行、注入按键、读取状态、截图、规划回放与 MOD 形式 HUD 的绘制、录制节拍与录像专用的游戏声音、新建存档 |
| 主控（orchestrator） | 本机 Python | 仓库 `orchestrator/` | 连接游戏、智能体、HUD 与 OBS；游戏工具服务（MCP）；执行、日志、回放、录制控制 |
| 智能体容器 | Docker Desktop | 仓库 `agent-container/` 构建镜像 | 断网沙箱里的 Claude Code（智能体），只能连模型平台与主控端口；原生 GPT 也在这里的零网络沙箱运行它生成的程序 |
| Codex CLI（可选） | 本机 | OpenAI 官方安装 | 原生 GPT 执行器（`--engine codex`、`start_gpt.ps1`）通过它的 app-server 调用模型，复用 Codex 已有的登录 |
| 知识库 | 只读挂进容器 `/kb` | 仓库 `kb/`；参考资料原文本机生成 | 物理常量、机制、技巧；探索模式只挂 `kb/explore` |
| 预置模拟器 | 只读挂进容器 `/sim` | **不在仓库里**，由游戏代码生成，只在开发者本机 | 逐帧精确的推演工具（速通模式开模拟器的版本用；探索模式的限额预测也用它） |
| HUD | 游戏画面内（MOD 形式）或本机网页 + 独立窗口 + OBS 浏览器源（网页形式） | 仓库 `overlay/`（MOD 形式由桥接绘制） | 右上角信息卡片；网页形式另有聊天与会话管理 |
| OBS + 锁步插件 | 本机 | OBS 官方安装；插件由仓库 `obs-plugin/` 编译 | 只录游玩帧（以及规划回放帧）的录制 |
| ffmpeg | 本机 | winget 安装 | 编码录像、合成成片、生成纯享版 |

## 2. 环境要求

### 2.1 硬件

| 项目 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| CPU | 4 核 | 8 核以上 | 预置模拟器按进程并行，核越多每秒推演越多（开发环境的容器能用 24 核） |
| 内存 | 16 GB | 32 GB | 游戏、OBS、Docker（WSL2）、智能体同时运行 |
| 显卡 | 能运行《蔚蓝》 | NVIDIA（支持 NVENC） | 有 NVIDIA 显卡时录制用 NVENC 硬件编码，否则用 libx264 软件编码（占 CPU） |
| 磁盘 | 20 GB 可用 | 100 GB 以上 | Docker 镜像约 2 GB；录像每分钟成片约 35 MB（1080p60，NVENC qp16；成片只有游玩帧与规划回放帧，比现实用时短得多。实测（DeepSeek V4.1 Flash，test 存档，30 分钟预算的短测）：成片约 6 分钟、约 200 MB），另有同样大小量级的纯享版 |
| 网络 | 能访问模型平台 | — | 部署时还要访问 winget 源、Docker Hub、GitHub（OBS 插件的 libobs 头文件）、celeste.ink（参考资料，可选） |

### 2.2 软件

| 软件 | 版本 | 用途 | 安装方式 | 必需？ |
| --- | --- | --- | --- | --- |
| Windows | 10 2004（build 19041）或更新、Windows 11 | — | — | 是 |
| 《蔚蓝》Celeste | Steam 版 1.4.0.0 | 游戏本体 | Steam | 是 |
| Everest | .NET 8 版本（桥接的 `everest.yaml` 要求 EverestCore 1.5935.0 或更新；开发中用的是 6642 以上的版本） | Mod 加载器 | [Olympus](https://everestapi.github.io/) 或 CeleMod | 是 |
| .NET SDK | 8.x | 编译桥接 Mod | `winget install Microsoft.DotNet.SDK.8` | 是（编译时） |
| Python | 3.11 或更新（开发中用 3.14） | 主控、工具、部署向导 | `winget install Python.Python.3.13` | 是 |
| WSL2 | 2.1.5 或更新 | Docker Desktop 的后端 | 管理员终端：`wsl --install --no-distribution`，重启 | 是 |
| Docker Desktop | 开发中用 29.9 | 智能体容器 | `winget install Docker.DockerDesktop` | 是（容器运行方式） |
| OBS Studio | 28 或更新（开发中用过 32 与 33） | 录制 | `winget install OBSProject.OBSStudio` 或 Steam | 录制时 |
| ffmpeg | 开发中用 9.0 | 录像编码、成片、纯享版 | `winget install Gyan.FFmpeg` | 录制时 |
| Claude Code（本机） | 任意新版 | 只用来生成订阅令牌（智能体用的是镜像里固定版本的 Claude Code） | `irm https://claude.ai/install.ps1 \| iex` | 用 Anthropic 平台时 |
| Codex CLI | 任意新版 | 原生 GPT 执行器 | 按 OpenAI 官方说明安装，然后 `codex login` | 用原生 GPT 时 |
| Git | 任意 | 获取与更新仓库 | `winget install Git.Git` | 建议 |
| 草莓酱 Mod（Strawberry Jam） | — | 生成知识库参考资料里的训练场与机制库原文 | Olympus | 可选 |
| NVIDIA 驱动 | 较新 | NVENC 编码 | — | 可选 |

### 2.3 账号与密钥

| 平台 | 需要 | 存放位置（都不入库） |
| --- | --- | --- |
| Anthropic（默认） | Claude Max（或 Pro）订阅；用 `claude setup-token` 生成的长期令牌（有效期一年） | 仓库根目录 `.env` 的 `CLAUDE_CODE_OAUTH_TOKEN` |
| DeepSeek | API 密钥 | 本机的密钥文件，由 `configs\local.providers.json` 的 `key_file` 指向；或环境变量 `DEEPSEEK_API_KEY` |
| 原生 GPT（Codex） | 能使用 Codex 的 ChatGPT / OpenAI 账号，在本机 `codex login` 登录 | 留在 Codex 自己的本机配置里；主控不读取、不复制，也不传进容器 |
| 其他兼容 Anthropic 接口的平台 | API 密钥 | 同 DeepSeek，用 `key_file` 或 `key_env` 指定的环境变量 |

令牌与密钥只在主控进程的内存里传给容器（`docker exec -e 变量名`，值不进命令行），智能体自写的程序在零网络的沙箱里运行并且看不到它们（见 8 节）。

## 3. 一键部署（推荐）

### 3.1 获取仓库

```powershell
git clone https://github.com/fluency753/Celeste_AI_Play.git
cd Celeste_AI_Play
```

没有 Git 时从 GitHub 页面下载 ZIP 并解压。**仓库路径只能用英文**（OBS 插件用 ANSI 接口写录像文件）。

### 3.2 运行部署脚本

在仓库根目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

（也可以在资源管理器里右键 `setup.ps1` →"使用 PowerShell 运行"。）

`setup.ps1` 先找 Python 3.11 以上（没有就问你要不要用 winget 装 Python 3.13），建虚拟环境 `.venv`、安装 `requirements.txt` 里的依赖，然后启动部署向导 `tools\setup_wizard.py`。向导一步一步来，每一步先检查，已经就绪的可以跳过；要安装或改动什么都会先问你。

| 步骤 | 向导做什么 | 需要你做什么 |
| --- | --- | --- |
| 1 system | 检查 Windows 版本、Python、winget、git | — |
| 2 game | 从 Steam 的库目录找《蔚蓝》，检查 Everest（`Celeste.dll`） | 确认或填写游戏目录；没装 Everest 时用 Olympus 装好、启动一次游戏 |
| 3 dotnet | 找 .NET 8 SDK，没有就用 winget 装 | 已装在别处时填 `dotnet.exe` 所在目录 |
| 4 bridge | 编译桥接 Mod 并装进游戏的 `Mods\CelesteAIBridge\` | 先关掉游戏 |
| 5 docker | 检查 Docker Desktop 与引擎，构建智能体镜像 `celeste-agent:0.1` | 没装时先装 WSL2（管理员、重启），再装 Docker Desktop 并启动到 Engine running |
| 6 platform | 选择智能体接入的平台（Anthropic、DeepSeek 或其他兼容 Anthropic 接口的平台），配置令牌或密钥，填模型名；模型不在平台配置里时，填它支持的推理强度档位 | Anthropic：浏览器授权 `claude setup-token`，把令牌粘贴回来（输入不显示）；DeepSeek 等：给密钥文件或粘贴密钥；其他平台：接口地址与密钥。原生 GPT 不在这一步配置，见 4.11 节 |
| 7 recording | 找 OBS 与 ffmpeg（没有就用 winget 装），编译安装锁步插件，写 `configs\local.recording.json`（有 NVIDIA 显卡时用 NVENC） | 选择要不要录制；编译插件前关掉 OBS |
| 8 sim | 检查预置模拟器 `sim\dist` | 有从开发机拷来的模拟器时给出目录；没有就用关模拟器的版本 |
| 9 kb | 生成知识库参考资料原文（Celeste Wiki 技巧页面；装了草莓酱 Mod 时一并提取） | 选择要不要生成（要能访问 celeste.ink） |
| 10 saves | 读出游戏里已有存档的名字 | 填调试存档与正式存档的名字；还没有的进游戏新建 |
| 11 switches | 生成启动脚本 `start_local.ps1` | 选择智能体标签、章节、时间上限、推理强度与各开关（模拟器版本、规划回放、录屏、准备阶段、规划轮次、HUD 窗口，见 6 节） |
| 12 checks | 自检 | 选择要不要跑：容器隔离验收、冒烟测试、模拟器自检、录制校准（后三项要先启动游戏） |

其他用法：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check                  # 只检查各项是否就绪，不改动任何东西
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Only platform,switches # 只重做其中几步（例如换平台、改开关）
.\.venv\Scripts\python.exe tools\setup_wizard.py --check                       # 虚拟环境建好以后，也可以直接运行向导
```

向导的回答保存在 `configs\local.deploy.json`（不入库），下次运行时作为默认值；中途按 Ctrl+C 也会保存已填的部分。它生成或修改的本机文件：

| 文件 | 内容 |
| --- | --- |
| `.env` | Anthropic 订阅令牌 `CLAUDE_CODE_OAUTH_TOKEN` |
| `%USERPROFILE%\.celeste-ai-play\<平台>.key` | 你粘贴的第三方密钥（只在选择"粘贴"时生成） |
| `configs\local.providers.json` | 第三方平台密钥文件的位置、自定义平台、新增模型及其推理强度档位 |
| `configs\local.recording.json` | OBS 目录、ffmpeg 路径、编码参数、声音偏移校准值 |
| `configs\local.deploy.json` | 向导的回答（不含密钥），其中的游戏目录也用来读取存档列表 |
| `start_local.ps1` | 启动脚本（见 5.1 节） |
| `<Celeste 安装目录>\Mods\CelesteAIBridge\` | 桥接 Mod |
| `<OBS 安装目录>\obs-plugins\64bit\celeste-lockstep.dll` | 锁步插件 |

部署完先用 `.\start_local.ps1 -Trial` 在调试存档上试跑一次（见 5 节）。

## 4. 手动部署（逐步）

不用向导时按下面的顺序做。命令都在仓库根目录的 PowerShell 里执行；`.\.venv\Scripts\python.exe` 可以先 `.\.venv\Scripts\Activate.ps1` 激活后简写成 `python`。

### 4.1 游戏与 Everest

1. **备份整个游戏目录**（以防万一）。
2. 用 [Olympus](https://everestapi.github.io/) 给游戏装最新稳定版 Everest（.NET 8 版本），装完启动一次游戏；游戏目录里出现 `Celeste.dll` 即为成功。
3. 在 Olympus 或 CeleMod 里建一个**只启用 CelesteAIBridge** 的 Mod 配置（例如叫 AIPlay），运行 AI 时用它；其他 Mod 可能改变手感、画面或抢占按键。平时自己玩时切回原来的配置。
4. 游戏设置：窗口化或无边框全屏，分辨率 1920×1080；辅助模式与变体关闭。
5. 在 Steam 里**关闭这个游戏的云同步**，避免存档被覆盖。

### 4.2 存档

主控只会进入 `--save` 指定的存档，存档名不一致时拒绝开始；开局把这个存档的全部文件备份到运行目录的 `save_backup\`。不要让 AI 碰你自己的存档。

- 一个调试用存档（本项目叫 `test`；菜单宏、校准、试跑都只用它）；
- 每个参加正式挑战的智能体一个新存档（本项目用 `opus5.5`、`fable5.1`、`dsflashv4.1`）。

可以在游戏里手动新建（新存档会先播开场，建好后退回标题即可），也可以让主控新建：`--create-save --save <名字> --chapter 0` 把新存档放在现有最新存档的下一个槽位（不填中间的空位，不会覆盖已有存档），名称允许重复，只能从序章开始。同名存档用 `--save-slot <槽位>` 区分。

### 4.3 Python 虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖：`mcp`（游戏工具服务）、`pillow`（截图放大、MOD 形式 HUD 的绘制）、`uvicorn`（本机 HTTP 服务）、`pywebview`（HUD 窗口）、`numpy`（规划回放、纯享版）。

### 4.4 .NET 8 SDK 与桥接 Mod

```powershell
winget install Microsoft.DotNet.SDK.8
# 关掉游戏后：
.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>"
```

Steam 版的默认位置是 `C:\Program Files (x86)\Steam\steamapps\common\Celeste`，装在其他 Steam 库里时以实际位置为准。脚本编译 `game-bridge\`，把 `everest.yaml` 与 `bin\CelesteAIBridge.dll` 复制到游戏的 `Mods\CelesteAIBridge\`。`dotnet` 不在 PATH 里时先执行 `$env:PATH = "<SDK 目录>;$env:PATH"`。启动游戏后 `log.txt` 里应有 `[CelesteAIBridge] loaded v<版本>`（当前版本 0.13.0）。主控开局前会检查桥接版本，低于 0.12.4 时拒绝开始；MOD 形式的 HUD 需要 0.13.0，录像的两条音轨需要 0.12.7 起。

冒烟测试（游戏停在标题画面即可）：

```powershell
.\.venv\Scripts\python.exe tools\enter_save.py test --leave-active      # 菜单宏进入 test 存档的关卡，停在可操作的第一帧
.\.venv\Scripts\python.exe tools\bridge_client.py smoke --out-dir smoke  # 冻结、单帧推进、轨迹与截图；最后交还控制
```

### 4.5 Docker Desktop 与智能体镜像

1. 管理员终端：`wsl --install --no-distribution`，重启电脑；之后 `wsl --version` 应为 2.1.5 或更新（否则 `wsl --update`）。
2. `winget install Docker.DockerDesktop`（或从 Docker 官网下载），安装时勾选 Use WSL 2；从开始菜单打开 Docker Desktop，接受协议，等左下角显示 Engine running。
3. 新开 PowerShell 验证：`docker version`、`docker run --rm hello-world`。
4. 构建镜像（约 1～2 分钟；镜像里有固定版本的 Claude Code、Python 3 与 numpy、scipy、numba、pillow、networkx、gcc/g++，没有游戏文件）：

   ```powershell
   docker build -t celeste-agent:0.1 agent-container
   ```

5. 隔离验收（Anthropic 平台、令牌配好之后）：`.\.venv\Scripts\python.exe -m orchestrator.verify --agent <标签>`，应全部"通过"（认证、防火墙、Bash 沙箱、重算程序沙箱、权限，见 USAGE 2.4 节）。

### 4.6 智能体平台

- **Anthropic（默认）**：本机装 Claude Code（`irm https://claude.ai/install.ps1 | iex`），执行 `claude setup-token`，浏览器里用订阅账号授权，把打印出来的整串令牌写进仓库根目录 `.env`：

  ```text
  CLAUDE_CODE_OAUTH_TOKEN=<令牌>
  ```

- **DeepSeek 等第三方**：见 9.1 节。
- **原生 GPT**：见 4.11 节与 9.2 节。

### 4.7 录制（OBS、ffmpeg、锁步插件）

```powershell
winget install OBSProject.OBSStudio     # 或 Steam 版
winget install Gyan.FFmpeg
# 关掉 OBS 后编译安装锁步插件（不需要 Visual Studio：脚本用 pip 装 zig 交叉编译，从 GitHub 取同版本的 libobs 头文件）
.\.venv\Scripts\python.exe tools\build_obs_plugin.py --obs-dir "<OBS 安装目录>"
```

OBS 安装版的默认目录是 `C:\Program Files\obs-studio`；`--obs-dir` 一定要写，脚本里的默认值不一定适合你的电脑。

新建 `configs\local.recording.json`（只写要改的键，默认值在 `configs\recording.json`）：

```json
{
  "obs_dir": "<OBS 安装目录>",
  "ffmpeg": "<ffmpeg.exe 的路径>",
  "video_args": "-c:v h264_nvenc -preset p5 -tune hq -rc constqp -qp 16 -g 120"
}
```

`ffmpeg` 可以省略：主控会依次找 PATH 与 winget 的默认链接位置（`%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`）。没有 NVIDIA 显卡时 `video_args` 用默认的 `-c:v libx264 -preset veryfast -crf 16 -g 120`。

录像的声音有两种来源（`audio_source`）：

- **`game_nrt`（默认）**：桥接另开 FMOD 混音，按游玩帧生成录像专用的游戏声音，不输出到声卡、不需要虚拟声卡，也不需要校准声音偏移；输出完整版 `audio.wav` 与纯享版 `audio_play.wav` 两条音轨。需要桥接 0.12.7 起与同时更新的 OBS 插件，版本不匹配时拒绝开始录制。它读取本机安装的官方音频 bank，针对 Windows 与 60 fps，不保证其他 Mod 的自定义 bank 可用。
- **`obs_mix`**：旧方式，从 OBS 混音里按段裁切，用 `audio_offset_ms`（声音相对画面的延迟）对齐；某段量不到延迟时用它作后备。这个值用下面的校准测试得到。

启动 OBS（自动建好专用的配置文件与场景集合 CelesteAI，不动你自己的配置）并校准：

```powershell
.\.venv\Scripts\python.exe tools\enter_save.py test --chapter 1 --leave-active
.\.venv\Scripts\python.exe tools\obs_setup.py launch
.\.venv\Scripts\python.exe calibration\m6_recording_test.py --rounds 18 --think 1.5
.\.venv\Scripts\python.exe tools\obs_setup.py stop
```

校准测试固定用 `obs_mix` 录音，应"全部通过"：帧数一致、帧号条码一致、声音恢复边在第一帧开头、暂停前最后一帧不被截断；最后一行给出建议的 `audio_offset_ms`（只在改用 `obs_mix` 时需要写进配置）。默认的 `game_nrt` 有自己的检查：`python -m unittest calibration.recording_audio_test`，以及录完后用 `python tools\check_recording_audio.py <recording 目录>\audio.wav` 离线检查断口。OBS 更新后要重新编译插件、重跑校准。

### 4.8 预置模拟器

预置模拟器由游戏代码生成，**不在仓库里，也不应公开分发**。在新电脑上有两种选择：

- 如果你有一台已经构建好模拟器的开发机，把 `sim\dist` 整个目录（`celeste-sim`、`sim.py`、`README.md`、`sim_assets.json`、`statemap.json`、`VERSION`）复制到新电脑仓库的 `sim\dist`；它是 Linux 单文件程序，只在容器里运行，与 Windows 版本无关。自检：游戏在 test 存档的关卡里时运行 `.\.venv\Scripts\python.exe tools\sim_check.py`，应"逐帧一致"。
- 没有模拟器时用**关模拟器的版本**：运行时加 `--no-sim`（提示词换成不教打法的版本，见 6 节）。探索模式的限额预测（`--sim-assist`）也需要它，没有时自动关闭。

### 4.9 知识库参考资料（可选）

```powershell
.\.venv\Scripts\python.exe tools\fetch_reference.py --celeste-dir "<Celeste 安装目录>"            # Wiki + 草莓酱
.\.venv\Scripts\python.exe tools\fetch_reference.py --celeste-dir "<Celeste 安装目录>" --skip-sj  # 没装草莓酱 Mod
.\.venv\Scripts\python.exe tools\import_reference_pdf.py 资料.pdf --name <英文名> --title "<标题>"   # 导入本机 PDF（可选）
```

`--celeste-dir` 一定要写（脚本里的默认值不一定适合你的电脑）。输出在 `kb\reference\`（不入库）。没有它时准备阶段只读知识库本身。

### 4.10 启动脚本（可选）

手动部署也可以只运行向导的最后几步生成启动脚本：`.\.venv\Scripts\python.exe tools\setup_wizard.py --only saves,switches`。

### 4.11 原生 GPT（可选）

1. 按 OpenAI 官方说明在本机安装 Codex CLI，执行 `codex login` 登录。主控按 `CODEX_APP_RUNTIME_CLI_PATH` 环境变量、PATH、`%LOCALAPPDATA%\OpenAI\Codex\bin\<版本>\codex.exe`（取最新的一个）的顺序找 `codex`。原生执行器启动 app-server 时关掉了宿主机上的 shell、浏览器、联网搜索等内置工具，模型只能用主控提供的游戏工具与沙箱工具。
2. Docker Desktop 与智能体镜像照常需要（4.5 节）：GPT 生成的程序在镜像的零网络沙箱里运行。
3. 用 `start_gpt.ps1` 准备与试玩（5.4 节），或在网页 HUD 的"会话管理"里选"Codex"执行器新建会话。

## 5. 运行

### 5.1 用启动脚本

```powershell
.\start_local.ps1                                   # 正式挑战（向导里选的正式存档、章节与开关）
.\start_local.ps1 -Trial                            # 在调试存档上试跑 30 分钟（标签 dev-<标签>，第一个章节）
.\start_local.ps1 -Resume runs\<标签>\<时间戳>       # 续跑一次中断的运行
```

开跑前：Docker Desktop 在运行；游戏开着（用只启用 CelesteAIBridge 的配置），停在标题画面或任意菜单即可（菜单宏会自己进存档）；录屏开着时主控会自己启动 OBS、结束时关掉它。

### 5.2 直接用命令

```powershell
# 正式挑战（Anthropic Opus，速通模式；预置模拟器、规划回放、OBS 录屏、游戏内 HUD 都是默认开启，OBS 由主控自己启动和关闭）
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --chapters 0,1,2 --max-minutes 600
# DeepSeek
.\.venv\Scripts\python.exe -m orchestrator.run --agent dsflashv4.1 --save dsflashv4.1 --provider deepseek --model deepseek-flash --chapters 0,1,2 --max-minutes 600
# 关模拟器的版本
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus-nosim --save <存档名> --model opus --chapters 0,1,2 --no-sim
# 不录屏的试跑
.\.venv\Scripts\python.exe -m orchestrator.run --agent dev-opus --save test --model opus --chapter 0 --max-minutes 30 --no-record
# 速通 + 直播（HUD 改为可交互的网页形式；直播时保留 OBS）
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --chapters 0,1,2 --live --keep-obs-open --continuous-music
```

主控依次：检查桥接版本；（录屏时）OBS 没开就启动它、退回选档页、开始录制；用菜单宏进入存档的章节并跳过开场；核对存档名、备份存档；启动游戏工具服务（`127.0.0.1:47780`）与 HUD 服务（`http://127.0.0.1:47790/hud`）；不用 MOD 形式 HUD 时打开 HUD 窗口；创建或复用容器 `celeste-agent-<标签>` 并启动智能体。智能体先读完知识库（准备阶段），之后游玩；一章通关后自动进入下一章。

### 5.3 查看与结束

- 进度：`.\.venv\Scripts\python.exe tools\run_status.py`（加 `-n 30` 显示更多决策）；HUD 在游戏画面右上角（MOD 形式），或在 HUD 窗口里，也可以浏览器打开 `http://127.0.0.1:47790/hud`。
- 提前结束：在运行主控的窗口按 Ctrl+C，主控会停止录制、写出 `summary.json`。Claude 执行器的运行没通关时游戏冻结在当前帧（章节计时不走），可以 `--resume` 续跑，不续跑时 `.\.venv\Scripts\python.exe tools\bridge_client.py deactivate` 交还控制；通关、退出关卡、原生 GPT 会话与 `--no-agent` 结束时照常交还控制。
- 产出：`runs\<标签>\<时间戳>\`（`metrics.json`、`decisions.jsonl`、`decision_timing.jsonl`、`frames.jsonl`、`agent_events.jsonl`、`system_prompt.md`、`summary.json`、`save_backup\`、`recording\`）。录像在 `recording\`：完整版 `gameplay.mkv`（含规划回放）与纯享版 `gameplay_pure.mkv`（只有真实游玩帧）。旧录像补生成纯享版：`.\.venv\Scripts\python.exe tools\make_pure.py runs\<标签>\<时间戳>\recording`。
- `decision_timing.jsonl` 用于诊断推进节奏：记录每轮请求的模型、推理强度、Fast 与服务档位、请求确认耗时、工具请求 / 完成，以及动作请求 / 完成。`decision_wait_s` 是本轮内两次动作之间累计的无工具等待，包含模型请求等待、推理、输出与压缩；`decision_active_s` 还包含中间的工具耗时。暂停与两轮之间的用户等待不计入这两项。请求档位不代表服务端确认的实际档位；不支持 Fast 的执行器记为 `null`。`decisions.jsonl` 同时保留该次动作的等待时间、请求档位、计划帧数与实际帧数，方便对照。

### 5.4 探索模式与原生 GPT

```powershell
.\start_gpt.ps1                                                                         # 准备：新会话读完入门资料（不连游戏、不录制）
.\start_gpt.ps1 -Play -Prepared runs\gpt-native\<准备目录> -Chapter 1 -Save <存档名> -SaveSlot <槽位>
.\start_gpt.ps1 -Play -Prepared runs\gpt-native\<准备目录> -Chapter 0 -Save <新存档名> -CreateSave
.\start_gpt.ps1 -Play -Resume runs\gpt-native\<运行目录>
```

- `start_gpt.ps1` 的参数：`-Knowledge explore|full`（默认 explore，即探索模式；full 为速通模式）、`-Effort`（默认 medium）、`-Model`、`-Chapter`（默认 1）或 `-Chapters`、`-Scope chapter|campaign`、`-Save` 与 `-SaveSlot`（用已有存档时必填）或 `-CreateSave`（新建，只能从序章 `-Chapter 0` 开始）、`-Foreground`。试玩自动带 `--live --keep-obs-open --playback --continuous-music`。
- 试玩默认由独立的后台宿主运行（`orchestrator.background`，用 Windows WMI 创建，不属于启动它的终端或聊天会话的进程树），所以关掉终端不会结束试玩；启动记录、输出日志与退出码在 `local\background\<启动时间>\`。同一时间只允许一个后台主控。原因见 [INCIDENT_20261024.md](INCIDENT_20261024.md)。
- 探索模式进房先核验精确几何数据；第一次明显受阻、死亡、偏离预期或决策等待偏长时就提示复查，不要求连续失败。直播时每次决策 10～20 秒的节奏是提示词约定，不是强制中断模型的超时。有限模拟由智能体主动决定，后台不会自动消耗体力。模拟次数和累计路线数在预测成功时记账，不依赖回放是否启用；HUD 的"累计推演"不会因回放结束清零。
- 网页 HUD 的会话管理、聊天、暂停与目标设置见 [LIVE_ADAPTATION.md](LIVE_ADAPTATION.md)。

## 6. 全部开关

### 6.1 主控 `python -m orchestrator.run`

续跑（`--resume`）时，标了"沿用"的项不写就沿用原运行的设置。

| 开关 | 默认 | 说明 |
| --- | --- | --- |
| `--agent <标签>` | 必填 | 智能体标签：容器 `celeste-agent-<标签>`、工作区 `workspaces\<标签>\`、日志 `runs\<标签>\`、会话数据卷 `celeste-claude-<标签>`（探索模式的容器、工作区与数据卷另加 `-explore` 后缀） |
| `--save <存档名>` | 必填 | 游戏内存档名；与当前存档不一致时拒绝开始 |
| `--save-slot <槽位>` | 无（沿用） | 已有存档的槽位编号；同名存档按槽位区分 |
| `--create-save` | 关 | 新建存档：放在现有最新存档的下一个槽位，名称允许重复；只能从序章开始，不能与 `--resume`、`--skip-enter`、`--save-slot` 合用 |
| `--engine claude` / `codex` | `claude`（沿用） | 执行器：Docker 里的 Claude Code，或宿主机 Codex app-server 的原生 GPT |
| `--mode speedrun` / `explore` | Claude 速通，原生 GPT 探索（沿用，续跑不能改） | 游玩模式；旧参数 `--knowledge full/explore` 等同 |
| `--provider` | `anthropic`（沿用） | 模型平台：`anthropic`（订阅令牌）、`deepseek` 或 `configs\local.providers.json` 里自定义的；原生 GPT 不用它 |
| `--model` | Claude：`opus`；原生 GPT：Codex 登录配置的默认模型（沿用） | Anthropic 用别名（`opus`、`fable`、`haiku`）；第三方用它自己的模型名（如 `deepseek-flash`）；原生 GPT 用 Codex 模型目录里的模型名 |
| `--effort` | Claude：`high`；原生 GPT：`medium`（沿用当时生效的） | 开局推理强度，可选档位由所选模型声明；智能体可以用 `set_effort` 自己调整 |
| `--chapters 0,1,2` | 无 | 依次通关的章节（0 序章、1 1A、2 2A……）；给了它就不用 `--chapter` |
| `--chapter N` | 选章页当前章节 | 只玩一章（或作为 `--scope campaign` 的起始章节） |
| `--scope chapter` / `campaign` | `chapter`（沿用） | 没给 `--chapters` 时：只玩一章，或从起始章节起按官方章节顺序一直玩下去 |
| `--max-minutes` | 240（沿用） | 现实时间安全上限（不含准备阶段与等待额度）；**不显示给智能体** |
| `--sim` / `--no-sim` | 速通模式且容器运行方式时开（沿用） | 预置模拟器开关，把速通模式分成两个版本：开时智能体可以随意调用模拟器寻找并优化路线（提示词写明做法）；关时不能调用模拟器，提示词只写目标、规则与工具接口，不教打法 |
| `--sim-assist` / `--no-sim-assist` | 探索模式且本机有预置模拟器时开（沿用） | 只用于探索模式：有额度的短路线预测，不开放模拟器文件 |
| `--live` / `--no-live` | 关（沿用） | 直播模式：提示词加直播一节，解说计时 10 秒，可按用户要求退出关卡，HUD 默认用网页形式。`narrate` 与思考提醒在所有模式下都有（不开直播时解说计时半分钟，`RunConfig.live_remind_s`） |
| `--hud-mod` / `--no-hud-mod` | 不开直播时开，直播模式关（沿用） | HUD 以 MOD 形式画在游戏画面右上角（需要桥接 0.13.0，旧桥接自动改用网页 HUD）；关时用网页 HUD |
| `--cutscene-mode auto` / `agent` | `auto`（沿用） | 剧情由主控跳过，或交给智能体用 cutscene 工具处理 |
| `--playback` / `--no-playback` | 开（沿用） | 规划回放：把推演过的路线按轮画进游戏（开模拟器时画 `/sim` 的推演，关模拟器时画智能体用 `show.trajectory()` 交来的轨迹） |
| `--record` / `--no-record` | 开（沿用） | OBS 录屏开关：开时 OBS 没在运行就由主控自己启动（专用配置、最小化到托盘），锁步录制完整版与纯享版，结束时关掉它启动的 OBS；OBS 已经开着就直接用、结束时不关；`--no-record` 不启动也不调用 OBS |
| `--keep-obs-open` | 关 | 结束时保留主控启动的 OBS（直播用）；手动关闭 OBS 后可用 `tools\obs_setup.py restore` 还原配置选择 |
| `--continuous-music` / `--freeze-music` | `configs\local.recording.json` 的 `continuous_music`，没写时冻结 | 冻结游戏逻辑时游戏本身的 BGM 继续播放（直播用）还是一起暂停；默认的 `game_nrt` 录像声音不受影响 |
| `--limit-planning` | 关 | 限制规划轮次（按章节记账，只用于开模拟器的版本）；`--rounds-per-room`（默认 3）、`--room-scale`（默认 2）调整轮次 |
| `--no-study` | 关 | 跳过准备阶段（不读知识库，调试用） |
| `--study-minutes` | 60 | 准备阶段上限，到时自动开放游戏工具 |
| `--prepared <准备目录>` | 无 | 用已准备好的会话新开一局（校验知识库没有变化）；不能与 `--resume`、`--no-study` 合用 |
| `--start-paused` | 关 | 原生 GPT 启动后保持暂停，可先聊天再点继续 |
| `--resume <运行目录>` | 无 | 续跑一次中断的运行（游戏须仍冻结在原处）；`--fresh-session` 开新会话 |
| `--max-usage 0~1` | 不设 | Max 订阅：用量到这个比例就提前暂停、等重置；默认用到真正用完再等 |
| `--no-quota-wait` / `--quota-wait-max-hours` | 关 / 24 | 额度用完时直接停止 / 最多等多少小时 |
| `--autocompact` | 平台默认 | 上下文自动压缩窗口：`auto` 或 100000～1000000 token |
| `--no-hud-window` / `--no-hud` | 关 / 关 | 不开 HUD 窗口 / 连 HUD 服务也不开 |
| `--hud-port` | 47790 | HUD 端口 |
| `--bridge-port` / `--mcp-port` | 47774 / 47780 | 桥接与游戏工具服务的端口 |
| `--runtime` | `container`（沿用） | `host`：在本机运行、只开放游戏工具（早期的过渡方式，没有沙箱、没有模拟器） |
| `--skip-enter` | 关 | 已在关卡内可操作时跳过菜单宏 |
| `--no-agent` | 关 | 只启动工具服务，不启动智能体（调试；可配合 `tools\game_tool.py` 手动调用游戏工具） |
| `--ffmpeg` / `--record-video-args` / `--audio-offset-ms` | 见配置 | 临时覆盖录制设置 |
| `--record-marker` | 关 | 画帧号条码（只用于校准） |
| `--play-speed` / `--auto-speed` | 1 / 1 | 快进（只用于调试，录像会失真） |

### 6.2 其他工具

| 命令 | 作用 | 主要参数 |
| --- | --- | --- |
| `setup.ps1` | 一键部署 | `-Check` 只检查；`-Only 步骤,...` |
| `tools\setup_wizard.py` | 部署向导 | `--check`；`--only` |
| `start_gpt.ps1` | 原生 GPT 的准备与试玩 | 见 5.4 节 |
| `tools\deploy_bridge.ps1` | 编译安装桥接 Mod | `-CelesteDir`（必填）、`-Configuration` |
| `tools\build_obs_plugin.py` | 编译安装锁步插件 | `--obs-dir`、`--obs-version`、`--no-install` |
| `tools\obs_setup.py` | OBS 配置与启动 | `setup`/`launch`/`stop`/`status`/`restore`；`--obs-dir`、`--show`、`--reset`、`--force` |
| `tools\enter_save.py <存档>` | 菜单宏进入存档的关卡 | `--chapter N`、`--leave-active` |
| `tools\bridge_client.py` | 桥接测试客户端 | `hello`、`activate`、`observe`、`step`、`advance`、`skip`、`cancel`、`smoke`、`deactivate` |
| `tools\game_tool.py` | 主控以 `--no-agent` 运行时，从命令行调用游戏工具 | `observe`/`act`/`narrate`/`exit_level`/`cutscene`；`--args`、`--args-file`、`--out` |
| `tools\run_status.py` | 查看运行进度 | `-n` |
| `tools\make_pure.py <recording 目录>` | 补生成纯享版 | `--ffmpeg`、`--video-args` |
| `tools\check_recording_audio.py <audio.wav>` | 离线检查录像声音的断口 | `--silence-threshold` |
| `tools\fetch_reference.py` | 生成知识库参考资料 | `--celeste-dir`、`--skip-wiki`、`--skip-sj`、`--index-only` |
| `tools\import_reference_pdf.py` | 导入 PDF 资料 | `--name`、`--title`、`--note` |
| `tools\sim_check.py` | 模拟器与游戏逐帧对照 | `--segments`、`--agent` |
| `python -m orchestrator.verify` | 容器隔离验收 | `--agent` |
| `python -m orchestrator.replay <运行目录>` | 不调用模型，按记录重放一次运行的决策 | `--save`、`--chapter` |
| `python -m orchestrator.preparation` | 只准备会话（读知识库、建会话，不连游戏） | `--engine`、`--provider`、`--agent`、`--knowledge`、`--model`、`--effort` |
| `python -m orchestrator.chat --run <运行目录>` | 运行结束后接着和同一个会话聊天（不连游戏） | — |
| `python -m orchestrator.background -- <主控参数>` | 用独立后台宿主启动主控 | — |
| `python -m overlay.server` / `python -m overlay.window` | 单独启动 HUD 服务（默认跟随 `runs\latest.txt` 指向的运行）/ HUD 窗口 | `--port`、`--run`；`--url` |
| `calibration\m6_recording_test.py` | 录制校准（`obs_mix`） | `--rounds`、`--think`、`--no-marker`、`--audio-offset-ms` |
| `calibration\chain_check.py` | 多章自动衔接自检 | — |
| `calibration\reset_test_save.py` | 把 test 存档重置成正式存档的初始进度 / 还原 | `--saves-dir`、`--like`、`--restore` |

## 7. 配置文件

| 文件 | 入库 | 内容 |
| --- | --- | --- |
| `configs\recording.json` | 是 | 录制默认值：`obs_dir`、`ffmpeg`、`video_args`、`audio_offset_ms`、`audio_source`（`game_nrt` 或 `obs_mix`）、OBS 配置文件与场景集合名、画布 1920×1080、60 fps、48 kHz、游戏窗口、HUD 浏览器源的地址、位置与大小 |
| `configs\local.recording.json` | 否 | 本机覆盖上面的键（还可以写 `continuous_music`） |
| `configs\providers.json` | 是 | 模型平台：`label`、`status`、`auth`、`base_url`、`allowed_domains`（防火墙放行的域名）、`allowed_host_ports`（放行的宿主机端口，例如本机的兼容服务）、`gateway_check`、`key_env`、`key_file`、`model_suffix`、`images`、`autocompact`、`env`（传给 Claude Code 的模型槽位等）、`models`（模型列表与各自支持的推理强度 `efforts`） |
| `configs\local.providers.json` | 否 | 本机覆盖与自定义平台；密钥文件位置写在这里 |
| `configs\hud.json` | 是 | HUD：智能体显示名 `names`、章节名、颜色、上下文窗口、`page_version`（变了时已打开的网页 HUD 自动重新加载）等 |
| `configs\local.hud.json` | 否 | 本机覆盖 HUD 配置 |
| `configs\local.hud-window.json`、`configs\local.hud-font.json` | 否 | HUD 窗口的位置、大小、置顶选择与字号（自动保存） |
| `.env` | 否 | `CLAUDE_CODE_OAUTH_TOKEN` |
| `configs\local.deploy.json` | 否 | 部署向导的回答 |
| `orchestrator\config.py`（`RunConfig`） | 是 | 不常改的默认值：规划回放的比例、倍速与每轮最长时长、思考语句提前量、镜像名 `celeste-agent:0.1` 等 |

## 8. 端口、网络与隔离

| 端口 | 谁监听 | 谁连 |
| --- | --- | --- |
| 127.0.0.1:47774 | 桥接 Mod（游戏进程） | 主控、测试客户端、菜单宏 |
| 127.0.0.1:47776 | 桥接（录制时钟） | OBS 锁步插件 |
| 47780 | 主控：游戏工具服务（MCP）与 `/gate` | 容器里的 Claude Code（经 `host.docker.internal`）、`tools\game_tool.py` |
| 127.0.0.1:47790 | 主控：HUD 页面与数据 | HUD 窗口、OBS 浏览器源、浏览器 |
| 127.0.0.1:47792 | `orchestrator.preparation` 默认启动的 HUD 服务 | 准备原生 GPT 会话时的 HUD |

另外，桥接与主控、OBS 插件之间还用本机共享内存交换 MOD 形式 HUD 的图片（`Local\CelesteAIHud-<桥接端口>`）与录像声音（`Local\CelesteAIRecordAudio-47776`）。原生 GPT 的 Codex app-server 由主控作为子进程启动，用标准输入输出通信，不占端口。

容器的出站防火墙（`agent-container\init-firewall.sh`，容器启动时生效）只放行：所用模型平台的域名（Anthropic 平台放行 Anthropic 官方域名；第三方平台只放行它自己的域名，不放行 Anthropic）、宿主机上主控的端口、平台配置里的宿主机端口；其余一律拒绝，DNS 也封掉。智能体用 Bash 运行的程序在零网络的沙箱（bubblewrap）里，连模型平台也连不上，环境里也没有令牌与密钥。Claude Code 以受限模式启动，文件工具只能访问工作区、知识库与模拟器目录；联网工具在托管配置里被拒绝；不允许后台任务。原生 GPT 的模型进程在宿主机上，它的文件工具只能读 `/workspace`、`/kb`、`/sim`、只能写 `/workspace`，生成的程序同样在零网络沙箱里运行。改了 `agent-container\` 之后要重新构建镜像，主控发现镜像或平台设置变了会自动重建容器（工作区与会话数据卷保留）。

## 9. 接入其他平台的模型

用 Claude 执行器时，容器里运行的始终是同一个 Claude Code，第三方平台通过兼容 Anthropic Messages 的接口接入；工具、提示词、知识库、沙箱、回放、HUD 与录制完全相同。GPT 则通过原生 Codex 执行器接入（9.2 节）。

### 9.1 DeepSeek（已实测）

1. 把 API 密钥存成一个文本文件（只有密钥一行），放在仓库目录之外，例如 `%USERPROFILE%\.celeste-ai-play\deepseek.key`（部署向导选"粘贴密钥"时也存在这里）。
2. `configs\local.providers.json`：

   ```json
   { "deepseek": { "key_file": "<密钥文件的完整路径>" } }
   ```

   或者设环境变量 `DEEPSEEK_API_KEY`。
3. 运行时加 `--provider deepseek --model deepseek-flash`（或 `deepseek-v4-pro`）。

### 9.2 原生 GPT

GPT 使用本机 Codex 已登录的原生执行器（`--engine codex`），不经过"Anthropic 接口 → OpenAI 接口"转换网关（旧的 `--provider openai` 预设已移除）。部署见 4.11 节；运行用 `start_gpt.ps1`（5.4 节），或在网页 HUD 里新建、准备并开始会话。详见 [LIVE_ADAPTATION.md](LIVE_ADAPTATION.md)。

### 9.3 其他兼容 Anthropic 接口的平台

在 `configs\local.providers.json` 里照 `deepseek` 加一项（部署向导的 platform 步骤选"其他平台"会帮你写）：

```json
{ "myprovider": {
    "label": "My Provider", "status": "未实测", "auth": "api_key",
    "base_url": "https://api.example.com/anthropic", "allowed_domains": ["api.example.com"],
    "key_env": "MYPROVIDER_API_KEY", "key_file": "<密钥文件的完整路径>", "model_suffix": "", "images": true,
    "env": { "ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}", "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model_base}", "CLAUDE_CODE_SUBAGENT_MODEL": "{model_base}",
             "API_TIMEOUT_MS": "600000" },
    "models": [ { "id": "<模型名>", "efforts": ["low", "medium", "high"] } ] } }
```

`models` 列出可用的模型与它们实际支持的推理强度档位（按平台文档填写）；没有列表时主控与 HUD 会报错，不会猜测档位。兼容服务运行在本机时，用 `allowed_host_ports` 放行它的端口（需要重新构建镜像）。`images: false` 用于只支持文字的模型（观测里另有字符地图与精确数据）。HUD 上的显示名写进 `configs\hud.json` 的 `names`。

## 10. 更新、迁移与卸载

**更新仓库**：`git pull`，然后视改动：

| 改动了 | 要做 |
| --- | --- |
| `game-bridge\`（桥接版本号变了） | 关游戏，`.\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>"` |
| `agent-container\` | `docker build -t celeste-agent:0.1 agent-container`（容器会自动重建） |
| `obs-plugin\` 或 OBS 更新了 | 关 OBS，`tools\build_obs_plugin.py --obs-dir "<OBS 安装目录>"`，重跑录制校准 |
| `overlay\hud.html` | 网页 HUD 会在 `configs\hud.json` 的 `page_version` 变化时自动重新加载；仍显示旧页面时，在 OBS 里刷新 HUD 浏览器源（或用 `tools\obs_setup.py stop` 关掉，下次运行时主控会重新启动它），并重开 HUD 窗口 |
| `requirements.txt` | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |

**迁移到另一台电脑**：按本文重新部署；要带走的本机内容有 `workspaces\`（智能体的程序与笔记）、`runs\`（运行记录与录像）、`sim\dist`（预置模拟器）、`kb\reference`（可以重新生成）、`configs\local*`（路径要按新电脑改）、`.env`（令牌可以重新生成）。

**卸载**：

```powershell
docker rm -f celeste-agent-<标签>; docker volume rm celeste-claude-<标签>; docker image rm celeste-agent:0.1
Remove-Item "<Celeste 安装目录>\Mods\CelesteAIBridge" -Recurse
Remove-Item "<OBS 安装目录>\obs-plugins\64bit\celeste-lockstep.dll"
.\.venv\Scripts\python.exe tools\obs_setup.py restore     # OBS 下次打开时选中你原来的配置文件与场景集合
```

## 11. 常见问题

| 现象 | 处理 |
| --- | --- |
| `setup.ps1` 提示无法运行脚本 | 用 `powershell -ExecutionPolicy Bypass -File .\setup.ps1` 运行 |
| PowerShell 里中文乱码 | 先执行 `chcp 65001`；日志文件本身是 UTF-8 |
| winget 不存在 | 从 Microsoft Store 安装"应用安装程序"，或按 2.2 节的链接手动安装各软件 |
| 找不到 docker | Docker Desktop 没启动，或装好后还没新开终端 |
| 主控提示找不到预置模拟器 | 拷来 `sim\dist`（4.8 节），或加 `--no-sim` 用关模拟器的版本 |
| 主控提示需要更新桥接 | 桥接低于 0.12.4；按 4.4 节重新编译安装（先关游戏） |
| `deploy_bridge.ps1` 提示文件被占用 | 游戏还开着，关掉再试 |
| 测试客户端连不上桥接 | 游戏没开，或 `log.txt` 里没有 `[CelesteAIBridge] loaded`（Mod 配置里没启用它） |
| `--record` 提示 OBS 插件没有连上桥接 | `tools\obs_setup.py status` 看插件是否加载；没加载就重新编译插件 |
| 录制提示独立录像音频需要更新 | 桥接与 OBS 插件版本不配套：两者都更新到当前仓库的版本，并重启游戏与 OBS |
| OBS 弹出"安全模式" | 用 `tools\obs_setup.py launch` 启动（会清掉崩溃标记）；已弹出时选正常模式 |
| 录像里没有游戏画面 | 录制时游戏窗口被最小化；主控会自动还原，失败时手动还原后重录 |
| 第三方平台提示找不到密钥 | 按 9 节写 `configs\local.providers.json` 的 `key_file`，或设环境变量 |
| 原生 GPT 提示找不到 Codex CLI | 按 4.11 节安装并 `codex login`；装在非标准位置时设 `CODEX_APP_RUNTIME_CLI_PATH` |
| 后台试玩提示主控已经运行 / 端口被占用 | 已有一个后台主控在运行（同一时间只允许一个）；看 `local\background\` 下最新目录的 `status.json` 与 `output.log` |
| 智能体想从零开始 | 删除 `workspaces\<标签>\`，再 `docker rm -f celeste-agent-<标签>`、`docker volume rm celeste-claude-<标签>` |
| 存档进度不对 | 从运行目录的 `save_backup\` 恢复（先关游戏），并确认已关闭云同步 |

更多问题见 [USAGE.md 第 6 节](USAGE.md#6-常见问题)。
