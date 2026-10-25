"""通关后按需唤起原 agent 会话，仅聊天，不建立游戏桥接或录制连接。

--allow-programs：整理工作区——仍然不碰游戏，但允许智能体在零网络沙箱里用 Bash 修改、运行自己写的程序
（例如在下一章开始前按用户要求改模拟器与搜索程序）。
配合 --sim 挂载只读模拟器，用保存的快照做离线验证；仍不连接游戏。"""
import argparse
import asyncio
import json
from pathlib import Path

from .lifecycle import host_runner
from .runtimes import runtime, config_from_state
from .container import ensure_container
from .metrics import Metrics


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


async def chat(directory, name_request=None, allow_programs=False, sim=False):
    if sim and not allow_programs:
        raise ValueError('离线模拟器需要同时开启 --allow-programs')
    if name_request and (allow_programs or sim):
        raise ValueError('存档命名不开放工作区程序或离线模拟器')
    if not can_wake(directory):
        raise ValueError("只能唤起已准备或已完成试玩的会话")
    state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
    if name_request and state.get('session_kind') != 'ready':
        raise ValueError('自动取名仅用于准备完成、尚未开始的新试玩')
    previous = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    cfg = config_from_state(state, previous)
    if allow_programs and cfg.runtime != 'container':
        raise ValueError('工作区程序和离线模拟器需要 container 运行方式；host 会话仅支持聊天，请使用容器会话')
    # 与原运行相同的游玩模式与可调参数（提示词按模式生成；探索模式的程序时长等跟原运行一致）
    from .modes import MODE_OF_KNOWLEDGE
    from .tuning import apply
    cfg.mode = state.get("mode") or MODE_OF_KNOWLEDGE.get(cfg.knowledge_profile, cfg.mode)
    cfg.sim_assist = bool(state.get("sim_assist")) and cfg.mode == "explore"
    apply(cfg, state.get("tuning") or {})
    runtime(cfg.engine).token(cfg)
    from .control import RunControl
    cfg.cutscene_mode=RunControl(directory).cutscene_preference() or state.get('cutscene_mode','auto')
    cfg.sim, cfg.live, cfg.chapters = bool(sim), True, []
    if name_request:
        cfg.max_minutes = 3
    container = await asyncio.to_thread(ensure_container, cfg)
    metrics = Metrics(directory / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    metrics.restore(previous, str(directory))
    try:
        async with host_runner(cfg, directory, metrics, container, session_id=state['session_id'],
                               resume=True, chat_only=True, naming_only=bool(name_request)) as runner:
            runner.ready_to_play = state.get('session_kind') == 'ready'
            runner.allow_programs = bool(allow_programs)
            if name_request:
                from .save_naming import PROMPT
                runner.control.enqueue('message', PROMPT)
            await runner.run()
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
    args = parser.parse_args()
    asyncio.run(chat(args.run.resolve(), args.name_save, args.allow_programs, args.sim))


if __name__ == "__main__":
    main()
