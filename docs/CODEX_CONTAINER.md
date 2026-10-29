# Codex 容器执行

Codex App Server 与原生 Shell、补丁编辑工具均运行在智能体容器内。宿主主控通过 `docker exec -i` 的标准输入输出管理模型会话，游戏操作仍进入同一主控实现，不开放桥接端口、宿主文件系统或 Docker socket。新建、准备、聊天、取名、恢复和模型列表查询均使用容器设施；旧宿主会话维护脚本只用于迁移前记录。

## 安装与账号

```powershell
docker build -t celeste-agent:0.1 agent-container
.\.venv\Scripts\python.exe -m orchestrator.codex_container --account login
```

镜像固定 Codex CLI 版本，账号环境使用同一个 `ensure_container`，不另建容器管理器。设备登录在浏览器完成授权，容器内无需图形界面。已有文件形式的登录缓存也可显式导入：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.codex_container --account import-auth --file <auth.json>
.\.venv\Scripts\python.exe -m orchestrator.codex_container --account status
```

普通启动不读取宿主登录文件。工作区尚未登录时，从已显式初始化的账号环境引入缓存，已有工作区缓存不覆盖。账号卷和工作区 Codex 卷均不入库，程序不能读取凭据；模型进程保留访问模型平台所需的网络，自写程序无网络。缓存副本分别刷新，本轮短验收不保证长期并发刷新无冲突；遇到刷新失败，对具体容器执行 `--container NAME login` 重新授权，不自动退回宿主或覆盖其他登录。

官方支持 [设备登录及向 Docker 复制登录缓存](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)。这里使用 ChatGPT 订阅认证，不要求改用 API 计费。

## 与 Claude 共用的部分

- 容器创建、复用、重建、工作区与知识库挂载、CPU 限额沿用 `container.ensure_container`。
- 原生 Shell、补丁与图像读取钩子进入同一 `/gate`，共用 `tool_gate.decide_tool`、`SessionControls` 的暂停／目标守卫、`PauseGate` 和程序次数检查；游戏工具沿用主控分发与同一业务实现，不另维护 Codex 业务许可表。两边门禁失联或请求无效时均拒绝本次调用。
- 时长上限都由 `agent.bash_limits_ms` 决定：未写 `timeout_ms` 时用默认值（速通模式 `bash_default_s`），写了则不超过最大值，与 Claude 的 Bash 相同；恢复、目标、游戏操作、录制和房间业务共用已有主控。
- 原生 Shell 用 `/bin/bash -c` 执行，`set -euo pipefail`、`source`、`[[ ... ]]` 与 Claude 的 Bash 一致；主控自动重试程序沿用原来的 `/bin/sh`。
- 只读聊天与 Claude 一样保留文件编辑：`apply_patch` 走 Write 门禁并可修改 `/workspace`（例如整理 `notes.md`），原生 Shell 仍被同一门禁拒绝。
- 普通程序的网络、可写范围和凭据隔离边界保持一致。Codex Shell 包装与主控自动重试程序使用同一份 `agent-container/program_sandbox.py`；Claude 内置 Bash 仍由 Claude 的 sandbox-runtime 实现隔离。

Codex 钩子负责原生工具格式、会话／轮次身份、执行前后计量、可靠超时和进程收尾。托管权限及钩子文件归 root，原生程序不能修改。工作区固定标为 untrusted，不加载 agent 可写的项目配置、MCP 或项目钩子，与 Claude 的 restricted 入口对齐。Shell 采用非登录执行；固定版本 CLI 实测将自定义 shell 路径规整为可信解释器，钩子的归一化参数不含原始 shell 字段，不能将此说成钩子拒绝了该字段。升级 CLI 必须复验该边界。内层程序得到稳定 PATH 与临时 HOME；外层 HOME 不设为程序可写目录。App Server 使用独立 owner 标识，关闭时按 PID、启动时间和 UID 核验自身进程树，不用 `killall`，不影响同容器其他进程。

## 保留的差异

| 项目 | Claude Code | Codex |
| --- | --- | --- |
| agent 循环与压缩 | Claude Code 管理 | App Server 管理 |
| 原生编程工具 | Bash、Read、Write、Edit、Glob、Grep | Codex 原生 Shell、补丁，以及主控登记的 `celeste_read`／`celeste_glob`／`celeste_grep` |
| 工具协议 | MCP 与 Claude 钩子 | App Server 事件、动态游戏工具与 Codex 钩子 |
| 图像交付 | 工具结果图像 | 将游戏图像送入同轮原生 image 通道，工具文字不序列化图像 |
| 知识库阅读核验 | Claude Read 结果中的完整行 | 必须使用 celeste_read；完整行正文经同轮 steer 投递，Codex 把它记入历史后，用相同行元数据计量 |
| 规划轮次分组 | 同一 assistant 消息内工具归为一组 | 当前协议按工具调用归组；不能用整个 turn 冒充一条消息而少计 |
| 普通文件查询 | Read／Glob／Grep 不属于 Bash | celeste_read／celeste_glob／celeste_grep 走共享 Read／Glob／Grep 门禁，不算程序；原生 Shell（包括 cat、ls、grep）统一按 Bash 检查、计次 |
| 订阅认证 | Claude 登录方式 | Codex 设备登录与独立缓存，刷新机制不同 |

这些是协议或产品设施差异，不据此维护不同的游戏规则。新会话登记 `celeste_read`、`celeste_glob`、`celeste_grep`，原生 Shell 和补丁工具继续负责编程；旧 `celeste_bash/write` 返回停用说明，不执行。两边通过 Bash 读取都不计入 KB 阅读进度。只读聊天和用户暂停时这三个工具照常可用，不消耗探索模式的程序次数，也不会因此开放程序运行。

笔记提醒与直播解说提醒只放进模型确定会收到的地方：动态工具（含读取失败、投递失败和异常）的结果、新一轮的输入，或原生工具执行时经同轮 steer 投递并在 Codex 记入历史后才确认。原生工具钩子的 `additionalContext` 不再承载提醒；轮次中断导致投递丢弃时，提醒保持待发，直播计时放回。

## celeste_read

必读知识库必须显式使用 `celeste_read`。接口以 `file_path` 指定文件或目录，兼容旧参数 `path`（同时提供时必须一致）。例如：

```json
{"file_path":"/kb/README.md","offset":1,"limit":200}
```

- 文本：`offset` 从第 1 行开始，`limit` 为行数，默认 2000 行。返回行号、完整行范围和总行数；分页或字符预算截断时明确给出续读位置。空文件、超出文件末尾和超长单行都有说明。仅完整行计入 KB，片段不计；当前仓库 KB 的最长行低于单行预算。行边界与主控统计总行数一致（Python `splitlines()`）；行内其余控制字符（如 ESC、BEL）显示为 `\x1b` 这类转义，整行照常计数，不再整份拒读。含 NUL 的文件按二进制处理。
- 目录：按名称列出目录项及类型、大小，`offset/limit` 按目录项分页；符号链接只标注、不跟随。
- 图片：支持 PNG、JPEG、GIF、WebP，作为模型可见图像交付，最长边缩至 1280 像素。动画只显示首帧，透明背景合成为白色。
- PDF：提取文字并渲染页面图像，`pages` 使用 `"1-5"` 或 `"1-5,8"`，最多 20 页；省略时读取前 5 页。需要重新构建包含 Poppler 的镜像。
- Notebook：展示单元源码、文本与支持的图像输出；此时 `offset/limit` 按单元分页，不执行其中的代码。

输出预算是本项目的明确限制，并非逐字复刻 Claude Code 的 token/文件大小上限：单文件最多 32 MiB，单次文本最多 100000 字符，单张处理后图片最多 1 MiB，多图合计最多 4 MiB／20 张。预算不足会告知未返回范围；不把未返回部分算已读。

读取仅限 `/workspace`、`/kb`、`/sim`，采用只读、零网络沙箱，不开放宿主路径、登录卷或凭据。拒绝符号链接、硬链接和特殊文件，避免绕过文件权限或路径竞态；这些安全边界比 Claude 通用文件接口更窄。普通文本支持 UTF-8/BOM、带 BOM 的 UTF-16；不支持的编码和二进制格式明确报错，可在获准的 Bash 中使用相应格式工具处理。

所有读取与搜索结果（不只 KB）的正文和图像都经原轮次 `turn/steer` 交付，带工具调用、文件、范围标签和回执编号（`clientUserMessageId`）；工具返回只有确认摘要和续读位置，不重复整段正文或把图像序列化为编码文本。固定版本 CLI 实测：动态工具文本会按模型截断策略缩短（9.6 万字符只保留约 1.2–5 万，中段省略），steer 的用户输入原样保留，因此正文不能放在工具结果里。

`turn/steer` 的确认只表示输入已排队：固定版本实测在下一次模型请求前中断轮次时，排队输入被丢弃且不会出现在后续请求中。所以 KB 行只在 Codex 把这条输入记入历史（发出 `clientId` 等于回执编号的 `userMessage` 事件，或轮次正常完成）后才计量；轮次中断、失败或正在暂停时不计量，允许重试读取，不隐式执行或重做游戏动作。即使 code-mode 不打印工具返回，正文也不会被隐藏。已移除原生 Shell 的原文匹配、raw 回执和 rollout 尾读计量，只有这一个 KB 读取入口。

## celeste_glob 与 celeste_grep

与 Claude 的 Glob／Grep 对应，同一只读工作器、同样的根目录和逐级不跟随符号链接的遍历；不运行程序，不消耗程序次数，不计入 KB 阅读进度。

- `celeste_glob`：`pattern` 支持 `*`、`?`、`**`、`[...]`、`{a,b}`，相对 `path`（默认 `/workspace`）匹配完整相对路径；以 `/` 开头的模式自带起始目录。结果按修改时间从新到旧，`offset/limit` 分页（默认 250 条）。
- `celeste_grep`：`pattern` 为 Python 正则，`path` 为文件或目录，`glob` 过滤文件名（不含 `/` 时只比较文件名），`output_mode` 为 `files_with_matches`（默认）、`content`（带行号，可加 `context` 行上下文）或 `count`，`ignore_case` 忽略大小写。跳过二进制、硬链接和超过 8 MiB 的文件；遍历超过 10 万项或读取超过 256 MiB 时停止并说明结果可能不完整。

## 旧会话迁移

先停止该会话的主控，再在其容器中显式导入原 rollout：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.codex_container --container NAME import-session --file <原会话.jsonl>
```

只导入该份会话记录，不复制宿主配置或其他会话。导入保留原文件，目标已存在时拒绝覆盖。之后按原运行目录恢复，继续同一会话 ID 和工作区；不存在或工具登记不完整（包括缺少 `celeste_read`）时明确报错，不另建会话、不回落宿主执行。旧 `--runtime host` 对 Codex 规范为 container；host Claude 的既有含义不变。

动态工具登记随原会话持久化，固定版本协议的 `thread/resume` 也没有 `dynamicTools` 字段，新增接口不会在恢复时补入。缺少 `celeste_read`／`celeste_glob`／`celeste_grep` 的旧会话仍可恢复：主控记录日志，并在恢复后的第一条消息里说明哪些工具不可用；只有还要按行核验必读知识库（准备未完成）却没有 `celeste_read` 时才拒绝，须新建准备会话。具有旧 `path/offset/limit` 登记的会话可继续兼容读取（含目录列表），但新增 PDF `pages` 参数和搜索工具需要使用新建会话的完整登记。

`tools/sync_codex_session_env.py` 是迁移前的宿主记录维护工具，不得用于容器记录。容器会话在运行状态里记录 `session_storage=container`，不再用宿主归档目录推断其状态。容器会话也不因宿主存在同 ID 的归档副本而从 HUD 隐藏。

## 验证边界

回归使用隔离的模型／Docker边界；真实验收使用独立工作区与专用登录卷，不连接用户游戏、OBS 或在用主控。应分别检查原生创建与增量编辑、程序反馈、同会话恢复、门禁拒绝、超时、中断及容器进程清理、图像接收。不能以工具最终成功代替正常路径成功：模型重试绕过缺 PATH、登录 Shell 差异等失败，也必须修复并重验。

进程归属的独立验收：`python -m calibration.codex_container_ownership_check`，仅操作临时专用容器，结束后清理。长期 OAuth 刷新、完整真实关卡试玩和真实上下文压缩不属于本轮短验收覆盖范围。
