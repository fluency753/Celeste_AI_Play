# 行为分析导出字段（schema_version=1）

`tools/export_session.py --analysis` 增量启用。默认导出不变。JSON/JSONL 为 UTF-8，CSV 为 UTF-8 BOM。CSV 对公式开头的字符串加单引号，原值在 JSONL。null/空单元格表示未获得可靠数据，不等于零、失败或未发生。数值时间为秒，帧为游戏帧；wall 为 Unix epoch，ISO 时间含北京时间偏移。

## 文件与关联

保留 session.json、public_text.csv、simulations.csv、manifest.json、export.zip。分析模式 README 只展示摘要，完整旧式可读记录移到 public_text.md。session.json 旧字段保持语义，新增 analysis 摘要与结构化文件引用，不重复嵌入大量分析记录。

| 文件 | 主键与内容 |
|---|---|
| tool_calls.csv | tool_id；精简索引，sequence 为选中记录时间排序序号 |
| tool_calls.jsonl | tool_id；全部可观察工具、参数和有界结果细节 |
| turns.jsonl | turn_id；主控或仅原生运行时可识别的回合 |
| timeline.jsonl | event_id；公开消息、工具边界、回合、压缩、错误、投递与控制请求 |
| actions.jsonl | action_id；decisions.jsonl 中实际动作和结果 |
| game_states.jsonl | state_id；已有工具结果中的状态证据 |
| metric_snapshots.jsonl | snapshot_id；已有 metrics/summary 累计快照 |
| workspace.json（可选） | 显式指定本机工作区的当前文件清单、笔记及结构化 Git 摘要 |
| notes.md（可选） | 工作区根目录笔记的当前脱敏文本，与 workspace.json 中对应的 `text` 相同 |
| git_changes.md（可选） | 同一份 Git 摘要的可读版本，包含时段、当前状态与缺口 |
| SCHEMA.md | 本字段契约随包副本 |

公共字段 session_id、run_id、run。run_id 对会话和源运行绝对路径哈希，移动路径后变化；tool_id 对 run_id+call_id 哈希；turn_id 对运行段+本地回合编号哈希。native_turn_id 为原生 UUID，通过主控 ACK 关联；未获 ACK 的记录可仅属于原生会话（run_id=null）。不按时间或顺序猜配，不合并不同运行段的同名调用。歧义在 run_ownership 标记。

source/sources 保存路径、record_index（成功解析的 JSON 对象序号，不是物理行号）及来源种类。tool_id、turn_id、context_state_id、after_state_ids、snapshot_id 为跨文件引用。窗口外引用可沿来源核查。

## 工具字段

- call_id：运行时原 ID；tool：名称；record_kind=tool_execution 为实际工具，code_mode_wrapper 为 exec/wait 包装调用；system_advance 为主控 Token 推进批次（tool=system.token_advance），不代表 Agent 又调用了一次工具。没有可靠父子 ID 时不猜配，包装和内部工具数量/时间不能相加。Token 批次执行范围不明时 success=null、status=execution_unknown，不推测成功、失败或游戏状态，详见 [Token 推进](TOKEN_DRIFT.md)。
- requested_wall/result_wall：已记录请求/结果可用时刻；requested_time_source/result_time_source：时间依据。timing_ended_wall 为主控计时闭合，中断清理也可闭合，不能视为返回。
- active_s：主控暂停扣除后的单调用占用，包含游戏执行且并发可重叠；runtime_duration_s：执行器时长；wall_span_s：两端墙钟差，不可互相替代。逆序标记 clock_order_warning，不取绝对值。
- thinking_before_s：主控在调用前测得的 thinking 累计差；thinking_interval_start_wall：已知计量开始边界。并发时可能为零，末尾未闭合思考不包含；不是消息间隔或私有推理时长。
- parameters：允许字段，如 inputs/candidates/plan_file/input_format/stop_on、观测 detail/region、path/file_path、command/cwd/code 等。写文件正文仅保存字符数/哈希。
- Codex Bash 的标准沙箱包装会在导出时还原为脱敏后的 `parameters.command`，保留 `cwd`，并将包装器时限以秒数字符串单列为 `parameters.timeout_s`。本机事件与原生会话日志均适用，旧记录可重新导出。
- result.structured：已有执行帧数、停止原因、事件、额度回执/余额、候选预测停止原因/帧数等；result.text_excerpt：有界公开工具结果摘录。exit_code 为显式 Shell 退出码；file_changes 为补丁路径/操作，不保存完整 diff。
- parameters_meta/result_meta：可用性、原字符数、保留量、截断、附件省略；details_omitted：来源已省略详情时保留其原因，当前导出不再按累计详情量丢弃后半记录。参数树约16K字符、结果树约24K、摘录8K，深度7、单数组128项、单对象64项。源行上限32MiB，超限跳过报警。原生日志逐条输出已脱敏投影到宿主临时文件，以完整结束标记和记录数核验；读取失败时保留本机日志并明确报告原生来源缺口，不接收半截流。参数、结果、事件、动作和完整状态详情采用24MiB临时分块，内存保留标识、时间、归属和块偏移；最终逐条回填原JSONL字段，不另设公开详情协议。单条投影限制保持不变；块数是内部存储计数，不是导出总量限制。分析模式模拟候选限8K并标记 analysis_candidates_truncated，旧默认不变。
- success 仅取显式结果/退出码；status 为 failed、returned、returned_unverified、interrupted_result_missing、result_missing、pending_or_missing。计时闭合不证明成功，缺失结果不等于失败。
- context_state_id/last_observed_room：请求前最近保存观测，可能过时；after_state_ids：本工具返回的状态；observed_delta：共同存在字段的比较，不证明因果。sources 最多32条引用，完整投递事件另在时间线。

敏感键、已知凭据、私有 reasoning/thinking/analysis、压缩正文、图片/音频/编码正文不导出。任意自然语言秘密无法穷尽识别，分享前需检查。不能通过独立 UserMessage 正文重建文件读取结果；投递 path、分页、截断和 recorded 证明可用时保存，queued 不等于接收。

`analysis.coverage.detail_storage` 记录 temporary_chunked_spool 模式、chunk_bytes、chunks、projected_bytes、records 及 cumulative_budget_omissions=false。`single_record_truncated_tools` 汇总达到单条参数／结果投影边界的工具数，`source_detail_omitted_tools` 汇总原来源已经缺失详情的工具数；README 汇总提醒，不逐条刷屏。状态索引独立保留，缺少新观测时 last_observed_room 仍指最近已记录房间；明确的 room:null 才清空，不从 Shell、模拟结果或时间空窗推测房间变化。临时块仅含脱敏投影，写出成功或实际写入失败后释放；目标目录被拒绝且尚未写入时可用同一报告换目录重试。NaN/Infinity 等非有限源数字在分析模式下变为 null 并记录警告，不以零替代。

## 回合、事件与计量

turns 中 controller_turn 为主控编号，主控在同一目录重启时可能重用，关联应使用 turn_id；首次编号保留原稳定 ID，重复请求的 ID 另包含来源 record_index，已见请求／ACK 的记录不会被后续同编号覆盖，即使请求时间未知。native_turn_id 为 ACK UUID；requested_wall、acknowledged_wall、ended_wall、active_s 来自已记录事件。运行时独有回合可无 run_id；runtime_status/pending_tools 保存已知中断信息。

timeline.kind 区分 controller.*、tool.requested/result、public.message、compaction.*、runtime.*、native.*、delivery.*、control.request/state 等，data 为各来源允许字段投影。public.message 通过 public_event_id+run_id 关联公开文本。token 事件仅保存运行时给出的 last/total 数字；累计 total 不相加；reasoningOutputTokens 是计数而非推理正文。不补算未知 token。

`agent.reported_blocked` 来自已接受的正式报告。`agent.blockage_attempt` 来自 `stop_run_diagnostics.jsonl`，按记录时间筛选。它的 `data.status` 区分 `classification_required`（待分类）、`confirmation_required`（待确认）、`recovery_available`（有恢复步骤）和 `duplicate`（重复报告）。`data` 还保留原因、分类、决策序号、游戏帧、恢复进度和控制说明。待确认时，`step` 与 `required_steps` 分别为已提交次数和所需总次数。历史日志未记录的字段为 `null`。时间线不读取 `confirmation_prefix` 字段。

metric_snapshots 保留 canonical、counters、game_clock、章节/房间与来源。canonical 优先 metrics，回退 summary；只用已有 written_wall，不用 mtime 冒充采样时间。

如源中有 chapter_base，保留其显式数值计量基线；null 时不补算。thinking_s/tool_s 可能包含读完知识库后的游玩准备活动，elapsed_s 从真正开始游玩计起，因此这些累计项不能直接相加验证总时长。单调用时长和主控累计值也不具有逐条可加关系。

| 累计字段 | 含义与重叠 |
|---|---|
| elapsed_s | 有效游玩现实时间，扣除主控暂停、额度/正式网络等待；执行器内部自动重连仍沿用现实预算口径。恢复累计，不累加各段快照 |
| thinking_s | 活跃模型回合无工具占用的阶段，包含生成、压缩等，不等于私有思考；1.23.4起排除运行时已明确报告的自动重连区间 |
| network_retry_s | 执行器原回合内已观测的自动重连时间，不计入思考；保留实际工具计量和现实预算，因此可与 tool_s 重叠。Claude 正式网络暂停仍追溯首错，可能与 net_wait_s 重叠，不能相加 |
| tool_s | 活跃工具区间并集，扣除游戏/自动推进及回放动作执行墙钟，不等于逐工具之和 |
| play_game_s / auto_game_s | 游戏操作/自动推进的墙钟占用，不是游戏时钟 |
| study_s | 知识库准备耗时 |
| compact_s | 运行时报告或主控观测的压缩耗时，通常与 thinking_s 重叠；压缩期间断连时也可能包含自动重连等待，不能相加 |
| replay_s | 回放流程时间，可与其他分类重叠 |
| waited_s / net_wait_s | 主控已识别额度/网络等待；无事件空窗不归因 |

game_clock 帧/60 为主控游戏时钟，章节 ticks/10,000,000 为章节时间，含义不同。消息间隔不能还原累计曲线。

analysis.snapshot_deltas 仅比较 resumed_from 两端 canonical 快照，逐字段给出 before/after/delta；下降时 delta=null、reset_or_decrease=true。前段可能受后续聊天/分支影响，不是已证明恢复基线或精确窗口统计。room_workload 按最近观测房间计数；thinking_before_tools_s 只加计量边界在窗口内的直接计量，不裁切未知边界。longest_thinking_before_tools 提供核查索引，不作行为优劣结论。

## 动作与游戏状态

actions 保存 i、attempt、thought（公开说明）、inputs、plan_file、input_format、stop_on、planned、consumed、stop、events、input_cursor、deviation 等已有字段。elapsed_s 为 decisions.t 有效累计时间；public_event_wall 仅为精确 act:i:attempt 的公开说明时刻，不是执行开始/完成。没有精确 call_id 时留 null，不按顺序猜配。

input_fingerprint 在展示截断前对完整输入、计划路径、格式、stop_on 哈希。相同路径不证明文件内容相同，相同输入不证明初始状态相同。repeated_inputs 给出次数/action_ids/停止原因，不自动判断无效研究或学习。

game_states 从已有工具结果提取 frame、sid、room、死亡数/flags、player 位置/速度/状态/重生、DreamBlock active 等显式信息；kind 为 observed/before/after，evidence 标明结构化结果或公开文本解析。wall 为结果可用时刻，非推算捕获时刻。不从模拟预测推断真实进度；未保存的死亡/重生/换房等中间转变不能重建。不读取录像或额外调用游戏。

## 筛选与工作区

会话来源除 `runs` 外还包含对应项目固定 `local/hud-idle/chats` 下同执行器、提供方和会话 ID 的准备聊天。公开消息与工具记录按原时间规则合并。`segments.source_kind` 标识 `run`、`preparation`（runs 内 session_kind=ready）或 `hud_idle_chat`。后两者的 `metrics_authoritative=false`，因为准备阶段不代表试玩累计量，HUD 聊天还可能沿用另一会话的统计。`metric_snapshots.jsonl` 保留同名标记，这些快照仅作来源证据，不用于恢复链计量差或覆盖 `time_summary`。只有这类快照时累计值为空。

`session.json.save_naming` 是各来源最后保存的取名结果索引。字段包含 `request_id`、`name`、`runs`、`sources`、`wall`、`time`、`window_membership`。同请求同名字合并来源，冲突名字分别保留并告警。现有结果文件未记录可靠发生时间，`wall/time` 为 null；无时间筛选时 membership 为 `unfiltered`，有筛选时为 `unknown`。此索引属于会话元数据，公开取名对话本身仍按消息时间筛选。

聊天来源记录了 `conversation_source` / `conversation_origin` 且能唯一对应本会话原运行时，`segments.copied_from` 指向该来源。复制的 public/codex/decisions 日志相同前缀保留在原来源，聊天只贡献新增记录；SQLite 中同 ID、类型、正文与时间的历史控制请求也只计一次。普通运行之间真实重复发生的动作仍分别保留。

--since/--until 对旧 public_text 仍是左闭右开（无时区按北京时间），旧累计/模拟字段仍为整个会话。分析工具/回合保留相交区间及完整边界；window_membership 区分 within_window、overlaps_window、end_observed_in_window、time_unknown、unfiltered。未知记录保留，不视为确定在窗口内。状态和快照保留整个会话用于引用。

`--workspace` 显式选取工作区，最多访问 2048 个目录、采集 2000 项清单和 20 份 notes/memory/journal，每份 128 KiB、合计 512 KiB。根 `notes.md` 优先采集。跳过链接、重解析点、依赖目录和敏感文件。当前笔记只代表采样时内容，mtime 不作历史归因。输出不能位于源运行或源工作区内。

`workspace.json.git` 继续表示工作区根目录的 Git 快照。新增 `git_repositories` 数组，每项包含工作区相对路径 `path`（根目录为 `.`）及相同 Git 字段，按遍历顺序最多 8 项（含根目录快照）。工作区子目录中的独立 `.git` 目录会单独采样，`git_changes.md` 按仓库显示，路径以各仓库根目录为准；根目录没有提交不影响子目录仓库。Git 文件指针、链接、重解析点不追踪；扫描达到上限或跳过不支持的 Git 目录时在 `workspace.warnings` 明确标记。各仓库依次使用隔离 Git 快照，不运行源配置、过滤器、钩子或联网。

`workspace.json.git` 中的 `*_wall` 均为 Unix 秒。Git 记录只有整秒精度，`since_wall` / `until_wall` 保留输入精度，窗口左闭右开。列表中的 `null` 表示证据不可用，`[]` 表示可用且没有匹配项。`*_truncated` 表示条数上限生效。

- `head` / `ref` 是采样到的 HEAD 与引用名。`commits` 最多 200 条，记录当前 HEAD 可达提交的 ID、提交者时间和脱敏标题。被重置掉的提交可能只出现在 reflog。
- `head_events` 最多 200 条，从本地 HEAD reflog 读取时间和更新前后的哈希。`window_start_head` / `window_end_head` 为相应边界之前可核实的 HEAD，无终点时取采样 HEAD。`window_changes` 最多 500 项，为这两个提交树之间净变化的 Git 状态和路径。记录缺失、引用不一致或端点不可核实时保留未知。reflog 可能被禁用或清理，也可能受本机时钟影响。
- `worktree_status` 最多 500 项，保存 `worktree_status_wall` 采样时的 Git porcelain XY 状态和路径。`X` 为索引、`Y` 为工作树，`??` 为未跟踪。忽略子模块变化，重命名按删除和新增列出。未提交变化没有历史快照，无法归入窗口；各来源分开读取，活动工作区可能在采样期间变化。
- Git 使用独立临时元数据读取，仅接收普通的 `.git` 目录。不加载源 Git 配置、hooks、过滤器、外部 diff 或对象库替代路径。当前状态使用本机 Git 默认的文件比较规则及工作区忽略规则，可能与原环境自定义配置不同。Git 缺失、源数据不全或超出大小/时间限制时，相应字段降级并说明。归档不含 Git 对象、diff 正文或任意工作区文件。
