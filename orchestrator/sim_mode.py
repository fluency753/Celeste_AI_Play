"""预置模拟器在两个执行器上的提供方式（速通模式 --sim）。

- Claude 执行器沿用原方式：本机 sim/dist 只读挂到智能体容器的 /sim，智能体在工作区程序里直接调用；
  新运行在容器运行方式下、本机构建了模拟器时默认开启，默认不限规划；--limit-planning 只按“调用了模拟器的消息”
  记账（pausegate.PauseGate），不封锁工作区，并关闭自动重试。
- Codex 执行器：模拟器只由主控私有持有，智能体通过 simulate 工具在硬预算内请求批量预测，须同时开启
  --limit-planning（simulation.py、planning_budget.py）；不向智能体容器挂载模拟器文件。
"""


def controller_owned(cfg) -> bool:
    """这个执行器的预置模拟器是否只由主控持有（Codex）。"""
    return getattr(cfg, 'engine', 'claude') == 'codex'


def agent_sim(cfg) -> bool:
    """Claude 执行器开了预置模拟器：/sim 挂进容器，智能体直接调用。"""
    return bool(getattr(cfg, 'sim', False)) and not controller_owned(cfg)


def controller_sim(cfg) -> bool:
    """Codex 执行器开了主控限额预测：simulate 工具 + 硬预算。"""
    return bool(getattr(cfg, 'sim', False) and getattr(cfg, 'pause_limit', False)) and controller_owned(cfg)


def sim_readme(cfg) -> dict | None:
    """Claude 开模拟器时准备阶段另要读完的 /sim/README.md（容器路径 → 本机路径）。"""
    return {'/sim/README.md': cfg.sim_dir / 'README.md'} if agent_sim(cfg) else None
