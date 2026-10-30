# 高级动作与计划文件（按需）

基础按键见 ../tools.md；本文用于摇杆、TAS、计划文件与轨迹比较。

可显式使用TAS按键输入格式 `input_format="celeste_tas"`，也可在JSON计划对象里写 `"input_format":"celeste_tas"`（旧会话工具没有新参数时用计划字段）。此格式下C是第二冲刺、S暂停、O确认，不能与下述本项目格式混用；须agent剧情模式与桥接1.0.20。/workspace/game/tas.py帮助显式读取纯输入区和生成窗口，动作返回来源游标与未执行后缀；不执行脚本命令、不自动补帧。模拟转换只支持游戏按键，不能表达的菜单等输入明确拒绝。

- `inputs` 是按顺序执行的按键段，每段写作 `"帧数,按键,按键..."`。只写帧数表示这些帧什么都不按，例如 `"5"`。一次 `act` 的帧数下限与上限按本次设置，见 `act` 的工具说明与观测开头的"当前 act 帧数范围"；不足下限会被退回、不消耗帧，自动重试重算出的计划不足时由主控在末尾补空帧。
- 按键：方向 `L` `R` `U` `D`，跳 `J`，冲刺 `X`，crouch dash `Z`（以下蹲姿态冲刺），抓墙 `G`，交互 `T`；`K`、`Y`、`V` 是第二个跳、冲刺、crouch dash 键。摇杆 `A:角度` 是推满的左摇杆：角度以度为单位、逆时针（0 右、90 上、180 左、270 下），方向分量为 (cos, sin)，出现时取代 `L` `R` `U` `D`；`L` `R` `U` `D` 是数字输入，斜向时两个分量都是 1。
- `C` 确认，两种剧情模式都可用：auto 模式下画面冻结等确认（如拿到磁带、吃心后）时主控先替你按，按了仍卡住才交回由你处理；agent 模式主控不代按，另开放 `B` 返回、`P` 暂停、`N` 日志。`menu`／`pauseMenu` 报告菜单状态，`ui` 报告 Poem 显示或结算 `confirmReady`。旧桥接可能没有 `ui`。
- 跳和冲刺是"按下"触发的：一直按住只算一次。要再按一次，中间至少松开 1 帧；或者换第二个键（按住 J 时按下 K 也算一次新的按下）。
- 跳和冲刺有约 5 帧的输入缓冲：提前几帧按下并按住，到能起跳或冲刺的那一帧会自动生效。
- `stop_on`（可选）：`death` 死亡、`room` 换房间、`land` 落地（起跳前站在地上的那一帧不算，离地后再落地才触发）、`refill` 冲刺恢复、`uncontrollable` 失去控制、`controllable` 恢复控制、`complete` 通关、`story_input` 到达剧情输入节点、`story_decision` 到达剧情决策机会；默认 `["death", "room", "complete"]`。death 总会生效；agent 剧情模式还会自动加上 story_input、story_decision（TAS 格式除外）及可操作时的 uncontrollable；agent 模式已登记通关后，不再用 complete 反复截断结算输入。触发后立即停下，剩余按键不执行。
- `plan_file`：把按键段写进工作区里的 JSON 文件，调用 `act` 时传 `plan_file`（工作区里的路径）代替 `inputs`。文件也可以写成对象 `{"inputs": [...], "expect": [...], "tolerance": 1}`：`expect` 每帧一项，是这一帧推进之后 Madeline 的预期位置 `[x, y]`（`null` 表示这一帧不检查），实际与预期相差超过 `tolerance` 像素就在那一帧停下（`deviate`）。
- 返回内容：实际执行了多少帧、停止原因、事件、取样轨迹（帧, x, y, vx, vy, 状态, 冲刺数, 是否着地），以及新的观测。

## 计划内自动重试（有程序通道时）

plan_file 对象可带 `retry={"command":"python3 planner.py","max":3,"timeout_s":20,"on":["death","deviate"]}`。死亡、偏离或指定 done（输入用完但仍在本房间）后，主控在 /workspace 的断网程序通道运行 command；程序须读取最新观测并改写同一个计划文件，主控再执行，最多 max 次。重试不能更换输入格式；规划轮次受限时 retry 被忽略，无容器时拒绝。`command` 最多 500 字符（长的写成脚本）；`max` 默认 3、`timeout_s` 默认 20 秒，上限按本次设置（默认 5 次、60 秒）；程序从环境变量 `CELESTE_PLAN_FILE` 得到计划文件路径，重试原因、偏差与已执行帧数见 `/workspace/game/retry.json`。本次程序门禁与限额仍生效。
