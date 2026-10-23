# 使用说明

> **项目尚未完成。** 目前可以使用的是桥接 Mod（M1）、测试客户端与菜单宏，以及主控（M2）：能让智能体在本机独立游玩一个章节（见第 4.1 节）。其余步骤按最终流程编写，尚未实现的标注为"未实现"，每轮开发后同步更新。
>
> 最后更新：2026-10-22（M2）

## 1. 环境要求

| 项目 | 要求 | 备注 |
| --- | --- | --- |
| 操作系统 | Windows 10 2004 或更新，或 Windows 11 | OBS 的应用音频捕获需要这一版本 |
| 游戏 | Steam 版《蔚蓝》（Celeste）1.4.0.0 | 示例安装目录：`D:\Fluency\software\steam\steamapps\common\Celeste` |
| Mod 加载器 | Everest（.NET 8 版本，已在 6642 上验证编译） | 用 Olympus 或 CeleMod 安装 |
| 构建工具 | .NET 8 SDK | 编译桥接 Mod；可用官方脚本做用户级安装，无需管理员权限 |
| 运行环境 | Python 3.11 或更新（已在 3.14 上验证） | 主控与测试客户端；依赖装在仓库内的虚拟环境 `.venv` 里 |
| 录制 | OBS Studio 28 或更新 | 示例安装目录：`D:\Fluency\software\steam\steamapps\common\OBS Studio` |
| 容器 | Docker Desktop（WSL2 后端） | M3 起智能体（Claude Code）整体运行在容器中；M2 开发阶段暂不需要 |
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
| 11 | 构建智能体镜像：固定版本的 Claude Code、防火墙脚本、托管配置、非 root 用户 | 未实现 |
| 12 | 运行容器隔离测试：`claude auth status` 显示订阅令牌；访问非白名单网站失败；Bash 里访问任何网站都失败 | 未实现 |
| 13 | 生成并审核知识库（物理参数与机制） | 未实现 |

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
| `python tools\bridge_client.py skip` | 跳过当前剧情 |
| `python tools\bridge_client.py deactivate` | 交还控制，游戏恢复正常运行 |
| `python tools\enter_save.py <存档名>` | 从标题画面进入该存档的关卡，停在可操作的第一帧；加 `--leave-active` 保持接管 |

按键写法：`L R U D` 方向，`J` 跳，`X` 冲刺，`Z` 下冲刺，`G` 抓，`T` 交互，`C` 确认，`B` 返回，`P` 暂停，`N` 日志；`A:角度` 表示左摇杆模拟方向（0 为右，90 为上）。每段以帧数开头，例如 `"6,R,J"` 表示按住右与跳 6 帧。

## 4. 运行智能体

### 4.1 开发阶段：本机运行一个章节（可用）

1. 启动游戏，停在标题画面或任意菜单即可（使用只启用 CelesteAIBridge 的 AIPlay 配置）。
2. 在仓库根目录执行：

   ```powershell
   .\.venv\Scripts\python.exe -m orchestrator.run --agent dev-opus --save test --model opus --effort high --max-minutes 20
   ```

3. 主控会依次：用菜单宏进入 `test` 存档、跳过开场；接管游戏并核对存档名；在 `127.0.0.1:47780` 启动游戏工具服务；启动 Claude Code（无头模式）开始游玩。章节完成、时间预算用完或智能体连续 3 轮不调用工具时结束，并交还游戏控制。
4. 另开一个 PowerShell 查看进度：`.\.venv\Scripts\python.exe tools\run_status.py`（加 `-n 30` 显示更多决策）。

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--agent` | 必填 | 智能体标签，决定日志目录 `runs/<标签>/` 与独立配置目录 `agent-home/<标签>/` |
| `--save` | 必填 | 游戏内存档名；与当前存档不一致时拒绝开始 |
| `--model` | `opus` | Claude Code 模型别名，如 `opus`、`fable` |
| `--effort` | `high` | 推理强度：`low`、`medium`、`high`、`xhigh`、`max` |
| `--max-minutes` | 120 | 现实时间预算，从接管游戏开始计 |
| `--skip-enter` | 关 | 已在关卡内可操作时跳过菜单宏 |
| `--no-agent` | 关 | 只启动游戏工具服务、不启动智能体，用于手动调试 |
| `--play-speed` / `--auto-speed` | 1 / 4 | 智能体动作段与转场、剧情、重生段的推进速度（每次渲染推进几帧） |

隔离方式（M2 过渡做法，M3 改为容器）：Claude Code 以 `--tools ""` 关闭全部内置工具（不能执行命令、读写文件、联网），只加载本项目的游戏工具；子进程的环境变量里去掉了所有继承的 Claude 与 Anthropic 变量，使用独立配置目录 `agent-home/<标签>/`，并关闭 CLAUDE.md 与自动记忆。主控在智能体启动时检查它拿到的工具列表，出现游戏工具以外的工具会立即停止。

智能体能用的两个工具：

| 工具 | 作用 |
| --- | --- |
| `observe` | 不推进游戏，返回当前帧截图（放大 2 倍）与文字状态：Madeline 位置、速度、状态、碰撞箱与计时器，视野内实体，带行列号的字符地图 |
| `act` | 按 `"帧数,按键"` 序列推进游戏，可设置截断条件（死亡、换房、落地等）；返回逐帧轨迹取样与新的观测。转场、剧情、死亡与重生由主控自动推进 |

实测（2026-10-22，Opus 5.5，推理强度 high，`test` 存档序章）：4 分 39 秒通关，21 次决策，死亡 3 次；其中思考 215 秒、游戏推进 58 秒、工具开销 0.8 秒；最终上下文约 7.2 万 token。Max 用量：这一局约占 5 小时窗口的 2%。

### 4.2 正式挑战（部分未实现）

计划中的流程如下，全部由主控自动串联：

1. **准备阶段（不计时）**：在 `test` 存档上让智能体编写模拟器与规划程序并完成校准；`test` 存档可随时重置，不计成绩。
2. **正式挑战**：先在 Claude Code 里用 `/usage` 确认 Max 额度充足，最好在一个新的 5 小时窗口里开跑；然后启动游戏与 OBS，启动主控并指定智能体（如 Opus 5.5）与对应的正式存档。主控会为该存档启动独立容器并在其中运行 Claude Code。
3. 主控校验存档名与槽位，备份存档，开始录制。
4. 从正式存档进入序章时开始 2 小时计时，依次通关序章、1A、2A。剧情能跳过的一律自动跳过。推理强度默认 high，可按房间难度动态调整。
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
| claude_stderr.log、mcp.json、agent_cwd/ | Claude Code 的错误输出、工具服务配置、空的工作目录 | 已实现 |
| recording.mkv | OBS 录像，只含游玩帧 | 未实现 |
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
| 运行中途停下，提示达到用量上限 | Max 额度触顶后无头运行不会自动等待；主控会暂停计时，额度恢复后续跑，成绩中注明 |
| 想用 Fable 5.1 | Max 上 Fable 另有用量限制，先在 Claude Code 的 `/model` 列表确认可用及计费方式 |
| 主控提示端口 47780 已被占用 | 另一个主控还在运行；结束它，或加 `--mcp-port 47781` |
| 主控提示存档名不一致 | 游戏当前进入的不是 `--save` 指定的存档；回到标题画面重新运行，菜单宏会自动选择 |
| 智能体卡住或想提前结束 | 在运行主控的窗口按 Ctrl+C；主控会交还游戏控制并写出 summary.json |
| PowerShell 里中文显示成乱码 | 日志文件本身是 UTF-8；用 `tools\run_status.py` 查看，或先执行 `chcp 65001` |
