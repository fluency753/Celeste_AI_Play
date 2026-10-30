# 观测格式参考（按需）

坐标与基本核验见 ../data.md。本文在需要解析观测、回退布局或区域分页时查阅。

## 截图与字符地图

- 截图是游戏画面放大 2 倍后的样子（原始 320×180），只覆盖当前画面；画面左上角的世界坐标写在观测文字里。背景、装饰和前景贴图不一定能碰撞，能碰撞的东西也不一定看得清，不要靠看图估计位置和尺寸。
- 字符地图（回退布局时出现）每格 8×8 像素，行列号与世界坐标的换算写在地图上方，图例随地图给出。`#` 是实心地形；其他符号只标出实体大致占了哪些格子，精确形状看 `latest_obs.json` 里的 `collision` 与 `entities`。

## 房间概况与精确几何

具备脚本能力且当前文件已核对时，默认 `observe/act/cutscene` 返回 `workspace-files-v1`：正文只给状态、快照身份、收集品状态和 `/workspace/game/latest_obs.json` 位置。`latest_obs.json` 是单行 JSON，可能有几百 KB，专用读取工具可能截断过长的行：用程序工具里的程序读取文件的 `obs` 并筛选需要的区域，不重复查询同帧已有几何、不整份打印。动作后文件会更新；默认 observe 获取新观测。无脚本能力、文件不可用或规划工具受限时，才回退为首次布局及同房变化，按下述格式维护几何。选择通道时先用精确实例与角色碰撞箱核对支撑、墙和开口；截图／碰撞图用于定位候选，不替代几何核验。

回退工具使用 `room-context-v2`，首次入房或默认 `observe` 返回 `mode=baseline` 的完整布局，替换旧基线。`groups` 只共用相同属性，不省略实例：每组 `instances` 的行按本组 `instanceColumns`（省略时为 `[id,x,y]`）读取，可能另有 `shape/state` 列；共同的形状或状态写在组上。`shape` 引用同包 `shapes`，形状及所有嵌套 x/y 加实例 x/y 还原世界坐标。所有实例位置保留，组不是实心包围区域，不判定可达性。默认 `stateDetail` 明示仅精选游戏状态，完整反射仍可精查。

同房 `act/cutscene` 返回 `mode=delta`：核对 `base_snapshot_id`，应用 `added/changed` 中的完整替换对象（按 source/id），`removed` 是本覆盖范围内不再观测的 `[source,id]`，不表示永久消失。每条变化自带所需形状定义；`roomChanges` 按同名字段替换首次布局外层的 `exits/spawns/respawnPoint/triggers/entities`，空值也必须应用。地形不变就不重复发送，移动、生成、形状或碰撞状态变化照常报告；范围变化、缓存失效或变化过大时重发 baseline。快照链缺失、历史压缩后记不清布局时默认 `observe` 重建，不猜测缺失数据。

`observe(detail="geometry", region=[x,y,w,h], limit=32)` 只读最近观测缓存，返回区域精确对象与完整反射状态，格式为 `shared-shapes-v1`（独立的本包形状表）。每个对象的 `at=[x,y]` 是世界坐标锚点，`shape` 指向本包 `shapes`：将该形状及所有嵌套 parts 的 x/y 分别加同一个 at，即还原世界坐标；不要逐层重复累加父锚点，也不要与原始 geometry 的世界坐标混用。后续页带 `offset=next_offset` 与同一 `snapshot_id`；动作或新观测后重新从 offset=0 查。`observe(detail="player")` 读取完整玩家内部状态。两种详情都不推进游戏，也不改变变化基线。局部查询超出来源范围、来源缺失或存在未知形状时，空结果不证明安全。完整原始工作区与完整碰撞图不变，不必把整房 JSON 打印进聊天。

以下字段说明专指原始工作区 `latest_obs.json` 的 `obs`，包括 `collision.objects[].geometry` 世界坐标；回退工具的 `groups/instances` 和区域精查的 `objects[].shape/at` 是不同格式，不要混读或重复加锚点。

- `collision.frame/sid/room` 必须匹配当前观测；`scope/bounds` 标覆盖范围，`view` 不代表全房。`complete` 表示范围内地形与实体均已提供，形状是否完整另看 `geometryComplete`。
- `collision.objects` 区分地形、实体固体、单向平台、危险物、触发类对象和未知对象。按真实 `geometry` 形状分析；Key 等触发类不是墙，未知形状的包围盒也不是已确认实心。
- 实体位置、形状和 `collidable` 每次观测按当前帧重取；`mutable=true` 表示以后可能变化，当前零速不表示静态，禁用碰撞仍保留形状。当前被挡不等于一直不可达。

- `room` 房间名；`bounds` 房间范围 `[x, y, 宽, 高]`。
- `exits`：相邻房间的边界投影，不保证有开口或从当前位置能到；应结合 `collision` 全部障碍和机制判断，不能只看旧 `solids` 地形。
- `solids`：地形的碰撞箱，每项 `[x, y, 宽, 高]`，由地形格子合并而成。会动、会塌、能撞碎的实心物不在这里，在 `entities` 里。
- `entities`：房间里可碰撞的实体，每项 `{"id", "type", "cat", "x", "y", "w", "h", …}`。`id` 在本次进入房间期间固定，用来跨暂停对照同一个实体；`type` 是游戏里的类型名；`cat` 是大类：`hazard` 致死物、`spring` 弹簧、`refill` 冲刺水晶、`booster` 泡泡、`feather` 羽毛、`collectible` 收集品、`dream_block` 梦境块、`dynamic_solid` 会动的实心块、`jumpthru` 单向平台、`solid` 其他实心块、`interactive` 可交互物、`other` 其他。尖刺有 `dir`（刺尖朝向）；碰撞形状不是矩形时有 `shape`；会动的实体附 `fields`（反射出的游戏字段）与 `moved`（与上一次暂停相比的位移）；回退布局里精简过的实体用 `state`。`Killbox` 是坑底的致死区。
- `triggers`：触发器 `{"type", "rect"}`，不挡路，进入范围时触发效果。
- 桥接 1.0.14 起，暂未启用碰撞的实体也会列出，`collidable:false` 只表示当前状态；远处圆刺可能在靠近时启用，不能按空地处理。组合形状在 `collider` 中递归给出 `shape,x,y,w,h`，圆形另有 `r`，`list.parts` 包含所有子形状；每层都是世界坐标。旧 `parts` 只包含矩形，会遗漏圆形；字符地图也只是粗略表示。`grid`/`unknown` 的外接范围不代表内部全实心。
- `spawns` 本房间的重生点；`respawnPoint` 当前生效的重生点。
- `player`：Madeline 的完整状态，`pos` 精确位置（含小数部分）、`speed` `[vx, vy]`、`state`、`dashes` / `maxDashes`、`stamina`、`onGround`、`facing`、`ducking`、`hitbox` `[左, 上, 宽, 高]`、`internal`（内部计时器与状态的原始数值，计时器单位是秒）。
- 观测的 `frame` 与 `level.sid`、`level.room`（`collision` 里另有同名字段）用于核对观测身份。`solids` 为 `null` 表示本次没有提供完整地形，不能按空房间处理；需重新观测。
- 主控不提供可达性标签或路线判断。自行根据整房间碰撞箱、身体尺寸、实际开口、支撑及实体机制分析路线，再用具体动作验证。
- `story`、`dialog`：当前画面上的对白、教学提示、纪念碑文字与对话选项（有可见文字时提供）。
