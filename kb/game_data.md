# 工作区里的游戏数据（格式说明）

主控在每次 `observe` / `act` 之后，把数据写进 `/workspace/game/`。这些文件是只给你的程序读的副本：改动它们不会影响游戏。

**房间具体信息以这些精确数据（碰撞箱、实体、触发器等）为准，截图的视觉识别仅作为参考。** 返回文字里"整个房间的精确数据"一段与这里的 `obs` 同源，只是省略了实体的 `fields`。

| 文件 | 何时写入 | 内容 |
| --- | --- | --- |
| `latest_obs.json` | 每次 `observe`、每次 `act` 结束 | `{"decision": n, "obs": 观测}`，当前帧的完整观测 |
| `last_act.json` | 每次 `act` 结束 | 上一次 `act` 的输入与逐帧轨迹（全精度） |
| `rooms/<章节>__<房间>.json` | 第一次在某个房间得到观测时 | 同 `latest_obs.json`，便于以后回看该房间的初始几何 |
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
  "exits": [...],
  "neighbors": [...],
  "spawns": [[x, y], ...],
  "respawnPoint": [x, y],
  "triggers": [...]
}
```

- `frame`：主控接管游戏以来推进过的总帧数（全局帧号），每推进一帧加 1。
- `controllable`：Madeline 此刻是否可操作（不在剧情、转场、暂停、冻结、死亡或入场动画中）。

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
- `parts`：由多个碰撞箱组合成的实体（`shape` 为 `list`），逐个给出其中矩形部分的 `[x, y, 宽, 高]`。
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

- `solids`：把 `tiles` 里所有 `#` 格子合并成的矩形碰撞箱 `[x, y, 宽, 高]`（像素，世界坐标）。与 `tiles` 表示同一片地形，只是换成碰撞箱形式；其他实心物（坠落块、交通块等）在 `entities` 里。
- `neighbors`：与本房间边界相接或只隔几个像素的房间 `{"room": 名称, "bounds": [x, y, 宽, 高]}`，只有范围，不含内容。有的相邻房间与本房间之间隔着几个像素的缝（例如 1A 开头几个房间上下相隔 4 像素）；游戏换房时是在 Madeline 中心外侧 8 像素（左右）或 12 像素（上下）处找房间，所以这样的缝照样能换房。
- `exits`：本房间与相邻房间正对着的边界段（含上面说的缝），即可能的出口：`{"side": "left/right/top/bottom", "x": ..., "y": [起, 止], "room": 相邻房间}`（左右出口给出 x 与 y 区间，上下出口给出 y 与 x 区间）。出口段上是否被地形挡住，要对照 `solids`。
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

写法与 `act` 的 `inputs` 完全一样：

- `"帧数"`：这些帧什么都不按；`"帧数,按键,按键…"`：这些帧按住列出的键。
- 按键：`L R U D` 方向，`J` 跳，`X` 冲刺，`Z` crouch dash（以下蹲姿态冲刺，见 `input.md`），`G` 抓，`T` 交互；`K`、`Y`、`V` 分别是第二个跳、冲刺、crouch dash 键；`A:角度` 表示左摇杆模拟方向（0 为右、90 为上、180 为左、270 为下，满幅度），出现时取代方向键。
- 段与段之间不会自动松键：`"1,R,J"` 后接 `"14,R,J"` 等于连续按住跳 15 帧，只算一次起跳。同一个键要再"按下"一次，中间至少要有 1 帧不按它；**换另一个键就不用**：按住 J 时按下 K 也算一次新的起跳输入（冲刺 X/Y、crouch dash Z/V 同理），所以相邻两帧可以各跳一次（`techniques.md` 7.5）。
- 单次 `act` 最多 600 帧，超过就拆成几次。

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
