"""通关后按需唤起原 agent 会话，仅聊天，不建立游戏桥接或录制连接。

--allow-programs：整理工作区——仍然不碰游戏，但允许智能体在零网络沙箱里用 Bash 修改、运行自己写的程序
（例如在下一章开始前按用户要求改模拟器与搜索程序）。
配合 --sim 挂载只读模拟器，用保存的快照做离线验证；仍不连接游戏。
章节中途显式中断、游戏保持冻结的运行也能这样唤起（例如暂停去改搜索程序），聊完再 --resume 接着玩。
--fresh-session：接班会话——同一个智能体、同一个工作区与容器里开一个全新的会话（不带原会话的上下文），
新建一个运行目录记下它；之后 --resume <新目录> 续跑就接着这个新会话。原运行目录不动。"""
import argparse
import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

from .lifecycle import host_runner
from .runtimes import runtime, config_from_state
from .container import ensure_container
from .metrics import Metrics
from .execution import workspace_capabilities


def can_wake(directory):
    if directory is None:
        return False
    try:
        state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
        runtime(state.get("engine"))
        if state.get("session_kind") == "ready":
            preparation = json.loads((directory / "prepare_summary.json").read_text(encoding="utf-8"))
            return (bool(state.get("session_id"))
                    and preparation.get('engine', 'codex') == state['engine']
                    and preparation.get('provider', 'codex') == state.get('provider', 'codex' if state['engine']=='codex' else 'anthropic')
                    and preparation.get("kb_study", {}).get("complete") is True
                    and not preparation.get("quick_check") and preparation.get("success") is not False)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        return bool(state.get("session_id")) and (summary.get("completed") is True or summary.get("user_stopped") is True)
    except (OSError, ValueError):
        return False


def can_wake_frozen(directory):
    """章节中途中断、游戏保持冻结（summary 的 game_released 为 False）且未通关的运行。

    这种运行之后要 --resume 接着玩；中间允许唤起聊天或整理工作区（不碰游戏、不碰录像）。
    会话管理（新建、切换会话）仍按 can_wake，要先停止试玩、保存录像。"""
    if directory is None:
        return False
    try:
        state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
        runtime(state.get("engine"))
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        return (bool(state.get("session_id")) and summary.get("game_released") is False
                and summary.get("completed") is not True)
    except (OSError, ValueError):
        return False


def successor_run(directory):
    """接班会话的运行目录：复制原运行的设置（run_state）、统计（metrics）与总结（summary 等），换上新的会话编号，
    并标记 session_pending（会话还没真正开始：第一次聊天用新会话开头，之后才按编号接续）。返回新目录。"""
    directory = Path(directory)
    state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
    if state.get("session_kind") == "ready":
        raise ValueError("已准备、尚未开始的会话不需要接班")
    from .action_policy import permissions_from_state
    from .control import RunControl
    preference = RunControl(directory).action_permissions_preference() if (directory / 'control.sqlite3').exists() else None
    state['action_permissions'] = permissions_from_state(state, preference).snapshot()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target, number = directory.parent / stamp, 1
    while target.exists():
        number += 1
        target = directory.parent / f"{stamp}-{number}"
    target.mkdir(parents=True)
    for name in ("metrics.json", "summary.json", "goal.json", "sim_energy.json"):
        if (directory / name).exists():
            shutil.copy2(directory / name, target / name)
    # 统计里属于原会话的几项从零算（新会话没有压缩过、上下文是空的）；现实用时、决策、死亡等累计项照旧
    if (target / "metrics.json").exists():
        metrics = json.loads((target / "metrics.json").read_text(encoding="utf-8"))
        metrics.update(compactions=0, compact_s=0.0, context_tokens=0)
        (target / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    state.update(session_id=str(uuid.uuid4()), session_pending=True, resumed_from=str(directory),
                 fresh_session_from=state.get("session_id"))
    state.pop("session_name", None)
    (target / "run_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _clear_pending(directory):
    path = Path(directory) / "run_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.pop("session_pending", None) is not None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


async def chat(directory, name_request=None, allow_programs=False, sim=False, fresh_session=False):
    if sim and not allow_programs:
        raise ValueError('离线模拟器需要同时开启 --allow-programs')
    if name_request and (allow_programs or sim):
        raise ValueError('存档命名不开放工作区程序或离线模拟器')
    if name_request and fresh_session:
        raise ValueError('存档命名不开接班会话')
    if not (can_wake(directory) or (not name_request and can_wake_frozen(directory))):
        raise ValueError("只能唤起已准备、已完成试玩，或中断后保持冻结的会话")
    if fresh_session:
        directory = successor_run(directory)
        print(f"[chat] 接班会话：新运行目录 {directory}（之后用 --resume 这个目录续跑）", flush=True)
    state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
    pending = bool(state.get("session_pending"))
    if name_request and state.get('session_kind') != 'ready':
        raise ValueError('自动取名仅用于准备完成、尚未开始的新试玩')
    previous = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    cfg = config_from_state(state, previous)
    capabilities = workspace_capabilities(cfg)
    capabilities.validate(programs=allow_programs, sim=sim)
    # 与原运行相同的游玩模式与可调参数（提示词按模式生成；探索模式的程序时长等跟原运行一致）
    from .modes import MODE_OF_KNOWLEDGE
    from .tuning import apply
    cfg.mode = state.get("mode") or MODE_OF_KNOWLEDGE.get(cfg.knowledge_profile, cfg.mode)
    cfg.sim_assist = bool(state.get("sim_assist")) and cfg.mode == "explore"
    apply(cfg, state.get("tuning") or {})
    # 提示词的其他开关也按原运行：仅正式思考、思考僵直、碰撞图、节省上下文（原来没带上，唤起的会话看到的规则与试玩时不同）
    cfg.stiffness = bool(state.get("stiffness")) and cfg.mode == "explore"
    cfg.formal_thought = bool(state.get("formal_thought"))
    cfg.collision_image = state.get("collision_image")
    cfg.save_context = bool(state.get("save_context"))
    runtime(cfg.engine).token(cfg)
    from .control import RunControl
    cfg.cutscene_mode=RunControl(directory).cutscene_preference() or state.get('cutscene_mode','auto')
    cfg.sim, cfg.live, cfg.chapters = bool(sim), True, []
    if name_request:
        cfg.max_minutes = 3
    if capabilities.needs_container:
        from .kb_access import prepare_view
        prepare_view(cfg)
    container = await asyncio.to_thread(ensure_container, cfg) if capabilities.needs_container else None
    metrics = Metrics(directory / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    metrics.restore(previous, str(directory))
    try:
        async with host_runner(cfg, directory, metrics, container, session_id=state['session_id'],
                               resume=not pending, chat_only=True, naming_only=bool(name_request)) as runner:
            runner.ready_to_play = state.get('session_kind') == 'ready'
            runner.allow_programs = bool(allow_programs)
            if name_request:
                from .save_naming import PROMPT
                runner.control.enqueue('message', PROMPT)
            try:
                await runner.run()
            finally:
                if pending and getattr(runner, 'turns', 0):
                    _clear_pending(directory)   # 新会话已经开始：之后按编号接续
            if name_request:
                if not runner.name_result:
                    raise ValueError('会话没有完成存档命名')
                result = directory / 'save_name_result.json'
                temporary = result.with_suffix('.tmp')
                temporary.write_text(json.dumps({'request_id': name_request, 'name': runner.name_result}, ensure_ascii=False), encoding='utf-8')
                temporary.replace(result)
    finally:
        metrics.update(status="done")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument('--name-save', help=argparse.SUPPRESS)
    parser.add_argument("--allow-programs", action="store_true",
                        help="整理工作区：允许智能体在沙箱里用 Bash 修改、运行自己写的程序（仍然不碰游戏）")
    parser.add_argument('--sim', action='store_true',
                        help='配合 --allow-programs 挂载只读模拟器，以保存快照做离线验证')
    parser.add_argument('--fresh-session', action='store_true',
                        help='接班会话：同一工作区与容器里开一个全新的会话，新建运行目录记下它，之后 --resume 新目录续跑')
    args = parser.parse_args()
    asyncio.run(chat(args.run.resolve(), args.name_save, args.allow_programs, args.sim, args.fresh_session))


if __name__ == "__main__":
    main()
