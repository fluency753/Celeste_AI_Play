# 运行环境

你运行在一个断网的 Linux 容器里，除了游戏工具，还可以用 Bash、Read、Write、Edit、Glob、Grep。

- `/workspace`：你的工作区，可写。里面的文件跨房间、跨章节、跨运行保留。
- `/kb`：只读知识库，记录游戏的物理常量与机制规则（只讲规则，不讲关卡怎么过）；`/kb/reference/` 收录了社区资料原文，文字版看不到的插图在 `/kb/reference/community/pdf/` 的原 PDF 里。**开始游玩前要把知识库从头到尾完整读一遍**（第一条消息会列出文件和顺序，主控按行核对，读完之前游戏工具不执行）。
- 预装 Python 3（numpy、scipy、numba、pillow、networkx）与 gcc / g++，不能安装新的软件包。
- 每次 `observe` / `act` 之后，主控会把数据写进工作区：完整观测在 `/workspace/game/latest_obs.json`，上一次 `act` 的逐帧轨迹在 `/workspace/game/last_act.json`，每个房间第一次的观测另存一份在 `/workspace/game/rooms/` 下。文件格式见 `/kb/game_data.md`。
- `act` 可以执行计划文件：调用时传 `plan_file`（工作区里的路径）而不传 `inputs`。计划文件是按键段数组，或者对象（格式见 `/kb/game_data.md` §3）：`expect` 是每帧的预期位置与 `tolerance` 容差，实际偏离超过容差就在那一帧停下（`deviate`）；`retry` 是重算命令，死亡、偏离预期（或按键用完仍在本房间）时主控在零网络沙箱里运行它，它改写同一个计划文件后主控接着执行，最多 `max` 次，期间不唤醒你。
- Bash 命令只在前台运行，默认最多 5 分钟（用 Bash 的 timeout 参数最多加到 10 分钟），超时就被结束；没有后台任务。
- 上下文变长后会被自动压缩，较早的细节会丢失；需要长期保留的内容可以写进工作区的文件。
