# 坐标与观测数据

## 坐标

- 世界坐标以像素为单位，x 向右增大，y 向下增大（向上的速度是负数）。游戏固定 60 帧/秒，速度单位是像素/秒，每帧位移 = 速度 / 60。
- 矩形一律写成 `[x, y, 宽, 高]`，(x, y) 是左上角；实体的 `x, y, w, h` 同样是碰撞箱的左上角与宽高。
- Madeline 的"位置"是她碰撞箱的底边中点。站立时碰撞箱宽 8 高 11（x−4 ~ x+4，y−11 ~ y），下蹲时高 6。判断能不能穿过缝隙、站上平台，用碰撞箱和地形比，不看角色图片。
- 掉出房间底部（且下面没有房间）会死亡；从房间边界走出去会进入相邻房间，屏幕转场由系统处理。

## 截图与字符地图

- 截图是游戏画面放大 2 倍后的样子（原始 320×180），只覆盖当前画面；画面左上角的世界坐标写在观测文字里。背景、装饰和前景贴图不一定能碰撞，能碰撞的东西也不一定看得清，不要靠看图估计位置和尺寸。
- 字符地图每格 8×8 像素，行列号与世界坐标的换算写在地图上方，图例随地图给出。`#` 是实心地形；其他符号只标出实体大致占了哪些格子，精确形状看精确数据里的 `entities`。

## 房间概况与精确几何

首次入房和默认 `observe` 返回布局；同房 `act/cutscene` 返回变化，按下述格式维护当前几何，不要求每步重新查询整房。选择通道时先用精确实例与角色碰撞箱核对支撑、墙和开口；截图／碰撞图用于定位候选，不替代几何核验。

默认工具使用 `room-context-v2`，首次入房或默认 `observe` 返回 `mode=baseline` 的完整布局，替换旧基线。`groups` 只共用相同属性，不省略实例：每组 `instances` 的行按本组 `instanceColumns`（省略时为 `[id,x,y]`）读取，可能另有 `shape/state` 列；共同的形状或状态写在组上。`shape` 引用同包 `shapes`，形状及所有嵌套 x/y 加实例 x/y 还原世界坐标。所有实例位置保留，组不是实心包围区域，不判定可达性。默认 `stateDetail` 明示仅精选游戏状态，完整反射仍可精查。

同房 `act/cutscene` 返回 `mode=delta`：核对 `base_snapshot_id`，应用 `added/changed` 中的完整替换对象（按 source/id），`removed` 是本覆盖范围内不再观测的 `[source,id]`，不表示永久消失。每条变化自带所需形状定义；`roomChanges` 按同名字段替换首次布局外层的 `exits/spawns/respawnPoint/triggers/entities`，空值也必须应用。地形不变就不重复发送，移动、生成、形状或碰撞状态变化照常报告；范围变化、缓存失效或变化过大时重发 baseline。快照链缺失、历史压缩后记不清布局时默认 `observe` 重建，不猜测缺失数据。

`observe(detail="geometry", region=[x,y,w,h], limit=32)` 只读最近观测缓存，返回区域精确对象与完整反射状态，格式为 `shared-shapes-v1`（独立的本包形状表）。后续页带 `offset=next_offset` 与同一 `snapshot_id`；动作或新观测后重新从第一页查。`observe(detail="player")` 读取完整玩家内部状态。两种详情都不推进游戏，也不改变变化基线。局部查询超出来源范围、来源缺失或存在未知形状时，空结果不证明安全。完整原始工作区与完整碰撞图不变，不必把整房 JSON 打印进聊天。

以下字段说明专指原始工作区 `latest_obs.json` 的 `obs`，包括 `collision.objects[].geometry` 世界坐标；默认工具的 `groups/instances` 和区域精查的 `objects[].shape/at` 是不同格式，不要混读或重复加锚点。

- `collision.frame/sid/room` 必须匹配当前观测；`scope/bounds` 标覆盖范围，`view` 不代表全房。`complete` 表示范围内地形与实体均已提供，形状是否完整另看 `geometryComplete`。
- `collision.objects` 区分地形、实体固体、单向平台、危险物、触发类对象和未知对象。按真实 `geometry` 形状分析；Key 等触发类不是墙，未知形状的包围盒也不是已确认实心。
- 实体位置、形状和 `collidable` 每次观测按当前帧重取；`mutable=true` 表示以后可能变化，当前零速不表示静态，禁用碰撞仍保留形状。当前被挡不等于一直不可达。

- `room` 房间名；`bounds` 房间范围 `[x, y, 宽, 高]`。
- `exits`：相邻房间的边界投影，不保证有开口或从当前位置能到；应结合 `collision` 全部障碍和机制判断，不能只看旧 `solids` 地形。
- `solids`：地形的碰撞箱，每项 `[x, y, 宽, 高]`，由地形格子合并而成。会动、会塌、能撞碎的实心物不在这里，在 `entities` 里。
- `entities`：房间里可碰撞的实体，每项 `{"id", "type", "cat", "x", "y", "w", "h", …}`。`id` 在本次进入房间期间固定，用来跨暂停对照同一个实体；`type` 是游戏里的类型名；`cat` 是大类：`hazard` 致死物、`spring` 弹簧、`refill` 冲刺水晶、`booster` 泡泡、`feather` 羽毛、`collectible` 收集品、`dream_block` 梦境块、`dynamic_solid` 会动的实心块、`jumpthru` 单向平台、`solid` 其他实心块、`interactive` 可交互物、`other` 其他。尖刺有 `dir`（刺尖朝向）；碰撞形状不是矩形时有 `shape`；会动的实体附 `state`（与运动有关的字段）与 `moved`（与上一次暂停相比的位移）。`Killbox` 是坑底的致死区。
- `triggers`：触发器 `{"type", "rect"}`，不挡路，进入范围时触发效果。
- 桥接 1.0.14 起，暂未启用碰撞的实体也会列出，`collidable:false` 只表示当前状态；远处圆刺可能在靠近时启用，不能按空地处理。组合形状在 `collider` 中递归给出 `shape,x,y,w,h`，圆形另有 `r`，`list.parts` 包含所有子形状；每层都是世界坐标。旧 `parts` 只包含矩形，会遗漏圆形；字符地图也只是粗略表示。`grid`/`unknown` 的外接范围不代表内部全实心。
- `spawns` 本房间的重生点；`respawnPoint` 当前生效的重生点。
- `player`：Madeline 的完整状态，`pos` 精确位置（含小数部分）、`speed` `[vx, vy]`、`state`、`dashes` / `maxDashes`、`stamina`、`onGround`、`facing`、`ducking`、`hitbox` `[左, 上, 宽, 高]`、`internal`（内部计时器与状态的原始数值，计时器单位是秒）。
- 完整房间 JSON 的 `frame`、`sid`、`room` 用于核对观测身份。`solids` 为 `null` 表示本次没有提供完整地形，不能按空房间处理；需重新观测。
- 主控不提供可达性标签或路线判断。自行根据整房间碰撞箱、身体尺寸、实际开口、支撑及实体机制分析路线，再用具体动作验证。
- `story`、`dialog`：当前画面上的对白、教学提示、纪念碑文字与对话选项（有可见文字时提供）。

## act 的输入

可显式使用TAS按键输入格式 `input_format="celeste_tas"`，也可写入JSON计划（旧会话工具没有新参数时用计划字段）。此格式下C是第二冲刺、S暂停、O确认，不能与下述本项目格式混用；须agent剧情模式与桥接1.0.20。game/tas.py帮助显式读取纯输入区和生成窗口，动作返回来源游标与未执行后缀；不执行脚本命令、不自动补帧。模拟转换只支持游戏按键，不能表达的菜单等输入明确拒绝。

- `inputs` 是按顺序执行的按键段，每段写作 `"帧数,按键,按键..."`。只写帧数表示这些帧什么都不按，例如 `"5"`。一次 `act` 的帧数下限与上限见 `act` 的工具说明。
- 按键：方向 `L` `R` `U` `D`，跳 `J`，冲刺 `X`，crouch dash `Z`（以下蹲姿态冲刺），抓墙 `G`，交互 `T`；`K`、`Y`、`V` 是第二个跳、冲刺、crouch dash 键。
- agent 剧情模式下，`C` 确认、`B` 返回、`P` 暂停、`N` 日志；`menu`／`pauseMenu` 报告菜单状态，`ui` 报告 Poem 显示或结算 `confirmReady`。旧桥接可能没有 `ui`。
- 跳和冲刺是"按下"触发的：一直按住只算一次。要再按一次，中间至少松开 1 帧；或者换第二个键（按住 J 时按下 K 也算一次新的按下）。
- 跳和冲刺有约 5 帧的输入缓冲：提前几帧按下并按住，到能起跳或冲刺的那一帧会自动生效。
- `stop_on`（可选）：`death` 死亡、`room` 换房间、`land` 落地（起跳前站在地上的那一帧不算，离地后再落地才触发）、`refill` 冲刺恢复、`uncontrollable` 失去控制、`complete` 通关；默认 `["death", "room", "complete"]`。death 总会生效；agent 模式已登记通关后，不再用 complete 反复截断结算输入。触发后立即停下，剩余按键不执行。
- `plan_file`：把按键段写进工作区里的 JSON 文件，调用 `act` 时传 `plan_file`（工作区里的路径）代替 `inputs`。文件也可以写成对象 `{"inputs": [...], "expect": [...], "tolerance": 1}`：`expect` 每帧一项，是这一帧推进之后 Madeline 的预期位置 `[x, y]`（`null` 表示这一帧不检查），实际与预期相差超过 `tolerance` 像素就在那一帧停下（`deviate`）。
- 返回内容：实际执行了多少帧、停止原因、事件、取样轨迹（帧, x, y, vx, vy, 状态, 冲刺数, 是否着地），以及新的观测。

## 工作区里的数据

需要程序分析通道时，可用 `/workspace/game/collision_matrix.py` 从当前快照按需生成多来源矩阵文件；用法见同目录 `collision_matrix.md`。只输出分析摘要，不把整个矩阵读进上下文；无采样通路不等于房间不可通过。

- `/workspace/game/latest_obs.json`：最新的完整观测，格式 `{"decision": n, "obs": 观测}`，例如房间范围在 `obs.level.bounds`。
- `/workspace/game/collision.json`：当前碰撞快照，`image` 指向同帧碰撞图；图像只保留两代，旧路径失效时重读此文件，不沿用旧位置。
- `/workspace/game/last_act.json`：上一次 `act` 的输入与全精度逐帧轨迹，`trajectoryFormat` 给出各列的名字；第 j 行是用第 j 帧的输入推进之后的状态。
- `/workspace/game/rooms/`：每房首次观测的历史快照，只供回看初始情况，不代表当前实体位置。
