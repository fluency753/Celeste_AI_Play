# 整章地图（速通模式）

速通模式下，每进入一章的某一面（第一次得到该面的观测时），主控把这一面的**整张官方地图**写进工作区：

`/workspace/game/map/<章节>_<面>.json`，`<章节>` 是章节编号里的 `/` 换成 `_`，例如 `Celeste_1-ForsakenCity_A.json`。

它是地图编辑器层面的静态数据：所有房间的边界、出生点、地形网格、实体与触发器的摆放和属性，包括还没去过的房间。它不是当前状态：实体的运动、开关、已被破坏的方块等以观测与 `rooms/` 为准；剧情或存档进度会让部分实体在游戏里不出现（例如已收集的草莓、按进度出现的石门）。文件只给你的程序读，改动不影响游戏。本机没有模拟器发行包或地图数据时不提供这个文件。

## 格式

```json
{
  "format": "celeste-map/1",
  "sid": "Celeste/1-ForsakenCity",
  "side": "A",
  "coordinates": "world pixels ...",
  "filler": [[x, y, w, h], ...],
  "rooms": [
    {
      "name": "1",
      "x": 0, "y": 0, "width": 320, "height": 184,
      "spawns": [[24, 168], ...],
      "defaultSpawn": [24, 168],
      "solids": ["#####......", "..."],
      "entities": [{"type": "spikesUp", "id": 12, "x": 96, "y": 168, "width": 16, "attrs": {...}}, ...],
      "triggers": [{"type": "cameraOffsetTrigger", "id": 3, "x": ..., "y": ..., "width": ..., "height": ..., "nodes": [[x, y]], "attrs": {...}}, ...]
    }
  ]
}
```

- **坐标**：一律是世界像素坐标，与观测里 Madeline 的 `x`/`y`、实体位置同一套。实体的 `nodes`（移动终点、路径点等）也已换成世界坐标。
- **`solids`**：房间的地形网格，从房间左上角起逐行给出，每个字符一格（8×8 像素）；`#` 是地形实心，`.` 是空。第 `r` 行第 `c` 列覆盖世界坐标 `x ∈ [room.x + 8c, room.x + 8c + 8)`、`y ∈ [room.y + 8r, room.y + 8r + 8)`。房间之外的实心还有 `filler`（房间之间的填充块，像素矩形）。房间高 184 时按游戏的做法记成 180。
- **房间属性**：只在不是默认值时出现，例如 `dark`、`underwater`、`space`、`disableDownTransition`、`enforceDashNumber`、`windPattern`、`cameraOffset`。
- **`entities` / `triggers`**：`type` 是地图里的名字（与观测里的类型名不同，例如 `spinner` 对应水晶刺或尘埃刺、`zipMover` 对应移动块），`id` 是地图里的编号；`width`、`height` 为 0 时省略；`attrs` 是其余属性，原样给出（颜色、速度、标记名、是否单次等）。实体的具体行为见本知识库的 `entities.md`、`mechanics.md`。

## 用法提示

- 换房间之前就能看到下一个房间的地形与实体：可以提前规划跨房间的路线、选进门的位置，或者在程序里按房间边界算出相邻房间。
- 与当前观测结合时，以观测为准（观测里的位置与状态是实时的）。
- 地图很大（一章可达数 MB），不要整段打印进上下文；用程序按房间或按区域读取需要的部分。
