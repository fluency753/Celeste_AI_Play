# 文件型碰撞矩阵

`/workspace/game/collision_matrix.py` 是只用标准库的离线几何工具，不连接游戏、不推进帧、不自动推演。源文件为 `latest_obs.json` 的 `obs.collision`；需要时构建，主控不在每步动作后生成矩阵。

## 生成与读取

```bash
python /workspace/game/collision_matrix.py --cell 2
# 可显式指定历史文件或输出位置；历史结果不代表当前实体位置
python /workspace/game/collision_matrix.py --obs /workspace/saved_obs.json --out /workspace/matrix.json
```

默认输出输入文件旁的 `collision_matrix.json`，stdout 只有文件路径、快照身份、尺寸及构建耗时。完整矩阵留在文件中，不用 Read、cat 或 print 把它整份送入上下文。

默认每格 **2×2 像素**，也支持 `--cell 1/4/8`。导出文件保留世界坐标原点、快照帧号、SID、房间、覆盖范围与完整性。`rows` 按行进行 `[连续格数, membership索引]` 游程编码；`memberships` 的每项是 `sources` 下标列表。同一格可以同时含地形和多个实体，不能只取一个来源。精确形状仍在来源表，网格只加速索引。

碰撞矩阵文件和分析结果只对所依据的快照成立。用帧号与身份核对当前观测；实体移动、碰撞状态变化后重建。文件以临时文件原子替换发布，读取期间不会看到半份 JSON；不要把成功写文件当成与随后游戏状态仍一致。

## Python 分析

```python
import sys
sys.path.insert(0, '/workspace/game')
from collision_matrix import Matrix

m = Matrix.from_file('/workspace/game/latest_obs.json', cell=2)
m.save('/workspace/matrix.json')
# 后续程序读取已经导出的矩阵；仍须核对其快照是否适用
m = Matrix.load('/workspace/matrix.json')
print([(s['key'], s['type']) for s in m.contacts([x, y, width, height])])
# start、goal 为角色位置锚点，不是碰撞箱左上角
result = m.route(start, goal, body=m.body(), tolerance=2, max_nodes=20000)
print({k: result[k] for k in
       ('status', 'visited', 'elapsed_ms', 'blocking_sources', 'entity_frontier')})
# result['path'] 留给程序消费或保存，不整条输出
```

`Matrix.from_file` 读取观测文件；`Matrix.load` 读取已导出的矩阵，校验尺寸、游程、来源索引与精确来源的一致性，再重建空间索引，拒绝把损坏的单元格当空地。导入上限为 32 MiB。`contacts(rect)` 用精确形状复查相交来源。`body()` 从同帧玩家位置和碰撞箱推导 `[左偏移, 上偏移, 宽, 高]`；也可显式传入已核实的姿态尺寸，不能把站立姿态的通路结论用于下蹲或反之。

`transition(start, end, body=..., policies=...)` 检查沿单一坐标轴移动时身体扫过的空间，拒绝对角线输入，返回阻挡来源和是否越出已知范围。`route` 用这些转换搜索四方向锚点通道；`step` 默认等于格长，必要时减小步长或换精确局部查询。身体查询保留精确几何，格子有占用不意味着整格实心。

## 策略与结论边界

`policies` 是字典，优先级为来源 `key`、实体 `type`、碰撞 `kind`；值为 `block`、`pass`、`one_way`、`unknown`。用 `sources` 中实际的 key，不自行猜 ID 格式。

默认实心物和危险物阻挡；危险物即使当前未启用也保守阻挡，因为靠近后可能启用。传感器及当前不参与碰撞的其他物体可通过；未知语义保守阻挡。默认朝上的单向平台允许上穿和横穿，阻止身体从上向下穿过顶面；不支持的方向或形状需要明确核实，不能套用普通平台规则。

改变策略只是在比较一个**假设**，不会触发、删除或移动真实实体。遇到动态阻挡，先查相关状态或机制，再做短动作或受限模拟，随后重新观测。`mutable` 不等于肯定会动，也不表示可以直接忽略。

`spatial_path` 只证明本次策略和快照下找到几何通道，未考虑重力、跳跃、冲刺、体力和危险物未来运动；必须结合真实动作或模拟器验证。`no_sampled_path`、`invalid_start`、`budget_exhausted` 都不是“关卡无法通过”。`blocking_sources` 是探索过的阻挡集合，不保证是切断所有路线的最小屏障。局部范围、缺失类别和未知形状会让结论附带条件。

## 分析预算

构建最多 300,000 格、3,000,000 个来源—格引用、6,000,000 次栅格工作量估算、100,000 个形状节点及 24 层嵌套；超限明确报错，不静默丢形状。`route(max_nodes=...)` 限制访问节点数，默认 100,000，最多 300,000。先分析近期目标，够用就转入动作验证，避免全房间穷举；记录构建与查询耗时，预算耗尽时缩小问题或调整分辨率。当前没有物理可达性自动求解，也不跨房间模拟。
