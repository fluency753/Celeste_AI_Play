# 回退布局与区域精查

回退工具使用 `room-context-v2`，首次入房或默认 `{tool:observe}` 返回 `mode=baseline` 的完整布局，替换旧基线。`groups` 只共用相同属性，不省略实例：每组 `instances` 的行按本组 `instanceColumns`（省略时为 `[id,x,y]`）读取，可能另有 `shape/state` 列；共同的形状或状态写在组上。`shape` 引用同包 `shapes`，形状及所有嵌套 x/y 加实例 x/y 还原世界坐标。所有实例位置保留，组不是实心包围区域，不判定可达性。默认 `stateDetail` 明示仅精选游戏状态，完整反射仍可精查。

同房 `{tool:act}/{tool:cutscene}` 返回 `mode=delta`：核对 `base_snapshot_id`，应用 `added/changed` 中的完整替换对象（按 source/id），`removed` 是本覆盖范围内不再观测的 `[source,id]`，不表示永久消失。每条变化自带所需形状定义；`roomChanges` 按同名字段替换首次布局外层的 `exits/spawns/respawnPoint/triggers/entities`，空值也必须应用。地形不变就不重复发送，移动、生成、形状或碰撞状态变化照常报告；范围变化、缓存失效或变化过大时重发 baseline。快照链缺失、历史压缩后记不清布局时默认 `{tool:observe}` 重建，不猜测缺失数据。

`{tool:observe}(detail="geometry", region=[x,y,w,h], limit=32)` 只读最近观测缓存，返回区域精确对象与完整反射状态，格式为 `shared-shapes-v1`（独立的本包形状表）。每个对象的 `at=[x,y]` 是世界坐标锚点，`shape` 指向本包 `shapes`：将该形状及所有嵌套 parts 的 x/y 分别加同一个 at，即还原世界坐标；不要逐层重复累加父锚点，也不要与原始 geometry 的世界坐标混用。后续页带 `offset=next_offset` 与同一 `snapshot_id`；动作或新观测后重新从 offset=0 查。`{tool:observe}(detail="player")` 读取完整玩家内部状态。两种详情都不推进游戏，也不改变变化基线。局部查询超出来源范围、来源缺失或存在未知形状时，空结果不证明安全。完整原始工作区与完整碰撞图不变，不必把整房 JSON 打印进聊天。
