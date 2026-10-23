# 工作区里的游戏数据（格式说明）

主控在每次 `observe` / `act` 之后，把数据写进 `/workspace/game/`。这些文件是只给你的程序读的副本：改动它们不会影响游戏。

| 文件 | 何时写入 | 内容 |
| --- | --- | --- |
| `latest_obs.json` | 每次 `observe`、每次 `act` 结束 | `{"decision": n, "obs": 观测}`，当前帧的完整观测 |
| `last_act.json` | 每次 `act` 结束 | 上一次 `act` 的输入与逐帧轨迹（全精度） |
| `rooms/<章节>__<房间>.json` | 第一次在某个房间得到观测时 | 同 `latest_obs.json`，便于以后回看该房间的初始几何 |

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
| `canPause`、`dialogOpen`、`dialogWaiting` | 主控跳过剧情时使用，可以忽略 |
| `chapterTimeTicks` | 章节计时，1 tick = 100 纳秒（÷ 10 000 000 得秒） |
| `deaths` / `deathsInRoom` | 本章死亡次数 / 本房间死亡次数 |

### 1.2 `player`

| 字段 | 含义 |
| --- | --- |
| `pos` | 精确位置 `[x, y]` = 整数位置 + 亚像素余量；是碰撞箱**底边中点** |
| `speed` | 速度 `[vx, vy]`，像素/秒，y 向下为正 |
| `state` / `stateId` | 状态机状态名与编号（Normal=0、Climb=1、Dash=2、Boost=4、RedDash=5、DreamDash=9、Dummy=11 等） |
| `dashes` / `maxDashes` | 当前冲刺次数 / 上限 |
| `stamina` | 体力（满值 110） |
| `onGround`、`facing`（`R`/`L`）、`ducking`、`canDash`、`dead`、`inControl` | 是否着地、朝向、是否下蹲、此刻能否冲刺、是否死亡、是否受玩家控制 |
| `holding` | 手里拿着的物体类型名（一般为 null） |
| `wallLeft` / `wallRight` | 左/右 1 像素处是否有实心物（贴墙） |
| `hitbox` | 当前碰撞箱 `[左, 上, 宽, 高]`（世界坐标） |
| `timers` | 几个计时器的剩余**帧数**（向上取整）：`jumpGrace` 土狼时间、`varJump` 可变跳、`dashCooldown` 冲刺冷却 |
| `internal` | 模拟器需要的内部状态，原始数值（计时器单位是**秒**），见下 |

`internal` 的字段：

| 字段 | 含义 |
| --- | --- |
| `position` / `remainder` | 整数位置与亚像素余量（`pos` = 二者之和） |
| `speed` | 同 `player.speed` |
| `liftSpeed` / `liftSpeedTimer` | 从移动平台继承的速度及其剩余保留时间 |
| `dashDir` / `lastAim` | 当前（或上一次）冲刺方向的单位向量 / 上一帧的瞄准方向 |
| `moveX` | 上一帧读到的水平输入（-1、0、1） |
| `onGround` / `wasOnGround` | 本帧、上一帧是否着地 |
| `jumpGraceTimer`、`varJumpTimer`、`varJumpSpeed` | 土狼时间剩余、可变跳剩余与可变跳保持的速度 |
| `autoJump` / `autoJumpTimer` | 弹簧等造成的"自动按住跳"及其剩余时间 |
| `maxFall` | 当前的下落速度上限 |
| `dashCooldownTimer`、`dashRefillCooldownTimer`、`dashAttackTimer`、`dashStartedOnGround` | 冲刺冷却、落地补充冲刺前的冷却、冲刺攻击窗口、本次冲刺是否从地面开始 |
| `forceMoveX` / `forceMoveXTimer` | 被强制的水平输入（如蹬墙跳后）及剩余时间 |
| `wallSlideTimer` / `wallSlideDir` | 贴墙下滑的剩余时间与方向 |
| `wallSpeedRetentionTimer` / `wallSpeedRetained` | 撞墙后保留水平速度的剩余时间与被保留的速度 |
| `wallBoostDir` / `wallBoostTimer` | 抓墙跳后的反向加速窗口 |
| `climbNoMoveTimer`、`hopWaitX`、`hopWaitXSpeed` | 攀爬相关计时与爬到墙顶后的跳跃等待 |
| `noWindTimer`、`lowFrictionStopTimer`、`minHoldTimer` | 风、低摩擦地面与抓取相关的计时（本轮章节基本用不到） |

这些字段的含义与更新规则见 `player_physics.md`。用观测里的 `internal` 作为模拟器的初始状态，就能从任意一帧接着推演。

### 1.3 `entities`

**整个房间**（房间范围外扩 16 像素）内可碰撞的实体，不含 Madeline、触发器与地形网格；已被吃掉、等待重生的冲刺水晶也会列出：

```json
{"type": "Spikes", "cat": "hazard", "x": 64, "y": 96, "w": 16, "h": 3, "dir": "Up"}
```

- `type`：游戏里的类型名（与 `entities.md` 的条目一一对应）。
- `cat`：大类：`hazard` 致死物、`spring` 弹簧、`refill` 冲刺水晶、`booster` 泡泡、`feather` 羽毛、`collectible` 收集品、`dream_block` 梦境块、`dynamic_solid` 会动的实心块、`jumpthru` 单向平台、`solid` 其他实心块、`interactive` 可交互物、`other` 其他。
- `x, y, w, h`：碰撞箱左上角与宽高（世界坐标）。`shape` 不是矩形时出现：`circle`（此时 `r` 为半径，`x, y, w, h` 是外接矩形）、`list`（多个碰撞箱的组合，给出的是外接矩形）、`grid`。
- `visible: false`：实体存在但当前不可见。
- `parts`：由多个碰撞箱组合成的实体（`shape` 为 `list`），逐个给出其中矩形部分的 `[x, y, 宽, 高]`。
- `Killbox`（`cat` 为 `hazard`）：坑底的致死区，碰到即死；它只在 Madeline 接近时才真正生效，但一直列出。
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
- `neighbors`：与本房间边界相接的房间 `{"room": 名称, "bounds": [x, y, 宽, 高]}`，只有范围，不含内容。
- `exits`：本房间与相邻房间共用的边界段，即可能的出口：`{"side": "left/right/top/bottom", "x": ..., "y": [起, 止], "room": 相邻房间}`（左右出口给出 x 与 y 区间，上下出口给出 y 与 x 区间）。出口段上是否被地形挡住，要对照 `solids`。
- `spawns`：本房间的重生点（死亡后可能在这些点之一重生）；`respawnPoint`：当前生效的重生点。
- `triggers`：触发器 `{"type": 类型名, "rect": [x, y, 宽, 高]}`。触发器不挡路，Madeline 进入范围时触发效果（例如改变重生点、开始剧情）；返回给你的文字里省略了镜头、音乐、光照等与操作无关的触发器，文件里保留全部。

## 2. 上一次动作 `last_act.json`

```json
{
  "decision": 12,
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

- `stop`：停止原因：`done` 全部执行完、`death`、`room` 换房、`land` 落地、`refill` 冲刺恢复、`uncontrollable`、`controllable`、`complete`。
- `consumed` / `planned`：实际执行 / 计划执行的帧数（提前停下时前者更小）。
- `trajectory`：每个执行帧一行，全精度。**第 j 行（从 1 数）是按下第 j 帧的按键、游戏更新完这一帧之后的状态**，帧号 = `startFrame + j`。所以 `obs.frame` 等于下一次 `act` 的 `startFrame`，轨迹第一行反映的是第一帧输入的效果。
- `events`：该段内发生的事件及其帧号。
- `auto_frames` / `auto_notes`：动作结束后主控自动推进的帧数与说明（转场、剧情、死亡与重生）。这些帧不在 `trajectory` 里。

## 3. 计划文件（`act` 的 `plan_file` 参数）

工作区里的 JSON 文件，内容是按键段数组，或带 `inputs` 字段的对象：

```json
["12,R", "1,R,J", "15,R,J", "8,R"]
```

```json
{"inputs": ["30,R", "1,R,X", "20,R"], "note": "可以附带任何其他字段，主控只读 inputs"}
```

按键段写法与 `act` 的 `inputs` 完全一样：

- `"帧数"`：这些帧什么都不按；`"帧数,按键,按键…"`：这些帧按住列出的键。
- 按键：`L R U D` 方向，`J` 跳，`X` 冲刺，`Z` 蹲冲（以下蹲姿态冲刺，见 `input.md`），`G` 抓，`T` 交互；`A:角度` 表示左摇杆模拟方向（0 为右、90 为上、180 为左、270 为下，满幅度），出现时取代方向键。
- 段与段之间不会自动松键：`"1,R,J"` 后接 `"14,R,J"` 等于连续按住跳 15 帧，只算一次起跳。要再跳一次，中间至少要有 1 帧不按 J（冲刺 X 同理）。
- 单次 `act` 最多 600 帧，超过就拆成几次。
