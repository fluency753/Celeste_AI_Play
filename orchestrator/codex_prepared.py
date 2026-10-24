"""复用独立 GPT 已完成的准备：校验真实覆盖结果、线程、工作区和文件摘要。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def load_preparation(path: str, cfg, study):
    directory = Path(path).resolve()
    data = json.loads((directory / "prepare_summary.json").read_text(encoding="utf-8"))
    if data.get("knowledge_profile", "full") != cfg.knowledge_profile:
        raise ValueError("准备会话的知识范围不同，不能将已读攻略的会话用于新手探索")
    if data.get("quick_check") or not data.get("kb_study", {}).get("complete"):
        raise ValueError("该目录没有完成真实的全库准备，不能跳过阅读")
    if not data.get("native_game_tools"):
        raise ValueError("该准备会话尚未登记完整游戏工具，请用新版 codex_prepare 重新准备")
    if data.get("metrics", {}).get("agent") != cfg.agent:
        raise ValueError("准备阶段的工作区标签与本次 --agent 不一致")
    if not data.get("session_id"):
        raise ValueError("准备阶段缺少独立 Codex 线程 ID")
    for rel in study.files:
        source = study.extra.get(rel) or (study.kb_dir / rel)
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != data.get("kb_hashes", {}).get(rel):
            raise ValueError(f"知识库已变化或没有读取证明，需重新准备：{rel}")
        counts = data["kb_study"].get("files", {}).get(rel)
        if counts != [study.total[rel], study.total[rel]]:
            raise ValueError(f"知识库行覆盖不完整：{rel}")
    for rel in study.files:
        study.read[rel] = set(range(1, study.total[rel] + 1))
    study.finished = time.monotonic()
    return data


if __name__ == "__main__":
    import argparse
    from .config import RunConfig
    from .kb_study import KbStudy

    parser = argparse.ArgumentParser(description="只校验准备会话，不连接游戏或改动 HUD")
    parser.add_argument("directory")
    parser.add_argument("--agent", default="gpt-native")
    parser.add_argument("--knowledge", choices=["explore", "full"], default="explore")
    args = parser.parse_args()
    cfg = RunConfig(agent=args.agent, save="test", model="", knowledge_profile=args.knowledge,
                    sim=args.knowledge == "full")
    study = KbStudy(cfg.kb_dir, 60, {"/sim/README.md": cfg.sim_dir / "README.md"} if cfg.sim else None)
    data = load_preparation(args.directory, cfg, study)
    print("准备会话校验通过：", data["session_id"])
