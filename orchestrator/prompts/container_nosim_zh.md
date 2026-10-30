# 运行环境

文件与程序使用本次启动正文列出的工具，均在断网容器内。

- `/workspace`：你的工作区，可写。里面的文件跨房间、跨章节、跨运行保留。
- `/kb`：只读知识库，记录游戏的物理常量与机制规则（只讲规则，不讲关卡怎么过）。{kb_reference_rule}**开始游玩前按第一条消息的必读清单完整读完**（主控按行核对，读完之前游戏工具不执行），其余资料需要时再查。
- 预装 Python 3（numpy、scipy、numba、pillow、networkx）与 gcc / g++，不能安装新的软件包。
- 自己写的模拟器、搜索、穷举、批量计算等程序建议用编译型语言（如 C++）；有多线程就用多线程（容器有多个 CPU 核），同一批推演放在一个进程里做，不要逐个启动进程。语言由你决定。
- 每次 `observe` / `act` 之后，主控会把数据写进工作区：完整观测在 `/workspace/game/latest_obs.json`，上一次 `act` 的逐帧轨迹在 `/workspace/game/last_act.json`，每个房间第一次的观测另存一份在 `/workspace/game/rooms/` 下。文件格式见 `/kb/game_data.md`。
- 这些文件是完整的精确数据（工具正文可能只给状态与文件位置），房间几何以观测里的 `collision` 为准，`entities` 的每一项还带全部状态字段 `fields`；`last_act.json` 是上一次 `act` 的全精度逐帧轨迹（`trajectoryFormat` 给出列名），以及画面附近实体的逐帧位置 `entityTrajectory`。房间具体信息以这些精确数据为准，截图的视觉识别仅作为参考。
- `act` 可以执行计划文件：调用时传 `plan_file`（工作区里的路径）而不传 `inputs`。计划文件是按键段数组，或者对象（格式见 `/kb/game_data.md` §3）：`expect` 是每帧的预期位置与 `tolerance` 容差，实际偏离超过容差就在那一帧停下（`deviate`）；`retry` 是重算命令，死亡、偏离预期（或按键用完仍在本房间）时主控在零网络沙箱里运行它，它改写同一个计划文件后主控接着执行，最多 `max` 次，期间不唤醒你。
- 不管用什么方法，如果需要计算 Madeline 逐帧轨迹，就在算出后输出到这个接口，系统会画给观众看，只用于展示，不影响成绩。计算与否由你自主决定。接口：`import sys; sys.path.insert(0, "/workspace/game"); from show import trajectory`，然后 `trajectory(points, result)`——`points` 是每帧推进之后 Madeline 的位置 `[(x, y), ...]`（与 `act` 返回的轨迹同一口径），`result` 是这条轨迹的结局：`"exit"` 到达出口、`"death"` 死亡、`"open"` 其他。说明见 `/workspace/game/show.py`。
- {program_timeout_rule}，超时就被结束；没有后台任务。
- 上下文变长后会被自动压缩，较早的细节会丢失；需要长期保留的内容可以写进工作区的文件。
