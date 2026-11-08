# 按输出 Token 推进游戏

探索模式可选开启 **Token 推进**，默认关闭。控制台的玩法开关启用后，在「可调参数 → 探索：Token 推进」设置 `tokens_per_frame`，默认 **500 token/帧**，接受 1～1,000,000 的整数。命令行在原启动参数后加：

```text
--token-drift --tune tokens_per_frame=500
```

开关和比例随配置、运行状态保存；续跑沿用，可显式用 `--no-token-drift` 关闭或 `--tune` 更改比例。比例不是运行中的热调参数。与按现实时间计费的「思考僵直」互斥，旧配置和预置配置不会自动开启。

新增输出总量包含推理 token，推理量是其子集，不再加一次；输入和缓存读取不计。每次可靠用量通知换算整数帧，不足一帧的 token 余数保留。500 token/帧时，30,000 个输出 token 为 60 帧，即游戏内一秒。收到整批立即推进，不加平滑延迟；每个输入块最多 60 帧，用于及时检查暂停和取消，剩余块可连续执行。

## 用量来源

- Codex：`thread/tokenUsage/updated` 的累计 `total.outputTokens`，用 `last.outputTokens` 限制本次新增量；不把历史总量补收为游戏帧。
- Claude Code：启用 `--include-partial-messages`，按响应 ID 接收 `message_delta.usage.output_tokens` 累计增量，`result.usage` 仅补本轮尚未覆盖的差额。忽略 `message_start` 的占位计数。响应、轮次覆盖量和结果 ID 持久化去重，重连不会重新收费。若上游只在响应结束报告 usage，就在该边界更新；不根据文本长度或等待时间猜 token。
- Claude 的推理 token 子项仅在提供方明确报告时更新；未报告不能据此认为实际没有推理。这里只消费用量元数据，新增的部分文本／推理流不会写入日志。

## 推进边界

只在正在试玩的模型回合接受新增计费，沿用最后真实游戏输入，包括中性输入；执行复用原桥接连接、动作锁、录制检查、停止条件、帧日志、游戏时钟和目标核验。它不占用单次 `act` 帧数限额，也不算一次 Agent 决策。准备、聊天、暂停、网络／额度等待、压缩及维护重载期间不推进；这些阶段仍更新用量游标，不在恢复后补收。恢复后首个正增量可能跨越排除期，归属不明时整批保守跳过；之后连续有效的增量才计费，零增量不清除此边界。暂停或场景变化会丢弃尚未执行的整数帧，保留小于一帧的余数。

角色不可操作或有待处理剧情时不推进。死亡、换房、通关、失控或剧情输入使当前块停止，并废弃旧队列；必须成功默认 `observe` 后才能继续推进或执行新动作。场景中断之前排队的旧动作即使在观察之后才取得锁，也会被拒绝。此功能不会代按确认、跳过死亡动画或选择剧情。

运行结束会等待在途推进归还连接；执行结果不明时暂停，记为 unknown，不重发、不推测已执行帧数。`token_drift.json` 的未确认批次在续跑时继续保持暂停，须人工核验账本；普通「继续」不会清除该保护。进程崩溃前排队但尚未提交的帧也不在续跑时补跑。用量快照先于推进账本提交；两次持久化之间崩溃可能少推进，恢复不补偿这段历史，以免重复输入。

## 记录与分析

- `output_usage.json`：Claude 用量权威累计、响应游标及本轮已覆盖量，供重连去重。
- `token_drift.json`：当前比例、源累计游标、余数、待执行／已执行／放弃帧数及最后批次状态。计数为本会话累计；整数队列恢复时丢弃，余数保留。
- `token_advances.jsonl`：带 Unix 墙钟秒的 usage、requested、committed、discarded、unknown 事件。批次 ID 在运行链内单调增加；requested 表示提交前记录，不证明执行成功，只有 committed 的 consumed 是已确认帧数。
- `metrics.json`：新增 `token_frames`、`token_s`（已确认游戏帧数 / 60）、`token_drift` 摘要；原 `auto_game_s` 仍为自动推进实际墙钟时间，`frames_played` 仍只统计显式动作帧。
- 正式分析导出将批次并入 `tool_calls.jsonl`（`tool=system.token_advance`、`record_kind=system_advance`），稳定工具 ID 关联 `timeline.jsonl` 和结果状态；unknown 的 `success=null`、`status=execution_unknown`，不认定失败、不生成假状态。累计快照附带 `token_frames`／`token_s`。

模型可在游戏推进时继续生成，因而 `thinking_s` 可能与 Token 推进的 `auto_game_s` 重叠，不能把所有时间列相加当成墙钟总时长。工具期间仍沿用原有扣除游戏执行时间的统计口径。`token_s` 为游戏内秒数，不是执行耗时或模型耗时。

验证使用真实 Claude Code 2.1.309 配合隔离本地 SSE 上游，以及真实执行器／游戏工具入口配合隔离桥接；不连接在用主控。上游实际报告频率和真实 Celeste 长程游玩效果不由协议夹具证明。
