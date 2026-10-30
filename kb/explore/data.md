# 坐标与观测：开局要点

- 世界坐标为像素，x 向右、y 向下；速度为像素/秒，游戏每秒 60 帧。矩形 `[x,y,宽,高]` 的 x/y 是左上角。
- 玩家位置是碰撞箱底边中点：站立宽 8 高 11，下蹲高 6。用实际碰撞箱、开口和支撑判断通行，截图与贴图仅供定位。
- 每次核对 frame、章节 sid、room 和数据覆盖范围；`complete` 与 `geometryComplete` 分别说明覆盖和形状完整性。未知形状、缺字段、局部空结果都不证明安全；实体当前状态可能变化，exits 只是相邻房间的边界投影。
- 默认 `workspace-files-v1` 给状态与当前 `/workspace/game/latest_obs.json` 路径。文件是单行 JSON，完整数据在 `obs`；过长时用程序筛选所需字段或区域，不整份打印。动作后文件会更新，不重复查询同帧已有数据。
- 文件不可用或程序受限时，工具返回 `room-context-v2` 的 baseline/delta；区域精查是 `shared-shapes-v1`。三种格式不同，解析前查对应参考；快照链缺失时默认 observe 重建，不能猜旧布局。
- 默认 observe 获取新观测；detail="geometry"/"player"/"time" 查询缓存详情或计时，不推进游戏。截图不含全部剧情文字，当前对白与选项读 story。

按需入口（无需在准备阶段全部读取）：
- 解析碰撞形状、baseline/delta 或区域分页：[help/observation.md](help/observation.md)。
- TAS、摇杆、计划文件和轨迹：[help/actions.md](help/actions.md)。
- 文件位置、碰撞矩阵与程序查询：[help/workspace.md](help/workspace.md)。
- 剧情、菜单与结算：[help/story.md](help/story.md)。
