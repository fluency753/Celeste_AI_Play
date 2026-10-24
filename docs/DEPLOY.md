# 部署说明：在一台新电脑上部署 Celeste_AI_Play

这份文档讲怎样把整套系统部署到另一台 Windows 电脑上：需要什么环境、怎样一键部署、怎样逐步手动部署、要运行哪些命令、有哪些开关与配置文件。日常使用的细节（每个功能的原理与实测）见 [USAGE.md](USAGE.md)，设计见 [DESIGN.md](DESIGN.md)。

> 项目尚未完成，部署流程会随开发更新。本文最后更新：2026-10-24。

## 目录

1. [系统由哪些部分组成](#1-系统由哪些部分组成)
2. [环境要求](#2-环境要求)
3. [一键部署（推荐）](#3-一键部署推荐)
4. [手动部署（逐步）](#4-手动部署逐步)
5. [运行](#5-运行)
6. [全部开关](#6-全部开关)
7. [配置文件](#7-配置文件)
8. [端口、网络与隔离](#8-端口网络与隔离)
9. [接入其他平台的模型（DeepSeek、GPT 与自定义）](#9-接入其他平台的模型)
10. [更新、迁移与卸载](#10-更新迁移与卸载)
11. [常见问题](#11-常见问题)

## 1. 系统由哪些部分组成

| 部分 | 在哪里运行 | 从哪里来 | 作用 |
| --- | --- | --- | --- |
| 《蔚蓝》+ Everest | 本机 | Steam 购买；Everest 用 Olympus 安装 | 游戏本体与 Mod 加载器 |
| 桥接 Mod（CelesteAIBridge） | 游戏进程内 | 仓库 `game-bridge/`，本机编译安装 | 逐帧暂停与放行、注入按键、读取状态、截图、规划回放的绘制、录制节拍 |
| 主控（orchestrator） | 本机 Python | 仓库 `orchestrator/` | 连接游戏、智能体、HUD 与 OBS；游戏工具服务（MCP）；执行、日志、回放、录制控制 |
| 智能体容器 | Docker Desktop | 仓库 `agent-container/` 构建镜像 | 断网沙箱里的 Claude Code（智能体），只能连模型平台与主控端口 |
| 知识库 | 只读挂进容器 `/kb` | 仓库 `kb/`；参考资料原文本机生成 | 物理常量、机制、技巧 |
| 预置模拟器 | 只读挂进容器 `/sim` | **不在仓库里**，由游戏代码生成，只在开发者本机 | 逐帧精确的推演工具（开模拟器的版本用） |
| HUD | 本机网页 + 置顶窗口 + OBS 浏览器源 | 仓库 `overlay/` | 右上角信息卡片 |
| OBS + 锁步插件 | 本机 | OBS 官方安装；插件由仓库 `obs-plugin/` 编译 | 只录游玩帧（以及规划回放帧）的录制 |
| ffmpeg | 本机 | winget 安装 | 编码录像、合成成片、生成纯享版 |

## 2. 环境要求

### 2.1 硬件

| 项目 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| CPU | 4 核 | 8 核以上 | 预置模拟器按进程并行，核越多每秒推演越多（作者本机容器能用 24 核） |
| 内存 | 16 GB | 32 GB | 游戏、OBS、Docker（WSL2）、智能体同时运行 |
| 显卡 | 能运行《蔚蓝》 | NVIDIA（支持 NVENC） | 有 NVIDIA 显卡时录制用 NVENC 硬件编码，否则用 libx264 软件编码（占 CPU） |
| 磁盘 | 20 GB 可用 | 100 GB 以上 | Docker 镜像约 2 GB；录像每分钟成片约 35 MB（1080p60，NVENC qp16；成片只有游玩帧与规划回放帧，比现实用时短得多，例如 DeepSeek 一次 30 分钟预算的短测，成片约 6 分钟、约 200 MB），另有同样大小量级的纯享版 |
| 网络 | 能访问模型平台 | — | 部署时还要访问 winget 源、Docker Hub、GitHub（OBS 插件的 libobs 头文件）、celeste.ink（参考资料，可选） |

### 2.2 软件

| 软件 | 版本 | 用途 | 安装方式 | 必需？ |
| --- | --- | --- | --- | --- |
| Windows | 10 2004（build 19041）或更新、Windows 11 | — | — | 是 |
| 《蔚蓝》Celeste | Steam 版 1.4.0.0 | 游戏本体 | Steam | 是 |
| Everest | .NET 8 版本（作者本机 6642 以上） | Mod 加载器 | [Olympus](https://everestapi.github.io/) 或 CeleMod | 是 |
| .NET SDK | 8.x | 编译桥接 Mod | `winget install Microsoft.DotNet.SDK.8` | 是（编译时） |
| Python | 3.11 或更新（作者本机 3.14） | 主控、工具、部署向导 | `winget install Python.Python.3.13` | 是 |
| WSL2 | 2.1.5 或更新 | Docker Desktop 的后端 | 管理员终端：`wsl --install --no-distribution`，重启 | 是 |
| Docker Desktop | 作者本机 29.9 | 智能体容器 | `winget install Docker.DockerDesktop` | 是（容器运行方式） |
| OBS Studio | 28 或更新（作者本机 32） | 录制 | `winget install OBSProject.OBSStudio` 或 Steam | 录制时 |
| ffmpeg | 作者本机 9.0 | 录像编码、成片、纯享版 | `winget install Gyan.FFmpeg` | 录制时 |
| Claude Code（本机） | 任意新版 | 只用来生成订阅令牌（智能体用的是镜像里固定版本的 Claude Code） | `irm https://claude.ai/install.ps1 \| iex` | 用 Anthropic 平台时 |
| Git | 任意 | 获取与更新仓库 | `winget install Git.Git` | 建议 |
| 草莓酱 Mod（Strawberry Jam） | — | 生成知识库参考资料里的训练场与机制库原文 | Olympus | 可选 |
| NVIDIA 驱动 | 较新 | NVENC 编码 | — | 可选 |

### 2.3 账号与密钥

| 平台 | 需要 | 存放位置（都不入库） |
| --- | --- | --- |
| Anthropic（默认） | Claude Max（或 Pro）订阅；用 `claude setup-token` 生成的长期令牌（有效期一年） | 仓库根目录 `.env` 的 `CLAUDE_CODE_OAUTH_TOKEN` |
| DeepSeek | API 密钥 | 本机的密钥文件，由 `configs\local.providers.json` 的 `key_file` 指向；或环境变量 `DEEPSEEK_API_KEY` |
| GPT（OpenAI） | OpenAI API 密钥（给本机网关）+ 网关的访问密钥 | OpenAI 密钥只给网关；网关访问密钥同上，用 `key_file` 或 `OPENAI_GATEWAY_KEY` |
| 其他兼容 Anthropic 接口的平台 | API 密钥 | 同上 |

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
| 6 platform | 选择智能体接入的平台，配置令牌或密钥，填模型名 | Anthropic：浏览器授权 `claude setup-token`，把令牌粘贴回来（输入不显示）；DeepSeek 等：给密钥文件或粘贴密钥；GPT：网关端口与访问密钥（见 9.2 节）；其他平台：接口地址与密钥 |
| 7 recording | 找 OBS 与 ffmpeg（没有就用 winget 装），编译安装锁步插件，写 `configs\local.recording.json`（有 NVIDIA 显卡时用 NVENC） | 选择要不要录制；编译插件前关掉 OBS |
| 8 sim | 检查预置模拟器 `sim\dist` | 从开发机拷来的话给出目录；没有就用关模拟器的版本 |
| 9 kb | 生成知识库参考资料原文（Celeste Wiki 技巧页面；装了草莓酱 Mod 时一并提取） | 选择要不要生成（要能访问 celeste.ink） |
| 10 saves | 读出游戏里已有存档的名字 | 填调试存档与正式存档的名字；还没有的进游戏新建 |
| 11 switches | 生成启动脚本 `start_local.ps1` | 选择智能体标签、章节、时间上限、推理强度与各开关（见 6 节） |
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
| `configs\local.providers.json` | 第三方平台密钥文件的位置、自定义平台、GPT 网关设置 |
| `configs\local.recording.json` | OBS 目录、ffmpeg 路径、编码参数、声音延迟校准值 |
| `configs\local.deploy.json` | 向导的回答（不含密钥） |
| `start_local.ps1` | 启动脚本（见 5.1 节） |
| `<游戏目录>\Mods\CelesteAIBridge\` | 桥接 Mod |
| `<OBS 目录>\obs-plugins\64bit\celeste-lockstep.dll` | 锁步插件 |

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

在游戏里手动新建存档（新存档会先播开场，建好后退回标题即可）：

- 一个调试用存档，例如 `test`（菜单宏、校准、试跑都只用它）；
- 每个参加正式挑战的智能体一个新存档，例如 `opus5.5`、`fable5.1`、`dsflashv4.1`。

主控只会进入 `--save` 指定的存档，存档名不一致时拒绝开始；开局把这个存档的全部文件备份到运行目录的 `save_backup\`。不要让 AI 碰你自己的存档。

### 4.3 Python 虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖：`mcp`（游戏工具服务）、`pillow`（截图放大）、`uvicorn`（本机 HTTP 服务）、`pywebview`（置顶 HUD 窗口）、`numpy`（规划回放、纯享版）。

### 4.4 .NET 8 SDK 与桥接 Mod

```powershell
winget install Microsoft.DotNet.SDK.8
# 关掉游戏后：
.\tools\deploy_bridge.ps1 -CelesteDir "D:\path\to\steamapps\common\Celeste"
```

脚本编译 `game-bridge\`，把 `everest.yaml` 与 `bin\CelesteAIBridge.dll` 复制到游戏的 `Mods\CelesteAIBridge\`。`dotnet` 不在 PATH 里时先执行 `$env:PATH = "<SDK 目录>;$env:PATH"`。启动游戏后 `log.txt` 里应有 `[CelesteAIBridge] loaded v<版本>`。

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
- **GPT**：见 9.2 节（只有接口，尚未实测）。

### 4.7 录制（OBS、ffmpeg、锁步插件）

```powershell
winget install OBSProject.OBSStudio     # 或 Steam 版
winget install Gyan.FFmpeg
# 关掉 OBS 后编译安装锁步插件（不需要 Visual Studio：脚本用 pip 装 zig 交叉编译，从 GitHub 取同版本的 libobs 头文件）
.\.venv\Scripts\python.exe tools\build_obs_plugin.py --obs-dir "C:\Program Files\obs-studio"
```

新建 `configs\local.recording.json`（只写要改的键，默认值在 `configs\recording.json`）：

```json
{
  "obs_dir": "C:\\Program Files\\obs-studio",
  "ffmpeg": "C:\\Users\\<你>\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe",
  "video_args": "-c:v h264_nvenc -preset p5 -tune hq -rc constqp -qp 16 -g 120",
  "audio_offset_ms": 130
}
```

没有 NVIDIA 显卡时 `video_args` 用默认的 `-c:v libx264 -preset veryfast -crf 16 -g 120`。`audio_offset_ms` 只在某段量不到声音延迟时作为后备，用下面的校准测试得到。

启动 OBS（自动建好专用的配置文件与场景集合 CelesteAI，不动你自己的配置）并校准：

```powershell
.\.venv\Scripts\python.exe tools\enter_save.py test --chapter 1 --leave-active
.\.venv\Scripts\python.exe tools\obs_setup.py launch
.\.venv\Scripts\python.exe calibration\m6_recording_test.py --rounds 18 --think 1.5
.\.venv\Scripts\python.exe tools\obs_setup.py stop
```

校准测试应"全部通过"：帧数一致、帧号条码一致、声音恢复边在第一帧开头、暂停前最后一帧不被截断；最后一行给出建议的 `audio_offset_ms`。OBS 更新后要重新编译插件、重跑校准。

### 4.8 预置模拟器

预置模拟器由游戏代码生成，**不在仓库里，也不应公开分发**。在新电脑上有两种选择：

- 从开发机把 `sim\dist` 整个目录（`celeste-sim`、`sim.py`、`README.md`、`sim_assets.json`、`statemap.json`、`VERSION`）复制到新电脑仓库的 `sim\dist`；它是 Linux 单文件程序，只在容器里运行，与 Windows 版本无关。自检：游戏在 test 存档的关卡里时运行 `.\.venv\Scripts\python.exe tools\sim_check.py`，应"逐帧一致"。
- 没有模拟器时用**关模拟器的版本**：运行时加 `--no-sim`（提示词换成不教打法的版本，见 6 节）。

### 4.9 知识库参考资料（可选）

```powershell
.\.venv\Scripts\python.exe tools\fetch_reference.py --celeste-dir "D:\path\to\Celeste"            # Wiki + 草莓酱
.\.venv\Scripts\python.exe tools\fetch_reference.py --celeste-dir "D:\path\to\Celeste" --skip-sj  # 没装草莓酱 Mod
.\.venv\Scripts\python.exe tools\import_reference_pdf.py 资料.pdf --name <英文名> --title "<标题>"   # 导入本机 PDF（可选）
```

输出在 `kb\reference\`（不入库）。没有它时准备阶段只读知识库本身。

### 4.10 启动脚本（可选）

手动部署也可以只运行向导的最后一步生成启动脚本：`.\.venv\Scripts\python.exe tools\setup_wizard.py --only saves,switches`。

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
# 正式挑战（Anthropic Opus；开模拟器、实时展示、规划回放、OBS 录屏都是默认开启，OBS 由主控自己启动和关闭）
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --chapters 0,1,2 --max-minutes 600
# DeepSeek
.\.venv\Scripts\python.exe -m orchestrator.run --agent dsflash --save dsflash --provider deepseek --model deepseek-flash --chapters 0,1,2 --max-minutes 600
# 关模拟器的版本
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus-nosim --save <存档> --model opus --chapters 0,1,2 --no-sim
# 不录屏的试跑
.\.venv\Scripts\python.exe -m orchestrator.run --agent dev-opus --save test --model opus --chapter 0 --max-minutes 30 --no-record
```

主控依次：（录屏时）OBS 没开就启动它、退回选档页、开始录制；用菜单宏进入存档的章节并跳过开场；核对存档名、备份存档；启动游戏工具服务（`127.0.0.1:47780`）与 HUD（`http://127.0.0.1:47790/hud`）；创建或复用容器 `celeste-agent-<标签>` 并启动 Claude Code。智能体先读完知识库（准备阶段），之后游玩；一章通关后自动进入下一章。

### 5.3 查看与结束

- 进度：`.\.venv\Scripts\python.exe tools\run_status.py`（加 `-n 30` 显示更多决策）；HUD 置顶窗口在游戏右上角，也可以浏览器打开 `http://127.0.0.1:47790/hud`。
- 提前结束：在运行主控的窗口按 Ctrl+C，主控会停止录制、写出 `summary.json`；没通关时游戏冻结在当前帧，可以 `--resume` 续跑，不续跑时 `.\.venv\Scripts\python.exe tools\bridge_client.py deactivate` 交还控制。
- 产出：`runs\<标签>\<时间戳>\`（`metrics.json`、`decisions.jsonl`、`frames.jsonl`、`agent_events.jsonl`、`system_prompt.md`、`summary.json`、`save_backup\`、`recording\`）。录像在 `recording\`：完整版 `gameplay.mkv`（含规划回放）与纯享版 `gameplay_pure.mkv`（只有真实游玩帧）。旧录像补生成纯享版：`.\.venv\Scripts\python.exe tools\make_pure.py runs\<标签>\<时间戳>\recording`。

## 6. 全部开关

### 6.1 主控 `python -m orchestrator.run`

| 开关 | 默认 | 说明 |
| --- | --- | --- |
| `--agent <标签>` | 必填 | 智能体标签：容器 `celeste-agent-<标签>`、工作区 `workspaces\<标签>\`、日志 `runs\<标签>\`、会话数据卷 `celeste-claude-<标签>` |
| `--save <存档名>` | 必填 | 游戏内存档名；与当前存档不一致时拒绝开始 |
| `--provider` | `anthropic` | 模型平台：`anthropic`（订阅令牌）、`deepseek`、`openai`（GPT，未实测）或 `configs\local.providers.json` 里自定义的 |
| `--model` | `opus` | Anthropic 用别名（`opus`、`fable`、`haiku`）；第三方用它自己的模型名（如 `deepseek-flash`；GPT 填网关里的 `model_name`） |
| `--effort` | `high` | 开局推理强度 `low`/`medium`/`high`/`xhigh`/`max`；智能体可以用 `set_effort` 自己调整 |
| `--chapters 0,1,2` | 无 | 依次通关的章节（0 序章、1 1A、2 2A……）；给了它就不用 `--chapter` |
| `--chapter N` | 选章页当前章节 | 只玩一章 |
| `--max-minutes` | 240 | 现实时间安全上限（不含准备阶段与等待额度）；**不显示给智能体** |
| `--sim` / `--no-sim` | 开 | 预置模拟器开关，把项目分成两个版本：开时智能体可以随意调用模拟器寻找并优化路线（提示词写明做法）；关时不能调用模拟器，提示词只写目标、规则与工具接口，不教打法 |
| `--live` / `--no-live` | 开 | 实时展示：智能体思考期间用 `narrate` 随时更新 HUD 上的思考，运行程序期间用 `show.status()` 报告进度（HUD 工具栏实时显示）；思考状态累计满半分钟没有更新时，在智能体下一次调用工具时提醒它（`RunConfig.live_remind_s`）；关时 HUD 的思考只在每次 `act` 时更新 |
| `--no-playback` | 关（即默认回放） | 关闭规划回放（开模拟器时画 `/sim` 的推演，关模拟器时画智能体用 `show.trajectory()` 记下的轨迹） |
| `--record` / `--no-record` | 开 | OBS 录屏开关：开时 OBS 没在运行就由主控自己启动（专用配置、最小化到托盘），锁步录制完整版与纯享版，结束时关掉它启动的 OBS；OBS 已经开着就直接用、结束时不关；`--no-record` 不启动也不调用 OBS |
| `--limit-planning` | 关 | 限制规划轮次（按章节记账，只用于开模拟器的版本）；`--rounds-per-room`（默认 3）、`--room-scale`（默认 2）调整轮次 |
| `--no-study` | 关 | 跳过准备阶段（不读知识库，调试用） |
| `--study-minutes` | 60 | 准备阶段上限，到时自动开放游戏工具 |
| `--resume <运行目录>` | 无 | 续跑一次中断的运行（游戏须仍冻结在原处）；`--fresh-session` 开新会话 |
| `--max-usage 0~1` | 不设 | Max 订阅：用量到这个比例就提前暂停、等重置；默认用到真正用完再等 |
| `--no-quota-wait` / `--quota-wait-max-hours` | 关 / 24 | 额度用完时直接停止 / 最多等多少小时 |
| `--autocompact` | 平台默认 | 上下文自动压缩窗口：`auto` 或 100000～1000000 token |
| `--no-hud-window` / `--no-hud` | 关 / 关 | 不开置顶 HUD 窗口 / 连 HUD 服务也不开 |
| `--hud-port` | 47790 | HUD 端口 |
| `--bridge-port` / `--mcp-port` | 47774 / 47780 | 桥接与游戏工具服务的端口 |
| `--runtime` | `container` | `host`：在本机运行、只开放游戏工具（早期的过渡方式，没有沙箱、没有模拟器） |
| `--skip-enter` | 关 | 已在关卡内可操作时跳过菜单宏 |
| `--no-agent` | 关 | 只启动工具服务，不启动智能体（调试） |
| `--ffmpeg` / `--record-video-args` / `--audio-offset-ms` | 见配置 | 临时覆盖录制设置 |
| `--record-marker` | 关 | 画帧号条码（只用于校准） |
| `--play-speed` / `--auto-speed` | 1 / 1 | 快进（只用于调试，录像会失真） |

### 6.2 其他工具

| 命令 | 作用 | 主要参数 |
| --- | --- | --- |
| `setup.ps1` | 一键部署 | `-Check` 只检查；`-Only 步骤,...` |
| `tools\setup_wizard.py` | 部署向导 | `--check`；`--only` |
| `tools\deploy_bridge.ps1` | 编译安装桥接 Mod | `-CelesteDir`（必填）、`-Configuration` |
| `tools\build_obs_plugin.py` | 编译安装锁步插件 | `--obs-dir`、`--obs-version`、`--no-install` |
| `tools\obs_setup.py` | OBS 配置与启动 | `setup`/`launch`/`stop`/`status`/`restore`；`--obs-dir`、`--show`、`--reset` |
| `tools\enter_save.py <存档>` | 菜单宏进入存档的关卡 | `--chapter N`、`--leave-active` |
| `tools\bridge_client.py` | 桥接测试客户端 | `hello`、`activate`、`observe`、`step`、`advance`、`smoke`、`deactivate` |
| `tools\run_status.py` | 查看运行进度 | `-n` |
| `tools\make_pure.py <recording 目录>` | 补生成纯享版 | `--ffmpeg`、`--video-args` |
| `tools\fetch_reference.py` | 生成知识库参考资料 | `--celeste-dir`、`--skip-wiki`、`--skip-sj`、`--index-only` |
| `tools\import_reference_pdf.py` | 导入 PDF 资料 | `--name`、`--title`、`--note` |
| `tools\sim_check.py` | 模拟器与游戏逐帧对照 | `--segments`、`--agent` |
| `python -m orchestrator.verify` | 容器隔离验收 | `--agent` |
| `calibration\m6_recording_test.py` | 录制校准 | `--rounds`、`--think`、`--no-marker`、`--audio-offset-ms` |
| `calibration\chain_check.py` | 多章自动衔接自检 | — |
| `calibration\reset_test_save.py` | 把 test 存档重置成正式存档的初始进度 / 还原 | `--saves-dir`、`--like`、`--restore` |

## 7. 配置文件

| 文件 | 入库 | 内容 |
| --- | --- | --- |
| `configs\recording.json` | 是 | 录制默认值：`obs_dir`、`ffmpeg`、`video_args`、`audio_offset_ms`、OBS 配置文件与场景集合名、画布 1920×1080、60 fps、48 kHz、游戏窗口、HUD 浏览器源的位置与大小 |
| `configs\local.recording.json` | 否 | 本机覆盖上面的键 |
| `configs\providers.json` | 是 | 模型平台：`label`、`status`、`auth`、`base_url`、`allowed_domains`（防火墙放行的域名）、`allowed_host_ports`（放行的宿主机端口，例如 GPT 网关）、`gateway_check`、`key_env`、`key_file`、`model_suffix`、`images`、`autocompact`、`env`（传给 Claude Code 的模型槽位等） |
| `configs\local.providers.json` | 否 | 本机覆盖与自定义平台；密钥文件位置写在这里 |
| `configs\hud.json` | 是 | HUD：智能体显示名 `names`、章节名、颜色、上下文窗口等 |
| `.env` | 否 | `CLAUDE_CODE_OAUTH_TOKEN` |
| `configs\local.deploy.json` | 否 | 部署向导的回答 |
| `orchestrator\config.py`（`RunConfig`） | 是 | 不常改的默认值：规划回放的比例与倍速、思考语句提前量、镜像名 `celeste-agent:0.1` 等 |

## 8. 端口、网络与隔离

| 端口 | 谁监听 | 谁连 |
| --- | --- | --- |
| 127.0.0.1:47774 | 桥接 Mod（游戏进程） | 主控、测试客户端、菜单宏 |
| 127.0.0.1:47776 | 桥接（录制时钟） | OBS 锁步插件 |
| 47780 | 主控：游戏工具服务（MCP）与 `/gate` | 容器里的 Claude Code（经 `host.docker.internal`） |
| 127.0.0.1:47790 | 主控：HUD 页面与数据 | 置顶窗口、OBS 浏览器源、浏览器 |
| 4000（可改） | GPT 网关（只在用 GPT 时） | 容器里的 Claude Code |

容器的出站防火墙（`agent-container\init-firewall.sh`，容器启动时生效）只放行：所用模型平台的域名（Anthropic 平台放行 Anthropic 官方域名；第三方平台只放行它自己的域名，不放行 Anthropic）、宿主机上主控的端口、平台配置里的宿主机端口；其余一律拒绝，DNS 也封掉。智能体用 Bash 运行的程序在零网络的沙箱（bubblewrap）里，连模型平台也连不上，环境里也没有令牌与密钥。Claude Code 以受限模式启动，文件工具只能访问工作区、知识库与模拟器目录；联网工具在托管配置里被拒绝；不允许后台任务。改了 `agent-container\` 之后要重新构建镜像，主控发现镜像或平台设置变了会自动重建容器（工作区与会话数据卷保留）。

## 9. 接入其他平台的模型

容器里运行的始终是同一个 Claude Code，第三方平台通过兼容 Anthropic Messages 的接口接入；工具、提示词、知识库、沙箱、回放、HUD 与录制完全相同。

### 9.1 DeepSeek（已实测）

1. 把 API 密钥存成一个文本文件（只有密钥一行），放在仓库目录之外，例如 `D:\secrets\deepseek.key`。
2. `configs\local.providers.json`：

   ```json
   { "deepseek": { "key_file": "D:\\secrets\\deepseek.key" } }
   ```

   或者设环境变量 `DEEPSEEK_API_KEY`。
3. 运行时加 `--provider deepseek --model deepseek-flash`（或 `deepseek-v4-pro`）。

### 9.2 GPT（OpenAI）：只有接口，尚未实测

OpenAI 的接口不是 Anthropic 格式，Claude Code 不能直接调用，所以要在本机运行一个"Anthropic 接口 → OpenAI 接口"的转换网关，例如 [LiteLLM 代理](https://docs.litellm.ai/docs/tutorials/claude_non_anthropic_models)。网关持有 OpenAI 的密钥，容器只连宿主机上网关的端口、用网关自己的访问密钥。

1. 安装网关（单独的环境，不装进本项目的 `.venv`）：

   ```powershell
   uv tool install 'litellm[proxy]'      # 或：python -m pip install "litellm[proxy]"
   ```

2. 写网关配置 `litellm.yaml`（`model_name` 是之后 `--model` 要填的名字，`model` 是 OpenAI 的模型名）：

   ```yaml
   model_list:
     - model_name: <给智能体用的名字>
       litellm_params:
         model: openai/<OpenAI 模型名>
         api_key: os.environ/OPENAI_API_KEY
   ```

3. 启动网关。只监听本机回环地址即可：Docker Desktop 把容器里对 `host.docker.internal` 的连接转到宿主机的 127.0.0.1（主控的游戏工具端口也是这样连的），这样不会弹出 Windows 防火墙询问，局域网里的其他机器也连不上网关：

   ```powershell
   $env:OPENAI_API_KEY = "<OpenAI 密钥>"
   $env:LITELLM_MASTER_KEY = "sk-<一串随机字符>"
   litellm --config litellm.yaml --host 127.0.0.1 --port 4000
   ```

   2026-10-24 在作者本机核对过防火墙（用一个只监听 127.0.0.1:4000 的临时网页服务代替网关）：按 `openai` 的配置建的容器能访问 `host.docker.internal:4000`，连不上宿主机的其他端口，解析不到 api.openai.com、api.anthropic.com 等任何域名，Anthropic 的地址段也被拒绝。

4. 把网关访问密钥（`LITELLM_MASTER_KEY` 的值）存成本机文件，写进 `configs\local.providers.json`（端口改了的话一并改）：

   ```json
   { "openai": { "key_file": "D:\\secrets\\litellm.key",
                 "base_url": "http://host.docker.internal:4000", "allowed_host_ports": [4000],
                 "gateway_check": "http://127.0.0.1:4000/health/liveliness" } }
   ```

5. **重新构建智能体镜像**（2026-10-24 起防火墙才支持放行宿主机上的网关端口）：`docker build -t celeste-agent:0.1 agent-container`。
6. 运行：`--provider openai --model <model_name>`。主控开跑前会访问 `gateway_check` 确认网关在运行，并提示这个平台尚未实测。

### 9.3 其他兼容 Anthropic 接口的平台

在 `configs\local.providers.json` 里照 `deepseek` 加一项（部署向导的 platform 步骤选"其他平台"会帮你写）：

```json
{ "myprovider": {
    "label": "My Provider", "status": "未实测", "auth": "api_key",
    "base_url": "https://api.example.com/anthropic", "allowed_domains": ["api.example.com"],
    "key_env": "MYPROVIDER_API_KEY", "key_file": "D:\\secrets\\myprovider.key", "model_suffix": "", "images": true,
    "env": { "ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}", "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model_base}", "CLAUDE_CODE_SUBAGENT_MODEL": "{model_base}",
             "API_TIMEOUT_MS": "600000" } } }
```

`images: false` 用于只支持文字的模型（观测里另有字符地图与精确数据）。HUD 上的显示名写进 `configs\hud.json` 的 `names`。

## 10. 更新、迁移与卸载

**更新仓库**：`git pull`，然后视改动：

| 改动了 | 要做 |
| --- | --- |
| `game-bridge\`（桥接版本号变了） | 关游戏，`.\tools\deploy_bridge.ps1 -CelesteDir ...` |
| `agent-container\` | `docker build -t celeste-agent:0.1 agent-container`（容器会自动重建） |
| `obs-plugin\` 或 OBS 更新了 | 关 OBS，`tools\build_obs_plugin.py --obs-dir ...`，重跑录制校准 |
| `overlay\hud.html` | 关掉开着的 OBS（`tools\obs_setup.py stop`），下次运行时主控会重新启动它（OBS 浏览器源不会自动刷新）；关掉旧的 HUD 窗口 |
| `requirements.txt` | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |

**迁移到另一台电脑**：按本文重新部署；要带走的本机内容有 `workspaces\`（智能体的程序与笔记）、`runs\`（运行记录与录像）、`sim\dist`（预置模拟器）、`kb\reference`（可以重新生成）、`configs\local*`（路径要按新电脑改）、`.env`（令牌可以重新生成）。

**卸载**：

```powershell
docker rm -f celeste-agent-<标签>; docker volume rm celeste-claude-<标签>; docker image rm celeste-agent:0.1
Remove-Item "<游戏目录>\Mods\CelesteAIBridge" -Recurse
Remove-Item "<OBS 目录>\obs-plugins\64bit\celeste-lockstep.dll"
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
| `deploy_bridge.ps1` 提示文件被占用 | 游戏还开着，关掉再试 |
| 测试客户端连不上桥接 | 游戏没开，或 `log.txt` 里没有 `[CelesteAIBridge] loaded`（Mod 配置里没启用它） |
| `--record` 提示 OBS 插件没有连上桥接 | `tools\obs_setup.py status` 看插件是否加载；没加载就重新编译插件 |
| OBS 弹出"安全模式" | 用 `tools\obs_setup.py launch` 启动（会清掉崩溃标记）；已弹出时选正常模式 |
| 录像里没有游戏画面 | 录制时游戏窗口被最小化；主控会自动还原，失败时手动还原后重录 |
| 第三方平台提示找不到密钥 | 按 9 节写 `configs\local.providers.json` 的 `key_file`，或设环境变量 |
| `--provider openai` 提示连不上网关 | 先按 9.2 节启动 LiteLLM，`gateway_check` 的地址在浏览器能打开 |
| 智能体想从零开始 | 删除 `workspaces\<标签>\`，再 `docker rm -f celeste-agent-<标签>`、`docker volume rm celeste-claude-<标签>` |
| 存档进度不对 | 从运行目录的 `save_backup\` 恢复（先关游戏），并确认已关闭云同步 |

更多问题见 [USAGE.md 第 6 节](USAGE.md#6-常见问题)。
