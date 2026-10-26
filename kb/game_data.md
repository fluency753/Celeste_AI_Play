# 工作区里的游戏数据（格式说明）

主控在每次 `observe` / `act` / `cutscene` 之后，把数据写进 `/workspace/game/`。这些文件是只给你的程序读的副本：改动它们不会影响游戏。

按需用 `/workspace/game/collision_matrix.py` 生成文件型多来源碰撞矩阵，按具体场景自行编写分析脚本，说明见同目录 `collision_matrix.md`（亦可读 `/kb/collision_matrix.md`）。工具不内置通行策略或路线，不能把某类来源一概当作永久阻挡。不要把完整矩阵打印进上下文；脚本结论仍需用动作或模拟验证。

**房间具体信息以这些精确数据（碰撞箱、实体、触发器等）为准，截图的视觉识别仅作为参考。** 默认文件模式 `workspace-files-v1` 只给状态、身份和当前文件位置；程序按需读取 `obs`，不额外查询同帧已有数据。无程序能力、文件失败或工具受限时，回退到共享形状布局／变化。显式区域查询仍保留为快捷查看入口。文件引用中的 `snapshot_id_scope=geometry-cache` 表示该 ID 与区域详情分页一致，并不是整个观测文件的内容哈希；主控发布引用前独立核对完整 `obs` 内容。

| 文件 | 何时写入 | 内容 |
| --- | --- | --- |
| `latest_obs.json` | 每次 `observe`、每次 `act` 结束 | `{"decision": n, "obs": 观测}`，当前帧的完整观测 |
| `collision.json` | 每次观测、动作和剧情操作结束 | 当前统一碰撞快照及 `image` 路径；图像只保留当前和前一代，旧路径失效时重读本文件 |
| `last_act.json` | 每次 `act` 结束 | 上一次 `act` 的输入与逐帧轨迹（全精度） |
| `rooms/<章节>__<房间>.json` | 第一次在某个房间得到观测时 | 历史首次观测，`snapshot.historical=true`；可回看初始几何，不能作为当前实体位置 |
| `sim_state.json` | 只在开放预置模拟器的运行里：每次游戏停下（`observe`、`act` 结束、自动重试之前） | 当前帧的完整场景，是 `/sim` 里预置模拟器的起始状态；格式不需要读懂，直接交给模拟器（见 `/sim/README.md`） |

`<章节>` 是章节编号里的 `/` 换成 `_`，例如 `Celeste_1-ForsakenCity__3.json`。

## 1. 观测 `obs`

```json
{
  "frame": 12345,
  "scene": "Level",
  "controllable": true,
  "save": {"name": "test", "slot": 7, "fileTimeTicks": 0},
  "level": {...},
  "player": {...},
  "entities": [...],
  "tiles": {...},
  "solids": [[x, y, 宽, 高], ...],
  "collision": {...},
  "exits": [...],
  "neighbors": [...],
  "spawns": [[x, y], ...],
  "respawnPoint": [x, y],
  "triggers": [...]
}
```

- `frame`：主控接管游戏以来推进过的总帧数（全局帧号），每推进一帧加 1。
- `controllable`：Madeline 此刻是否可操作（不在剧情、转场、暂停、冻结、死亡或入场动画中）。

### 1.0 `collision`：当前统一碰撞快照

桥接 1.0.24 起，直接读取当前地形 Grid 与实体 Collider，不从展示字符图反推。本节专指原始 `latest_obs.json` 的 `obs.collision`；默认工具投影为 `room-context-v2` 的布局／变化，区域精查另用 `shared-shapes-v1`，不能混用字段或坐标锚点。原始快照不存在或身份不匹配时明确不可用，不拼接旧实体。

- `version=1`；`frame/sid/room` 与当前观测对应；`scope` 为 `room` 或 `view`，`bounds` 是包含边距的覆盖范围。
- `coverage.terrain/entities` 表示两类来源是否请求并提供；`complete=true` 仅表示在该范围内来源均已覆盖，局部视图仍不代表全房间。`geometryComplete` 单独表示所有形状是否能精确表达。
- `objects` 每项保留 `id/source/type/kind/collidable/mutable/geometry`。`source` 区分地形和实体；`kind` 为 solid、one_way、hazard、sensor、unknown；Key 等触发类对象不是墙。`category` 与其他实体属性保留梦境块、门等机制语义，kind 不表示永不可穿越。
- `geometry` 用世界坐标表达 rect、circle、list 或 grid；list/grid 的 `parts` 保留真实组成与空洞。unknown 的包围范围不是已确认实心。地形序号只标识本次快照条目，实体 ID 可在同一场景跨帧对照。
- 实体 `mutable=true`：后续可能移动、变形、禁用或消失，不能因当前零速判成静态。`collidable=false` 保留形状供识别，当前不参与碰撞。当前阻挡不等于永久不可达。
- `player.hitbox` 是同一快照帧的角色矩形。工具同时给分色碰撞图；图像只帮助看清几何，不预测运动或输出可达性结论。`collision.json.image` 指向帧号与内容哈希命名的不可变 PNG；只保留两代，路径不存在时重读当前 manifest，不回退到旧帧。

### 工具布局、变化与查询

默认工具使用 `room-context-v2`，首次入房或默认 `observe` 返回 `mode=baseline` 的完整布局，替换旧基线。`groups` 只共用相同属性，不省略实例：每组 `instances` 的行按本组 `instanceColumns`（省略时为 `[id,x,y]`）读取，可能另有 `shape/state` 列；共同的形状或状态写在组上。`shape` 引用同包 `shapes`，形状及所有嵌套 x/y 加实例 x/y 还原世界坐标。所有实例位置保留，组不是实心包围区域，不判定可达性。默认 `stateDetail` 明示仅精选游戏状态，完整反射仍可精查。

同房 `act/cutscene` 返回 `mode=delta`：核对 `base_snapshot_id`，应用 `added/changed` 中的完整替换对象（按 source/id），`removed` 是本覆盖范围内不再观测的 `[source,id]`，不表示永久消失。每条变化自带所需形状定义；`roomChanges` 按同名字段替换首次布局外层的 `exits/spawns/respawnPoint/triggers/entities`，空值也必须应用。地形不变就不重复发送，移动、生成、形状或碰撞状态变化照常报告；范围变化、缓存失效或变化过大时重发 baseline。快照链缺失、历史压缩后记不清布局时默认 `observe` 重建，不猜测缺失数据。

`observe(detail="geometry", region=[x,y,w,h], limit=32)` 只读最近观测缓存，返回区域精确对象与完整反射状态，格式为 `shared-shapes-v1`（独立的本包形状表）。后续页带 `offset=next_offset` 与同一 `snapshot_id`；动作或新观测后重新从第一页查。`observe(detail="player")` 读取完整玩家内部状态。两种详情都不推进游戏，也不改变变化基线。局部查询超出来源范围、来源缺失或存在未知形状时，空结果不证明安全。完整原始工作区与完整碰撞图不变，不必把整房 JSON 打印进聊天。

### 1.1 `level`

| 字段 | 含义 |
| --- | --- |
| `sid` | 章节编号，如 `Celeste/0-Intro`、`Celeste/1-ForsakenCity`、`Celeste/2-OldSite` |
| `side` | `A` / `B` / `C` 面 |
| `room` | 房间名（游戏内部名，字符串） |
| `bounds` | 房间范围 `[x, y, 宽, 高]`（世界坐标，像素）。走出边界会进入相邻房间；从底部掉出且下面没有房间会死亡 |
| `camera` | 画面左上角的世界坐标 `[x, y]`；画面大小 320×180 |
| `inCutscene` / `skippingCutscene` / `paused` / `transitioning` / `frozen` / `completed` | 剧情中、正在跳过剧情、暂停、房间转场中、游戏冻结（如冲刺开始的几帧）、章节已完成 |
| `freezeTimer` / `freezeFrames` | freeze frames：剩余冻结秒数，以及接下来还会冻结几帧（冲刺开始、吃冲刺水晶、撞碎冲刺砖、离开梦境块等，见 `README.md` §3.3）。例如 `act` 在吃到水晶那一帧停下（`stop_on` 含 `refill`）时，这里会告诉你接下来还有几帧 Madeline 不动 |
| `wind` | 当前风的速度 `[x, y]`（像素/秒）；没有风时为 `[0, 0]` |
| `canPause`、`dialogOpen`、`dialogWaiting` | 主控跳过剧情时使用，可以忽略 |
| `chapterTimeTicks` | 章节计时，1 tick = 100 纳秒（÷ 10 000 000 得秒） |
| `deaths` / `deathsInRoom` | 本章死亡次数 / 本房间死亡次数 |

### 1.2 `player`

| 字段 | 含义 |
| --- | --- |
| `pos` | 精确位置 `[x, y]` = 整数位置 + subpixel 余量；是碰撞箱**底边中点** |
| `speed` | 速度 `[vx, vy]`，像素/秒，y 向下为正 |
| `state` / `stateId` | 状态机状态名与编号（Normal=0、Climb=1、Dash=2、Boost=4、RedDash=5、DreamDash=9、Dummy=11 等） |
| `dashes` / `maxDashes` | 当前冲刺次数 / 上限 |
| `stamina` | 体力（满值 110） |
| `onGround`、`facing`（`R`/`L`）、`ducking`、`canDash`、`dead`、`inControl` | 是否着地、朝向、是否下蹲、此刻能否冲刺、是否死亡、是否受玩家控制 |
| `holding` | 手里拿着的物体类型名（一般为 null） |
| `wallLeft` / `wallRight` | 左/右 1 像素处是否有实心物（贴墙） |
| `hitbox` | 当前碰撞箱 `[左, 上, 宽, 高]`（世界坐标） |
| `timers` | 几个计时器的剩余**帧数**（向上取整）：`jumpGrace` coyote time、`varJump` variable jump、`dashCooldown` dash cooldown |
| `internal` | 模拟器需要的内部状态，原始数值（计时器单位是**秒**），见下 |

`internal` 的字段：

| 字段 | 含义 |
| --- | --- |
| `position` / `remainder` | 整数位置与 subpixel 余量（`pos` = 二者之和） |
| `speed` | 同 `player.speed` |
| `liftSpeed` / `liftSpeedTimer` | 从移动平台继承的速度及其剩余保留时间 |
| `dashDir` / `lastAim` | 当前（或上一次）冲刺方向的单位向量 / 上一帧的瞄准方向 |
| `moveX` | 上一帧读到的水平输入（-1、0、1） |
| `onGround` / `wasOnGround` | 本帧、上一帧是否着地 |
| `jumpGraceTimer`、`varJumpTimer`、`varJumpSpeed` | coyote time 剩余、variable jump 剩余与 variable jump 保持的速度 |
| `autoJump` / `autoJumpTimer` | 弹簧等造成的"自动按住跳"及其剩余时间 |
| `maxFall` | 当前的下落速度上限 |
| `dashCooldownTimer`、`dashRefillCooldownTimer`、`dashAttackTimer`、`dashStartedOnGround` | dash cooldown、落地补充冲刺前的冷却、dash attack 窗口、本次冲刺是否从地面开始 |
| `forceMoveX` / `forceMoveXTimer` | 被强制的水平输入（如 walljump 后）及剩余时间 |
| `wallSlideTimer` / `wallSlideDir` | wallslide 的剩余时间与方向 |
| `wallSpeedRetentionTimer` / `wallSpeedRetained` | 撞墙后保留水平速度的剩余时间与被保留的速度 |
| `wallBoostDir` / `wallBoostTimer` | climbjump 后的反向加速窗口 |
| `climbNoMoveTimer`、`hopWaitX`、`hopWaitXSpeed` | 攀爬相关计时与爬到墙顶后的跳跃等待 |
| `noWindTimer`、`lowFrictionStopTimer`、`minHoldTimer` | 风、低摩擦地面与抓取相关的计时（本轮章节基本用不到） |

这些字段的含义与更新规则见 `player_physics.md`。用观测里的 `internal` 作为模拟器的初始状态，就能从任意一帧接着推演。

### 1.3 `entities`

**整个房间**（房间范围外扩 16 像素）内可碰撞的实体，不含 Madeline、触发器与地形网格；已被吃掉、等待重生的冲刺水晶也会列出：

```json
{"type": "Spikes", "cat": "hazard", "x": 64, "y": 96, "w": 16, "h": 3, "dir": "Up"}
```

- `id`：实体编号。同一次进入房间期间固定不变，用来跨暂停、跨帧对照同一个实体（离开房间再回来会重新编号）。
- `type`：游戏里的类型名（与 `entities.md` 的条目一一对应）。
- `cat`：大类：`hazard` 致死物、`spring` 弹簧、`refill` 冲刺水晶、`booster` 泡泡、`feather` 羽毛、`collectible` 收集品、`dream_block` 梦境块、`dynamic_solid` 会动的实心块、`jumpthru` 单向平台、`solid` 其他实心块、`interactive` 可交互物、`other` 其他。
- `x, y, w, h`：碰撞箱左上角与宽高（世界坐标）。`shape` 不是矩形时出现：`circle`（此时 `r` 为半径，`x, y, w, h` 是外接矩形）、`list`（多个碰撞箱的组合，给出的是外接矩形）、`grid`。
- `visible: false`：实体存在但当前不可见。
- `collidable`：当前是否启用碰撞。暂时禁用的实体仍保留在观测中；例如远处圆刺会在接近时启用，不能把 `false` 当成永久安全。
- `collider`（桥接 1.0.14 起）：组合碰撞体的形状树。每层含 `shape,x,y,w,h`；圆形另含半径 `r`，圆心为外接矩形中心；`list` 的 `parts` 递归包含矩形、圆形及子组合。所有层均为世界坐标，不再叠加父位置。`grid`/`unknown` 只描述外接范围，不能假定内部全实心。
- `parts`：旧的矩形部分列表 `[x, y, 宽, 高]`，仅保留供旧程序使用，**不包含圆形，不能用它代替完整组合**；新程序读取 `collider`。字符地图也是粗略外接范围，精确判断使用实体形状。
- `Killbox`（`cat` 为 `hazard`）：坑底的致死区，碰到即死；它只在 Madeline 接近时才真正生效，但一直列出。
- `fields`：该实体当前的全部状态字段（自动读取实体自身声明的数值、布尔、枚举、向量、矩形与节点数组，例如速度、所处状态、计时器、轨道节点、进度、目标点），外加 `liftSpeed`（平台或角色当前带给乘客的速度）与 `stateMachine`（实体有状态机时的当前状态编号）。字段名就是游戏内部的名字，含义可结合 `entities.md` 推断；只有数值，没有代码。
- `moved`：与上一次暂停时相比的位移 `[dx, dy]`，只在移动过的实体上出现。
- 附加属性：
  - 尖刺 `dir`（Up / Down / Left / Right，刺尖朝向）；弹簧 `orient`（Floor / WallLeft / WallRight，含义见 `entities.md`）；泡泡 `red`。
  - 冲刺水晶 `twoDashes`；被吃掉后 `collected: true` 与 `respawnTimer`（距重生的秒数）。
  - 交通块 ZipMover：`start` / `target` 轨道两端（实体左上角的世界坐标）、`percent` 当前进度（0 在起点，1 在终点）。
  - 坠落块 FallingBlock：`triggered` 已被触发、`falling` 已开始下落。
  - 冲刺砖 DashBlock：`canDash` 能否被冲刺撞碎。
  - 梦境块 DreamBlock：`active` 是否已能冲刺穿过；会移动的梦境块有 `node`（另一端位置）。
  - 开关门 SwitchGate：`node`（打开后移动到的位置）；触碰开关 TouchSwitch：`activated`。
  - 桥块 BridgeTile：`fallen` 是否已塌落。

### 1.4 `tiles`

**整个房间**（外扩 16 像素）的字符地图，每格 8×8 像素（`scope` 字段为 `room`）：

```json
{"origin": [0, -8], "cell": 8, "rows": ["####......", "..."], "legend": {"#": "地形（实心）", ...}}
```

- 第 `r` 行第 `c` 列格子覆盖世界坐标 `x ∈ [origin.x + 8c, origin.x + 8c + 8)`，`y ∈ [origin.y + 8r, origin.y + 8r + 8)`。
- `#` 是地形网格（实心）。`= - ^ v < > * S o O F ~ $ ?` 是叠加上去的实体（含义见 `legend`），精确形状以 `entities` 为准；`M` 是 Madeline。
- 地形网格本身就是 8 像素对齐的，`#` 格子就是精确的实心区域。

### 1.5 `solids`、`exits`、`neighbors`、`spawns`、`respawnPoint`、`triggers`

由主控根据整房间数据补充：

- `solids`：旧地形专用字段，不包含实体。新快照存在且覆盖地形时，直接取 `collision` 的地形矩形；旧桥接仍从字符图的 `#` 推导，可能受展示符号遮盖影响。全部当前碰撞体以 `collision` 为统一入口，不能只根据 `solids` 判断通路。
- `neighbors`：与本房间边界相接或只隔几个像素的房间 `{"room": 名称, "bounds": [x, y, 宽, 高]}`，只有范围，不含内容。有的相邻房间与本房间之间隔着几个像素的缝（例如 1A 开头几个房间上下相隔 4 像素）；游戏换房时是在 Madeline 中心外侧 8 像素（左右）或 12 像素（上下）处找房间，所以这样的缝照样能换房。
- `exits`：本房间与相邻房间正对着的边界段（含上面说的缝），即候选出口：`{"side": "left/right/top/bottom", "x": ..., "y": [起, 止], "room": 相邻房间}`。是否能通过须核对 `collision` 中的地形、实体、危险物及机制，不以边界投影作为通路证明。
- `spawns`：本房间的重生点（死亡后可能在这些点之一重生）；`respawnPoint`：当前生效的重生点。
- `triggers`：触发器 `{"type": 类型名, "rect": [x, y, 宽, 高]}`。触发器不挡路，Madeline 进入范围时触发效果（例如改变重生点、开始剧情）；返回给你的文字里省略了镜头、音乐、光照等与操作无关的触发器，文件里保留全部。

## 2. 上一次动作 `last_act.json`

```json
{
  "decision": 12,
  "attempt": 0,
  "thought": "……",
  "inputs": ["12,R", "1,R,J", "15,R,J"],
  "plan_file": null,
  "stop_on": ["death", "room", "complete"],
  "result": {
    "stop": "done",
    "consumed": 28, "planned": 28, "loadingFrames": 0,
    "startFrame": 1000, "endFrame": 1028,
    "events": [{"frame": 1015, "type": "land", "detail": null}],
    "trajectoryFormat": "frame,x,y,vx,vy,state,dashes,onGround",
    "trajectory": [[1001, 120.5, 96.0, 16.6666, 0.0, "Normal", 1, 1], ...]
  },
  "auto_frames": 0,
  "auto_notes": []
}
```

- `attempt`：0 是你提交的计划；1、2…是自动重试时重算出的计划（§3.3）。
- `stop`：停止原因：`done` 全部执行完、`death`、`room` 换房、`land` 落地、`refill` 冲刺恢复、`uncontrollable`、`controllable`、`complete`，以及 `deviate` 偏离预期轨迹（计划里给了 `expect` 时，见 §3.2；此时 `result` 里还有 `deviation`）。
- `consumed` / `planned`：实际执行 / 计划执行的帧数（提前停下时前者更小）。
- `trajectory`：每个执行帧一行，全精度。**第 j 行（从 1 数）是按下第 j 帧的按键、游戏更新完这一帧之后的状态**，帧号 = `startFrame + j`。所以 `obs.frame` 等于下一次 `act` 的 `startFrame`，轨迹第一行反映的是第一帧输入的效果。
- `events`：该段内发生的事件及其帧号。
- `entityTrajectory`：画面附近（外扩 64 像素）实体的逐帧位置，格式 `[帧, 实体编号, x, y]`（碰撞箱左上角），只在位置变化的帧记录一行；编号与观测里的 `id` 对应。用它可以拟合移动平台、追逐者、落石等的运动规律。
- `auto_frames` / `auto_notes`：动作结束后主控自动推进的帧数与说明（转场、剧情、死亡与重生）。这些帧不在 `trajectory` 里。

## 3. 计划文件（`act` 的 `plan_file` 参数）

工作区里的 JSON 文件。最简单的写法是按键段数组：

```json
["12,R", "1,R,J", "15,R,J", "8,R"]
```

完整写法是对象，除 `inputs` 外都可省略（其他字段，例如 `note`，主控不读，可以自由附带）：

```json
{
  "inputs": ["1,R,Z", "3,R", "6,R", "1,R,J", "11,R,J", "30,R"],
  "expect": [[1312.0, -1040.0], [1312.0, -1040.0, "Dash"], null, "…每帧一项…"],
  "tolerance": 0.5,
  "stop_on": ["room", "land"],
  "retry": {"command": "python3 planner.py --replan", "max": 3, "timeout_s": 20, "on": ["death", "deviate"]}
}
```

### 3.1 按键段

JSON计划或act可显式声明 `input_format="celeste_tas"`（agent剧情模式、桥接1.0.20起）。此时字母按TAS按键解释：J/K双跳、X/C双冲刺、Z/V双蹲冲、G/H双抓、S暂停、Q重试、N日志、O确认，并保留跳跃兼确认、冲刺兼取消/交互等绑定。省略仍用下列本项目格式，不按文件扩展名猜测。
工作区 `game/tas.py` 可读取明确选定的纯输入行并按帧窗口写JSON计划，保留来源行与游标；拒绝console/Set/Read等命令。执行后读取input_cursor与remaining_inputs，按实际consumed续接；TAS模式不自动补剧情/转场帧。详见系统输入职责说明。F摇杆、A/M独立方向、自定义键暂不支持。

写法与 `act` 的 `inputs` 完全一样：

- `"帧数"`：这些帧什么都不按；`"帧数,按键,按键…"`：这些帧按住列出的键。
- 按键：`L R U D` 方向，`J` 跳，`X` 冲刺，`Z` crouch dash（以下蹲姿态冲刺，见 `input.md`），`G` 抓，`T` 交互；`K`、`Y`、`V` 分别是第二个跳、冲刺、crouch dash 键；`A:角度` 表示左摇杆模拟方向（0 为右、90 为上、180 为左、270 为下，满幅度），出现时取代方向键。
- agent 剧情模式另开放 `C` 确认、`B` 返回、`P` 暂停、`N` 日志。菜单、重试、剧情和结算同样由智能体下指令；这套字母映射与 CelesteTAS 不完全相同。
- `ui` 报告当前 Poem 的显示状态或 AreaComplete 的 `confirmReady`（桥接 1.0.19 起）；Poem 可见不保证已接受确认。`menu` 和 `pauseMenu` 给出当前菜单及选项，不替智能体选择。
- 原生 `completed=true` 仅表示登记成绩；agent 模式仍须自行走完结算并回到稳定的选章地图，主控才确认整章完成。
- 段与段之间不会自动松键：`"1,R,J"` 后接 `"14,R,J"` 等于连续按住跳 15 帧，只算一次起跳。同一个键要再"按下"一次，中间至少要有 1 帧不按它；**换另一个键就不用**：按住 J 时按下 K 也算一次新的起跳输入（冲刺 X/Y、crouch dash Z/V 同理），所以相邻两帧可以各跳一次（`techniques.md` 7.5）。
- 单次 `act` 最多 600 帧，超过就拆成几次；角色可以操作时至少 40 帧（不足时在末尾补空帧，截断条件照样提前停下）。

### 3.2 预期轨迹 `expect` 与容差 `tolerance`

- `expect` 每帧一项，与按键逐帧对齐：第 i 项是**计划第 i 帧推进之后** Madeline 的位置，口径与 `last_act.json` 轨迹里的 `x`、`y` 相同（整数位置 + subpixel 余量，全精度）。可以写成 `[x, y]`，也可以附状态名 `[x, y, "Dash"]`（状态名与轨迹的 `state` 列相同，如 `Normal`、`Dash`、`Climb`）；`null` 表示这一帧不检查。
- `expect` 可以比按键短（只检查前面若干帧），不能比按键长。
- freeze frames 里 Madeline 不动，预期位置要与上一帧相同；动作执行时的加载帧不算计划帧，不需要写。
- 桥接在每帧推进后立即对照：x 或 y 与预期相差超过 `tolerance` 像素（默认 1），或给出的状态名不符，就在**这一帧**停下，停止原因 `deviate`。`last_act.json` 的 `result` 里会有：
  - `expectFrames`、`tolerance`：本次对照了几帧、容差多少；
  - `deviation`：`{"frame": 全局帧号, "index": 计划第几帧（从 1 起）, "expected": [x, y(, 状态)], "actual": [x, y, 状态]}`。
- 用途：模拟器的预测与实际不符时，及早停下重算，而不是带着错误把整个房间按完；同时得到"第几帧开始不一致"的精确线索，用来修正模拟器。

### 3.3 自动重试 `retry`

在计划里声明一个重算命令，主控遇到指定情况时自己运行它、执行它改写后的计划，**不需要你再调用工具**：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `command` | 必填 | 在 `/workspace` 里用 `sh -c` 运行的命令，最多 500 字符（长的写成脚本） |
| `max` | 3 | 本次 `act` 最多重算几次（1～5） |
| `timeout_s` | 20 | 每次重算的时限（1～60 秒），超时连同子进程一起结束 |
| `on` | `["death", "deviate"]` | 哪些情况触发：`death` 死亡（主控先推进到重生、可操作之后再运行）、`deviate` 偏离预期、`done` 按键用完但仍在本房间（用来"先走一段、再算下一段"） |

- 触发条件还要满足：Madeline 可操作，并且仍在本次 `act` 开始时的房间里。换了房间、章节完成、或停在其他原因（例如 `land`）都直接返回给你。
- 运行前主控已经写好最新数据：`game/latest_obs.json`（当前完整观测）、`game/last_act.json`（刚才那次执行的逐帧轨迹与偏差）、开放预置模拟器时还有 `game/sim_state.json`（当前帧的模拟器起始状态），以及 `game/retry.json`：

```json
{"decision": 12, "attempt": 1, "max": 3, "reason": "deviate", "plan_file": "/workspace/plans/room.json",
 "deviation": {"frame": 867, "index": 15, "expected": [1356.0, -1062.75], "actual": [1356.0, -1064.75, "Normal"]},
 "events": [...], "consumed": 15, "planned": 71}
```

- 环境变量：`CELESTE_RETRY_ATTEMPT`（第几次重算）、`CELESTE_RETRY_REASON`、`CELESTE_RETRY_FILE`（即 `/workspace/game/retry.json`）、`CELESTE_PLAN_FILE`（计划文件的路径）。
- 命令要**从当前状态出发**重算，并把新计划写回同一个计划文件（`CELESTE_PLAN_FILE`），然后以退出码 0 结束。主控读回文件：退出码不为 0、超时、文件没有改动、或新计划不合法，都会停止重试，把经过（含错误输出的末尾）返回给你。
- 新计划里的 `inputs`、`expect`、`tolerance`、`stop_on` 生效；`retry` 一律沿用第一次提交时的设置，重算出的计划不能增加次数。
- 运行环境与 Bash 沙箱相同：完全没有网络，只能写 `/workspace` 与 `/tmp`，环境变量只有 PATH、HOME（= /tmp）、LANG 与上面的 `CELESTE_*`。预装的 Python 库都能用。
- 重算用时计入工具时间；每次重算与每次执行都记进决策日志（`attempt` 字段），`act` 的返回里有"自动重试经过"。
