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
- 时长上限都由 `agent.bash_limits_ms` 决定，恢复、目标、游戏操作、录制和房间业务共用已有主控。
- 普通程序的网络、可写范围和凭据隔离边界保持一致。Codex Shell 包装与主控自动重试程序使用同一份 `agent-container/program_sandbox.py`；Claude 内置 Bash 仍由 Claude 的 sandbox-runtime 实现隔离。

Codex 钩子负责原生工具格式、会话／轮次身份、执行前后计量、可靠超时和进程收尾。托管权限及钩子文件归 root，原生程序不能修改。工作区固定标为 untrusted，不加载 agent 可写的项目配置、MCP 或项目钩子，与 Claude 的 restricted 入口对齐。Shell 采用非登录执行；固定版本 CLI 实测将自定义 shell 路径规整为可信解释器，钩子的归一化参数不含原始 shell 字段，不能将此说成钩子拒绝了该字段。升级 CLI 必须复验该边界。内层程序得到稳定 PATH 与临时 HOME；外层 HOME 不设为程序可写目录。App Server 使用独立 owner 标识，关闭时按 PID、启动时间和 UID 核验自身进程树，不用 `killall`，不影响同容器其他进程。

## 保留的差异

| 项目 | Claude Code | Codex |
| --- | --- | --- |
| agent 循环与压缩 | Claude Code 管理 | App Server 管理 |
| 原生编程工具 | Bash、Read、Write、Edit、Glob、Grep | Codex 原生 Shell、补丁和文件能力，具体名称由 CLI 提供 |
| 工具协议 | MCP 与 Claude 钩子 | App Server 事件、动态游戏工具与 Codex 钩子 |
| 图像交付 | 工具结果图像 | 将游戏图像送入同轮原生 image 通道，工具文字不序列化图像 |
| 知识库阅读核验 | Claude Read 结果中的完整行 | 匹配原生读取命令与模型实际收到的完整原文行；恢复时核验当前轮的容器会话记录 |
| 规划轮次分组 | 同一 assistant 消息内工具归为一组 | 当前协议按工具调用归组；不能用整个 turn 冒充一条消息而少计 |
| 普通文件查询 | Read／Glob／Grep 不属于 Bash | 单条 cat／sed -n／head／tail 和简单 ls／pwd 映射共享 Read；其余 Shell 按 Bash 检查、计次 |
| 订阅认证 | Claude 登录方式 | Codex 设备登录与独立缓存，刷新机制不同 |

这些是协议或产品设施差异，不据此维护不同的游戏规则。新会话不再登记 `celeste_bash/read/write`；旧会话仍可能保留其登记，调用时明确说明停用且不执行，引导使用原生工具。只读聊天可直接使用上述原生读取命令；复合命令、脚本和修改操作不视为 Read。

知识库计量只接受模型最终收到的工具文本，隐藏的内部 stdout、摘要、自报进度及被截断的半行不算已读。新建线程使用原始结果事件，恢复线程使用可信会话记录的增量最终结果；两路按调用去重。一次外层工具调用中混合多个读取无法唯一归属时不计量，应逐文件分页显示原文。

## 旧会话迁移

先停止该会话的主控，再在其容器中显式导入原 rollout：

```powershell
.\.venv\Scripts\python.exe -m orchestrator.codex_container --container NAME import-session --file <原会话.jsonl>
```

只导入该份会话记录，不复制宿主配置或其他会话。导入保留原文件，目标已存在时拒绝覆盖。之后按原运行目录恢复，继续同一会话 ID 和工作区；不存在或工具登记不完整时明确报错，不另建会话、不回落宿主执行。旧 `--runtime host` 对 Codex 规范为 container；host Claude 的既有含义不变。

`tools/sync_codex_session_env.py` 是迁移前的宿主记录维护工具，不得用于容器记录。容器会话在运行状态里记录 `session_storage=container`，不再用宿主归档目录推断其状态。容器会话也不因宿主存在同 ID 的归档副本而从 HUD 隐藏。

## 验证边界

回归使用隔离的模型／Docker边界；真实验收使用独立工作区与专用登录卷，不连接用户游戏、OBS 或在用主控。应分别检查原生创建与增量编辑、程序反馈、同会话恢复、门禁拒绝、超时、中断及容器进程清理、图像接收。不能以工具最终成功代替正常路径成功：模型重试绕过缺 PATH、登录 Shell 差异等失败，也必须修复并重验。

进程归属的独立验收：`python -m calibration.codex_container_ownership_check`，仅操作临时专用容器，结束后清理。长期 OAuth 刷新、完整真实关卡试玩和真实上下文压缩不属于本轮短验收覆盖范围。
