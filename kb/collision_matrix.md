# 文件型碰撞矩阵

`/workspace/game/collision_matrix.py` 只提供碰撞数据和基础几何查询，不连接游戏、不推进帧、不改变碰撞启用状态，也不提供通行规则或寻路。是否阻挡、单向如何处理、能否移动及怎样到达，由 agent 根据当前场景自行编写脚本分析，再用动作或模拟验证。

## 生成与读取

```bash
python /workspace/game/collision_matrix.py --cell 8
python /workspace/game/collision_matrix.py --obs /workspace/saved_obs.json --out /workspace/matrix.json
```

优先用程序读取当前观测文件的 `obs.collision`；同一快照的全局或局部数据已在文件时，无须再调用查询工具获取一遍。只有想快捷查看或没有脚本能力时，才按需使用局部查询。源为观测文件的 `obs.collision`。默认输出输入文件旁的 `collision_matrix.json`，stdout 只有文件路径、身份、尺寸和构建耗时；矩阵正文留在文件中，不用 Read、cat 或 print 整份送入上下文。主控不在每次动作后生成矩阵。

默认每格 **8×8 像素**，与原生砖格一致；也支持 1/2/4。格长只决定空间索引，不取整精确形状，也不把覆盖部分扩大成整格实心。每格索引所有覆盖它的来源，包含地形、实体、危险物、传感器、未知形状及当前 `collidable=false` 的来源；不自动删去、启用或解释它们。

## Python 数据接口

```python
import sys
sys.path.insert(0, '/workspace/game')
from collision_matrix import Matrix

m = Matrix.from_file('/workspace/game/latest_obs.json', cell=8)
m.save('/workspace/matrix.json')
m = Matrix.load('/workspace/matrix.json')
print('snapshot', m.identity['frame'], m.identity['room'])

# 当前角色矩形的几何相交来源；不是阻挡判断，也不是脚下支撑检查
hits = m.contacts(m.player['hitbox'])
print('intersections', len(hits), [(s['key'], s['type']) for s in hits[:8]])

# 行列从0开始；格子只作空间索引，不把有覆盖的格子视为整格实心
sources = m.cell_sources(0, 0)
print('cell_sources', len(sources), [s['key'] for s in sources[:8]])
# 后续在自己的脚本里读取 m.sources/m.player/m.level 等数据，定义本次问题的规则与算法
```

- `identity`：帧、SID、房间、快照 ID、`scope`、`coverage`、`complete` 与 `geometryComplete`。局部未覆盖或未知形状不等于空地。
- `bounds`、`cell`、`width/height`：世界范围、格长、列数/行数。`cells[row*width+col]` 索引 `memberships`，其成员为 `sources` 下标。
- `sources`：每个来源的原始精确形状、类型、分类、`collidable`、`mutable` 等；实体观测附在 `entity` 中。世界坐标和子形状保持原样；只有来源确实提供的信息才存在，缺字段不表示 false。
- `player`、`level`、`exits`：同帧玩家状态与碰撞箱、关卡信息（包括源观测提供的镜头信息）和房间出口。它们不预测未来状态。
- `cell_sources(row,col)`：返回该格全部来源字典；拒绝负数、越界或非整数行列。
- `contacts(rect)`：按精确矩形、圆形、组合形状查询几何相交，不应用通行策略、不按启用状态筛选。未知形状仅按外接范围返回候选，须检查原形状与完整性；无相交也不表示有支撑或物理可达。

文件中 `rows` 使用 `[连续格数,membership索引]` 游程编码。`Matrix.load` 校验维度、游程和来源一致性后重建索引，拒绝将损坏单元格当空地。

## 时序与分析边界

矩阵只描述指定快照。实体位置、镜头、能力或机制状态变化后，应读取新观测；历史房间文件不代表当前情况。工具保留当前启用状态，不将某种实体或砖块判为始终阻挡或始终可通过。`mutable` 也不等于必然移动。

spinner 的启用可能与距离、镜头、定时刷新及具体类型有关；单帧 `collidable` 不足以推出下一帧状态，更不能对所有静态、移动和自定义 spinner 套同一规则。若缺少判断依据，应查机制、做小规模实验或报告缺失数据；不凭空补启用条件。是否忽略某些条件应在 agent 自己的分析脚本中显式选择并核验。

此前内置的 `policy`、`transition`、`route`、`body` 已移除。不要复用调用这些方法的旧脚本；基础几何相交只是事实查询，通行、动作能力和时间变化由使用者分析，不把未知或一次失败写成“整个房间无法通过”。

## 一致性与预算

矩阵拥有输入快照的独立副本；导出以临时文件原子替换发布。读者仍要检查帧与范围，不把文件写入成功当成游戏状态未变。若自行修改几何，须重新建立索引，不沿用旧格子。

最多 300,000 格、3,000,000 个来源—格引用、6,000,000 次栅格工作量估算、100,000 个形状节点及 24 层嵌套；导入上限32 MiB。超限明确报错，不静默丢来源。脚本仅输出局部结论、依据及耗时，完整矩阵和长轨迹留文件。
