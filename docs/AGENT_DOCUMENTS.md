# Agent 文档与工具入口

## 来源与适用范围

| 源码目录 | 内容 | 挂载位置 |
| --- | --- | --- |
| `kb/shared/` | 两种模式共用的坐标、操作和完整工具参考 | `/kb/shared/` |
| `kb/explore/` | 探索索引、基本游戏规则和尝试提示 | 探索视图的 `/kb/` |
| `kb/speedrun/` | 速通索引、物理、实体、技巧、机制与搜索资料 | 速通视图的 `/kb/` |
| `kb/reference/` | 本机生成的速通社区原文，保留旧生成位置，不自动搬用户数据 | 仅速通的 `/kb/reference/` |

`orchestrator/kb_layout.py` 的文档清单统一决定来源、必读、兼容别名、控制台目录与挂载。探索默认不含速通知识；显式开放的指定文件才进入 `/kb/speedrun/`，仍为选读。

共享短入门是 `shared/data.md` 和 `shared/tools.md`；探索另外必读模式索引和 `basics.md`，速通保留自身知识准备。完整格式和高级输入均为选读，不因目录迁移变成必读。以每次准备消息的清单为准。

## 工具与相关文件

`interface_zh.md` 是短目录，不再同时堆放坐标、原始字段、输入语法和自动处理策略。它根据实际登记的工具和文件／程序能力，插入对应工具章节：

- `observe`：当前状态与坐标；`latest_obs.json`、`collision.json` 和历史 `rooms/`。完整观测、几何和数据文件格式在 `shared/tools/observe*`。
- `act`：显式输入与返回；计划文件、`last_act.json`、`retry.json`、`tas.py` 以及调用核验。完整输入、计划、TAS 与续输入在 `shared/tools/act.md`。
- `cutscene`：可见对白与选项；完整说明在 `shared/tools/cutscene.md`。
- `narrate`、`set_effort`、`stop_run` 及可选的 `exit_level`、`simulate`、`travel`：各自只说明当前职责，不列未注册的工具。
- 文件和程序工具：按执行器显示实际名称；工作区与程序参考分别在 `shared/tools/files.md`、`programs.md`。

没有文件能力时不要求查文件，工具声明提供详细参数；必要的布局／分页格式从同一份 `observe/geometry.md` 内联。参考被禁读时不提供对应入口。

## 提示词源码与动态工具名

`orchestrator/prompts/shared/` 保存通用提示；`modes/explore/`、`modes/speedrun/` 保存模式专属提示；`tools/` 保存工具章节；`buttons/` 保存消息正文。

文件名仍是编辑器与本机覆盖的稳定逻辑 ID，例如 `pause_zh.md`。`repo_part_path()` 将 ID 映射到新源码位置；原有 `local/prompts/pause_zh.md` 或 profile 下的同名覆盖继续优先使用，不自动搬动、拆分或重写。

受控模板和共享知识库用 `{tool:act}`、`{tool:Bash}` 等显式标记插入当前执行器名称；Codex 对应 `celeste_act` 和原生 Shell，Claude 对应 `act` 和 Bash。代码块、路径和 JSON 协议值保持字面值，个人追加正文不进入改名流程。旧模板的普通叙述兼容原有能力名，但不扫描受保护的引号字符串和代码块。程序时限参数仍由环境生成，不能只替换工具名而保留另一执行器的参数约定。

## 兼容、准备与快照

旧 `data.md`、`tools.md`、`help/actions.md` 及速通旧 `explore/help/actions.md` 等路径是同一文档的兼容别名；不给看任一路径即对所有别名生效，别名不重复计入必读。

旧知识库覆盖的源码路径继续识别，例如 `explore/tools.md`；新路径为 `shared/tools.md`。新旧路径存在多份覆盖时，读取和保存前明确报错，要求合并为一份，避免保存后才冲突或恢复后旧内容重新出现。追加资料可按旧名覆盖；用户文件不自动改写。

首次 `prepare_view()` 将本次文档生成到独立视图并固定在配置实例中。准备阅读、准备结果核验、容器标签和挂载使用同一快照；源文件之后变化不覆盖它，新的配置实例生成新快照。知识授权或工具环境变化需新的配置／会话。发布使用同目录临时目录与原子改名。

`.knowledge.json` 显式记录必读项及完整视图身份。准备结果同时保存实际阅读文件摘要和视图身份；可选授权变化也会使旧准备失效。新布局清单缺失、损坏或版本不符时明确拒绝，不按旧布局跳过校验。旧布局测试及旧格式的本机数据仍可识别，但旧准备不能冒充新布局的完整准备。
