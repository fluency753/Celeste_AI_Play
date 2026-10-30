有文件工具时，先检查 /workspace/game/last_tool.json（若存在），核对 last_action 的运行／存档槽位、参数、状态和执行结果，再调用 observe 核对当前状态，继续原来的任务，不重复已经执行的操作。文件缺失、仍为 running 或结果未确认，都不能据此断言上次动作没有执行。
