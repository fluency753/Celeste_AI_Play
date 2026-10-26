# 当前帧统一碰撞视图

桥接 1.0.24 在同一次观测中直接读取地形 Grid、实体 Collider 和玩家碰撞箱，输出 `obs.collision`。对象的移动、禁用、生成和消失随观测替换，不将实体永久烘焙进地形，也不根据零速度推定静态。

## 数据与范围

- `frame/sid/room` 标明身份；`scope/bounds` 标明整个房间或视野的覆盖范围，包含观测边距。
- `coverage.terrain/entities` 分别标明两类来源是否提供；`complete` 是当前范围内的来源完整性，`geometryComplete` 是形状表达完整性。两者不能互相代替，局部完整也不是全房完整。
- `objects` 统一呈现地形、实体固体、单向平台、危险物、触发类和未知对象，保留 ID、来源、机制类别、当前 `collidable`、可变属性和真实 `geometry`。Key 等触发类不会被当成墙。
- Circle 保留半径，ColliderList 保留各子形状，Grid 保留实际占用矩形和空洞。未知形状只报告未知与外接范围，不将其外接矩形解释为精确实心。类别不是永久通行结论，梦境块、门等仍需结合机制和状态。

地形直接取游戏 Grid，展示字符图的 `M`、实体符号等不会抹掉底下的碰撞。旧 `solids` 仍是地形专用字段；新桥接下从权威地形投影生成，不能代表全部障碍。旧桥接没有统一快照时明确缺失，保留原观测供使用，不伪造“完整碰撞图”。

## 工具和工作区

`observe`、每次 `act`（包括同房间）及 `cutscene` 返回对应当前快照的数据，开启图像时另返回分色碰撞 PNG。图中标帧号、房间、世界范围、来源／形状完整性及玩家碰撞箱；地形、实体固体、单向平台、危险物、触发类分色，禁用碰撞画虚线，未知形状不填实。

`game/latest_obs.json` 保留完整观测。`game/collision.json` 保存验证过的当前快照，`image` 指向由帧号和内容哈希命名的 PNG。先写图再原子发布 manifest，已发布 PNG 内容不覆盖；仅保留当前及前一代，旧路径不存在时重读 manifest。`game/rooms/` 是首次入房历史，不能当作当前实体状态。

输出图最长边为 1280，最多处理 16000 个绘图节点、12 层嵌套，超限或不支持的绘图形状在图中明确标记，不能据空白推定安全。精确对象数据仍以 JSON 为准。碰撞图不计算可达性或预测障碍物未来位置。

## 验证

- `python -m unittest calibration.collision_contract_test calibration.collision_view_test calibration.observation_context_test`：真实会话与工具分发、同房移动／消失、剧情更新、历史与当前文件隔离、旧协议和显示保护。
- `dotnet run --project calibration/collision_snapshot "-p:CelestePrefix=<游戏目录>"`：使用实际 Collider、Grid 与实体的无 GPU 原生检查。

这些检查不会连接正在运行的游戏。模型能否据此稳定规划路线、实际游戏中的图像观感以及特殊 MOD 碰撞语义，应分别实测；接口成功和合成图不能替代这些验收。
