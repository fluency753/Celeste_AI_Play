# Celeste_AI_Play

![状态：开发中（未完成）](https://img.shields.io/badge/%E7%8A%B6%E6%80%81-%E5%BC%80%E5%8F%91%E4%B8%AD%EF%BC%88%E6%9C%AA%E5%AE%8C%E6%88%90%EF%BC%89-orange)

> **项目尚未完成。** 主要功能已经可用，仍在持续开发与调整。完成全部计划之前，此处会一直保留"未完成"标注。

让大模型智能体在断网的沙箱里，逐帧独立游玩《蔚蓝》（Celeste）的官方章节。

游戏可以逐帧暂停：智能体想好下一段操作，游戏才往前走。OBS 只录下真正推进的游戏帧，思考的时间在录像里完全不存在。可以接入 Claude 订阅里的模型、提供 Anthropic 兼容接口的其他平台（如 DeepSeek），也可以通过 Codex 接入 GPT。

## 快速开始

**推荐让一个 AI 编程助手帮你部署和操作**（例如 Claude Code、Codex 等能在你电脑上读文件、执行命令的 AI）。对它说：

> 请阅读本仓库的 docs/AI_GUIDE.md，按它帮我部署并启动一次试玩。

两份使用说明：

| 文档 | 给谁看 | 内容 |
| --- | --- | --- |
| [docs/GUIDE.md](docs/GUIDE.md) | 你自己 | 需要准备什么、一步步部署、用控制台开第一次运行、怎么看、暂停与续跑、录像在哪、常见问题 |
| [docs/AI_GUIDE.md](docs/AI_GUIDE.md) | 帮你操作的 AI 助手 | 先读什么、环境检查、部署、启动、监督、介入、收集产出、安全与礼节规则 |

自己动手时，在一台装好正版《蔚蓝》的 Windows 电脑上：

```powershell
git clone https://github.com/fluency753/Celeste_AI_Play.git
cd Celeste_AI_Play
powershell -ExecutionPolicy Bypass -File .\setup.ps1            # 一键部署向导：逐步检查与安装
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check     # 只检查各项是否就绪，不改动
.\CelesteAIConsole.exe                                           # 打开控制台，在图形界面里设置并开始运行
```

环境要求与每一步的细节见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 效果是什么样

- **逐帧冻结思考**：智能体思考、写程序、运行程序时，游戏冻结在当前帧，章节计时也停住。它想好之后，用按键序列推进一段（默认一次最多 600 帧），再停下来观察。
- **锁步录像，只录游玩帧**：OBS 每要一帧，游戏才推进一帧。成片的帧数等于游玩帧数，声音由游戏按帧单独混出。每次录制出两个版本：完整版 `gameplay.mp4` 和只有真实游玩帧的纯享版 `gameplay_pure.mp4`。
- **HUD**：游戏画面右上角显示章节计时（逐帧准确）、推理强度、思考与工具用时、上下文、死亡次数、正在调用的工具，以及智能体写给观众的思考语句。也可以改用网页 HUD（独立窗口与 OBS 浏览器源）。
- **规划回放**（默认关闭，`--playback` 打开）：把智能体算过的候选路线按轮画进游戏：绿线延伸，死亡的变红，最快的变金。回放不推进游戏、不计章节计时，照常录进完整版录像。只有智能体把自己算出的逐帧轨迹交给工作区的绘制接口 `show.trajectory()` 时才有内容，所以通常要在提示词里要求它主动写推演程序并适配这个接口（速通模式开了预置模拟器时，模拟器的推演也会画出来）。
- **控制台**：图形界面里设置全部参数、开始运行；运行中暂停、继续、给智能体发消息、中断后续跑。

## 两种游玩模式

速通模式与探索模式二选一（`--mode`）。直播模式（`--live`）可以叠加在任一个上。

| 模式 | 目标 | 智能体能读的资料 | 模拟器 |
| --- | --- | --- | --- |
| 探索 `--mode explore` | 像新手一样探索、学习并完成指定章节 | 只有 `kb/explore` 的入门资料（基本规则、坐标与观测数据、按键与工具） | 不开放模拟器文件；可选"限额短路线预测"（`--sim-assist`，默认关闭，需自配标准模拟器） |
| 速通 `--mode speedrun` | 以最短的游戏内章节计时通关 | 完整知识库 `kb/`（物理参数、机制、技巧、数据格式） | 本机有预置模拟器时默认开放（`--sim`），没有时运行关模拟器的版本（`--no-sim`） |
| 直播 `--live`（叠加） | 观众实时观看：解说更勤，HUD 默认用可交互的网页形式 | — | — |

- 控制台默认选探索模式。命令行不写 `--mode` 时，Claude Code 执行器默认速通，Codex 执行器默认探索。
- 探索模式里智能体可以自己写辅助程序，单次运行默认最长 120 秒（可调参数 `explore_program_s`）。
- 速通模式下，大模型负责规划路线，具体按键由程序搜索生成，执行时 60 fps 全速推进。
- "预置模拟器"由游戏代码逐帧推演，含反编译代码，**不随仓库分发**，需要自己准备（见下文"不在仓库里的本机文件"）。

## 执行器与模型接入

智能体运行在 Docker 容器里。有两种执行器，共用同一套游戏工具、提示词、知识库、HUD 与录制。

| 执行器 | 接入的模型 | 登录与密钥（都只存本机，不入库） |
| --- | --- | --- |
| Claude Code（`--engine claude`，默认） | Anthropic：Claude 订阅（Pro/Max）里的模型，别名 `opus`、`fable`、`haiku` | 用 `claude setup-token` 生成长期令牌，写进仓库根目录的 `.env`；不需要 API 密钥 |
| Claude Code + 第三方平台（`--provider`） | 提供 Anthropic 兼容接口的平台。DeepSeek 已有预设（`--provider deepseek --model deepseek-flash`） | 平台列表在 [configs/providers.json](configs/providers.json)；密钥文件位置与自定义平台写在本机的 `configs/local.providers.json`，部署向导可以代写 |
| Codex（`--engine codex`） | GPT | 能使用 Codex 的 ChatGPT 账号，在容器里设备登录：`python -m orchestrator.codex_container --account login` |

推理强度默认 `high`（Codex 默认 `medium`），可选档位由所选模型声明。智能体可以用 `set_effort` 自己调整。接入方法见 [docs/DEPLOY.md 第 9 节](docs/DEPLOY.md#9-接入其他平台的模型)。

## 系统组成

![总体架构](docs/images/architecture.png)

| 组成 | 位置 | 作用 |
| --- | --- | --- |
| 桥接 Mod CelesteAIBridge（1.10.0） | `game-bridge/`，运行在游戏进程内（Everest Mod） | 逐帧暂停与放行、注入按键、提取状态与碰撞箱、截图、画游戏内 HUD 与规划回放、录像专用的游戏声音、新建存档 |
| 常驻核心 CelesteAIRuntime（1.0.5） | `game-runtime/`，Everest Mod | 持有冻结、连接、录制时钟与录音资源；桥接逻辑可以在不重启游戏的情况下受控重载（见 [docs/BRIDGE_RELOAD.md](docs/BRIDGE_RELOAD.md)） |
| 主控 | `orchestrator/`，本机 Python | 唯一同时连接游戏、智能体、HUD 与 OBS 的组件：游戏工具服务、决策循环、执行与自动重试、规划回放、录制、换章、额度与断网等待、中断续跑 |
| 智能体容器 | `agent-container/`，Docker 镜像 `celeste-agent:0.1` | 容器里的执行器只能连所用模型平台与主控端口；智能体自写的程序在零网络沙箱里运行，看不到令牌与密钥 |
| 知识库 | `kb/`，只读挂进容器 | 物理常量、机制规则、技巧与数据格式；只讲游戏规则，不讲关卡怎么过 |
| HUD 与控制台 | `overlay/` | 游戏内 HUD 的绘制、网页 HUD、桌面控制台（`CelesteAIConsole.exe`，1.3.0） |
| OBS 锁步插件 | `obs-plugin/` | 只把游玩帧（和规划回放帧）交给 ffmpeg 编码 |
| 部署与诊断 | `setup.ps1`、`tools/` | 部署向导、桥接与插件构建、OBS 配置、状态查看、测试客户端 |

主控要求桥接 1.4.0 或更新；桥接依赖 Everest（EverestCore 1.5935.0 起，.NET 8 版本）。容器的隔离可以用 `python -m orchestrator.verify --agent <标签>` 逐项验收。完整设计见 [docs/DESIGN.md](docs/DESIGN.md)。

<a id="开关"></a>

## 命令行与开关

控制台里的全部设置都对应主控 `python -m orchestrator.run` 的参数，控制台页面底部实时显示等价的命令行。几个例子：

```powershell
# 在测试存档上试跑序章：探索模式，30 分钟，不录屏
.\.venv\Scripts\python.exe -m orchestrator.run --agent dev-trial --save <测试存档名> --mode explore --chapter 0 --max-minutes 30 --no-record
# 速通模式依次通关序章、1A、2A（录屏默认开启）
.\.venv\Scripts\python.exe -m orchestrator.run --agent <标签> --save <存档名> --mode speedrun --chapters 0,1,2
# DeepSeek
.\.venv\Scripts\python.exe -m orchestrator.run --agent <标签> --save <存档名> --provider deepseek --model deepseek-flash --chapter 0
# 续跑一次中断的运行
.\.venv\Scripts\python.exe -m orchestrator.run --agent <标签> --save <存档名> --resume runs\<标签>\<时间戳>
```

- `--save` 是游戏里的存档名，与当前存档不一致时主控拒绝开始；同名存档用 `--save-slot` 区分，`--create-save` 让主控新建存档。
- 章节可以选官方章节的 A/B/C 面；`--chapters` 依次通关多章，一章通关后自动进入下一章。
- `--max-minutes` 是现实时间安全上限，默认 1000 分钟，不含准备阶段与等待额度的时间，也不显示给智能体。
- 长时间运行建议用控制台，或用独立后台宿主启动：`python -m orchestrator.background -- <主控参数>`。从聊天助手的命令工具里直接启动，聊天关闭或回退时主控可能被一起结束（见 [docs/INCIDENT_20261024.md](docs/INCIDENT_20261024.md)）。

**全部参数**：主控参数的完整一览表见 [docs/USAGE.md 4.1 节](docs/USAGE.md#41-让智能体游玩一个章节) 与 [docs/DEPLOY.md 6.1 节](docs/DEPLOY.md#61-主控-python--m-orchestratorrun)；可调参数（`--tune`）见 [USAGE.md 4.11 节](docs/USAGE.md#411-可调参数--tune)；其他工具的参数见 [DEPLOY.md 6.2 节](docs/DEPLOY.md#62-其他工具)。`python -m orchestrator.run --help` 列出同样的内容。

**部署与启动脚本**

| 命令 | 作用 |
| --- | --- |
| `setup.ps1` | 一键部署：找到或安装 Python、建虚拟环境 `.venv`，然后启动部署向导 `tools\setup_wizard.py` |
| `setup.ps1 -Check` | 只检查各项是否就绪，不改动 |
| `setup.ps1 -Only 步骤,...` | 只重做其中几步（system、game、dotnet、bridge、docker、platform、recording、sim、kb、saves、switches、checks） |
| `start_local.ps1` | 部署向导生成的本机启动脚本：用向导里选好的存档、章节与开关运行 |
| `start_local.ps1 -Trial` | 在调试存档上试跑 30 分钟（标签 `dev-<标签>`，第一个章节） |
| `start_local.ps1 -Resume <运行目录>` | 续跑一次中断的运行 |
| `start_gpt.ps1` | Codex 执行器（GPT）的准备与试玩，见 [docs/LIVE_ADAPTATION.md](docs/LIVE_ADAPTATION.md) |

**手动调用游戏工具（不启动智能体）**：用 `--no-agent --no-sim --keep-obs-open` 启动主控后，可以用 `tools/game_tool.py` 从命令行调用同一套 `observe`、`act`、`narrate` 等游戏工具，不需要配置模型。参数用 `--args-file` 指向 JSON 文件，截图用 `--out` 保存，详见 `python tools/game_tool.py --help`。这种方式下 HUD 上的推理强度只是启动参数，也没有准备阶段的阅读核对，不能算作"已读知识库"的运行。

**直播**：OBS 正常输出实时画面，暂停时显示冻结的游戏画面和更新的 HUD；锁步插件另外保存去掉等待时间的录像。两者共用 OBS 的渲染线程，编码负载与音画同步要部署后一起验证。直播时加 `--keep-obs-open`（主控结束时保留 OBS），可加 `--continuous-music`（冻结时 BGM 照常播放，音效冻结）。

## 项目状态

- **只支持《蔚蓝》官方章节**（A/B/C 面）。MOD 地图在未来版本中支持（控制台"存档与章节"页已经预留了选项）。
- 已经可用：桥接 Mod 与常驻核心、主控、断网的智能体容器、知识库、两种执行器、速通与探索两种模式、直播模式、游戏内 HUD 与网页 HUD、控制台、OBS 锁步录制（完整版与纯享版、每章一个视频）、额度与断网等待、中断续跑、一键部署。
- 尚未完成：MOD 地图、探索模式限额短路线预测的实际试玩验收、隔离的独立游戏实例等，见 [TODO.md](TODO.md)。
- 部署流程与默认值会随开发调整，以仓库里的代码和最新文档为准。

## 不在仓库里的本机文件

运行需要、但不入库的东西（部署到新电脑时要另外复制或生成）：

| 本机位置 | 是什么 | 新电脑上怎么办 |
| --- | --- | --- |
| `sim\dist\` | 预置模拟器：`celeste-sim`（Linux 单文件程序，在容器里运行）、`sim.py`、`README.md`、`sim_assets.json`、`statemap.json`、`VERSION`。由游戏代码生成，不公开分发 | 有构建好的模拟器时把整个文件夹复制到同一位置；没有时速通模式运行关模拟器的版本，探索模式不能开限额短路线预测 |
| `kb\reference\` | 知识库参考资料原文：Celeste Wiki 技巧页面、草莓酱训练场与机制库、导入的社区资料 | 用 `tools\fetch_reference.py`、`tools\import_reference_pdf.py` 生成；没有时只读知识库正文 |
| `.env` | Claude Code 订阅令牌 | 用 `claude setup-token` 重新生成（部署向导会引导） |
| `configs\local.*.json` | 本机路径、密钥文件位置、录制参数、HUD 窗口与字号、控制台记住的设置 | 由部署向导与控制台重新生成 |
| `local\` | 后台宿主的启动记录与输出日志、控制台里改过的提示词与个人追加要求、按阅读权限生成的知识库副本等 | 不需要复制；想沿用改过的提示词时复制 `local\prompts\` 与 `local\personal_prompts.json` |
| `workspaces\`、`runs\` | 智能体写的程序与笔记；运行记录、录像、存档备份 | 想延续进度时复制 |

知识库正文（`kb\*.md`）在仓库里。反编译的游戏源码、存档、录像、令牌与 API 密钥一律不入库。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/GUIDE.md](docs/GUIDE.md) | 使用指南（给人看）：准备、部署、第一次运行、暂停与续跑、录像、常见问题 |
| [docs/AI_GUIDE.md](docs/AI_GUIDE.md) | 操作手册（给帮你操作的 AI 助手看） |
| [docs/DEPLOY.md](docs/DEPLOY.md) | 部署说明：环境要求、一键部署、手动部署、全部命令与开关、配置文件、端口与隔离、接入其他平台、更新与卸载 |
| [docs/USAGE.md](docs/USAGE.md) | 使用说明（详细）：每个功能的原理与参数、HUD、录制、模拟器、续跑、控制台、可调参数、提示词 |
| [docs/DESIGN.md](docs/DESIGN.md) | 设计文档：整体框架、实现原理、提速方案 |
| [docs/LIVE_ADAPTATION.md](docs/LIVE_ADAPTATION.md) | Codex 执行器（GPT）、直播、可交互网页 HUD、独立游戏声音、存档选择与剧情控制 |
| [docs/CODEX_CONTAINER.md](docs/CODEX_CONTAINER.md) | Codex 执行器在容器里的运行方式与账号登录 |
| [docs/BRIDGE_RELOAD.md](docs/BRIDGE_RELOAD.md) | 桥接受控重载：不重启游戏更新桥接逻辑 |
| [docs/CHAPTER_RECORDING.md](docs/CHAPTER_RECORDING.md)、[docs/RECORDING_RECOVERY.md](docs/RECORDING_RECOVERY.md) | 分章录制与菜单冻结；录像中途失败后的恢复 |
| [docs/INCIDENT_20261024.md](docs/INCIDENT_20261024.md) | 故障说明：后台进程依附在聊天会话上，随会话关闭而退出 |
| [CHANGELOG.md](CHANGELOG.md) | 更新记录 |
| [TODO.md](TODO.md) | 待办 |

## 目录结构

```text
Celeste_AI_Play/
├── setup.ps1             一键部署入口（Python 与虚拟环境，然后启动部署向导）
├── CelesteAIConsole.exe  控制台的启动器（Windows；源码在 tools/launcher）
├── start_gpt.ps1         Codex 执行器（GPT）的准备与试玩入口
├── game-bridge/          桥接 Mod（C#，Everest）
├── game-runtime/         常驻核心（C#，Everest）
├── obs-plugin/           OBS 锁步插件（C，libobs）
├── orchestrator/         主控：游戏工具服务、决策循环、执行与重试、规划回放、录制、容器与执行器、后台宿主、提示词
├── agent-container/      智能体镜像（Claude Code 与 Codex、出站防火墙、托管配置、工具门控钩子、程序沙箱）
├── kb/                   知识库（探索模式的入门资料在 kb/explore；只读挂进容器）
├── overlay/              HUD 与控制台：网页、服务、独立窗口，以及游戏内 HUD 的绘制
├── configs/              HUD、录制、模型平台与检查点的共享配置
├── calibration/          回归测试、校准与验收
├── docs/                 文档
├── tools/                部署向导、构建、OBS 配置、菜单宏、测试客户端、状态查看、资料抓取
├── sim/                  预置模拟器（只在本机，不入库）
├── local/                本机运行数据（不入库）
├── workspaces/           智能体工作区（不入库）
└── runs/                 运行产出（不入库）
```

## 作者

作者：[Fluency](https://space.bilibili.com/490510003)、[Mechanic Pig](https://space.bilibili.com/384651096)

## 游戏文件与版权

本仓库不包含《蔚蓝》的游戏文件、资源或反编译代码，运行需要自行购买正版游戏。知识库只收录事实性的物理参数与机制描述。录像专用的独立游戏声音读取的是你本机安装的官方音频文件，不会复制进仓库。

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

## 参考

- [Everest 与 Olympus](https://github.com/EverestAPI)：蔚蓝的 Mod 加载器与安装器
- [CelesteTAS-EverestInterop](https://github.com/EverestAPI/CelesteTAS-EverestInterop)：逐帧推进与确定性回放的参考实现
- [OBS Studio](https://obsproject.com/)
- [Codex app-server](https://developers.openai.com/codex/app-server/)：Codex 执行器使用的协议
