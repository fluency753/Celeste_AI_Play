# 操作手册（给 AI 助手）

这份手册写给在用户电脑上替用户操作本系统的 AI 助手（例如 Claude Code、Codex）。你的角色是**操作者**：阅读文档、检查环境、部署、启动运行、在运行中监督与介入、结束后收集产出。你不是游戏里的智能体；游戏里的智能体在 Docker 容器里，由主控启动。

规则：

- 按章节顺序做。每一步先检查，再动手。
- 命令都在仓库根目录的 PowerShell 里执行。Python 一律用虚拟环境里的 `.\.venv\Scripts\python.exe`。
- 状态查询、构建、部署、启动分开执行，一条命令只做一件事，前一步失败就停下，不要接着做依赖它的步骤。
- 先读第 8 节"安全与礼节规则"，整个过程都要遵守。

## 1. 先读哪些文件

按顺序读：

1. `README.md`：项目是什么、组成、模式。
2. `docs/GUIDE.md`：用户视角的流程；第 0 节列出了必须由用户亲手做的事。
3. 本手册。
4. `docs/DEPLOY.md`：第 2 节（环境要求）、第 3 节（一键部署）、第 4 节（手动部署）、第 5 节（运行）、第 11 节（常见问题）。
5. 按需查阅：`docs/USAGE.md` 第 4.1 节（主控参数）、4.8 节（额度等待与中断续跑）、4.10 节（控制台）、4.11 节（可调参数）；`docs/BRIDGE_RELOAD.md`（不重启游戏更新桥接）；`docs/INCIDENT_20261024.md`（后台进程为什么要独立启动）。

读完后，用几句话向用户复述你接下来要做什么，等用户确认再开始。

## 2. 环境检查

只读检查，不改动任何东西：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check
```

- 没有 `.venv` 且找不到 Python 3.11+ 时，它只报"缺少"并退出。这时告诉用户需要安装 Python（`setup.ps1` 不带 `-Check` 运行时会提议用 winget 安装）。
- 有 `.venv` 后，也可以直接 `.\.venv\Scripts\python.exe tools\setup_wizard.py --check`。
- 输出里每一项是"就绪"或"缺少"。把缺少的项整理给用户，标明哪些你能处理、哪些要用户亲手做。
- 它的最后一步（checks）会用 `tools\bridge_client.py hello` 连一次游戏桥接。**有主控在运行时不要跑完整的 `-Check`**，改用 `-Only` 跳过 checks：`powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check -Only system,game,dotnet,bridge,docker,platform,recording,sim,kb,saves,switches`。

另外几个只读查询（分开执行）：

```powershell
docker version                                                         # Client 与 Server 都有版本号 = Docker 引擎在运行
docker image inspect celeste-agent:0.1                                 # 智能体镜像是否已构建
.\.venv\Scripts\python.exe tools\obs_setup.py status                   # OBS 是否在运行、锁步插件是否加载
.\.venv\Scripts\python.exe -X utf8 tools\restart_controller.py check   # 有没有在用的主控（读 HUD 服务，不连桥接）
```

`restart_controller.py check` 默认查 47792 端口的 HUD 服务；控制台和主控用的是 47790，查它时加 `--hud-port 47790`。它报"Connection refused"（退出码 2）表示那个端口上没有 HUD 服务，通常就是没有在用的运行，不是故障。最直接的判断是控制台顶栏的"主控"灯。

游戏的版本要求：桥接 Mod CelesteAIBridge 1.10.0（`game-bridge/everest.yaml`），依赖常驻核心 CelesteAIRuntime 1.0.5 与 EverestCore 1.5935.0；主控要求桥接 1.4.0 或更新。游戏启动后，游戏目录的 `log.txt` 里应有 `[CelesteAIBridge] loaded v1.10.0`，以及 `[CelesteAIRuntime]` 一行含 `input re-init guard on`。

## 3. 部署

### 3.1 推荐：交互式部署向导

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

向导是交互式的，会提问。逐步把问题转述给用户、按用户的回答作答；你无法代用户回答的（令牌、密钥、存档名、要不要录像）一律问用户。只重做某几步：`setup.ps1 -Only 步骤,...`（步骤名：system、game、dotnet、bridge、docker、platform、recording、sim、kb、saves、switches、checks）。

### 3.2 各部分的单独命令

向导做不完或需要单独重做时，按下面的顺序。每一步都是独立的一条命令。

1. **Python 依赖**：`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
2. **桥接 Mod 与常驻核心**（需要 .NET 8 SDK；**先确认游戏已关闭**）：

   ```powershell
   .\tools\deploy_bridge.ps1 -CelesteDir "<Celeste 安装目录>"
   ```

   脚本先把已安装的两个 Mod 备份到 `local\backups\<时间>-bridge-runtime-deploy\`，再安装。游戏开着时它会拒绝并报错。已安装相同常驻核心、只更新桥接逻辑时，可以不重启游戏：`-HotReload -HudPort <在用主控的 HUD 端口>`（脚本默认 47792；控制台与命令行启动的主控默认 47790），这一步经在用的主控进行，也要先得到用户同意，规则见 `docs/BRIDGE_RELOAD.md`。回滚：关游戏后 `-Rollback "<部署时打印的备份目录>"`。
3. **智能体镜像**（Docker 引擎要在运行，约 1～2 分钟）：

   ```powershell
   docker build -t celeste-agent:0.1 agent-container
   ```

4. **模型平台**（需要用户参与，见 3.3 节）：`powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Only platform`
5. **OBS 锁步插件**（**先确认 OBS 已关闭**。`.\.venv\Scripts\python.exe tools\obs_setup.py stop` 会正常关闭正在运行的 OBS，并把 OBS 的配置选择还原成用户原来的；用户可能正在用 OBS 直播或录像，关之前先问用户）：

   ```powershell
   .\.venv\Scripts\python.exe tools\build_obs_plugin.py
   ```

   找不到 OBS 时加 `--obs-dir "<OBS 安装目录>"`。录制参数写在 `configs\local.recording.json`（`setup.ps1 -Only recording` 会代写）。
6. **启动脚本**（可选）：`.\.venv\Scripts\python.exe tools\setup_wizard.py --only saves,switches`，生成本机的 `start_local.ps1`。
7. **容器隔离验收**（可选，Anthropic 平台、令牌配好后，约 2 分钟）：`.\.venv\Scripts\python.exe -m orchestrator.verify --agent <标签>`，应全部"通过"。
8. **控制台启动器**：仓库根目录的 `CelesteAIConsole.exe` 已在仓库里，不需要构建。它需要 `.venv` 存在。

### 3.3 必须由用户亲手做的部分

遇到这些时停下，告诉用户怎么做，等用户完成：

- 购买、安装《蔚蓝》；用 Olympus 安装 Everest 并启动一次游戏。
- 以管理员身份运行 `wsl --install --no-distribution` 并重启电脑；首次打开 Docker Desktop 接受协议。
- **登录与密钥**：
  - Anthropic：用户在浏览器里授权 `claude setup-token`，把令牌粘贴进向导（输入不显示）。令牌写进仓库根目录的 `.env`。不要让用户把令牌发给你，不要自己读出或打印它。
  - 第三方平台（如 DeepSeek）：用户在向导里给出密钥文件或粘贴密钥。密钥存在仓库目录之外（`%USERPROFILE%\.celeste-ai-play\`），`configs\local.providers.json` 只记文件位置。平台预设见 `configs\providers.json`。
  - Codex（GPT）：用户在浏览器完成设备登录：`.\.venv\Scripts\python.exe -m orchestrator.codex_container --account login`。这条命令会输出授权链接与代码，让用户自己打开。
- 在游戏里建测试存档（或在运行时用 `--create-save` 让主控新建）；建只启用 CelesteAIBridge 的 Mod 配置；在 Steam 里关闭云同步。
- 预置模拟器 `sim\dist` 不随仓库分发。用户有的话由用户提供；没有就不用（探索模式照常，速通模式运行 `--no-sim`）。

## 4. 启动运行

### 4.1 启动前检查

- Docker 引擎在运行（`docker version` 有 Server 版本）。
- 游戏开着，用只启用 CelesteAIBridge 的 Mod 配置，停在标题画面或任意菜单（主控会自己进存档）。
- 没有别的主控在运行（2 节的 `restart_controller.py check`，或控制台顶栏"主控"灯）。
- **用户已同意这次运行**，并确认了存档、章节、模式、时间上限、录不录像（见第 8 节）。

### 4.2 方式一：控制台（推荐给用户自己看）

```powershell
.\CelesteAIConsole.exe
```

或 `.\.venv\Scripts\python.exe -m overlay.console --browser`（用浏览器打开 `http://127.0.0.1:47790/console`）。用户在"存档与章节"页选存档与章节，在"运行"页选执行器、模型、模式与开关，点"开始运行"。控制台启动的主控在独立后台宿主里运行，关掉控制台窗口不影响它。页面底部显示等价的命令行。

### 4.3 方式二：命令行

**不要在你自己的命令工具里前台启动长时间运行的主控**：你的会话被关闭、回退或重建时，它的子进程可能被一起结束（见 `docs/INCIDENT_20261024.md`）。用独立后台宿主启动：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.background -- --agent dev-trial --save <测试存档名> --mode explore --chapter 0 --max-minutes 30 --no-record
```

它立即返回一行 JSON，`directory` 是宿主目录 `local\background\<启动时间>\`：`status.json` 记状态、PID 与退出码，`output.log` 是主控输出。同一时间只允许一个主控。

前台运行 `.\.venv\Scripts\python.exe -m orchestrator.run <参数>` 只适合很短的调试。

最小可用参数：`--agent <标签> --save <存档名>`，再加章节（`--chapter N`）。常用参数：

| 参数 | 作用 |
| --- | --- |
| `--agent <标签>` | 必填。决定容器 `celeste-agent-<标签>`、工作区 `workspaces\<标签>\`、日志 `runs\<标签>\`（探索模式的容器与工作区另加 `-explore`）。调试用 `dev-` 开头 |
| `--save <存档名>`、`--save-slot <槽位>` | 必填存档名；同名存档用槽位区分。存档名与游戏里的不一致时主控拒绝开始 |
| `--create-save` | 让主控新建存档（放在最新存档的下一个槽位，不覆盖已有存档）；只能从序章 `--chapter 0` 开始 |
| `--chapter N` / `--chapters 0,1,2` | 只玩一章 / 依次通关多章。可以选官方章节的 A/B/C 面 |
| `--mode explore` / `--mode speedrun` | 游玩模式。不写时 Claude Code 执行器默认速通，Codex 执行器默认探索 |
| `--engine claude` / `--engine codex` | 执行器，默认 claude |
| `--provider`、`--model`、`--effort` | 模型平台（默认 `anthropic`）、模型（默认 `opus`）、推理强度（默认 `high`） |
| `--max-minutes` | 现实时间安全上限，默认 1000 分钟（不含准备阶段与等待额度） |
| `--no-record` | 不录像（默认录：主控自己启动并在结束时关闭 OBS） |
| `--playback` | 打开规划回放（默认关） |
| `--sim-assist` | 探索模式的限额短路线预测（默认关，需要本机有 `sim\dist`） |
| `--no-sim` | 速通模式不开放预置模拟器 |
| `--resume <运行目录>` | 续跑（见 6 节） |

完整参数见 `.\.venv\Scripts\python.exe -m orchestrator.run --help`、`docs/USAGE.md` 4.1 节、`docs/DEPLOY.md` 6.1 节。可调参数用 `--tune 名称=值`，见 `docs/USAGE.md` 4.11 节（例如探索模式规划程序单次最长 `explore_program_s`，默认 120 秒；思考语句汉字上限 `thought_max_hanzi`，默认 30）。

规划回放只有在智能体把逐帧轨迹交给工作区的绘制接口 `show.trajectory()` 时才有内容。用户想要回放时，除了 `--playback`，还要在提示词里要求智能体主动写推演程序并适配这个接口（控制台"提示词"页的个人追加要求，保存在本机 `local\personal_prompts.json`）。

## 5. 运行中怎么监督

| 看什么 | 怎么看 |
| --- | --- |
| 当前运行目录 | `runs\latest.txt` 记最近一次运行的目录，即 `runs\<标签>\<时间戳>\` |
| 概况 | `.\.venv\Scripts\python.exe tools\run_status.py`（加 `-n 30` 多看决策，或直接给运行目录） |
| 实时统计 | 运行目录的 `metrics.json`：`status`（studying 读知识库、thinking、tool、executing、paused、net_wait 等）、用时、死亡、上下文、章节与房间、当前思考 |
| 每次动作 | `decisions.jsonl`（每次 `act` 一行：按键、停止原因、帧数、思考语句） |
| 智能体的完整过程 | `agent_events.jsonl` |
| 结束与成绩 | `summary.json`（结束原因、是否通关、`splits` 分段成绩、`total_time_ticks` 总计时）；运行中没有这个文件 |
| 后台主控的输出 | `local\background\<启动时间>\output.log` 与 `status.json` |
| HUD | 游戏画面右上角；或浏览器打开 `http://127.0.0.1:47790/hud` |
| 控制台 | "运行"页的"当前运行"与"对话" |

监督时只读这些文件，不要连游戏桥接（见第 8 节）。游戏长时间不动通常是智能体在思考；先看 `metrics.json` 的 `status` 和写入时间再判断。

## 6. 怎么介入

### 6.1 通过控制台（首选）

在控制台"运行"页：

- **暂停 / 继续**：等正在执行的动作走完后暂停，游戏保持冻结。
- **发消息**：右边"对话"框。Claude Code 执行器先停下当前这一轮再交给它；Codex 执行器插入当前这一轮。暂停时智能体只回复，不操作游戏。
- **停止并保存录像**：结束运行、收尾录像、交还游戏控制。
- **中断（保持冻结，可续跑）**：结束主控，游戏冻结在当前帧、章节计时不走，录像会话保留。
- **推理强度、剧情处理、操作权限、目标与停止条件、要通关的范围**：都可以在"当前运行"里实时改。

### 6.2 通过代码

控制台和 HUD 都是往运行目录的控制队列里写指令，你也可以直接写：

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from orchestrator.control import RunControl; print(RunControl(Path(r'runs\<标签>\<时间戳>')).enqueue('message', '请先 observe 再继续'))"
```

- `enqueue(kind, text)` 返回指令编号。常用 `kind`：`'message'`（发消息，1～4000 字）、`'pause'`、`'resume'`、`'interrupt'`（中断并保持冻结）、`'stop'`（停止并保存）、`'effort'`（推理强度档位）。
- 指令由主控读取并处理。执行结果用 `RunControl(运行目录).snapshot()` 查看：`connected` 表示主控是否在线，`actions` 里每条指令有 `status`（`queued` 等待、`waiting`、`submitted` 已处理、`error` 失败）与 `detail`。
- 主控未连接时写入的指令不会被处理。控制台会先检查 `connected`；你也应该先检查。
- 运行目录要写对：`RunControl` 在不存在的路径上会新建目录和控制数据库，指令会石沉大海。目录取自 `runs\latest.txt`。
- 只在用户要求或第 8 节允许的情况下发 `stop`、`interrupt`。

### 6.3 续跑

中断后游戏冻结在原处时可以续跑。Claude Code 执行器没通关就停下（时间上限到、出错、额度等待超限）时也保持冻结；Codex 执行器只有显式中断、模型服务故障后停止重试、自动停止条件触发时保持冻结，其余结束方式交还控制（见 `docs/USAGE.md` 4.8 节）。控制台"运行记录"里只有最近一次、游戏仍冻结的运行能续跑：

- 控制台"运行记录"里点**续跑**（原录像接不上时点"另起录像续跑"）。
- 命令行（用后台宿主启动）：

  ```powershell
  .\.venv\Scripts\python.exe -m orchestrator.background -- --agent <标签> --save <存档名> --resume runs\<标签>\<时间戳>
  ```

  模型、章节、模式、开关都沿用原运行；可以显式改 `--max-minutes` 等。`--fresh-session` 开新的智能体会话；`--note-file <文件>` 给智能体留一段话。
- 不续跑时，在控制台点**交还游戏控制**：先收尾录像，再让游戏恢复正常。

续跑的前提：游戏和 OBS 都没关。游戏关掉后章节进度丢失，不能续跑。

## 7. 结束后收集产出

运行目录 `runs\<标签>\<时间戳>\`：

| 产出 | 位置 |
| --- | --- |
| 录像 | `recording\`。默认每章一个子目录 `chapter-001\`、`chapter-002\`……，各有完整版 `gameplay.mp4`（含规划回放）与纯享版 `gameplay_pure.mp4`（只有游玩帧）；连续录制模式时直接在 `recording\` 下 |
| 成绩 | `summary.json` 的 `completed`、`splits`、`total_time_ticks`（单位是 100 纳秒的 tick，除以 10^7 得秒）、`deaths`、`stop_reason` |
| 存档备份 | `save_backup\`（开跑前的存档） |
| 发给智能体的提示词 | `system_prompt.md` |

旧录像补生成纯享版：`.\.venv\Scripts\python.exe tools\make_pure.py runs\<标签>\<时间戳>\recording`（分章录像时指向对应的 `chapter-NNN` 目录）。

向用户汇报：结束原因、是否通关、各章成绩、死亡数、录像路径。录像体积较大，不要自己移动、上传或删除。

## 8. 安全与礼节规则

**正式运行与用户同意**

- 每一次正式运行（会占用游戏、录像、消耗模型额度）都要先得到用户明确同意。说清楚：存档、章节、模式、模型、时间上限、录不录像。
- 部署后的第一次试玩，用测试存档、短时间上限（例如 30 分钟）、只玩一章。
- 续跑、停止、中断、交还游戏控制，也先问用户。用户明确交代过的情况除外。

**游戏连接**

- 桥接只接受一个连接，新连接会挤掉主控的连接。**有主控在运行时，不要运行任何会连接桥接的东西**：`tools\bridge_client.py`、`tools\enter_save.py`、`tools\dump_checkpoints.py`、`tools\sim_check.py`、`tools\reload_bridge.py`（桥接重载只按 `docs/BRIDGE_RELOAD.md` 经在用主控进行）、`setup.ps1` 的 checks 步骤（包括 `-Check` 不带 `-Only` 时）、`calibration\` 下的实机检查。
- 运行中只通过文件（`metrics.json` 等）、HUD、控制台和 `RunControl` 观察与介入。控制台本身也不连桥接，它读桥接写在共享内存里的状态。
- 不要直接覆盖在用的常驻核心，不要绕过主控另连桥接。

**运行期间不改环境**

- 运行期间不要 `git pull`、不要部署（桥接、OBS 插件、镜像）、不要跑 `orchestrator.verify`（它会创建或复用智能体容器）、不要改 `orchestrator\`、`kb\`、`configs\` 等仓库文件。等运行结束或中断后再做。控制台的一键更新在有主控运行时也会拒绝。唯一的例外是用户明确要求、按 `docs/BRIDGE_RELOAD.md` 经在用主控做的桥接受控重载。
- 运行期间不要关闭游戏、OBS 或 Docker Desktop；不要最小化游戏窗口（录像会出问题）。
- 不要在你自己的命令会话里前台跑长时间的主控，用后台宿主（4.3 节）。

**部署**

- 部署前备份被替换的文件。`deploy_bridge.ps1` 会自动备份到 `local\backups\`，`obs_setup.py setup --reset` 会先备份原场景集合；其他手动替换的文件先自己备份。
- 保留用户自己的 OBS 配置和 Mod 管理器配置。本项目的 OBS 设置都在单独的配置文件与场景集合 CelesteAI 里，不改用户自己的。
- 桥接部署要关闭游戏，OBS 插件安装要关闭 OBS。关闭前确认没有运行在用它们，并告诉用户。

**存档**

- 不碰用户自己的存档。调试、试玩一律用单独的测试存档，命令里显式写 `--save`（有同名存档时加 `--save-slot`）。
- 主控开跑前会把存档备份到运行目录的 `save_backup\`。需要恢复时先关游戏，并征得用户同意。

**密钥与令牌**

- 不打印、不复述、不上传 `.env`、`*.key`、`configs\local.providers.json` 指向的密钥文件，以及 Codex 登录缓存。需要确认是否配置好时，用 `setup.ps1 -Check`，它只报"就绪/缺少"。
- 不把密钥写进命令行参数、日志或仓库。不提交 `configs\local*`、`.env`、`local\`、`runs\`、`workspaces\`、`sim\`（`.gitignore` 已排除）。

**删除**

- 删除文件、目录、容器（`docker rm`）、数据卷（`docker volume rm`）、镜像之前，要用户逐项点名同意。数据卷里有智能体的会话记录，工作区里有它写的程序与笔记，删除后无法恢复。

**Docker Desktop**

- 先确认 Docker 引擎在运行：`docker version` 有 Server 版本号。没有时请用户从开始菜单打开 Docker Desktop，等左下角显示 Engine running。
- 刚装好 Docker 时，已打开的终端里可能找不到 `docker`，新开一个终端再试；主控也会到默认安装位置找。
- 改了 `agent-container\` 后要重新 `docker build`；主控发现镜像或平台设置变了会自动重建容器，工作区与会话数据卷保留。

**出问题时**

- 报告看到的现象与相关文件（`output.log`、`metrics.json` 的 `status`），说明你打算怎么做，等用户确认。
- 不要用强制结束进程、删除状态文件的办法"修复"。游戏冻结时先看能否续跑或交还控制。
- 常见问题对照 `docs/GUIDE.md` 第 7 节、`docs/DEPLOY.md` 第 11 节、`docs/USAGE.md` 第 7 节。
