# 使用说明

> **项目尚未完成。** 目前可以使用的是桥接 Mod（M1）、测试客户端与菜单宏、主控（M2）、智能体容器、工作区与知识库（M3）、分层执行（M4）与 HUD（M5）：智能体在断网容器里先读完知识库，再独立游玩一个章节，可以自己写模拟器与规划程序，按计划文件执行并自动重试（见第 4.1、4.2 节）。其余步骤按最终流程编写，尚未实现的标注为"未实现"，每轮开发后同步更新。
>
> 最后更新：2026-10-22（M3）

## 1. 环境要求

| 项目 | 要求 | 备注 |
| --- | --- | --- |
| 操作系统 | Windows 10 2004 或更新，或 Windows 11 | OBS 的应用音频捕获需要这一版本 |
| 游戏 | Steam 版《蔚蓝》（Celeste）1.4.0.0 | 示例安装目录：`D:\Fluency\software\steam\steamapps\common\Celeste` |
| Mod 加载器 | Everest（.NET 8 版本，已在 6642 上验证编译） | 用 Olympus 或 CeleMod 安装 |
| 构建工具 | .NET 8 SDK | 编译桥接 Mod；可用官方脚本做用户级安装，无需管理员权限 |
| 运行环境 | Python 3.11 或更新（已在 3.14 上验证） | 主控与测试客户端；依赖装在仓库内的虚拟环境 `.venv` 里 |
| 录制 | OBS Studio 28 或更新 | 示例安装目录：`D:\Fluency\software\steam\steamapps\common\OBS Studio` |
| 容器 | Docker Desktop（WSL2 后端），已在 29.9 上验证 | 智能体（Claude Code）整体运行在容器中，安装见 2.3 节 |
| 模型 | Claude Max 套餐，无需 API 密钥 | 在 Windows 上装好 Claude Code，用 `claude setup-token` 生成长期令牌 |

## 2. 一次性准备

| 步骤 | 操作 | 状态 |
| --- | --- | --- |
| 1 | 备份整个游戏目录 | 可执行 |
| 2 | 安装 Everest | 可执行 |
| 3 | 在游戏里手动创建三个新存档：正式存档 `opus5.5`、`fable5.1`，以及调试用的 `test`，记下各自的槽位 | 可执行 |
| 4 | 在 Steam 中关闭本游戏的云同步，避免存档被覆盖 | 可执行 |
| 5 | 游戏设为窗口化或无边框全屏，分辨率 1920×1080；辅助模式与变体保持关闭 | 可执行 |
| 6 | 在 Mod 管理器里建一个只启用 CelesteAIBridge 的配置，避免其他 Mod 改变手感、画面或抢占按键 | 可执行 |
| 7 | 构建并安装桥接 Mod，见第 3 节 | 可执行 |
| 8 | 安装 OBS 锁步插件，放入 OBS 目录下的 `obs-plugins\64bit` 与 `data\obs-plugins\<插件名>` | 未实现 |
| 9 | 在 OBS 中建场景：游戏捕获（Celeste.exe）、应用音频捕获（Celeste.exe）、HUD 浏览器源；输出 60 fps，录制格式 MKV，关闭桌面音频与麦克风 | 可预先配置 |
| 10 | 生成 Claude Code 令牌，见 2.1 节 | 可执行 |
| 10a | 建立 Python 虚拟环境并安装依赖，见 2.2 节 | 可执行 |
| 10b | 安装 Docker Desktop，见 2.3 节 | 可执行 |
| 11 | 构建智能体镜像，见 2.4 节 | 可执行 |
| 12 | 运行容器隔离验收，见 2.4 节 | 可执行 |
| 13 | 审核知识库 `kb/`（物理参数与机制，只讲规则，不讲关卡怎么过） | 已生成，待审核 |

### 2.1 生成 Claude Code 令牌

在自己的电脑上、普通（非管理员）PowerShell 里操作，任意目录都可以：

1. 安装 Claude Code：执行 `irm https://claude.ai/install.ps1 | iex`。安装过程不显示进度，结束后关闭这个窗口。
2. 新开一个 PowerShell，执行 `claude --version`，能打印版本号即安装成功。若提示找不到 `claude`，改用完整路径 `& "$env:USERPROFILE\.local\bin\claude.exe"` 代替下面命令里的 `claude`。
3. 执行 `claude setup-token`。浏览器会打开 Claude 授权页，用 Max 账号登录并点授权；若浏览器没有自动打开，就把终端里显示的链接复制到浏览器。
4. 授权完成后，终端会打印一长串令牌（有效期一年）。用鼠标选中整串令牌并复制，注意换行显示的部分也要选全。
5. 执行 `notepad .env`（在仓库根目录下），把令牌粘贴在 `CLAUDE_CODE_OAUTH_TOKEN=` 后面，不加引号和空格，保存并关闭。
6. 令牌等同于账号凭据：不要发给任何人，也不要提交到仓库。`.env` 已被 `.gitignore` 排除。
7. 核对：带上该令牌执行 `claude auth status`，`authMethod` 应为 `oauth_token`。已在作者本机验证（Claude Code 2.1.309），`opus`（claude-opus-5-5）与 `fable`（claude-fable-5-1）均可调用，上下文窗口均为 100 万 token。

### 2.2 Python 虚拟环境

在仓库根目录的 PowerShell 中执行一次：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

之后所有 `python` 命令都可以用 `.\.venv\Scripts\python.exe` 代替，或先执行 `.\.venv\Scripts\Activate.ps1` 激活虚拟环境。依赖只有三个：`mcp`（游戏工具服务）、`pillow`（截图放大）、`uvicorn`（本机 HTTP 服务）。

### 2.3 安装 Docker Desktop

1. **装 WSL**（管理员权限，需要重启）：右键开始按钮 →"终端（管理员）"，执行 `wsl --install --no-distribution`，完成后重启电脑。Docker 不需要 Ubuntu 等发行版。
2. **检查 WSL 版本**：重启后在普通 PowerShell 执行 `wsl --version`，应为 2.1.5 或更新；否则执行 `wsl --update`。
3. **下载并安装**：从 [Docker 官网](https://docs.docker.com/desktop/setup/install/windows-install/) 下载 `Docker Desktop Installer.exe`，双击安装，配置页勾选 **Use WSL 2 instead of Hyper-V**。
4. **首次启动**：在开始菜单打开 Docker Desktop，接受服务协议（个人使用免费），登录与问卷可以跳过；左下角显示 Engine running 即可。
5. **验证**：新开 PowerShell 执行 `docker version`（Client 与 Server 都有版本号）和 `docker run --rm hello-world`（打印 `Hello from Docker!`）。

装好后已经打开的终端里可能找不到 `docker` 命令，新开一个终端即可；主控也会自动去默认安装位置找。

### 2.4 构建智能体镜像与隔离验收

在仓库根目录执行（Docker Desktop 需在运行）：

```powershell
docker build -t celeste-agent:0.1 agent-container
.\.venv\Scripts\python.exe -m orchestrator.verify --agent opus
```

第一次构建约 1～2 分钟。镜像里有固定版本的 Claude Code（2.1.309）、Python 3 与 numpy、scipy、numba、pillow、networkx、gcc / g++，没有任何游戏文件。

验收脚本会创建（或复用）智能体容器 `celeste-agent-opus`，逐项检查并打印"通过 / 不通过"：

| 类别 | 检查内容 |
| --- | --- |
| 认证 | 容器里的 Claude Code 使用订阅令牌（`oauth_token`），环境里没有 API 密钥 |
| 防火墙 | Claude Code 本身只能连到 Anthropic 官方域名与宿主机上主控的游戏工具端口；任意网站、直连 IP、外部 DNS、宿主机其他端口（包括游戏桥接端口）全部失败 |
| Bash 沙箱 | 让容器里的 Claude Code 用 Bash 工具运行同一套检查：全部失败，包括 Anthropic 与主控端口；命令环境里看不到登录令牌；只能写工作区与 `/tmp` |
| 重算程序沙箱 | 自动重试时主控运行智能体程序用的沙箱（bubblewrap，独立网络命名空间）：同一套检查全部失败；看不到登录令牌；只能写工作区与 `/tmp`，不能写家目录与知识库 |
| 权限 | 智能体用户改不了防火墙、托管配置与 `/etc/hosts`；托管配置拒绝联网工具、禁止退出沙箱 |

2026-10-22 在作者本机实测：全部通过。

### 2.5 生成知识库里的参考资料原文

知识库 `kb/` 里的物理常量与机制文件已入库；另外两份社区资料的原文不入库（第三方文本，不在公开仓库里再分发），需要在本机生成一次，生成后随 `kb/` 一起只读挂载进容器的 `/kb/reference/`：

- Celeste Wiki（celeste.ink）的 Tech 总表与全部技巧子页面（转成 Markdown，许可 GPLv3）；
- 草莓酱（Strawberry Jam Collab）训练场 Beginner → Grandmaster 的全部说明文字与机制库全文（英文、简体中文），直接从本机安装的 Mod 压缩包读取。

```powershell
.\.venv\Scripts\python.exe tools\fetch_reference.py --celeste-dir "D:\Fluency\software\steam\steamapps\common\Celeste"
```

需要能访问 celeste.ink，并且已经安装草莓酱 Mod（`Mods\StrawberryJam2021.zip`）。只要其中一份时可加 `--skip-wiki` 或 `--skip-sj`。输出在 `kb\reference\`，索引见其中的 `README.md`。2026-10-22 实测：wiki 17 个页面，草莓酱训练场 230 条、机制库 86 条，共约 310 KB。

另外可以导入你手上的 PDF 资料（同样只放本机），文字版每页标出原文页码，原 PDF 一并复制，智能体需要看插图时可以直接打开：

```powershell
.\.venv\Scripts\python.exe tools\import_reference_pdf.py 资料.pdf --name celeste_mechanics_explained --title "Celeste 机制详解（反向钟）" --note "来源：……"
```

提取文字需要 poppler 的 `pdftotext` 或 `pip install pypdf`。目前已导入两份：反向钟《Celeste 机制详解》（125 页，源码解读与机制详解，文字版约 240 KB）与《岩浆块机制说明》。只重新生成索引：`tools\fetch_reference.py --index-only`。准备阶段会要求智能体把这些文字版也读完，整份知识库约 34 万 token。

## 3. 桥接 Mod：构建、安装与测试

桥接 Mod（CelesteAIBridge）运行在游戏进程内，只监听本机 `127.0.0.1:47774`。未接管时游戏照常可玩；测试客户端发送 `activate` 后，游戏冻结在当前帧，只有收到 `step` 才逐帧推进。

### 3.1 构建与安装

先关闭游戏。如果 .NET SDK 没有加入 PATH，先执行 `$env:PATH = "<SDK 目录>;$env:PATH"`。然后在仓库根目录的 PowerShell 中执行：

```powershell
.\tools\deploy_bridge.ps1 -CelesteDir "D:\Fluency\software\steam\steamapps\common\Celeste"
```

脚本会编译 `game-bridge/`，再把 `everest.yaml` 与 `bin\CelesteAIBridge.dll` 复制到游戏的 `Mods\CelesteAIBridge\`。启动游戏后，在 `log.txt` 中能看到 `[CelesteAIBridge] loaded`。

### 3.2 冒烟测试

1. 启动游戏，停在标题画面即可。执行 `python tools\enter_save.py test --leave-active`，菜单宏会自动选中 `test` 存档、进入关卡、跳过开场，停在 Madeline 可以操作的第一帧（实测约 7 秒）。
2. 在仓库根目录执行 `python tools\bridge_client.py smoke --out-dir smoke`。
3. 通过的标准：冻结 1 秒帧号不变；单帧推进恰好 1 帧；向右走并起跳 51 帧后有轨迹与截图；最后自动交还控制。截图保存在 `smoke\` 里。

### 3.3 测试客户端常用命令

| 命令 | 作用 |
| --- | --- |
| `python tools\bridge_client.py hello` | 查看桥接版本与当前场景 |
| `python tools\bridge_client.py activate` | 接管并冻结在当前帧 |
| `python tools\bridge_client.py observe --out shot.png` | 读取当前帧的状态、实体、字符地图与截图 |
| `python tools\bridge_client.py step "30,R" "1,R,J" "20,R" --stop death,room --out after.png` | 按帧推进，遇到死亡或换房提前停下 |
| `python tools\bridge_client.py advance --until controllable --max 600` | 空输入推进，直到恢复可操作（过场、重生时用） |
| `python tools\bridge_client.py skip` | 调试用：直接跳过当前剧情，不经过暂停菜单（主控与菜单宏都改用暂停菜单跳过） |
| `python tools\bridge_client.py deactivate` | 交还控制，游戏恢复正常运行 |
| `python tools\enter_save.py <存档名>` | 从标题画面进入该存档的关卡，用暂停菜单跳过开场，停在可操作的第一帧；加 `--chapter 0` 指定章节（0 序章、1 被遗弃之城、2 旧址），加 `--leave-active` 保持接管 |

按键写法：`L R U D` 方向，`J` 跳，`X` 冲刺，`Z` 蹲冲，`G` 抓，`T` 交互，`C` 确认，`B` 返回，`P` 暂停，`N` 日志；`A:角度` 表示左摇杆模拟方向（0 为右，90 为上）。每段以帧数开头，例如 `"6,R,J"` 表示按住右与跳 6 帧。

## 4. 运行智能体

### 4.1 让智能体游玩一个章节（可用）

1. 启动 Docker Desktop 与游戏，游戏停在标题画面或任意菜单即可（使用只启用 CelesteAIBridge 的 AIPlay 配置）。
2. 在仓库根目录执行：

   ```powershell
   .\.venv\Scripts\python.exe -m orchestrator.run --agent opus --save test --model opus --chapter 0 --max-minutes 20
   ```

3. 主控会依次：用菜单宏进入 `test` 存档、跳过开场；接管游戏并核对存档名；在 `127.0.0.1:47780` 启动游戏工具服务；创建或复用智能体容器 `celeste-agent-<标签>`，在容器里启动 Claude Code（无头模式）。智能体先进入**准备阶段**：按顺序把知识库从头到尾读一遍（见下文），整理笔记，然后才开始游玩。章节完成、时间预算用完或智能体连续 3 轮不调用工具时结束，并交还游戏控制。
4. 另开一个 PowerShell 查看进度：`.\.venv\Scripts\python.exe tools\run_status.py`（加 `-n 30` 显示更多决策）。

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--agent` | 必填 | 智能体标签，决定容器 `celeste-agent-<标签>`、工作区 `workspaces/<标签>/` 与日志目录 `runs/<标签>/`。同一个模型在 test 存档准备、正式存档挑战时用同一个标签，准备好的程序就能带进正式挑战 |
| `--save` | 必填 | 游戏内存档名；与当前存档不一致时拒绝开始 |
| `--model` | `opus` | Claude Code 模型别名，如 `opus`、`fable` |
| `--effort` | `high` | 推理强度：`low`、`medium`、`high`、`xhigh`、`max` |
| `--max-minutes` | 240 | 现实时间上限，从智能体第一次真正执行游戏工具时开始计（准备阶段不算）；主要指标是游戏内章节计时，这里只防止无限运行 |
| `--study-minutes` | 60 | 准备阶段（读知识库、写笔记与程序）的上限，不计入现实用时；到时自动开始计时并开放游戏工具 |
| `--no-study` | 关 | 跳过准备阶段，直接开始游玩（调试用） |
| `--max-usage` | 0.9 | Max 套餐 5 小时或 7 天窗口的已用比例达到该值就停止本次运行（同一账号还要留给其他会话）；用量来自 Claude Code 输出流里的限额事件 |
| `--chapter` | 选章页当前章节 | 菜单宏进入的章节：0 序章、1 被遗弃之城、2 旧址 |
| `--skip-enter` | 关 | 已在关卡内可操作时跳过菜单宏 |
| `--no-agent` | 关 | 只启动游戏工具服务、不启动智能体，用于手动调试 |
| `--runtime` | `container` | `container`：智能体在容器里运行；`host`：在本机运行，只开放两个游戏工具（M2 的过渡方式，不需要 Docker） |
| `--autocompact` | Claude Code 默认 | 上下文自动压缩的窗口：`auto`，或 100000～1000000 的 token 数 |
| `--play-speed` / `--auto-speed` | 1 / 1 | 智能体动作段与转场、剧情、重生段的推进速度（每次渲染推进几帧）。1 与原版一致；大于 1 是快进，只用于调试，录像会失真 |

容器里的智能体：

- **工具**：Bash、Read、Write、Edit、Glob、Grep 与两个游戏工具。WebSearch、WebFetch 在托管配置里被拒绝；Claude Code 以受限模式启动，不读取任何用户、项目或本地设置文件，文件工具只能访问 `/workspace` 与 `/kb`。
- **网络**：容器出站只放行 Anthropic 官方域名与宿主机上的游戏工具端口；Bash 命令（智能体自写的程序）在零网络的沙箱里运行，连 Anthropic 也连不上。验收方法见 2.4 节。
- **工作区** `/workspace`：对应宿主机的 `workspaces\<标签>\`，跨房间、跨章节、跨运行保留。每次观测与动作后，主控把完整观测（`game/latest_obs.json`）、上一次动作的全精度逐帧轨迹（`game/last_act.json`）与每个房间的首次观测（`game/rooms/`）写进来，供智能体的程序读取。
- **知识库** `/kb`：只读挂载仓库的 `kb/`，记录物理常量、状态机、实体规则、全部技巧与机制库、工作区数据格式，以及 `reference/` 里的社区资料原文（见 2.5 节）。
- **计划文件**：智能体可以把程序算出的按键序列写成 JSON 文件，调用 `act` 时传 `plan_file`，不必把按键抄进参数。
- 主控在每轮开始时核对智能体拿到的工具列表，出现上面以外的工具会立即停止。

`--runtime host` 时（M2 的过渡方式）：Claude Code 直接在本机运行，以 `--tools ""` 关闭全部内置工具，只能调用两个游戏工具；使用独立配置目录 `agent-home/<标签>/`，并关闭 CLAUDE.md 与自动记忆。

准备阶段（读知识库）：

- 第一条消息按顺序列出要读的文件：`kb/` 下的全部文件，加上 `reference/` 里的 wiki 页面与草莓酱英文原文（中文版是同一内容的翻译，不要求读），目前共 27 个文件。
- 主控从 Claude Code 输出流里 Read 工具的结构化结果（起始行、行数、总行数）逐行核对读过哪些行，不依赖模型自己汇报。长文件被 Read 按长度截断时，截断之后的行不算读过。
- 读完之前，`observe` 与 `act` 不执行，只返回"还有哪些文件的哪几行没读"；智能体一轮结束时还没读完，主控就把这份清单发给它。
- 准备阶段单独计时（`metrics.json` 的 `study_s`、`kb_progress`），不计入现实用时；第一次成功执行游戏工具时开始计现实用时。超过 `--study-minutes` 时自动开始计时并开放游戏工具。
- 结束后 `summary.json` 的 `kb_study` 记录每个文件读了多少行。
- Opus 5.5 与 Fable 5.1 在 Claude Code 里的上下文窗口都是 100 万 token（在容器里用 `/context` 实测），整份知识库约 24 万 token，读完后还有约 72 万 token 才会触发自动压缩。

2026-10-22 用 Haiku 5.5 测试准备阶段：27 个文件全部读完，用时 35 秒（并行读取），上下文约 24.4 万 token；读完后智能体写好 `notes.md` 再调用 `observe`，主控在这时开始计时，时间预算按预期生效。

非可控段由主控处理，节奏与原版游戏一致：

- 房间转场、死亡动画与重生：按原速播放（转场约 39 帧，死亡到重生约 103 帧）。
- 能跳过的剧情：像玩家一样按暂停键，停留约 0.3 秒后在菜单里下移到"跳过剧情"，再停留约 0.17 秒后确认；序章开场同样在它的暂停菜单里选"跳过"。录像里会出现暂停菜单与跳过时的转场。
- 不能跳过的剧情：按原速播放，对话框等待翻页时按确认键。
- 智能体只能按游玩按键（方向、跳、冲刺、抓、交互），暂停、确认、返回等菜单键只由主控使用。

两个游戏工具：

| 工具 | 作用 |
| --- | --- |
| `observe` | 不推进游戏，返回当前帧截图（放大 2 倍）、文字状态，以及整个房间的精确数据（JSON）：地形碰撞箱矩形、出口、所有实体的碰撞箱与状态、致死区、触发器、重生点、Madeline 的完整内部状态；另附带行列号的字符地图作为直观参考 |
| `act` | 按 `"帧数,按键"` 序列（或工作区里的计划文件）推进游戏，可设置截断条件（死亡、换房、落地等）；返回逐帧轨迹取样与新的观测；进入新房间时附截图与整个房间的精确数据（同 `observe`），其余时候只描述画面附近。转场、剧情、死亡与重生由主控自动推进。计划文件可以附预期轨迹（偏离超过容差就在那一帧停下）与重算命令（死亡或偏离时由主控自动重算重试，见下文） |

按键：方向 `L R U D`，跳 `J`，冲刺 `X`，蹲冲 `Z`，抓 `G`，交互 `T`，摇杆角度 `A:角度`；`K`、`Y`、`V` 是第二个跳、冲刺、蹲冲键（同一动作绑两个键，按住一个时按下另一个也算新的按下，相邻两帧可以各跳一次）。

分层执行（M4）：

- **计划文件**：除了按键段数组，还可以写成对象 `{"inputs", "expect", "tolerance", "stop_on", "retry"}`，格式见 `kb/game_data.md` §3。
- **预期轨迹与偏离停止**：`expect` 是智能体的模拟器预测的逐帧位置（可附状态名）。桥接每帧推进后立即对照，x 或 y 偏差超过 `tolerance` 像素（或状态不符）就在这一帧停下，停止原因 `deviate`，返回第几帧、预期与实际。
- **自动重试**：`retry` 声明一个重算命令。遇到死亡（先推进到重生）、偏离预期、或按键用完仍在本房间时，主控在容器的零网络沙箱里运行它；它读 `game/retry.json` 与最新观测、从当前状态重算并改写同一个计划文件，主控接着执行，最多 5 次，期间不唤醒智能体。命令出错、超时（最长 60 秒）、没改动计划或新计划不合法时停止，把经过与错误输出返回给智能体。重算用时计入工具时间，次数记在 `metrics.json` 的 `retries`。

M4 验收（2026-10-22，`test` 存档 1A，桥接 0.6.0）：`python calibration\m4_acceptance.py --agent dev-haiku` 全部通过——

| 项目 | 结果 |
| --- | --- |
| 第二套按键 | 按住 J 落地后不再起跳，此时按下 K 立即起跳；Y 冲刺、V 蹲冲（按下后碰撞箱变为 8×6）生效 |
| 预期轨迹 | 录下一段 71 帧的原地跳，再以 0.01 像素容差回放：整段执行完、无偏差；把第 15 帧的预期改动 2 像素：恰好在第 15 帧停下并报出偏差；第 5 帧写错状态名：在第 5 帧停下；预期比按键长：拒绝执行 |
| 自动重试 | 偏离 → 运行重算程序（0.2 秒）→ 执行新计划直到结束；重算程序里访问网络失败；重算程序出错（退出码 3）时停止重试并带回错误输出；重算程序超过时限被结束（2.2 秒） |
| 隔离验收 | `python -m orchestrator.verify --agent opus` 新增"重算程序沙箱"一项，全部通过 |

M4 实测（2026-10-22，容器方式，Opus 5.5，推理强度 high，`test` 存档 1A 从房间 6 开始，现实时间预算 20 分钟）：

| 项目 | 结果 |
| --- | --- |
| 准备阶段 | 27 个文件 40 秒读完；随后写出约 40 KB 的逐帧模拟器、束搜索与按房间配置的规划脚本，共 598 秒（不计入用时） |
| 游玩 | 通过房间 6、6a、6b、6c 并进入下一个房间；14 次决策，死亡 2 次；其中 6 次用计划文件执行，计划里附了模拟器预测的逐帧轨迹 |
| 模拟器精度 | 智能体记录：房间 6、6a 的两份计划全程零偏离（最长一份 212 帧）；几次偏离都发生在计划第 1 帧（吃水晶后的冻结帧、换房后的头几帧），它随后在计划前补等待帧解决 |
| 自动重试 | 触发 1 次：偏离后运行它的重算命令（17 秒），命令没有改动计划，主控按设计停止重试并交回 |
| 用时 | 现实 1230 秒：思考 983、工具 17、游戏推进 21 |
| 上下文与用量 | 最终约 41 万 token，没有触发压缩；Max 5 小时窗口 66% → 82%（含准备阶段），7 天窗口 66% → 67% |

M3 实测（2026-10-22，容器方式，Opus 5.5，推理强度 high，`test` 存档）：

| 场景 | 结果 |
| --- | --- |
| 序章（从第 2 个房间开始） | 136 秒通关，10 次决策，死亡 2 次 |
| 1A，预算 20 分钟，自动压缩窗口 `--autocompact 100000` | 推进到房间 6a；54 次决策、死亡 6 次；思考 1152 秒、游戏推进 74 秒、工具 2 秒 |
| 上下文压缩 | 约 6.9 万 token 时自动触发，共 4 次，每次压到 3300～6000 token、用时 20～26 秒；压缩后继续正常游玩，主控计数正确 |

两局里智能体都没有自己写程序，只在工作区留下了笔记 `notes.md`；思考占用时的九成以上，这是第四阶段（分层执行）要解决的问题。

M2 实测（2026-10-22，本机方式，Opus 5.5，推理强度 high，`test` 存档序章）：4 分 39 秒通关，21 次决策，死亡 3 次；其中思考 215 秒、游戏推进 58 秒、工具开销 0.8 秒；最终上下文约 7.2 万 token。Max 用量：这一局约占 5 小时窗口的 2%。

### 4.2 HUD：右上角信息卡片（可用）

HUD 是一个本地网页，显示智能体名称与状态、当前房间、章节计时（主计时）、思考与工具总时间、对话上下文、压缩次数、现实时间预算和当前思考。同一个页面既放进置顶窗口供现场观看，也作为 OBS 浏览器源进入录像。

- **服务**：主控运行时会在进程内启动 HUD 服务：`http://127.0.0.1:47790/hud`（`--hud-port` 改端口，`--no-hud` 关闭）。也可以单独启动：`.\.venv\Scripts\python.exe -m overlay.server`。它总是显示最近一次运行（`runs\latest.txt`），换一局不用刷新页面；`--run <运行目录>` 可以固定显示某一局。
- **置顶窗口**：`.\.venv\Scripts\python.exe -m overlay.window`（需要 `pip install -r requirements.txt` 装上 pywebview，使用系统自带的 WebView2）。窗口透明、无边框、置顶、点击穿透、不进任务栏、不抢焦点；贴在游戏窗口客户区右上角，宽度为游戏窗口的 22%、边距 2%，随游戏窗口移动与缩放；游戏最小化时隐藏。游戏要用窗口化或无边框全屏，独占全屏盖不上去。
- **OBS**：`tools\obs_setup.py` 建的场景里已经有 HUD（见 4.4 节）。它用"本地文件"方式打开 `overlay\hud.html`，页面再去连 HUD 服务取数据，所以 OBS 比主控先启动也没关系。自己加时：添加"浏览器"来源，勾选本地文件选这个页面（或 URL 填 `http://127.0.0.1:47790/hud`，但 OBS 启动时服务必须已在运行），1080p 下宽 430、高 344，放在右上角（右、上各留约 38 像素）。页面背景透明，不用额外设置。
- **实时性**：执行中桥接每 6 帧（0.1 秒）推送一次章节计时、冲刺次数与房间，页面每 0.1 秒取一次数据并在两次之间按实时外推，所以主计时逐帧走动、左侧色条随冲刺状态变色；思考期间思考时间持续走动。思考语句每 3 秒检查一次，有新语句才淡入替换。
- **配置**：配色、名称与参数在 `configs\hud.json`；本机想改的项写进 `configs\local.hud.json`（不入库），会覆盖同名项。

| 区域 | 内容 |
| --- | --- |
| 页眉 | 智能体名称（按 `configs\hud.json` 的 `names` 映射，如 Opus 5.5）、状态（准备中、思考中、执行中、自动推进、自动重试、压缩上下文、已完成）、章节与房间（如 1A · 6c） |
| 主计时 | 章节计时，精确到毫秒 |
| 统计 | 思考总时间、工具调用总时间、对话上下文（进度条按自动压缩阈值，达到 90% 变橙）、压缩次数 |
| 时间预算 | 已用现实时间 / 预算，用到 85% 变橙；准备阶段改为显示知识库阅读进度与准备用时（不计时） |
| 思考 | 当前思考语句，最多两行 |
| 左侧色条 | Madeline 的发色：一次冲刺绯红、冲刺用尽冰蓝、两次冲刺粉色 |

M5 验收（2026-10-23，桥接 0.7.0）：`python calibration\m5_hud_demo.py` 不调用模型，按"思考 3 秒 → 执行"的节奏驱动游戏三轮，全部通过。执行中每轮收到 8～11 次进度推送，章节计时随之前进；连拍置顶窗口：思考时主计时静止、思考时间走动，执行时主计时逐帧前进，向上冲刺时色条变成冰蓝、落地恢复绯红，思考语句按 3 秒间隔淡入。窗口启动前后前台窗口不变（不抢焦点）。

### 4.3 重放一次运行（不调用模型）

按某次运行记录下的决策逐条重放，用来复现问题或检查主控、桥接的改动是否影响游戏结果：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.replay runs\dev-opus\20261022-200717 --save test --chapter 0
```

每条决策会打印停止原因与实际帧数，并与原记录比对（"一致"或"不同"）。重放的日志写在 `runs\replay\<时间戳>\`。实测：重放 Opus 的序章记录，21 次决策逐帧一致，两段剧情都通过暂停菜单跳过。

### 4.4 录制：OBS 只录游玩帧（可用）

录制时 OBS 的每个视频帧都向游戏要一帧：游戏推进一帧、画到屏幕上，OBS 采下这一帧；智能体思考、游戏停住时 OBS 一帧也不写。成片的帧数等于游玩帧数，声音也只取游玩帧对应的那 1/60 秒，思考时间在成片里完全不存在。HUD 作为场景里的浏览器源一起录进去。

**一次性准备**

1. 安装 OBS 32（Steam 版即可）与 ffmpeg（`winget install Gyan.FFmpeg`）。
2. 构建并安装锁步插件（先关闭 OBS；不需要 Visual Studio，脚本会用 pip 装 zig 交叉编译）：

   ```powershell
   .\.venv\Scripts\python.exe tools\build_obs_plugin.py --obs-dir "D:\path\to\OBS Studio"
   ```

   OBS 更新后要重新构建，并重跑下面的校准测试。
3. 本机设置写进 `configs\local.recording.json`（不入库，只写要改的键；默认值见 `configs\recording.json`）：

   ```json
   {
     "obs_dir": "D:\\path\\to\\OBS Studio",
     "ffmpeg": "C:\\Users\\<你>\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe",
     "video_args": "-c:v h264_nvenc -preset p5 -tune hq -rc constqp -qp 16 -g 120",
     "audio_offset_ms": 130
   }
   ```

   `video_args` 是 ffmpeg 的视频编码参数：有 NVIDIA 显卡用上面的 NVENC，否则保留默认的 libx264。`audio_offset_ms` 只在某段量不到声音延迟时作为后备（见下）。

**每次录制**

```powershell
# 1. 启动游戏（桥接 0.8.3 以上），再启动 OBS：自动建好专用的配置文件与场景集合 CelesteAI 并最小化到托盘
.\.venv\Scripts\python.exe tools\obs_setup.py launch
# 2. 运行主控时加 --record
.\.venv\Scripts\python.exe -m orchestrator.run --agent opus5.5 --save opus5.5 --model opus --record
# 3. 结束后关闭 OBS，并把 OBS 下次打开时选中的配置还原成你原来的
.\.venv\Scripts\python.exe tools\obs_setup.py stop
```

- `obs_setup.py` 不动你自己的配置文件与场景：CelesteAI 配置文件是 1920×1080、60 fps、NV12、48 kHz；场景里只有游戏采集、应用音频捕获（只录 Celeste.exe）与 HUD 浏览器源，没有桌面音频和麦克风。场景集合已存在时不改动（你在 OBS 里的调整会保留），`setup --reset` 重建（原来的先备份）。`status` 查看插件是否加载、是否连上桥接。
- 主控接管游戏后开始录制，结束（通关、预算用完或中断）时先停止录制、等 ffmpeg 收尾，再交还游戏控制；结果写进 `summary.json` 的 `recording`。
- 录制期间动作块按 60 fps 实时播放（节奏由 OBS 决定），`--play-speed` 不起作用。
- 其他参数：`--ffmpeg`、`--record-video-args`、`--audio-offset-ms` 临时覆盖配置；`--record-marker` 在画面左下角画帧号条码（只用于校准）。

**产出**（`runs\<智能体>\<时间>\recording\`）

| 文件 | 内容 |
| --- | --- |
| gameplay.mkv | 成片：画面 + 声音（AAC 256 kbps） |
| video.mkv、audio.wav | 只有画面、只有声音（32 位浮点，每帧 800 个样本） |
| frames.csv | 每个成片帧对应的游戏帧号、章节计时与 OBS 时间戳 |
| segments.csv | 连续推进的各段，以及每段开头量到的声音延迟 |
| recording.json | 结果摘要：帧数、缺帧与重复检查、声音对齐情况 |

**声音怎么对齐**：OBS 采到的声音比画面晚 110～170 毫秒，而且录制中途 OBS 增加音频缓冲时会突然变化约 70 毫秒，不能用一个固定值。桥接在游戏停住超过 60 毫秒时暂停全部声音，所以每段动作开头的声音都从完全静音恢复；插件在每段开头找这条边，量出当时的延迟（取最近 5 次的中位数）用于这一段。

**校准测试**（游戏停在 test 存档的关卡里、Madeline 站在平地上，OBS 已启动）：

```powershell
.\.venv\Scripts\python.exe calibration\m6_recording_test.py --rounds 40 --think 0.7
```

不调用模型，模拟 40 轮"停 0.7 秒 → 推进约 1 秒"并录制，检查：成片帧数 = 推进的帧数、游戏帧号连续；每一帧画面里的帧号条码与记录一致（证明 OBS 采到的正是那一帧）；WAV 长度 = 帧数 × 800；每段都量到声音延迟、对齐误差不超过一帧；gameplay.mkv 含画面与声音。

M6 验收（2026-10-23，桥接 0.8.3，OBS 32.2.2，RTX 5080 + NVENC）：校准测试全部通过——2219 帧画面与条码逐帧一致，40 段全部量到声音延迟，对齐误差中位数 0 毫秒、最大 12.7 毫秒。实测 Opus 5.5 游玩 4 分 31 秒（15 次决策、死亡 4 次），成片 1633 帧（27.2 秒），帧号连续、无缺帧无重复，19 段中 15 段量到声音延迟（130～152 毫秒），HUD 一同录入。

### 4.5 正式挑战（部分未实现）

计划中的流程如下，全部由主控自动串联：

1. **准备阶段（不计时）**：在 `test` 存档上让智能体编写模拟器与规划程序并完成校准；`test` 存档可随时重置，不计成绩。
2. **正式挑战**：先在 Claude Code 里用 `/usage` 确认 Max 额度充足，最好在一个新的 5 小时窗口里开跑；然后启动游戏，用 `tools\obs_setup.py launch` 启动 OBS，启动主控并指定智能体（如 Opus 5.5）与对应的正式存档，加 `--record`。主控会为该存档启动独立容器并在其中运行 Claude Code。
3. 主控校验存档名与槽位，备份存档（未实现），开始录制。
4. 从正式存档进入序章时开始计时，依次通关序章、1A、2A（章节之间的衔接未实现）。成绩以游戏内章节计时为主，现实时间只设宽松上限。剧情能跳过的一律通过暂停菜单跳过。推理强度默认 high，可按房间难度动态调整。
5. 运行期间右上角 HUD 显示游戏时间、思考与工具时间、上下文大小、压缩次数、当前房间与思考语句，思考语句约每 3 秒刷新一次。
6. 通关或时间用完后停止录制，产出写入 `runs/`。

## 5. 查看结果

每次运行的产出在 `runs/<智能体>/<时间戳>/`（不入库），`runs/latest.txt` 记录最近一次运行的目录：

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| metrics.json | 实时统计：用时、思考时间、工具时间、游戏推进时间、决策数、死亡数、上下文大小、压缩次数、当前房间与思考语句、Max 用量；HUD 将读取这个文件 | 已实现 |
| decisions.jsonl | 每次 `act` 一行：思考语句、按键序列、截断条件、停止原因、实际帧数、事件、自动推进帧数 | 已实现 |
| frames.jsonl | 每个推进帧一行：帧号、按键、思考语句、所属决策、Madeline 位置速度状态；自动推进的帧标 `auto` | 已实现 |
| agent_events.jsonl | Claude Code 输出的全部事件（图片数据已省略），可复盘智能体的每一步 | 已实现 |
| summary.json | 结束时的汇总与结束原因 | 已实现 |
| system_prompt.md | 本次运行发给智能体的完整提示词 | 已实现 |
| claude_stderr.log、mcp.json、agent_cwd/ | Claude Code 的错误输出；本机方式下的工具服务配置与空工作目录 | 已实现 |
| recording/ | OBS 锁步录制的成片 gameplay.mkv 与对账文件（见 4.4 节） | 已实现（加 `--record`） |
| inputs.tas | 回放用的逐帧按键与系统事件 | 未实现 |
| manifest.json | 版本、配置与哈希 | 未实现 |

## 6. 常见问题

| 现象 | 处理 |
| --- | --- |
| `deploy_bridge.ps1` 提示文件被占用 | 游戏还在运行，关闭后重试 |
| 平时自己玩时少了很多 Mod | 运行 AI 时用的是 AIPlay 配置；在 CeleMod 里切回自己的配置即可 |
| 测试客户端连不上 | 确认游戏已启动且 `log.txt` 里有 `[CelesteAIBridge] loaded`；端口被占用时日志会提示 |
| 接管后按键没有反应 | 检查是否有其他 Mod 改了输入；建议用只启用 CelesteAIBridge 的 Mod 配置 |
| Steam 更新了游戏或 OBS 后出错 | 插件与 Mod 和版本绑定，需重新编译并重跑校准测试 |
| 存档进度不对 | 从 `runs/` 中的存档备份恢复，并确认已关闭云同步 |
| OBS 启动时弹出"安全模式"对话框 | 上次 OBS 没有正常退出。用 `tools\obs_setup.py launch` 启动会先清掉过时的崩溃标记；已弹出时选正常模式（安全模式不加载锁步插件） |
| `--record` 提示 OBS 插件没有连上桥接 | 先启动游戏再启动 OBS 都可以，插件每秒重连一次；用 `tools\obs_setup.py status` 看插件是否加载，没加载就重新运行 `tools\build_obs_plugin.py` |
| 录像里没有 HUD | 场景是旧版本建的（网址模式）：关闭 OBS 后运行 `tools\obs_setup.py setup --reset` 重建场景 |
| 运行中途停下，提示达到用量上限 | Max 额度触顶后无头运行不会自动等待；主控会暂停计时，额度恢复后续跑，成绩中注明 |
| 想用 Fable 5.1 | Max 上 Fable 另有用量限制，先在 Claude Code 的 `/model` 列表确认可用及计费方式 |
| 主控提示端口 47780 已被占用 | 另一个主控还在运行；结束它，或加 `--mcp-port 47781` |
| 主控提示存档名不一致 | 游戏当前进入的不是 `--save` 指定的存档；回到标题画面重新运行，菜单宏会自动选择 |
| 智能体卡住或想提前结束 | 在运行主控的窗口按 Ctrl+C；主控会交还游戏控制并写出 summary.json |
| 提示找不到 docker | Docker Desktop 没有启动，或装好后还没新开终端；启动 Docker Desktop 后新开 PowerShell 再试 |
| 想让智能体从零开始（清空它写的程序与笔记） | 删除 `workspaces\<标签>\`；会话记录在数据卷 `celeste-claude-<标签>` 里，可用 `docker volume rm` 删除（需先 `docker rm -f celeste-agent-<标签>`） |
| 改了 `agent-container/` 之后 | 重新执行 `docker build -t celeste-agent:0.1 agent-container`；主控发现镜像变了会自动重建容器，工作区保留 |
| PowerShell 里中文显示成乱码 | 日志文件本身是 UTF-8；用 `tools\run_status.py` 查看，或先执行 `chcp 65001` |
