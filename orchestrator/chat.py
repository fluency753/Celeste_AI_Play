"""通关后按需唤起原 GPT 会话，仅聊天，不建立游戏桥接或录制连接。"""
import argparse
import asyncio
import json
from pathlib import Path

from .codex_agent import CodexRunner
from .config import RunConfig
from .container import ensure_container
from .metrics import Metrics


def can_wake(directory):
    if directory is None:
        return False
    try:
        state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
        if state.get("session_kind") == "ready":
            preparation = json.loads((directory / "prepare_summary.json").read_text(encoding="utf-8"))
            return (state.get("engine") == "codex" and bool(state.get("session_id"))
                    and preparation.get("kb_study", {}).get("complete") is True
                    and not preparation.get("quick_check"))
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        return state.get("engine") == "codex" and bool(state.get("session_id")) and (summary.get("completed") is True or summary.get("user_stopped") is True)
    except (OSError, ValueError):
        return False


async def chat(directory, name_request=None):
    if not can_wake(directory):
        raise ValueError("只能唤起已完成试玩的原生 GPT 会话")
    state = json.loads((directory / "run_state.json").read_text(encoding="utf-8"))
    if name_request and state.get('session_kind') != 'ready':
        raise ValueError('自动取名仅用于准备完成、尚未开始的新试玩')
    previous = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    cfg = RunConfig(agent=state["agent"], save=state["save"], model=state["model"],
                    effort=previous.get("effort", state["effort"]))
    cfg.engine, cfg.provider = "codex", "codex"
    cfg.provider_info = {"label": "Codex（ChatGPT 登录）", "images": True, "allowed_domains": ["api.openai.com"]}
    cfg.knowledge_profile = state.get("knowledge_profile", "explore")
    from .control import RunControl
    cfg.cutscene_mode=RunControl(directory).cutscene_preference() or state.get('cutscene_mode','auto')
    cfg.sim, cfg.live, cfg.chapters = False, True, []
    if name_request:
        cfg.max_minutes = 3
    container = await asyncio.to_thread(ensure_container, cfg)
    metrics = Metrics(directory / "metrics.json", cfg.agent, cfg.model, cfg.effort, cfg.budget_seconds)
    metrics.restore(previous, str(directory))
    runner = CodexRunner(cfg, directory, None, metrics, container, session_id=state["session_id"],
                         resume=True, chat_only=True, naming_only=bool(name_request))
    runner.ready_to_play = state.get("session_kind") == "ready"
    try:
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
    args = parser.parse_args()
    asyncio.run(chat(args.run.resolve(), args.name_save))


if __name__ == "__main__":
    main()
