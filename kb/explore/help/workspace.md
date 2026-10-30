# 工作区文件与程序查询（按需）

需要程序分析通道时，可用 `/workspace/game/collision_matrix.py` 从当前快照按需生成多来源矩阵文件；用法见同目录 `collision_matrix.md`。矩阵只提供来源、几何与当前状态，不内置通行判断；按具体场景自行写脚本分析，不能把某类来源一概视为永久阻挡。只输出分析摘要，不把整个矩阵读进上下文。

- `/workspace/game/latest_obs.json`：最新的完整观测，格式 `{"decision": n, "obs": 观测}`，例如房间范围在 `obs.level.bounds`。
- `/workspace/game/collision.json`：当前碰撞快照，`image` 指向同帧碰撞图；图像只保留两代，旧路径失效时重读此文件，不沿用旧位置。
- `/workspace/game/last_act.json`：上一次 `act` 的输入与全精度逐帧轨迹，`trajectoryFormat` 给出各列的名字；第 j 行是用第 j 帧的输入推进之后的状态。
- `/workspace/game/rooms/`：每房首次观测的历史快照，只供回看初始情况，不代表当前实体位置。
- `/workspace/game/last_tool.json`：最近游戏调用及最近推进动作的执行记录；核对 run_id/save/save_slot 和当前快照。running/raised/unconfirmed 时先观察核实，不盲目重放；returned 只证明已返回，不证明动作成功或模型已收到。观察与解说不会覆盖最近动作。

文件读取、编辑及程序工具的实际名称与门禁，以启动正文为准。单行 JSON 过长时用程序筛选，不整份打印；同类查询可保存成参数化脚本复用。
