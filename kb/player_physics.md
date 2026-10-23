# Madeline 物理与状态机（核心）

约定见 `README.md`：px、y 向下、dt = 0.0166667 s（float32）、`Position` = 碰撞箱底边中点、subpixel 余量模型。本文中"帧"指一次正常的逻辑更新（freeze frames 不算，除非特别说明）。

记号：
- `Approach(v, t, m)`：若 v > t 则 `max(v − m, t)`，否则 `min(v + m, t)`。即"向 t 靠近最多 m，不越过"。
- `sign(x)`：−1 / 0 / 1。
- `moveX`：本帧使用的水平输入（−1/0/1，可能被"强制水平移动"覆盖，见 §3 第 14 步）。
- `Facing`：朝向，+1 右，−1 左。
- `GrabCheck`：抓键当前是否按住（本项目是按住模式）。
- `Jump.Pressed` / `Jump.Check`：跳键"本帧算作按下"（含缓冲）/"当前按住"。详见 `input.md`。
- 加速度类的每帧步长 `rate × mult × dt` 先用双精度相乘再转成 float32（Everest 的修补），然后 `Approach` 用 float32。

---

## 1. 常量总表

### 1.1 跑、跳、重力

| 名称 | 值 | 单位 | 含义 |
| --- | --- | --- | --- |
| MaxFall | 160 | px/s | 正常下落速度上限 |
| Gravity | 900 | px/s² | 重力；每帧 +15 px/s |
| HalfGravThreshold | 40 | px/s | \|vy\| < 40 且（按住跳 或 AutoJump）时重力减半 |
| FastMaxFall | 240 | px/s | 按住下时的下落上限 |
| FastMaxAccel | 300 | px/s² | 下落上限 maxFall 本身趋向目标值的速率（每帧 5） |
| MaxRun | 90 | px/s | 跑步速度上限 |
| RunAccel | 1000 | px/s² | 向输入方向加速（或无输入减速） |
| RunReduce | 400 | px/s² | 超过上限且仍按同方向时的减速 |
| AirMult | 0.65 | 倍 | 空中时 RunAccel / RunReduce 乘此系数 |
| DuckFriction | 500 | px/s² | 地面下蹲时水平速度趋向 0 的速率 |
| DuckCorrectCheck | 4 | px | 起身被挡时，向左右各找最多 4 px 的可起身位置 |
| DuckCorrectSlide | 50 | px/s | 起身被挡时向空位挪动的速度 |
| JumpGraceTime | 0.1 | s | coyote time（离地后仍可起跳） |
| JumpSpeed | −105 | px/s | 起跳垂直速度 |
| JumpHBoost | 40 | px/s | 起跳时水平速度额外加 40 × moveX |
| VarJumpTime | 0.2 | s | variable jump：按住跳期间 vy 不大于起跳速度 |
| CeilingVarJumpGrace | 0.05 | s | 顶头时，若起跳不足 0.05 s（varJumpTimer ≥ 0.15）则 variable jump 不取消 |
| UpwardCornerCorrection | 4 | px | 向上顶到角时，左右最多挪 4 px |
| DashingUpwardCornerCorrection | 5 | px | dash attack 中且几乎没有水平速度时，上述距离为 5 |
| LiftXCap | 250 | px/s | 平台给的水平速度上限（绝对值） |
| LiftYCap | −130 | px/s | 平台给的垂直速度只取向上部分，且不小于 −130 |
| JumpThruAssistSpeed | −40 | px/s | 上升中与单向平台重叠时额外向上推 |
| WalkSpeed | 64 | px/s | 剧情自动行走速度 |

### 1.2 墙

| 名称 | 值 | 单位 | 含义 |
| --- | --- | --- | --- |
| WallSpeedRetentionTime | 0.06 | s | 撞墙后保留原水平速度、等待绕过墙角的时间 |
| WallJumpCheckDist | 3 | px | walljump 检测：碰撞箱平移 3 px 后与实心物重叠（即间隙 ≤ 2 px） |
| SuperWallJumpCheckDist | 5 | px | super walljump 检测：平移 5 px（间隙 ≤ 4 px） |
| WallJumpForceTime | 0.16 | s | walljump 后强制水平输入为跳离方向的时间（仅当起跳时有水平输入） |
| WallJumpHSpeed | 130 | px/s | walljump 水平速度 |
| WallSlideStartMax | 20 | px/s | wallslide 开始时的下落上限 |
| WallSlideTime | 1.2 | s | wallslide 计时：上限从 20 线性增到 160 所用时间 |
| SuperWallJumpSpeed | −160 | px/s | super walljump（wallbounce）垂直速度 |
| SuperWallJumpVarTime | 0.25 | s | super walljump 的 variable jump 时间 |
| SuperWallJumpForceTime | 0.2 | s | 代码中**未使用**（super walljump 不强制水平输入） |
| SuperWallJumpH | 170 | px/s | super walljump 水平速度 |

### 1.3 冲刺

| 名称 | 值 | 单位 | 含义 |
| --- | --- | --- | --- |
| DashSpeed | 240 | px/s | 冲刺速度（方向为单位向量） |
| EndDashSpeed | 160 | px/s | 冲刺结束时（方向不朝下）速度 = 方向 × 160 |
| EndDashUpMult | 0.75 | 倍 | 结束时若 vy < 0 再乘 0.75 |
| DashTime | 0.15 | s | 冲刺持续（协程等待） |
| SuperDashTime | 0.3 | s | 仅辅助模式"超级冲刺"使用 |
| DashCooldown | 0.2 | s | 两次冲刺开始之间的最短间隔 |
| DashRefillCooldown | 0.1 | s | 冲刺开始后，站地补充冲刺次数之前的等待 |
| DashHJumpThruNudge | 6 | px | 水平冲刺时，若脚陷入单向平台不超过 6 px，直接抬到平台上 |
| DashCornerCorrection | 4 | px | 水平冲刺撞墙角时，上下最多挪 4 px |
| DashVFloorSnapDist | 3 | px | 水平 dash attack 中、离地且下方 3 px 内有地面时，向下贴地 |
| DashAttackTime | 0.3 | s | dash attack 窗口（DashAttacking），从冲刺开始算 |
| DodgeSlideSpeedMult | 1.2 | 倍 | 斜下冲刺在地面上转为水平时 vx × 1.2 |
| SuperJumpSpeed | −105 | px/s | super 跳垂直速度 |
| SuperJumpH | 260 | px/s | super 跳水平速度（方向 = Facing） |
| DuckSuperJumpXMult | 1.25 | 倍 | 下蹲状态的 super（即 hyper）：vx × 1.25 |
| DuckSuperJumpYMult | 0.5 | 倍 | hyper：vy × 0.5 |
| ReboundSpeedX / Y | 120 / −120 | px/s | 冲碎冲刺砖后的反弹速度 |
| ReboundVarJumpTime | 0.15 | s | 反弹后的 variable jump 时间 |
| ReflectBoundSpeed | 220 | px/s | "反射弹开"速度（本三章少见） |
| DreamDashSpeed | 240 | px/s | 梦境冲刺速度 |
| DreamDashEndWiggle | 5 | px | 冲进实心物时的挤出搜索范围 |
| DreamDashMinTime | 0.1 | s | 梦境冲刺最短持续 |
| BoostMoveSpeed | 80 | px/s | 泡泡内向中心移动的速度 |
| BoostTime | 0.25 | s | 泡泡内自动发射前的时间 |

### 1.4 攀爬与体力

| 名称 | 值 | 单位 | 含义 |
| --- | --- | --- | --- |
| ClimbMaxStamina | 110 | — | 满体力 |
| ClimbUpCost | 45.454544 | /s | 向上爬时每秒消耗（每帧 0.7576） |
| ClimbStillCost | 10 | /s | 抓墙不动时每秒消耗（每帧 0.1667）；向下爬不消耗 |
| ClimbJumpCost | 27.5 | — | 不在地面时 climbjump 一次的消耗 |
| ClimbTiredThreshold | 20 | — | 体力 < 20 为"疲劳"：不能开始抓墙 |
| ClimbCheckDist | 2 | px | 抓墙检测：碰撞箱平移 2 px 后与墙重叠（间隙 ≤ 1 px） |
| ClimbUpCheckDist | 2 | px | 抓墙辅助：墙在头顶上方最多 2 px 处开始也能抓（会向上挪） |
| ClimbNoMoveTime | 0.1 | s | 刚抓上墙后不能上下移动的时间 |
| ClimbUpSpeed | −45 | px/s | 向上爬 |
| ClimbDownSpeed | 80 | px/s | 向下爬 |
| ClimbSlipSpeed | 30 | px/s | 手部够不到墙时向下滑 |
| ClimbAccel | 900 | px/s² | 攀爬时 vy 趋向目标的速率（每帧 15） |
| ClimbGrabYMult | 0.2 | 倍 | 抓上墙时 vy × 0.2 |
| ClimbHopY | −120 | px/s | 爬到墙顶"翻上去"时 vy 至少为 −120 |
| ClimbHopX | 100 | px/s | 翻上去后（越过墙顶时）水平速度 |
| ClimbHopForceTime | 0.2 | s | 翻上去后强制水平输入为 0 的时间 |
| ClimbJumpBoostTime | 0.2 | s | climbjump（无水平输入）后，按反方向可转为 walljump 速度的窗口 |
| ClimbHopNoWindTime | 0.3 | s | climb hop 后不受风的时间（本三章无风） |

### 1.5 其他（本三章基本用不到，列出以求完整）

| 名称 | 值 | 含义 |
| --- | --- | --- |
| HoldingMaxRun / HoldMinTime / ThrowRecoil / CarryOffsetTarget | 70 / 0.35 / 80 / (0,−12) | 搬运物体（本三章无） |
| BounceAutoJumpTime | 0.1 | `Bounce()` 的 AutoJump 持续时间 |
| BounceSpeed / BounceVarJumpTime | −140 / 0.2 | `Bounce()` |
| SuperBounceSpeed / SuperBounceVarJumpTime | −185 / 0.2 | 地面弹簧 |
| SideBounceSpeed / SideBounceThreshold / SideBounceForceMoveXTime | 240 / 240 / 0.3 | 墙面弹簧 |
| LaunchSpeed / LaunchCancelThreshold | 280 / 220 | 爆炸弹射（本三章无） |
| HitSquashNoMoveTime / HitSquashFriction | 0.1 / 800 | 红泡泡撞墙后的硬直 |
| ChaserStateMaxTime | 4 | 记录 Madeline 历史状态（给追逐者用）的最长时长，秒 |
| DuckWindMult / WindWallDistance | 0 / 3 | 风（本三章无） |
| LowFriction*、Hiccup*、Glider*、Swim*、StarFly*、WallBooster*、IceBooster*、SpacePhysicsMult | — | 辅助模式、水、羽毛、传送带、太空等，本三章不出现 |

---

## 2. 状态编号

| 编号 | 名称 | 本三章是否出现 | 玩家可控（InControl） |
| --- | --- | --- | --- |
| 0 | StNormal | 是 | 是 |
| 1 | StClimb | 是 | 是 |
| 2 | StDash | 是 | 是 |
| 3 | StSwim | 否（无水） | 是 |
| 4 | StBoost | 否（无泡泡） | 是 |
| 5 | StRedDash | 否 | 是 |
| 6 | StHitSquash | 否 | 是 |
| 7 | StLaunch | 否 | 是 |
| 8 | StPickup | 否 | 是 |
| 9 | StDreamDash | 是（2A） | 是 |
| 10 | StSummitLaunch | 否 | 是 |
| 11 | StDummy | 是（剧情） | **否** |
| 12 | StIntroWalk | 可能 | **否** |
| 13 | StIntroJump | 可能 | **否** |
| 14 | StIntroRespawn | 是（死亡重生） | **否** |
| 15 | StIntroWakeUp | 可能 | **否** |
| 16 | StBirdDashTutorial | 是（序章剧情） | **否** |
| 17 | StFrozen | — | **否** |
| 18 | StReflectionFall | 否 | 是 |
| 19 | StStarFly | 否 | 是 |
| 20 | StTempleFall | 否 | 是 |
| 21 | StCassetteFly | 是（拾取磁带后） | 是（但由脚本移动） |
| 22 | StAttract | 否 | 是 |
| 23 | StIntroMoonJump | 否 | **否** |
| 24 | StFlingBird | 否 | 是 |
| 25 | StIntroThinkForABit | 否 | **否** |

状态切换规则（状态机）：
1. 每帧先调用当前状态的"更新函数"，它返回下一状态编号；若与当前不同，立即切换：先调用旧状态的"结束"回调，再调用新状态的"开始"回调，然后启动新状态的协程（若有，否则取消旧协程）。
2. 然后，若协程处于活动状态，推进协程一步。**所以同一帧里：先跑更新函数，后跑协程**；且新状态的协程在切换当帧就会执行到第一个让步点。
3. 其他代码也可以随时直接设置状态（例如碰撞回调里进入梦境冲刺、弹簧把状态设为 Normal），同样触发结束/开始回调。
4. 状态机可被锁定（死亡时），锁定后不再切换。

---

## 3. 每帧更新顺序（Player.Update，严格按此顺序）

Madeline 的 Depth = 0，在大多数平台之前、背景类实体之后更新（见 `README.md` §6.1）。一帧内：

1. `PreviousPosition = Position`；`climbTriggerDir = 0`（本帧是否"wallslide 并按住抓"导致算作骑在墙上，见第 30 步）。
2. 几个与本三章无关的计时器（滑翔、低摩擦、爆炸弹射等）递减。
3. 若 `JustRespawned` 且速度非零，清除 `JustRespawned`。
4. **地面判定 `onGround`**：
   - 若处于 StDreamDash：`onGround = false`。
   - 否则若 `Speed.Y >= 0`：若"碰撞箱下移 1 px 后与某个实心物重叠"，或"下移 1 px 后与某个单向平台重叠、且当前位置不与它重叠"，则 `onGround = true`；否则 false。
   - 否则（正在上升）：`onGround = false`。
   - 注意：这是用**本帧开始时**的位置与速度判定的；本帧移动之后不会重新判定。
5. wallslide 计时：若 `wallSlideDir ≠ 0`（上一帧在 wallslide），`wallSlideTimer = max(wallSlideTimer − dt, 0)`，然后 `wallSlideDir = 0`。
6. climbjump 转 walljump（wallboost）：若 `wallBoostTimer > 0`：`wallBoostTimer −= dt`；若此时的 `moveX`（**仍是上一帧的值**）等于 `wallBoostDir`，则 `Speed.X = 130 × moveX`，`Stamina += 27.5`（退还 climbjump 的消耗），`wallBoostTimer = 0`。
7. 若 `onGround` 且不在 StClimb：`AutoJump = false`，`Stamina = 110`，`wallSlideTimer = 1.2`。
8. `dashAttackTimer > 0` 则 `−= dt`。
9. coyote time：若 `onGround`：`jumpGraceTimer = 0.1`（并清除 dreamJump 标记）；否则若 `jumpGraceTimer > 0`：`−= dt`。
10. `dashCooldownTimer > 0` 则 `−= dt`。
11. 补充冲刺次数：
    - 若 `dashRefillCooldownTimer > 0`：只 `−= dt`（**本帧不检查补充**）。
    - 否则：若 `onGround`，且（下移 1 px 后与某个实心物重叠 或 下移 1 px 后与单向平台重叠且当前不重叠），且**当前碰撞箱不与任何尖刺实体重叠**，则 `Dashes = MaxDashes`（只在 Dashes < MaxDashes 时生效）。
12. `varJumpTimer > 0` 则 `−= dt`。
13. AutoJump 计时：若 `AutoJumpTimer > 0`：若 `AutoJump` 为真则 `AutoJumpTimer −= dt`，减到 ≤ 0 时 `AutoJump = false`；若 `AutoJump` 已为假则 `AutoJumpTimer = 0`。（`AutoJumpTimer = 0` 而 `AutoJump = true` 时，AutoJump 一直保持到落地或被其他动作清除。）
14. **水平输入**：若 `forceMoveXTimer > 0`：`forceMoveXTimer −= dt`，`moveX = forceMoveX`；否则 `moveX = Input.MoveX`（本帧输入），并清除"climb hop 所依附的实心物"记录。
15. 若记录着"climb hop 所依附的实心物"且它移动了，Madeline 跟着它整像素移动同样的位移（climb hop 途中墙在动的情况）。
16. **朝向**：若 `moveX ≠ 0`、可控（InControl）、且状态不是 StClimb / StPickup / StRedDash / StHitSquash，则 `Facing = moveX`。（StDash 中会跟随输入改变朝向。）
17. **瞄准**：`lastAim = 根据本帧瞄准输入得到的 8 向单位向量；若无方向输入则为 (Facing, 0)`（见 `input.md`）。冲刺方向就取自它。
18. wall speed retention（见 §6.3）。
19. climb hop 水平速度等待（hopWait）：若在等待中：若 `sign(Speed.X) == −hopWaitX` 或 `Speed.Y > 0` 则取消；否则若"碰撞箱沿 hopWaitX 方向平移 1 px 后不再与实心物重叠"（已越过墙顶），则 `Speed.X = hopWaitXSpeed`（100 × Facing），结束等待。
20. 其他与本三章无关的计时（风、头发、持物）。
21. **组件更新**：状态机在这里运行——先当前状态的更新函数（§5、§6……），再协程（见 §2）。
22. Actor 收尾：`currentLiftSpeed = 0`（平台给的速度的"当前值"清零，但"最近值"按 0.16 s 宽限保留，见 §7）；宽限计时 `−= dt`，到期清除最近值。
23. **单向平台上推**：若 `!onGround` 且 `Speed.Y <= 0` 且（不在 StClimb，或攀爬中上一步是向上爬）且当前碰撞箱与某个单向平台重叠，且没有尖刺等"防边缘"实体在头顶 2 px 内：额外 `MoveV(−40 × dt)`（每帧约 −0.667 px，经余量累积）。
24. **冲刺贴地**：若 `!onGround` 且 dash attack 中（DashAttacking）且冲刺方向水平（DashDir.Y == 0），且下移 3 px 后与实心物重叠或与单向平台"外部重叠"，且下移 3 px 不会让受击箱碰到尖刺：`MoveVExact(3)`（逐像素下移，遇地面停止）。
25. **空中自动起身**：若 `Speed.Y > 0` 且可以起身（换回普通碰撞箱不与实心物重叠）且 `!onGround` 且 `jumpGraceTimer <= 0`：取消下蹲。
26. **水平移动**：若不在 StDreamDash / StAttract：`MoveH(Speed.X × dt)`，撞墙时调用水平碰撞回调（§6.1）。
27. **垂直移动**：同上 `MoveV(Speed.Y × dt)`，碰撞时调用垂直碰撞回调（§6.2）。
28. 水的进入/离开检查（本三章无水）。
29. 动画、持物位置。
30. **触发器**：与每个触发器做（当前碰撞箱，普通或下蹲）重叠检测：新进入则 OnEnter，持续则 OnStay，离开则 OnLeave。
31. 相机跟随（InControl 时）。
32. **危险物/拾取判定**：若未死亡且不在 StCassetteFly：临时把碰撞箱换成**受击箱**，对场景里每个 PlayerCollider 组件做重叠检测并触发其效果（尖刺、Badeline、弹簧、冲刺水晶、草莓……）。若因此死亡，本帧到此结束。
33. **房间边界**：若可控、未死亡、不在 StDreamDash：执行边界处理（§13.2：换房、夹住、掉出死亡）。
34. 记录本帧状态到"追逐者历史"（位置、时间戳、动画等，最长保留 4 s；给 BadelineOldsite 用）。
35. 头发、音效；`wasOnGround = onGround`。

**由此得到的关键时序**：
- 所有计时器（第 5–13 步）在状态更新**之前**递减；状态更新里设置的计时器，下一帧才开始递减。
- `moveX`、`Facing`、`lastAim` 在状态更新前就读好了本帧输入；但第 6 步的 wall boost 用的是上一帧的 `moveX`。
- 本帧在状态更新里设置的速度，在本帧第 26–27 步就生效。
- 平台（多数在 Madeline 之后更新）本帧的推/载发生在第 27 步之后。

---

## 4. 计时器一览（按 dt = 0.0166667 计帧）

"帧数"指设置之后的后续帧中，该计时器（递减后）仍 > 0 的帧数。

| 计时器 | 设置时机与值 | 递减位置 | 作用 | 帧数 |
| --- | --- | --- | --- | --- |
| jumpGraceTimer | 每个 onGround 帧设为 0.1；起跳/walljump 等清零 | 第 9 步 | > 0 可地面起跳；冲刺中 > 0 可 super | 离地后 5 帧 |
| varJumpTimer | 起跳 0.2；super 0.2；super walljump 0.25；弹簧 0.2；反弹 0.15 | 第 12 步 | > 0 且按住跳/AutoJump 时 vy ≤ varJumpSpeed | 0.2 → 起跳帧后 11 帧 |
| dashCooldownTimer | 冲刺开始 0.2；向上换房 0.2 | 第 10 步 | > 0 时不能冲刺 | 12 帧 |
| dashRefillCooldownTimer | 冲刺开始 0.1 | 第 11 步 | > 0 时不站地补充 | 冲刺开始后第 7 个正常帧起可补 |
| dashAttackTimer | 冲刺开始 0.3 | 第 8 步 | > 0 即 DashAttacking | 冲刺开始后 17 个正常帧（不含冻结） |
| forceMoveXTimer | walljump 0.16（仅当有水平输入）；climb hop 0.2（强制 0）；墙面弹簧 0.3 | 第 14 步 | 期间 moveX 被强制 | 0.16 → 10 帧；0.2 → 12 帧；0.3 → 18 帧 |
| wallSlideTimer | 落地、起跳、抓墙、冲刺开始等重置为 1.2 | 第 5 步（仅 wallslide 帧） | 决定 wallslide 速度上限 | 72 帧下滑后到 0 |
| wallSpeedRetentionTimer | 水平撞墙时 0.06 | 第 18 步（仍被挡时） | 绕过墙角后恢复原速 | 4 次机会 |
| wallBoostTimer | 无水平输入的 climbjump 0.2 | 第 6 步 | 按反方向转为 walljump 速度 | 12 帧 |
| climbNoMoveTimer | 抓上墙 0.1 | 攀爬更新开头 | > 0 时不能上下爬、不耗体力 | 抓墙后前 5 个攀爬帧锁定，第 6 帧起可动 |
| AutoJumpTimer | Bounce 0.1；其他多为 0（表示一直保持） | 第 13 步 | 见第 13 步 | — |
| dreamDashCanEndTimer | 进入梦境冲刺 0.1 | 梦境冲刺更新中 | > 0 时不能结束梦境冲刺 | 至少 6 帧 |
| liftSpeedTimer | 每次被平台设置速度时 0.16 | 第 22 步 | liftboost 的宽限保留 | 平台停止后约 10 帧内仍可借力 |

---

## 5. StNormal（编号 0）

### 5.1 进入与离开

- 进入（Begin）：`maxFall = 160`。
- 离开（End）：`wallBoostTimer = 0`，`wallSpeedRetentionTimer = 0`，取消 climb hop 水平速度等待。

### 5.2 NormalUpdate 的执行顺序

1. **离地继承平台向上速度**：若"平台给的速度"（LiftBoost，§7）的 Y < 0，且上一帧在地面、本帧不在地面，且 `Speed.Y >= 0`：`Speed.Y = LiftBoost.Y`。
2. **抓墙**（不持物时）：若 `GrabCheck` 且不疲劳（体力 + 加成 ≥ 20）且不下蹲：
   1. （拾取可搬运物体，本三章无。）
   2. 若 `Speed.Y >= 0` 且 `sign(Speed.X) ≠ −Facing`（不在背离朝向方向移动）：
      - 若 ClimbCheck(Facing)（§5.11）：取消下蹲，**进入 StClimb**，返回。
      - 否则，若没有按住下（MoveY < 1）：对 i = 1、2：若头顶上移 i px 无实心物，且 ClimbCheck(Facing, 上移 i)：整像素上移 i px，取消下蹲，进入 StClimb，返回。（抓墙辅助）
   - **注意：上升中（Speed.Y < 0）不能在 StNormal 里抓墙。**
3. **冲刺**：若 CanDash（§8.1）：`Speed += LiftBoost`，开始冲刺（进入 StDash），返回。
4. **下蹲与起身**（不持物时）：
   - 若正在下蹲：若 `onGround` 且没有按住下：若可起身则起身；否则若 `Speed.X == 0`：对 n = 4, 3, 2, 1：若右移 n px 处可起身，`MoveH(+50 × dt)` 并停止搜索；否则若左移 n px 处可起身，`MoveH(−50 × dt)` 并停止搜索。（先比较距离较远的 4 px，右侧优先。）
   - 否则（不在下蹲）：若 `onGround` 且按住下（MoveY == 1）且 `Speed.Y >= 0`：开始下蹲。
5. **水平速度**：
   - 若下蹲且在地面：`Speed.X = Approach(Speed.X, 0, 500 × dt)`。**地面下蹲时输入完全不加速。**
   - 否则：`mult = onGround ? 1 : 0.65`；`max = 90`：
     - 若 `|Speed.X| > max` 且 `sign(Speed.X) == moveX`：`Speed.X = Approach(Speed.X, max × moveX, 400 × mult × dt)`（超速时按住同方向，慢慢减速）。
     - 否则：`Speed.X = Approach(Speed.X, max × moveX, 1000 × mult × dt)`（包括无输入时减速到 0、反向时转向）。
6. **下落上限 maxFall**：
   - 若按住下（MoveY == 1）且 `Speed.Y >= 160`：`maxFall = Approach(maxFall, 240, 300 × dt)`。
   - 否则：`maxFall = Approach(maxFall, 160, 300 × dt)`。
7. **重力**（仅当 `!onGround`）：
   1. 目标 `target = maxFall`。
   2. **wallslide**：若（`moveX == Facing` 或（`moveX == 0` 且按住抓））且没有按住下（因为 Facing 每帧随非零 moveX 更新，实际含义是：按住某个水平方向时检测该方向的墙；不按水平方向但按住抓时检测朝向一侧的墙）：
      - 若 `Speed.Y >= 0` 且 `wallSlideTimer > 0` 且不持物，且碰撞箱沿 Facing 平移 1 px 后与实心物重叠，且该侧没有"禁止攀爬边缘"（ClimbBlocker），且离房间左右边界足够远（§5.11 的边界检查），且可以起身：取消下蹲，`wallSlideDir = Facing`。
      - 若本帧 `wallSlideDir ≠ 0`：若按住抓，则本帧算作"骑在这面墙上"（会触发踩踏类机关，如坠落块、交通块）；若 `wallSlideTimer > 0.6` 且墙上有 ClimbBlocker，则 `wallSlideTimer = 0.6`；`target = lerp(160, 20, wallSlideTimer / 1.2)`，即计时满时上限 20，计时归零时上限 160。
   3. 重力倍率：若 `|Speed.Y| < 40` 且（`Jump.Check` 或 `AutoJump`）则 0.5，否则 1。（用的是施加重力**之前**的速度。）
   4. `Speed.Y = Approach(Speed.Y, target, 900 × 倍率 × dt)`。若当前速度大于目标（例如刚开始贴墙），这一步会以 15（或 7.5）每帧减速到目标。
8. **variable jump**：若 `varJumpTimer > 0`：若 `AutoJump` 或按住跳：`Speed.Y = min(Speed.Y, varJumpSpeed)`；否则 `varJumpTimer = 0`（松开跳即永久取消本次 variable jump）。**顺序：重力先，variable jump 钳制后。**
9. **跳跃输入**：若 `Jump.Pressed`（且不是在和可交互对象说话的那一下）：
   1. 若 `jumpGraceTimer > 0`：**普通起跳** Jump()（§5.9）。地面起跳优先于一切墙跳。
   2. 否则若可以起身：
      - 若 WallJumpCheck(+1)（右侧有墙，§5.11）：
        - 若 Facing 为右、按住抓、体力 > 0、不持物、右侧 3 px 内无 ClimbBlocker：**climbjump** ClimbJump()。
        - 否则若 DashAttacking 且冲刺方向为正上（|DashDir.X| ≤ 0.2 且 DashDir.Y ≤ −0.75）：**super walljump** SuperWallJump(−1)。
        - 否则：**walljump** WallJump(−1)（向左跳离）。
      - 否则若 WallJumpCheck(−1)：镜像处理（Facing 为左时 climbjump；SuperWallJump(+1)；WallJump(+1)）。
      - （否则若脚下 2 px 是水面，起跳；本三章无水。）
   - 先检查右墙再检查左墙。
10. 返回 0（保持 StNormal）。

### 5.3 水平运动的实用数值

- 地面从静止按住方向：每帧 +16.667，第 6 帧达到 90。
- 地面松开方向：每帧 −16.667，从 90 到 0 用 6 帧。
- 地面反向：每帧向反方向 16.667。
- 空中：加速/减速每帧 10.833；超速且按同方向每帧 4.333；超速但不按或反按，每帧 10.833。
- 地面超速且按同方向：每帧 6.667。

### 5.4 下蹲细节

- 下蹲 = 碰撞箱 8×6，受击箱 8×4（`README.md` §4）。
- 开始下蹲只在 StNormal 中、地面、按住下、`Speed.Y >= 0` 时发生；冲刺开始时也可能进入下蹲（§8.11）。
- 起身需要"换回 8×11 碰撞箱后不与任何实心物重叠"。
- 起跳（Jump）**不会**取消下蹲：可以保持下蹲姿态起跳，直到下落时第 25 步的空中自动起身（需 `jumpGraceTimer <= 0` 且可起身）。
- walljump、super 跳、super walljump 会取消下蹲。

### 5.5 Fastfall

按住下且已在以 ≥160 下落：上限每帧 +5，16 帧后到 240；速度跟随上限上升（重力步长 15 > 5）。松开下：上限每帧 −5 回到 160，速度随之每帧 −5。

### 5.6 Wallslide

- 条件见 §5.2 第 7 步。wallslide 每帧消耗 `wallSlideTimer`（dt），上限从 20 线性升到 160（1.2 s，72 帧）。
- `wallSlideTimer` 在以下情况重置为 1.2：站在地面（非攀爬）、任何起跳/walljump/super、抓上墙、冲刺开始、弹簧、换房等。
- wallslide 时若按住抓（但因体力不足等原因没进入攀爬），本帧算骑在该墙上。

### 5.7 Variable jump 与起跳高度（按本文规则推算，请用游戏轨迹核对）

原地起跳（无 liftboost），vy 每帧序列（单位 px/s，"起跳帧"为第 0 帧）：

- **一直按住跳**：−105 ×12 帧（第 0–11 帧），然后 −90、−75、−60、−45、−30，接着进入半重力：−22.5、−15、−7.5、0、7.5、15、22.5、30、37.5，然后 45、60、75…直到 160。最高点约比起跳点高 **27 px**。
- **第 12 帧起松开**（按满 variable jump 但顶点不按）：顶点约 26 px（没有半重力）。
- **只按 1 帧**（第 1 帧就松开）：−105，然后 −90、−75…；最高约 7 px。
- 按住 n 帧（n ≤ 12）：前 n 帧保持 −105，之后每帧 +15。

### 5.8 Coyote time 与 jump buffer（帧数）

- 最后一个 `onGround` 帧之后，还有 **5 帧**可以普通起跳（`jumpGraceTimer` 0.0833…0.0167）。
- 跳键在按下帧及之后 4 帧共 **5 帧**内都算"按下"（前提是一直按住；松开立即失效）。所以在落地前最多 4 帧按下跳并按住，落地那帧（onGround 为真的第一帧）就会起跳。详见 `input.md`。
- 落地帧的判定：第 k 帧移动中碰到地面，第 k+1 帧开始时 `onGround` 才为真，起跳发生在第 k+1 帧。

### 5.9 起跳类函数的效果

所有这些函数都会：消耗跳键缓冲；`jumpGraceTimer = 0`；`AutoJump = false`（除注明）；`dashAttackTimer = 0`；`wallSlideTimer = 1.2`；`wallBoostTimer = 0`；`varJumpSpeed = 设置后的 Speed.Y`。

| 函数 | 速度 | variable jump 时间 | 其他 |
| --- | --- | --- | --- |
| Jump()（普通起跳） | `Speed.X += 40 × moveX`；`Speed.Y = −105`；然后 `Speed += LiftBoost`（两轴） | 0.2 | 不取消下蹲 |
| ClimbJump()（climbjump） | 先：若不在地面，`Stamina −= 27.5`；然后执行 Jump()（所以 `Speed.X += 40 × moveX`） | 0.2 | 若 `moveX == 0`：`wallBoostDir = −Facing`，`wallBoostTimer = 0.2`（§3 第 6 步） |
| WallJump(dir)（walljump，dir = 跳离方向） | `Speed.X = 130 × dir`；`Speed.Y = −105`；然后 `+= LiftBoost` | 0.2 | 取消下蹲；若起跳时 `moveX ≠ 0`：`forceMoveX = dir`，`forceMoveXTimer = 0.16`（之后 10 帧水平输入被强制为 dir）；若自身没有 liftboost，则改用所 walljump（反方向 3 px 处实心物）自身记录的 liftboost（推断：多数由协程驱动的移动块在自己更新结束时把该值清零，所以这里通常取到 0） |
| SuperWallJump(dir)（wallbounce） | `Speed.X = 170 × dir`；`Speed.Y = −160`；然后 `+= LiftBoost` | **0.25** | 取消下蹲；**不**强制水平输入 |
| SuperJump()（super / hyper，见 §8.6） | `Speed.X = 260 × Facing`（**覆盖**原水平速度）；`Speed.Y = −105`；`+= LiftBoost`；若下蹲中：取消下蹲，`Speed.X × 1.25`，`Speed.Y × 0.5` | 0.2 | — |

### 5.10 Wallboost（climbjump 转 walljump）

无水平输入的 climbjump 之后 0.2 s 内（第 6 步检查 12 次），若某帧的 `moveX` 等于 `−Facing`（远离墙），则下一帧开头 `Speed.X = 130 × moveX` 并退还 27.5 体力。效果等于免费的 walljump。存在 1 帧延迟（因为第 6 步读的是上一帧的 moveX）。窗口内"疲劳"判断用的体力额外加 27.5。

### 5.11 墙与抓取的距离检测

- **WallJumpCheck(dir)**：检测距离 `d = 3`；若 DashAttacking 且冲刺方向恰为 (0, −1)，且沿 dir 平移 5 px 处没有"刺尖朝向自己"的尖刺（dir 为右时检查朝左的尖刺，dir 为左时检查朝右的尖刺），则 `d = 5`。成立条件：边界检查(dir) 通过，且沿 dir 平移 d px 处没有带"边缘"标记的 ClimbBlocker，且**碰撞箱沿 dir 平移 d px 后与某个实心物重叠**。整数位置下即"与墙的间隙 ≤ d − 1 px"。
- **ClimbCheck(dir, yAdd)**：边界检查(dir) 通过，且（碰撞箱下移 yAdd、并沿 **Facing** 平移 2 px 处）没有 ClimbBlocker，且碰撞箱平移 (2 × dir, yAdd) 后与实心物重叠（间隙 ≤ 1 px）。
- **边界检查(dir)**：`碰撞箱左边 + 2 × dir >= 房间左边界` 且 `碰撞箱右边 + 2 × dir < 房间右边界`。紧贴房间左右边界时不能朝边界方向抓墙/walljump/wallslide。
- ClimbBlocker 来自 InvisibleBarrier 等实体（见 `entities.md`）。

---

## 6. 碰撞回调与位置修正

Madeline 每帧先 `MoveH` 后 `MoveV`。逐像素移动中，下一步将与实心物重叠时：余量清零，调用回调，**本轴剩余移动全部放弃**（见 §14.2）。

### 6.1 水平碰撞（OnCollideH）

按顺序：
1. （梦境冲刺中：什么都不做。）
2. **dash attack 撞可冲碎物**：若 DashAttacking，且撞到的实心物有"被冲撞"处理（如 DashBlock），且碰撞方向的 x 与 `sign(DashDir.X)` 相同：调用其处理，结果为：
   - Rebound（冲刺砖）：`Rebound(−sign(Speed.X))`（§12），返回。
   - Bounce：反射弹开，返回。
   - Ignore：返回。
   - 正常碰撞：继续往下。
3. **冲刺中的修正**（仅 StDash / StRedDash）：
   - 若 `onGround` 且"用下蹲碰撞箱在前方 1 px 处"无实心物：**进入下蹲**，返回（不损失速度，下一帧继续前进——地面冲刺可钻过 6 px 高的缝）。
   - 若 `Speed.Y == 0` 且 `Speed.X ≠ 0`（纯水平冲刺）：对 i = 1..4，先向下（+i）再向上（−i）：若"前方 1 px 并偏移 ±i px"处无实心物，且"前方 1 px 并偏移 ±(i−1) px"处有实心物，且偏移后受击箱不会碰到尖刺：整像素垂直移动 ±i，再水平移动 1 px，返回（冲刺角修正，最多 4 px，向下优先）。
4. **进入梦境冲刺**：若满足梦境冲刺条件（§10.1），切换到 StDreamDash，返回。
5. **wall speed retention**：若 `wallSpeedRetentionTimer <= 0`：`wallSpeedRetained = Speed.X`，`wallSpeedRetentionTimer = 0.06`。
6. 调用被撞实心物的 OnCollide（若有）。
7. `Speed.X = 0`；`dashAttackTimer = 0`。（StRedDash 时转为 StHitSquash。）
- **注意：撞墙不会结束 StDash 状态**，冲刺计时继续；但 DashAttacking 立即结束。

### 6.2 垂直碰撞（OnCollideV）

1. （梦境冲刺中：什么都不做。）
2. **dash attack 撞可冲碎物**：若被撞物有"被冲撞"处理且 DashAttacking 且碰撞方向的 y 与 `sign(DashDir.Y)` 相同：Rebound → `Rebound(0)`（vx = 0，vy = −120）；Bounce → 反射；Ignore → 返回；否则继续。
3. **向下碰撞（落地，Speed.Y > 0）**：
   1. 若在 StDash/StRedDash 且冲刺**不是**从地面开始的：
      - 若 `Speed.X <= 0.01`：对 n = 1..4：若左移 n px 后"不在地面上"（脚下 1 px 无实心物/单向平台），则整像素左移 n、下移 1，返回（从平台边缘滑下，保持冲刺）。
      - 若 `Speed.X >= −0.01`：同理向右 1..4。
   2. 若满足梦境冲刺条件：进入 StDreamDash，返回。
   3. **斜下冲刺落地转换**：若 `DashDir.X ≠ 0` 且 `DashDir.Y > 0` 且 `Speed.Y > 0`：`DashDir = (sign(DashDir.X), 0)`，`Speed.Y = 0`，`Speed.X × 1.2`，**进入下蹲**。（这是 wavedash 的基础：之后在冲刺状态内起跳即为 hyper，见 §8.6。注意此判断不要求处于 StDash，只看 DashDir；DashDir 在冲刺结束后保留上一次的值。）
   4. 着陆动画/音效（无物理影响）。
4. **向上碰撞（顶头，Speed.Y < 0）**：
   1. 顶角修正距离 `c = 4`；若 DashAttacking 且 `|Speed.X| < 0.01`，`c = 5`。
   2. 若 `Speed.X <= 0.01`：对 j = 1..c：若位置 + (−j, −1) 处碰撞箱无实心物：**直接**把位置移到那里（不经碰撞检测、不损失速度），返回。
   3. 若 `Speed.X >= −0.01`：同理 (+j, −1)。（`Speed.X` 为 0 时左右都试，左优先。）
   4. 若 `varJumpTimer < 0.15`：`varJumpTimer = 0`（起跳超过 0.05 s 后顶头会取消 variable jump；起跳后前 3 帧内顶头不取消）。
   5. 若满足梦境冲刺条件：进入 StDreamDash，返回。
5. 调用被撞物的 OnCollide（若有）；`dashAttackTimer = 0`；`Speed.Y = 0`。（StRedDash 时转为 StHitSquash。）

### 6.3 Wall speed retention

每帧第 18 步，若 `wallSpeedRetentionTimer > 0`：
- 若 `sign(Speed.X) == −sign(wallSpeedRetained)`（已经反向）：取消保留。
- 否则若"碰撞箱沿 `sign(wallSpeedRetained)` 平移 1 px 后不与实心物重叠"（例如已经上升越过墙角）：`Speed.X = wallSpeedRetained`，取消保留。
- 否则：计时 `−= dt`。
- 撞墙后共有 4 帧机会（0.06 s）。离开 StNormal / StClimb 时清除。

### 6.4 其他每帧修正（§3 第 23–25 步）

- **单向平台上推**：上升（或静止）中与单向平台重叠时每帧额外上移 40 px/s × dt，帮助"跳穿"单向平台站上去。
- **冲刺贴地**：水平 dash attack 中离地不超过 3 px 时直接落到地面（便于贴地冲刺保持 onGround）。
- **空中起身**：下落且 coyote time 已过时自动起身（若空间允许）。
- **水平冲刺抬上单向平台**：见 §8.5 第 2 步。

---

## 7. 平台给的速度（LiftSpeed / LiftBoost）

- 每个 Actor（含 Madeline）有"当前 liftboost"与"最近 liftboost"。实心物或单向平台在移动整像素、并因此推动/载着 Madeline 时，把自己本帧的速度（本次请求的位移 ÷ dt，不是取整后的像素数）写入 Madeline 的 liftboost；写入非零值时同时记为"最近值"并启动 0.16 s 宽限计时。
- 读取 liftboost：当前值非零则用当前值，否则用最近值（宽限期内）。
- Madeline 每帧在状态更新**之后**（§3 第 22 步）把当前值清零；宽限计时每帧 −dt，到期清除最近值。
- 因为多数平台在 Madeline 之后更新，状态更新里读到的通常是**上一帧**平台写入的值；平台停下后约 10 帧内仍可借到最近值。
- **LiftBoost = 截断后的 liftboost**：`x` 截断到 [−250, 250]；`y` 若 > 0（向下）取 0，若 < −130 取 −130。即只继承向上的部分，最多 −130。
- LiftBoost 的使用点：Jump / ClimbJump / WallJump / SuperWallJump / SuperJump（`Speed += LiftBoost`）；冲刺开始前（`Speed += LiftBoost`，然后作为"冲刺前速度"参与 §8.4 的水平 speed retention）；攀爬中松手、体力耗尽、攀爬中冲刺；StNormal 第 1 步的离地继承。
- **单向平台水平移动时不写入 liftboost**（只在垂直移动时写入）。
- walljump 时若自身 liftboost 为零，会改用所 walljump 记录的 liftboost（通常为 0，见 §5.9 表注）。攀爬状态下 Madeline 算骑在面前的墙上，墙移动时会正常写入她的 liftboost。

---

## 8. StDash（编号 2）

### 8.1 能否冲刺（CanDash）

同时满足：
1. 本帧"冲刺键按下"或"crouch dash 键按下"（含 0.08 s 缓冲，见 `input.md`）；
2. `dashCooldownTimer <= 0`；
3. `Dashes > 0`；
4. 不是"站在可对话对象旁边且本帧按了交互键"。

检查位置：StNormal（抓墙检查之后、下蹲/跑步之前）、StClimb（跳跃检查之后）、StLaunch、StHitSquash。StDash 内**不能**再次冲刺。

开始冲刺（StartDash）：`Dashes = max(0, Dashes − 1)`；记录本次是否为 crouch dash；消耗冲刺键与 crouch dash 键的缓冲；进入 StDash。

### 8.2 时间线（F0 = 冲刺被受理的帧；F1–F3 是 freeze frames）

| 帧 | 发生的事 |
| --- | --- |
| F0 | 状态更新中受理冲刺：`Speed += LiftBoost` → StartDash → **DashBegin**：登记冻结 0.05 s（时间倍率 > 0.25 时）；`dashCooldownTimer = 0.2`；`dashRefillCooldownTimer = 0.1`；`dashAttackTimer = 0.3`；`wallSlideTimer = 1.2`；记录 `dashStartedOnGround = onGround`；**`beforeDashSpeed = Speed`；`Speed = (0,0)`；`DashDir = (0,0)`**；下蹲处理（§8.11）。协程执行到第一个让步点。本帧速度为 0，不按速度移动（但 §3 第 24 步的"冲刺贴地"此时已可能生效：DashDir = (0,0) 被视为水平，空中离地 ≤ 3 px 会被吸到地面）。 |
| F1–F3 | **冻结**：Madeline 与几乎所有实体都不更新；输入照常读取（input buffer 照常递减）。 |
| F4 | 第一个正常帧。§3 前半照常（读取本帧 moveX、Facing、`lastAim`）。状态更新：**先 DashUpdate，此时 DashDir 仍为 (0,0)**（§8.6 的特殊情况），然后协程：用 `lastAim`（即 **F4 的方向输入**）确定方向与速度（§8.3、§8.4），开始 0.15 s 等待。本帧按冲刺速度移动。 |
| F5–F13 | DashUpdate；按当前速度移动。冲刺速度共作用于 F4–F13 **10 帧**：直线冲刺位移 40 px，斜向每轴 28.28 px。 |
| F10 | 从这一帧起，若站在地面（§3 第 11 步），冲刺次数可以补满（冲刺仍在进行）。 |
| F14 | DashUpdate 仍然运行（**最后一次**在冲刺状态内 super/walljump 的机会），然后协程结束冲刺：设置结束速度（§8.7），`AutoJump = true`（`AutoJumpTimer = 0`，即保持到落地），切回 StNormal。本帧按结束速度移动。 |
| F15 | 第一帧 StNormal；dash cooldown 恰好到期，若还有次数可再次冲刺。 |
| F20 | DashAttacking 为真的最后一帧（F21 起为假），除非更早被起跳、碰撞清零。 |

（帧数基于 dt = 0.0166667；若冲刺开始于时间倍率 ≤ 0.25 的慢动作中则没有 freeze frames。）

### 8.3 方向

- 方向 = `lastAim`，取自 **F4（冻结结束后第一帧）**的瞄准输入：8 个方向之一的单位向量；对角为 (±0.70710677, ±0.70710677)。无方向输入时为 (Facing, 0)（Facing 也是 F4 时的值，已按 F4 的水平输入更新）。8 向吸附规则见 `input.md`。
- 冲刺按下帧 F0 的方向输入**不决定**冲刺方向（但 F0 的"按住下"会影响下蹲，§8.11）。
- 若方向有水平分量，`Facing = sign(DashDir.X)`。之后冲刺中朝向仍会随水平输入改变（§3 第 16 步）。

### 8.4 速度

在 F4 的协程中：
1. `speed = 方向 × 240`。
2. 若 `sign(beforeDashSpeed.X) == sign(speed.X)` 且 `|beforeDashSpeed.X| > |speed.X|`：`speed.X = beforeDashSpeed.X`（同向时保留更快的水平速度）。竖直冲刺（speed.X = 0）不保留。
3. `Speed = speed`；`DashDir = 方向`。
4. **地面斜下冲刺转水平**：若（F4 开始时）`onGround` 且 `DashDir.X ≠ 0` 且 `DashDir.Y > 0` 且 `Speed.Y > 0`（且脚下不是可穿越的梦境块）：`DashDir = (sign(DashDir.X), 0)`，`Speed.Y = 0`，`Speed.X × 1.2`（169.7 → 203.6），**进入下蹲**。
- 冲刺中没有重力、没有跑步加速；速度只被碰撞改变（撞墙 vx = 0，顶头/落地 vy = 0）。

### 8.5 DashUpdate（F4–F14 每帧，状态更新时执行）

1. （拾取物体，本三章无。）
2. 若 `|DashDir.Y| < 0.1`（水平冲刺，**或 F4 时 DashDir 仍为 (0,0)**）：
   1. 对每个单向平台：若与之重叠且 `碰撞箱底 − 平台顶 <= 6`，且抬升后受击箱不会碰到尖刺：整像素上移到平台顶上。
   2. **super 跳**：若可以起身、`Jump.Pressed`、`jumpGraceTimer > 0`：SuperJump()，切回 StNormal（冲刺立即结束）。
3. 若冲刺方向为正上（|DashDir.X| ≤ 0.2 且 DashDir.Y ≤ −0.75）：若 `Jump.Pressed` 且可以起身：WallJumpCheck(+1) 成立则 SuperWallJump(−1)；否则 WallJumpCheck(−1) 成立则 SuperWallJump(+1)；切回 StNormal。（正上冲刺中**只有**super walljump，没有普通 walljump。）
4. 否则：若 `Jump.Pressed` 且可以起身：
   - WallJumpCheck(+1)：Facing 为右且按住抓且体力 > 0 且无 ClimbBlocker → ClimbJump()；否则 WallJump(−1)。
   - 否则 WallJumpCheck(−1)：镜像。
   - 成立则切回 StNormal。
5. 否则保持 StDash。

### 8.6 Super、hyper、wavedash（由上面的规则推出）

- **super**：水平（含被转为水平的）冲刺中，`jumpGraceTimer > 0`（在地面或 coyote time 内）时按跳。结果 `Speed = (260 × Facing, −105) + LiftBoost`，variable jump 0.2 s。水平方向取 **Facing**（若此时按住反方向，Facing 已在本帧第 16 步翻转——反向 super）。
- **hyper**：同上但处于下蹲：`Speed = (325 × Facing, −52.5)`。下蹲来源：冲刺开始时按住下或用 crouch dash 键（§8.11）、地面斜下冲刺转水平（§8.4）、斜下冲刺落地转换（§6.2）、或地面冲刺钻缝自动下蹲（§6.1）。
- **wavedash**：空中斜下冲刺，在冲刺状态内落地（§6.2 第 3 步转为水平+下蹲），下一帧起 `onGround` 使 `jumpGraceTimer > 0`，在 F14 之前按跳即为 hyper。
- **F4 的特殊情况**：F4 的 DashUpdate 先于方向设置执行，DashDir = (0,0) 被当成"水平"。所以若跳键在 F0–F4 之间按下并一直按住（缓冲 5 帧可覆盖到 F4），且在地面/coyote time 内，**F4 就会触发 super（方向 = Facing），冲刺方向根本不会被应用**——即使你想冲正上或斜上。若 F4 不满足 super 条件（不在地面也不在 coyote time 内）但 3 px 内有墙，F4 的跳键则会触发普通 walljump/climbjump（因为 DashDir 为零，不算"正上冲刺"，不会是 super walljump）。想要地面向上冲刺时，跳键不要在 F0–F4 内处于"按下"状态。
- super / hyper 之后冲刺立即结束；冲刺次数是否已补回取决于是否已到 F10 且在地面。

### 8.7 结束（F14）

1. 若 `DashDir.Y <= 0`（水平或向上）：`Speed = DashDir × 160`（**覆盖**冲刺中保留的更快水平速度）。
2. 然后若 `Speed.Y < 0`：`Speed.Y × 0.75`。
3. 若 `DashDir.Y > 0`（向下或斜下）：速度**保持不变**（240 或 169.7/169.7，或碰撞后的值）。
4. `AutoJump = true`（保持到落地）：之后在 |vy| < 40 时即使不按跳也是半重力。
5. 切回 StNormal（`maxFall = 160`）。

| 方向 | 冲刺中速度 | 结束后速度 |
| --- | --- | --- |
| 右 | (240, 0)（或更快的原水平速度） | (160, 0) |
| 右上 | (169.71, −169.71) | (113.14, −84.85) |
| 上 | (0, −240) | (0, −120) |
| 右下 | (169.71, 169.71) | 不变 |
| 下 | (0, 240) | 不变 |

### 8.8 冷却与补充

- 冷却：两次冲刺受理之间至少 12 个正常帧（F0 → F15）。
- 站地补充：冲刺开始后第 7 个正常帧（F10）起，每帧在 §3 第 11 步检查；需要 `onGround` 且脚下是实心物或单向平台，且碰撞箱不与尖刺重叠。
- 其他补充来源：冲刺水晶（Refill，也可在冲刺中吃）、弹簧、离开梦境块、换房、未冲刺时被水晶之心弹开（详见各实体）。**冲碎冲刺砖不补充。**
- 补充把 `Dashes` 设为上限（`MaxDashes`，即章节库存的冲刺数：序章初始 0，1A/2A 为 1）。

### 8.9 Dash attack（DashAttacking）

`dashAttackTimer > 0`（或处于 StRedDash）。用途：冲碎冲刺砖、进入梦境块、super walljump、冲刺贴地与顶角修正加大、收集水晶之心等。被清零的情况：任何起跳类函数、水平/垂直碰撞回调、弹簧等。

### 8.10 什么会结束或打断冲刺

- 正常结束：F14。
- DashUpdate 内：super、super walljump、walljump、climbjump（切回 StNormal）。
- 碰撞：冲碎冲刺砖反弹（Rebound → StNormal）；进入梦境块（→ StDreamDash）。
- 实体：弹簧（→ StNormal）、死亡。
- 换房：向上换房会把状态设为 StNormal 并给 −105 的上升速度（§13.3）。
- **撞墙、顶头、落地不会结束 StDash**（只把对应速度分量清零，或落地转水平）。

### 8.11 冲刺开始时的下蹲（含 crouch dash）

DashBegin 中（F0）：
- 若不在地面且正在下蹲且可以起身：**取消下蹲**（空中已下蹲时冲刺会起身，即使用的是 crouch dash 键）。
- 否则若不在下蹲，且（用 crouch dash 键冲刺 或 F0 按住下）：**进入下蹲**（地面或空中都可）。
- 下蹲中冲刺使用 8×6 碰撞箱，可以穿过更矮的缝；冲刺中起跳则为 hyper。
- 冲刺结束后：空中下落时自动起身（§3 第 25 步）；在地面上回到 StNormal 后，若不按住下且可起身则起身。

---

## 9. StClimb（编号 1）

### 9.1 进入

见 §5.2 第 2 步：按住抓、不疲劳、不下蹲、`Speed.Y >= 0`、没有背离朝向移动、面前 1 px 间隙内有可抓的实心物（或头顶 1–2 px 抓墙辅助）。也可从梦境冲刺结束时直接进入（§10.2）。

ClimbBegin：`AutoJump = false`；`Speed.X = 0`；`Speed.Y × 0.2`；`wallSlideTimer = 1.2`；`climbNoMoveTimer = 0.1`；`wallBoostTimer = 0`；向墙方向最多挪 2 px 直到贴墙（只要前方 1 px 没有实心物就挪 1 px，直接改位置）。

### 9.2 ClimbUpdate 的顺序

1. `climbNoMoveTimer −= dt`。
2. 若 `onGround`：`Stamina = 110`。
3. **跳**：若 `Jump.Pressed` 且（不下蹲或可起身）：若 `moveX == −Facing`：WallJump(−Facing)（**不耗体力**）；否则 ClimbJump()（不在地面时耗 27.5，不检查体力是否足够）。切回 StNormal。
4. **冲刺**：若 CanDash：`Speed += LiftBoost`，开始冲刺。
5. **松手**：若没有按住抓：`Speed += LiftBoost`，切回 StNormal。
6. **墙没了**：若碰撞箱沿 Facing 平移 1 px 不再与实心物重叠：若 `Speed.Y < 0` 则 climb hop()（§9.3）；切回 StNormal。
7. **上下移动**（目标速度 `t`，是否打滑 `slip`）：
   - 若 `climbNoMoveTimer > 0`：`t = 0`，`slip = true`。
   - 否则若面前有 ClimbBlocker：`slip = true`。
   - 否则若按住上（MoveY == −1）：`t = −45`；
     - 若头顶 1 px 有实心物，或（climb hop 被阻挡 且 手部上方够不到墙）：`Speed.Y = max(Speed.Y, 0)`，`t = 0`，`slip = true`。
     - 否则若手部够不到墙（SlipCheck，见下）：**climb hop** ClimbHop()，切回 StNormal，返回。
   - 否则若按住下（MoveY == 1）：`t = 80`；若在地面：`Speed.Y = min(Speed.Y, 0)`，`t = 0`。
   - 否则：`slip = true`。
   - `lastClimbMove = sign(t)`。
   - 若 `slip` 且手部够不到墙：`t = 30`（向下滑）。
   - `Speed.Y = Approach(Speed.Y, t, 900 × dt)`。
8. 若没有按住下且 `Speed.Y > 0` 且"沿 Facing 1 px、向下 1 px"处没有实心物（脚下已经越过墙的下端）：`Speed.Y = 0`（不会从墙底滑出去，除非按住下）。
9. **体力**：若 `climbNoMoveTimer <= 0`：上一步向上（lastClimbMove = −1）则 `Stamina −= 45.4545 × dt`；静止（0）则 `Stamina −= 10 × dt`；向下不扣。
10. 若 `Stamina <= 0`：`Speed += LiftBoost`，切回 StNormal（掉下来）。否则保持 StClimb。

- 攀爬中水平速度始终为 0；朝向不随输入改变。
- 攀爬中仍正常执行 §3 的移动（垂直按 Speed.Y 移动）。
- 攀爬中算作"骑在面前的墙上"：墙移动会带着人走，也会触发踩踏类机关。
- **SlipCheck（手部够不到墙）**：在碰撞箱朝 Facing 一侧外边紧邻的那一列像素（右侧为 x = 右边缘；左侧为 x = 左边缘 − 1），取"顶边 + 4"和"顶边"两个点；两个点都没有实心物即为"够不到"。带参数 −1 的版本取"顶边 + 3"和"顶边 − 2"。
- **climb hop 被阻挡**（ClimbHopBlockedCheck）：头顶 6 px 内有实心物，或朝 Facing 方向 8 px 处有带"防边缘"组件的实体（朝上/朝左/朝右的尖刺都带此组件），或身后跟着草莓种子。

### 9.3 Climb hop（ClimbHop）

- 若面前 1 px 有实心物：记住它，等越过墙顶后才给水平速度（`hopWaitX = Facing`，`hopWaitXSpeed = 100 × Facing`，§3 第 19 步）；否则立即 `Speed.X = 100 × Facing`。
- `Speed.Y = min(Speed.Y, −120)`。
- `forceMoveX = 0`，`forceMoveXTimer = 0.2`：之后 **12 帧水平输入被强制为 0**（StNormal 会以空中减速 10.833/帧让 vx 衰减）。
- 若所依附的墙在移动，climb hop 期间 Madeline 跟随它整像素移动。

### 9.4 疲劳

- 疲劳 = 体力（climbjump 转 walljump 窗口内 +27.5）< 20。
- 疲劳时不能**开始**抓墙；已经在攀爬中不会因疲劳掉下，只在体力 ≤ 0 时掉下。
- 从满体力向上爬到疲劳约 119 帧，到 0 约 145 帧；静止抓墙到 0 约 660 帧。
- 体力恢复：站在地面（非攀爬）、攀爬中站到地面、冲刺水晶、弹簧、梦境冲刺、换房。

---

## 10. StDreamDash（编号 9，2A 的梦境块）

### 10.1 进入条件（在碰撞回调中检查，方向 d 为碰撞方向的单位向量）

1. 章节库存允许梦境冲刺（梦境块处于激活状态）；
2. DashAttacking；
3. `d.x == sign(DashDir.X)` 或 `d.y == sign(DashDir.Y)`（朝冲刺方向撞上）；
4. 位置 + d 处碰撞箱与某个梦境块重叠；
5. 若位置 + d 处还与**梦境块以外的**实心物重叠：沿垂直于 d 的方向尝试偏移 1..4 px（先负方向：若 d 水平则先试向上，前提是 vy ≤ 0；再正方向，前提是 vy ≥ 0；d 垂直时同理用 vx 判断先左后右），找到"偏移后位置 + d 处不与梦境块以外的实心物重叠"的偏移，就把位置直接挪过去并进入；都不行则不进入（按正常碰撞处理）。

DreamDashBegin：`Speed = DashDir × 240`（覆盖原速度，包括冲刺保留的更快水平速度）；不再按正常方式碰撞（"天真移动"）；`dreamDashCanEndTimer = 0.1`；`Stamina = 110`。

### 10.2 DreamDashUpdate

1. 直接按 `Speed × dt` 移动（累积余量后取整，**不做任何碰撞检测**）。§3 的第 26–27 步与房间边界处理在此状态下都跳过。
2. `dreamDashCanEndTimer −= dt`（若 > 0）。
3. 若当前仍与梦境块重叠：继续（速度恒定，方向不变）。
4. 若已不与任何梦境块重叠：
   - 若与其他实心物重叠：尝试对角偏移 (±i, ±k)，i、k ∈ 1..5（i 由小到大、先负后正，内层 k 同样），找到不重叠处就直接挪过去，然后按下一条继续判断；全都重叠则**死亡**。
   - 若 `dreamDashCanEndTimer <= 0`：**离开**：
     1. 冻结 0.05 s（3 帧）。
     2. 若 `Jump.Pressed` 且 DashDir 有水平分量：立即 Jump()（梦境跳）。
     3. 否则若 DashDir.Y ≥ 0 或有水平分量：若向右冲出且身后 5 px 有实心物，整像素左移最多 5（贴回块边）；向左同理。然后若按住抓且 moveX 指向一侧可抓的墙：Facing = moveX，**进入 StClimb**。
     4. 否则切回 StNormal。
   - 若计时未到：继续以原速前进（即离开梦境块后至少飞满 0.1 s 才结束）。

### 10.3 离开时（DreamDashEnd）

- 若不是梦境跳：`AutoJump = true`（保持到落地）。
- **补满冲刺次数**，补满体力。
- 若 DashDir 有水平分量：`jumpGraceTimer = 0.1`（离开后有 5 帧 coyote time）；否则 `jumpGraceTimer = 0`。
- 速度保持冲刺速度（240 或对角 169.7），离开后按 StNormal 规则受重力和空中减速。

### 10.4 其他

- 梦境冲刺中 Madeline 算作"骑在"与她重叠的实心物上：移动的梦境块会带着她走。
- 没有激活时梦境块就是普通实心物（可站、可抓、可蹬）。
- 进入梦境块的那一刻（碰撞回调中）状态立即改变，本帧剩余位移放弃。

---

## 11. 其他状态

- **StBoost（4）/ StRedDash（5）**：泡泡。本三章 A 面地图中没有泡泡。规则摘要：进入泡泡时补满冲刺与体力；每帧以 80 px/s 向泡泡中心（+瞄准方向 × 3 px）移动；按冲刺键或 0.25 s 后自动发射，进入 StDash（绿）或 StRedDash（红），**不消耗冲刺次数**；方向同样取冻结结束后第一帧的 `lastAim`。
- **StHitSquash（6）**：红泡泡冲刺撞墙后的硬直：速度以 800 px/s² 趋向 0，0.1 s 后回到 StNormal；期间可跳、冲刺、抓墙。
- **StLaunch（7）**：被爆炸类物体弹射（本三章无）：上升时 vy 以 450、下降时以 225 趋向 160；vx 以 200 趋向 0；速度大小 < 220 时回到 StNormal；可冲刺。
- **StDummy（11）**：剧情控制。不可控；默认：可起身则起身；若不在地面受重力（同 StNormal 的半重力规则，目标 160）；variable jump 规则同样适用；不在剧情行走时水平以 1000 减速到 0、超过 90 时以 2500 压回 90。
- **StIntroRespawn（14）**：死亡后重生的入场动画，约 0.6 s 后自动回到 StNormal。不受重力，不能操作。
- **StIntroWalk（12）/ StIntroJump（13）/ StIntroWakeUp（15）**：章节开头的入场方式，脚本控制，结束后回到 StNormal。
- **StBirdDashTutorial（16）**：序章结尾的冲刺教学剧情。剧情先把时间倍率逐帧降到 0（DeltaTime = 0，一切静止），并显示提示；剧情脚本等待"瞄准方向的 x > 0 且 y < 0（右上）并且冲刺键按下"，然后把状态设为 16、冲刺次数设为 0、章节库存冲刺数设为 1、时间倍率恢复 1。状态 16 的冲刺是脚本固定的右上冲刺，之后由脚本处理下落，玩家不可控。
- **StCassetteFly（21）**：拾取磁带后被泡泡沿曲线带走，脚本控制，结束后回到 StNormal。
- 可控（InControl）为假的状态：11–17、23、25。不可控时不更新朝向、不做房间边界处理、相机不跟随。

---

## 12. 实体触发的"速度设定"函数

| 函数 | 由谁触发 | 效果 |
| --- | --- | --- |
| SuperBounce(顶边y) | 地面弹簧 | 换成普通碰撞箱，按 `顶边y − 碰撞箱底` 垂直移动（带碰撞）；补满冲刺（章节允许时）与体力；状态 StNormal；`Speed.X = 0`；`Speed.Y = −185`；variable jump 0.2 s；`AutoJump = true`（保持到落地）；清 dashAttack、wallSlide 等 |
| SideBounce(dir, x, y) | 墙面弹簧 | 若已经以 > 240 的速度朝 dir 移动则无效（返回失败）；否则换成普通碰撞箱，先把碰撞箱底边向弹簧中心高度移动（最多 ±4 px），再把碰撞箱的近侧边缘移到弹簧外侧边缘（均为带碰撞的移动）；补满冲刺与体力；StNormal；`Speed = (240 × dir, −140)`；variable jump 0.2 s；AutoJump 保持；`forceMoveX = dir` 持续 0.3 s（18 帧） |
| Bounce(顶边y) | （本三章少见） | 同 SuperBounce 但 `Speed.Y = −140`，`Speed.X` 不变，AutoJump 只持续 0.1 s |
| Rebound(dir) | 冲碎冲刺砖 | `Speed = (120 × dir, −120)`（垂直撞碎时 dir = 0）；variable jump 0.15 s；AutoJump 保持；清强制水平输入；StNormal |
| ReflectBounce(d) | 部分实体 | d 的非零分量把对应速度设为 ±220；StNormal |
| PointBounce(点) | 未冲刺触碰水晶之心 | 结束冲刺；补满冲刺与体力；速度 = 从点指向身体中心的单位向量 × 220，若该单位向量 y ∈ (−0.2, 0.4] 则先把 y 设为 −0.2；vx × 1.5；若 \|vx\| < 100 则设为 100（vx 为 0 时取 −Facing 方向） |
| UseRefill(两次) | 冲刺水晶 | 若 `Dashes < 目标`（普通水晶目标 = 冲刺上限，粉水晶 = 2）或体力 < 20：`Dashes = 目标`，体力补满，返回成功（水晶消失）；否则无效果，水晶不消失 |

---

## 13. 死亡、房间边界、换房、重生

### 13.1 死亡原因（本三章）

1. **尖刺**：受击箱与尖刺重叠且满足方向条件（见 `entities.md` Spikes）。
2. **追逐的 Badeline** 碰到受击箱。
3. **被实心物挤死**：实心物推动 Madeline 时她被另一实心物挡住，且挤压求生失败（§14.4）。
4. **掉出房间底部**：碰撞箱顶边 > 房间下边界 + 4，且下方没有可进入的房间（或该房间禁止向下换房）。
5. **梦境冲刺冲出后卡在实心物里**且挤出失败。
- 死亡时：速度清零、状态机锁定、不再可碰撞；生成尸体动画。辅助模式无敌时大多数死亡被忽略（本项目不用）。

### 13.2 房间边界处理（§3 第 33 步，仅可控且不在梦境冲刺时）

按顺序：
1. **左边**：若碰撞箱左边 < 房间左边界：若碰撞箱完全在房间上下界之间（顶 ≥ 上界且底 < 下界）且左侧有相邻房间：开始向左换房；否则把碰撞箱左边夹到边界，`Speed.X = 0`。
2. **右边**：对称处理。
3. **上边**：若碰撞箱中心 y < 房间上边界：若上方有相邻房间：开始向上换房；否则若碰撞箱顶边 < 上边界 − 24，夹到上边界 − 24，`Speed.Y = 0`（可以冒出房间顶部最多 24 px）。
4. **下边**：若碰撞箱底边 > 房间下边界，且下方有相邻房间且允许向下换房：若脚下 4 px 内没有实心物，开始向下换房。否则若碰撞箱顶边 > 下边界 + 4：死亡。

### 13.3 换房时对 Madeline 的影响

- 向上换房前：若不在红泡泡冲刺中，`Speed.X = 0`；且（非红泡泡等特殊状态）`Speed.Y = −105`、`varJumpSpeed = −105`、`varJumpTimer = 0.2`、`AutoJump = true`、状态设为 StNormal；`dashCooldownTimer = 0.2`。
- 向下换房前：状态设为 StNormal，`Speed.Y = max(0, Speed.Y)`，取消 AutoJump 与 variable jump。
- 换房过程中 Madeline 由脚本以每帧最多 1 px 的速度移到新房间内的目标点；完成后速度分量四舍五入为整数，余量清零。
- 换房完成：`wallSlideTimer = 1.2`，`jumpGraceTimer = 0`，`forceMoveXTimer = 0`，清空追逐者历史，**补满冲刺与体力**；重生点设为新房间里离入口位置最近的出生点（若站在"重生目标触发器"内，用其目标点）。
- 换房的自动推进帧由主控处理（不在轨迹里）。

### 13.4 重生

- 死亡动画结束后整个房间重新加载（房间内实体恢复初始状态；**永久型冲刺砖被撞碎后不会恢复**；跟随中的草莓丢失）。
- Madeline 在当前重生点出现，状态 StIntroRespawn（约 0.6 s，不可控），冲刺次数为上限，体力满。
- 重生点会被"更改重生点触发器"更新（进入触发器且目标点无实心物时）。

---

## 14. Monocle 碰撞与平台运动

### 14.1 重叠判定

- 两个实体相撞需要：双方都有碰撞体、不是同一实体、**被检测的一方**可碰撞（Collidable 为真）。（检测方自己的 Collidable 不影响。）
- 矩形与矩形：严格相交（A 右 > B 左 且 A 底 > B 顶 且 A 左 < B 右 且 A 顶 < B 底）。贴边不算。
- 矩形与地形网格：取矩形的整数包围盒，检查覆盖到的 8×8 格中是否有实心格。位置为整数时等价于与实心格做严格矩形相交。
- 圆形碰撞体（刺球、泡泡）：矩形与圆相交（本三章不涉及）。
- "外部重叠"（CollideCheckOutside）：当前不重叠、移动到目标位置后重叠。单向平台的落地与地面判定都用它。
- 碰撞列表（多个碰撞箱组合）：任一部分相交即相交。

### 14.2 Actor 移动（Madeline 与其他会动的角色）

**MoveH(m) / MoveV(m)**：余量 += m；`n = 银行家舍入(余量)`；若 n ≠ 0：余量 −= n，调用 MoveHExact(n) / MoveVExact(n)。

**MoveHExact(n)**（逐像素）：
1. 每一步：检查"碰撞箱沿移动方向平移 1 px 后"是否与某个实心物重叠。
2. 若重叠：水平余量清零；以（方向、已移动像素、目标位置、被撞物、推动者）调用碰撞回调；**停止**，返回"撞到了"。
3. 否则 x 移动 1 px，继续。
- **水平移动不与单向平台碰撞。**

**MoveVExact(n)**：同上，但：
- 每一步先检查实心物；
- 若向下移动（n > 0）且不忽略单向平台：再检查"下移 1 px 后与某个单向平台**外部重叠**"（当前不与它重叠），是则视为撞到（落在平台上）。
- **向上移动不与单向平台碰撞**（可从下方穿过）。已经与单向平台重叠时向下移动也不会被它挡住，直到离开它再重新从上方碰到。

**NaiveMove**（梦境冲刺用）：余量累加、取整后直接改位置，不做碰撞。

**OnGround**：下移 1 px 后与实心物重叠，或（不忽略单向平台时）与单向平台外部重叠。

**是否"骑在"某平台上**（决定平台移动时是否被带走）：
- 一般：实心物——碰撞箱下移 1 px 后与它重叠；单向平台——与它外部重叠（下移 1 px）。
- Madeline 特例：StDreamDash 时骑在任何与她重叠的实心物上、不骑任何单向平台；StClimb / StHitSquash 时骑在 Facing 方向 1 px 处的实心物上；本帧"wallslide 并按住抓"时骑在那面墙上；单向平台只在非攀爬且 `Speed.Y >= 0` 时才算骑着。

### 14.3 平台（Platform：实心物 Solid 与单向平台 JumpThru）的移动

**Platform.MoveH(m)**：`LiftSpeed.X = m / dt`（本帧请求的位移折算的速度）；余量 += m；n = 舍入；若 n ≠ 0：余量 −= n，调用 MoveHExact(n)。MoveV 同理。MoveTo(x, y) 是先 MoveToX 再 MoveToY。平台每帧更新时，组件（多为移动协程）先运行，然后把自身 LiftSpeed 清零。

**Solid.MoveHExact(n)**（一次移动 n 像素，不逐像素）：
1. 先收集所有"骑在它上面"的 Actor。
2. 特例：若 Madeline 不是骑手，且本帧水平输入方向与 n 同号、她的水平速度也与 n 同号，且"实心物移动 n 并上移 1 px"后会与她重叠：让 Madeline `MoveV(+1)`（向下 1 px）。
3. 实心物 x += n，带动附着在它上面的静态附着物（尖刺、弹簧）。
4. 若实心物可碰撞，对每个 Actor：
   - 若与移动后的实心物**重叠**（且不是天真移动）：把它推出去——推动距离使其边缘贴到实心物移动后的边缘（向右推时：n − (Actor 左边 − 原右边)）；推的过程中实心物暂时不可碰撞；Actor 用 MoveHExact 移动，被别的实心物挡住时调用其"挤压回调"（§14.4）；Actor 的 LiftSpeed = 实心物的 LiftSpeed。
   - 否则若是骑手：Actor 移动 n（MoveHExact，可被别的实心物挡住，不触发挤压）；LiftSpeed 同上。
5. **推优先于载**。垂直 MoveVExact 同理（向下移动的平台把骑手一起带下）。

**JumpThru.MoveHExact(n)**：先把所有骑手移动 n（**不设置 LiftSpeed**），再移动平台。
**JumpThru.MoveVExact(n)**：
- 向上（n < 0）：骑手随之上移并设置 LiftSpeed；另外，"当前不与平台重叠、但平台上移后会重叠"的 Actor 被直接抬到平台顶上（等于从下方被兜住）。
- 向下：骑手随之下移并设置 LiftSpeed。
- 然后移动平台。

**静态附着物（StaticMover）**：尖刺、弹簧等在关卡加载时若贴着某个实心物/单向平台，就附着其上，随其移动、震动；附着平台被禁用时它们也被禁用（如磁带块）。弹簧被踩时会"触发"其附着平台（例如让坠落块开始坠落）。

### 14.4 被挤压（Madeline 的挤压回调）

当实心物推 Madeline 而她被另一实心物挡住时：
1. 若不在下蹲且不在攀爬：先切换为下蹲碰撞箱；（推动者暂时视为可碰撞）若此时已无重叠则成功；否则试着直接放到推动的目标位置，若无重叠则成功；否则恢复原位置。
2. 挤压求生搜索：在当前位置周围找偏移 (i, j)，|i| ≤ 3、|j| ≤ 5（按 i、j 从小到大，各自先正后负，跳过 (0，0)），第一个不与实心物重叠的偏移直接挪过去；都不行，再以"推动目标位置"为中心做同样搜索。
3. 都失败：**死亡**。若成功且第 1 步临时下蹲过，能起身就起身。

### 14.5 其他

- 平台的"震动"（如坠落块开始前）只影响贴图，不影响碰撞箱位置。
- 实体被移除/加入在下一帧开始时才生效（`README.md` §6），但实体通常会先把自己设为不可碰撞（如冲刺砖被撞碎时立即不可碰撞）。

---

## 15. 未核实 / 推断事项

- 帧数换算全部基于 dt = 0.0166667（float32）；若实测 dt 不同，0.05、0.1、0.15、0.2、0.25、0.3 s 这些边界值对应的帧数会差 1。
- §5.7 的起跳高度表是按本文规则推算的，没有在游戏中实测。
- Solid.MoveHExact 第 2 步那条"向下 1 px"特例的设计意图未深究，只按字面规则记录。
- 挤压求生搜索的遍历顺序按代码结构转述，个别边界情况未逐一核实。
