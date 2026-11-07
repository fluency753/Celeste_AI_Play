"""速通预置模拟器（--sim）的两种提供方式，按是否限额区分。

- 不限额：本机 sim/dist 只读挂到智能体容器的 /sim，智能体在工作区程序里直接调用（Sim、SimPool、检查点），
  每次游戏停下把当前帧的完整场景写到 game/sim_state.json；推演多少都由智能体决定，适合大规模搜索最优路线。
  目前只有 Claude 执行器提供（容器运行方式下本机构建了模拟器时默认开启）。
- 限额（同时开 --limit-planning）：模拟器只由主控私有持有，不挂进容器；智能体经 simulate 工具在硬预算内请求
  批量预测（simulation.py、planning_budget.py）。挂着 /sim 就没法计量（任何 Bash 都能调用它），只能封锁 Shell，
  会让暂停与聊天期间也不能操作工作区；所以限额只走主控入口，工作区文件与程序始终可用。两个执行器相同，
  Codex 只提供这一种。
"""


def controller_owned(cfg) -> bool:
    """这个执行器只提供主控限额预测（Codex）。"""
    return getattr(cfg, 'engine', 'claude') == 'codex'


def controller_sim(cfg) -> bool:
    """主控限额预测：开了模拟器并且限额（两个执行器相同）。"""
    return bool(getattr(cfg, 'sim', False) and getattr(cfg, 'pause_limit', False))


def agent_sim(cfg) -> bool:
    """不限额的 /sim 直接调用（目前只有 Claude 执行器）。"""
    return bool(getattr(cfg, 'sim', False)) and not controller_sim(cfg) and not controller_owned(cfg)


def kb_variant(cfg) -> str:
    """知识库里与模拟器用法有关的段落（<!-- sim:direct --> / <!-- sim:controller -->）选哪一种。"""
    return 'controller' if controller_owned(cfg) or controller_sim(cfg) else 'direct'


def sim_readme(cfg) -> dict | None:
    """直接调用 /sim 时准备阶段另要读完的 /sim/README.md（容器路径 → 本机路径）。"""
    return {'/sim/README.md': cfg.sim_dir / 'README.md'} if agent_sim(cfg) else None
