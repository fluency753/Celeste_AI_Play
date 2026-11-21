"""游戏快照的身份摘要：规划回放核对推演是不是从游戏当前这一帧出发时，主控写快照与代跑通道读入快照两边共用这一份实现
（代跑容器把本文件只读挂成 /snapshot_identity.py）。只用标准库。

快照里有两样是这次游戏运行的记账，不是场景本身：
- 全局帧号：顶层的 frame 与 engine.frameCounter（模拟器只拿后者给补间对象池计时，不影响推演）；
- 运行中新建实体的跨快照编号：实体的 u（只用来在同一次游戏运行的不同快照之间认出同一个实体）。
重开游戏或续跑后重新取的快照，场景一模一样，这两样却会变；智能体存下的旧快照副本因此对不上，回放就不画。
摘要不计帧号，u 按出现的先后重新编号，其余内容按键名排序后以紧凑写法计入：快照文件怎么排版都不影响。
"""
from __future__ import annotations

import hashlib
import json


def snapshot_identity(state: dict) -> str:
    canonical = {key: value for key, value in state.items() if key != 'frame'}
    engine = state.get('engine')
    if isinstance(engine, dict):
        canonical['engine'] = {key: value for key, value in engine.items() if key != 'frameCounter'}
    entities = state.get('entities')
    if isinstance(entities, list):
        order: dict = {}
        canonical['entities'] = [{**entity, 'u': order.setdefault(entity['u'], len(order))}
                                 if isinstance(entity, dict) and 'u' in entity else entity for entity in entities]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
