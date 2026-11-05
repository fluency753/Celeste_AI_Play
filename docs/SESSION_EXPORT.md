# 导出会话公开历史

长程行为评估使用增量分析模式（保留旧文件）：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/export_session.py --run runs/<agent>/<运行时间> --analysis --since "2026-11-03 23:02:46"
```

新增全部可观察工具索引、回合/统一时间线、动作/游戏状态证据、累计快照及房间工作量摘要。分析模式 README 为简明摘要，原完整可读记录在 public_text.md。详见 [分析字段与计量契约](SESSION_ANALYSIS_SCHEMA.md)。

同时导出指定时段与工作区成果：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/export_session.py --run "runs/<agent>/<运行时间>" --analysis --since "2026-11-04T01:40:41.730920+08:00" --until "2026-11-04T14:50:16.663815+08:00" --workspace "workspaces/<本机工作区>"
```

`--workspace` 自动生成 `workspace.json`、`git_changes.md`，以及可安全读取时的根目录 `notes.md`，随 ZIP 一并交付并纳入校验清单。`notes.md` 是导出时的脱敏投影；Git 历史按同一窗口筛选，当前未提交状态单独注明采样时间。历史工作区已清理时，可省略 `--workspace` 继续导出会话；工作区必须显式指定，以免把其他游玩实例的成果归给目标会话。

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/export_session.py --run runs/<agent>/<运行时间>
```

可选择同一会话的任一运行或 `chats/<会话目录>`，工具自动合并所属 `runs` 及对应项目 `local/hud-idle/chats` 中执行器、提供方和会话 ID 均相同的记录。准备聊天、存档取名的公开请求和回复也会一并导出。同名 agent、不同会话的记录不混入；自动发现的来源拒绝链接、重解析点和项目外路径。默认输出到新的 `local/exports/<会话ID>-<时间>/`；`--output <新目录>` 可指定位置，已有目录和源运行目录内的目标会被拒绝。

能通过已记录的来源关系唯一核对原运行时，聊天目录复制的历史日志前缀和控制请求只在原来源计算一次，聊天中新增的记录继续保留。`segments.copied_from` 给出核对到的原目录，避免将复制的旧动作当成新尝试。

只有会话 ID 时，可先执行 `rg -l -F "<会话ID>" runs -g run_state.json -g codex_session.json`，用匹配文件所在的运行目录作为 `--run`。要接续上次导出，可从旧 `session.json` 的 `public_text_filter` 取终点作为新起点，避免重复选入边界消息。

使用 `--since` / `--until` 按时间筛选**公开消息**，例如只保留一次 2A 游玩的可见消息：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/export_session.py --run runs/<agent>/<运行时间> --since "2026-11-03 23:02:46" --until "2026-11-04 01:00:00" --local-only
```

示例终点须替换为实际游玩结束时间。支持 ISO 日期时间（也可只写日期，表示当日零点），无时区按北京时间；显式 `Z` 或 UTC 偏移会换算为北京时间。包含起点、不包含终点，任一参数均可单独使用；起点不早于终点或格式无效时拒绝导出。先按原有来源优先级合并、去重，再按原始消息时间筛选，不会因较晚的回复副本将范围外消息带入；筛选时排除时间未知的消息并报告数量，不指定范围时仍保留未知时间消息。没有匹配消息也正常生成空的公开消息产物。

范围内的可见消息见 `public_text.csv`、`README.md` 的公开文本区和 `session.json.public_text`。**时间统计、模拟调用和额度仍覆盖整个会话**，不是该时段或 2A 的统计；JSON 的 `public_text_filter` 和 README 开头记录筛选范围及此区别。此功能不自动识别章节，跨恢复运行的同一会话消息仍能按时间选入。`--local-only` 只读本机日志，适合只需要公开消息时使用；无须暂停、重启主控或连接游戏。

产物：

- `README.md`：公开文本的完整时间线、时间统计、模拟调用表及来源缺口。
- `public_text.csv`：全量公开解说、动作说明、系统公开提示和用户消息，不受 HUD 最近条数限制。
- `simulations.csv`：模拟器的调用时间、耗时、候选输入、扣费回执、成本、余额与停止原因/帧数。
- `session.json`：上述内容、跨运行来源、分段累计统计与 `save_naming` 取名索引。索引保留各目录最后保存的请求 ID、名字和来源，原结果没有可靠时间时留空；同请求同名字合并来源，冲突名字保留并告警。
- `manifest.json`：生成文件的 SHA256；`export.zip` 仅打包生成产物。

Markdown/CSV 的时间默认显示为 `月-日 时:分:秒`，例如 `11-03 12:51:05`。`--time-format` 使用 Python `datetime.strftime` 的格式字符串，默认 `%m-%d %H:%M:%S`，例如 `--time-format "%Y-%m-%d %H:%M:%S.%f %z"` 可显示年份、微秒及 UTC 偏移。所有展示时间均为北京时间；JSON 继续保留完整 ISO 时间和可用的 epoch 精度。此参数不改变排序、耗时统计或唯一输出目录命名。

未知时间为空，不用导出时间或 1970 年代补齐。重复发生的相同文本仍保留；同一真实消息与回复副本去重，同一公开事件先后显示过的不同文本保留为更新历史。

公开文本来自 `public_events.jsonl`、缺失时的公开消息回退、`decisions.jsonl` 的公开 thought，以及以 SQLite 只读事务查询的控制消息。不会导出工具结果正文、模型内部推理、压缩摘要、图像编码或模拟器逐帧轨迹。仅能复原已保存的公开历史，不能恢复未写入日志的瞬时 HUD 状态或截图中的文字。

时间总计使用 `runs` 内排除准备段后的末次主控 `metrics.json` 快照，没有时才使用 `summary.json`，各次恢复的累计值不相加。`local/hud-idle/chats` 的快照可能复制自其他 HUD 锚点，因此通过 `source_kind=hud_idle_chat` 和 `metrics_authoritative=false` 标记；`runs` 内 `session_kind=ready` 的准备段标记为 `source_kind=preparation`。两者都不覆盖试玩累计统计或模拟器末次余额，也不用于恢复链快照差。只有准备聊天来源时累计字段留空。各时间类别可能重叠，不把类别求和当作总时长。HUD 主游戏时钟为 `game_clock.frames / 60`，章节计时为 ticks / 10000000；两者不同，保留原字段供核对。

`--since` / `--until` 同样筛选准备聊天与取名的公开消息。`save_naming` 属于会话元数据，时间未知的结果仍保留并标记 `window_membership=unknown`，不能据此判断命名发生在筛选时段内。

探索模式的模拟额度以 `sim_energy.json` 的唯一回执为权威，跨恢复合并；`used` 计净消费，退款和仍预留的费用分列。详情按回执 ID 关联，不以调用顺序猜配。扣费前余额只在同次返回同时含成本与余额时相加还原，字段为 `balance_before_derived`；跨调用余额还受到真实操作恢复、新房奖励、上限和人工调整影响，不能用余额差代替消费。

Codex 的完整模拟详情可从现有容器的原生会话日志只读补齐。默认根据工作区标签寻找已有容器；`--codex-container <名称>` 可显式指定，但仍须通过归属校验。固定读取器使用受信解释器的隔离模式，不从智能体工作区导入 Python 模块，不启动、重建容器，不连接模型或游戏桥接。仅投影目标会话的模拟计量字段，不复制原生日志。Claude 使用本地 `agent_events.jsonl` 中对应模拟工具的结构化结果。

`--local-only` 完全不访问容器。容器不可用、历史详情缺失、账本冲突、超长行、坏行和活动文件未完成尾行均写入提醒。账本存在但调用详情缺失时，仍输出成本，时间/输入保持未知；未关联的工具请求另列，不能把这两类行简单相加当作调用次数。CSV 对公式前缀转义，JSON 保留原始公开文字。

导出文件包含本机公开对话和路径，默认放在不入库的 `local/` 下。工具不修改源日志、用户配置、存档或录像；在线导出按各文件捕获长度及 SQLite 事务读取，不宣称整个活跃会话是单一原子快照。

验证：`python -m unittest calibration.session_export_test` 覆盖跨恢复/聊天入口、全量消息、消息时间范围与时区/精度/副本边界、非法范围拒绝、空范围产物、WAL 只读、累计时间、账本对账、无容器降级、来源身份拒绝、尾行容错、CSV 公式转义、完整 ZIP 投影及工作区 Python 导入隔离。
