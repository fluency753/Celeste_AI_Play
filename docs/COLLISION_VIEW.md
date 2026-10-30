# 当前帧统一碰撞视图

桥接 1.0.24 在同一次观测中直接读取地形 Grid、实体 Collider 和玩家碰撞箱，输出 `obs.collision`。对象的移动、禁用、生成和消失随观测替换，不将实体永久烘焙进地形，也不根据零速度推定静态。

## 数据与范围

- `frame/sid/room` 标明身份；`scope/bounds` 标明整个房间或视野的覆盖范围，包含观测边距。
- `coverage.terrain/entities` 分别标明两类来源是否提供；`complete` 是当前范围内的来源完整性，`geometryComplete` 是形状表达完整性。两者不能互相代替，局部完整也不是全房完整。
- `objects` 统一呈现地形、实体固体、单向平台、危险物、触发类和未知对象，保留 ID、来源、机制类别、当前 `collidable`、可变属性和真实 `geometry`。Key 等触发类不会被当成墙。
- Circle 保留半径，ColliderList 保留各子形状，Grid 保留实际占用矩形和空洞。未知形状只报告未知与外接范围，不将其外接矩形解释为精确实心。类别不是永久通行结论，梦境块、门等仍需结合机制和状态。

地形直接取游戏 Grid，展示字符图的 `M`、实体符号等不会抹掉底下的碰撞。旧 `solids` 仍是地形专用字段；新桥接下从权威地形投影生成，不能代表全部障碍。旧桥接没有统一快照时明确缺失，保留原观测供使用，不伪造“完整碰撞图”。

## 工具和工作区

`observe`、每次 `act`（包括同房间）及 `cutscene` 返回对应当前快照的数据。开启图像时，碰撞图默认按 `room` 只在 `observe` 与进入新房间时附加；`--collision-image always` 每次附加，`off` 不附加，工作区图像仍按当前快照更新。图中标帧号、房间、世界范围、来源／形状完整性及玩家碰撞箱；地形、实体固体、单向平台、危险物、触发类分色，禁用碰撞画虚线，未知形状不填实。

`game/latest_obs.json` 保留完整观测。`game/collision.json` 保存验证过的当前快照，`image` 指向由帧号和内容哈希命名的 PNG。先写图再原子发布 manifest，已发布 PNG 内容不覆盖；仅保留当前及前一代，旧路径不存在时重读 manifest。`game/rooms/` 是首次入房历史，不能当作当前实体状态。

`game/last_tool.json` 是有工作区时默认更新的游戏工具调用记录，与当前观测和 `last_act.json` 的轨迹职责不同。`last_call` 保存最近调用，`last_action` 保留最近显式推进类工具的调用，观察、解说和明确未执行的前置拒绝不覆盖它；观察发现的快照变化只记入 `last_call`，不能凭帧差认定由观察推进。调用信息含唯一 id、顺序号、运行／存档名及槽位身份、原始参数、处理状态、前后快照、返回文字及图片元数据；图片编码不写入该文件。`act.detail` 直接记录实际结果、各次执行／重算摘要及自动推进说明，各次执行保留实际输入和格式。

`running` 表示处理尚未确认结束，`returned` 表示工具已返回，`raised` 表示处理抛错；这些都不等于模型已收到结果，普通文字拒绝也可能是 `returned`。`execution_status=unconfirmed` 时可能已有部分输入或后台线程仍在执行，不能从快照未变化推出零推进。开始写入失败会拒绝尚未执行的调用；完成写入失败则保留实际返回并提示核验，不能直接重试动作。文件按实际处理顺序原子替换，旧调用较晚完成不会覆盖新记录。跨运行保留的同 agent／存档槽位动作带原 `run_id`，只能作为历史证据；不同 agent／存档或未知槽位不继承该动作。文件读取、Shell 及 Claude 前置钩子未送达游戏服务的请求不更新此文件。

输出图最长边为 1280，最多处理 16000 个绘图节点、12 层嵌套，超限或不支持的绘图形状在图中明确标记，不能据空白推定安全。精确对象数据仍以 JSON 为准。碰撞图不计算可达性或预测障碍物未来位置。

## 首次布局、变化与精查

默认工具使用 `room-context-v2`，首次入房或默认 `observe` 返回 `mode=baseline` 的完整布局，替换旧基线。`groups` 只共用相同属性，不省略实例：每组 `instances` 的行按本组 `instanceColumns`（省略时为 `[id,x,y]`）读取，可能另有 `shape/state` 列；共同的形状或状态写在组上。`shape` 引用同包 `shapes`，形状及所有嵌套 x/y 加实例 x/y 还原世界坐标。所有实例位置保留，组不是实心包围区域，不判定可达性。默认 `stateDetail` 明示仅精选游戏状态，完整反射仍可精查。

同房 `act/cutscene` 返回 `mode=delta`：核对 `base_snapshot_id`，应用 `added/changed` 中的完整替换对象（按 source/id），`removed` 是本覆盖范围内不再观测的 `[source,id]`，不表示永久消失。每条变化自带所需形状定义；`roomChanges` 按同名字段替换首次布局外层的 `exits/spawns/respawnPoint/triggers/entities`，空值也必须应用。地形不变就不重复发送，移动、生成、形状或碰撞状态变化照常报告；范围变化、缓存失效或变化过大时重发 baseline。快照链缺失、历史压缩后记不清布局时默认 `observe` 重建，不猜测缺失数据。

`observe(detail="geometry", region=[x,y,w,h], limit=32)` 只读最近观测缓存，返回区域精确对象与完整反射状态，格式为 `shared-shapes-v1`（独立的本包形状表）。后续页带 `offset=next_offset` 与同一 `snapshot_id`；动作或新观测后重新从第一页查。`observe(detail="player")` 读取完整玩家内部状态。两种详情都不推进游戏，也不改变变化基线。局部查询超出来源范围、来源缺失或存在未知形状时，空结果不证明安全。完整原始工作区与完整碰撞图不变，不必把整房 JSON 打印进聊天。

## 验证

- `python -m unittest calibration.geometry_context_test calibration.collision_contract_test calibration.collision_view_test calibration.observation_context_test`：真实会话与工具分发、同房移动／消失、剧情更新、历史与当前文件隔离、旧协议和显示保护。
- `dotnet run --project calibration/collision_snapshot "-p:CelestePrefix=<游戏目录>"`：使用实际 Collider、Grid 与实体的无 GPU 原生检查。

这些检查不会连接正在运行的游戏。模型能否据此稳定规划路线、实际游戏中的图像观感以及特殊 MOD 碰撞语义，应分别实测；接口成功和合成图不能替代这些验收。
