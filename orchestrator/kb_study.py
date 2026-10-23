"""开始游玩前的"读知识库"阶段：跟踪智能体用 Read 读过知识库的哪些行，没读完之前游戏工具不执行。

判断依据是 Claude Code 输出流里 Read 工具的结构化结果（tool_use_result.file：起始行、行数、总行数），
不依赖模型自己汇报。文件太长时 Read 会按 token 上限截断（truncatedByTokenCap），截断之后的行不算读过，
需要智能体用 offset / limit 接着读。
"""
from __future__ import annotations

import time
from pathlib import Path

CONTAINER_KB = "/kb"

# 先读规则（按依赖顺序），再读技巧，最后读社区原文
CORE_ORDER = ["README.md", "player_physics.md", "input.md", "entities.md", "techniques.md", "mechanics.md",
              "game_data.md"]


def required_files(kb_dir: Path) -> list[str]:
    """需要读完的文件（知识库内的相对路径）。草莓酱文本只要求英文原版，中文版是同一内容的翻译。"""
    files = [name for name in CORE_ORDER if (kb_dir / name).exists()]
    files += sorted(p.name for p in kb_dir.glob("*.md") if p.name not in files)
    ref = kb_dir / "reference"
    if (ref / "README.md").exists():
        files.append("reference/README.md")
    wiki = ref / "celeste_wiki"
    if wiki.is_dir():
        pages = sorted(p.name for p in wiki.glob("*.md"))
        if "Tech.md" in pages:  # 总表放在子页面前面
            pages.remove("Tech.md")
            pages.insert(0, "Tech.md")
        files += [f"reference/celeste_wiki/{name}" for name in pages]
    sj = ref / "strawberry_jam"
    for name in ("gyms_en.md", "library_en.md"):
        if (sj / name).exists():
            files.append(f"reference/strawberry_jam/{name}")
    return files


def _ranges(lines: list[int]) -> str:
    """[3,4,5,9] → "3–5、9"。"""
    if not lines:
        return ""
    out = []
    start = prev = lines[0]
    for n in lines[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}–{prev}" if prev > start else f"{start}")
        start = prev = n
    out.append(f"{start}–{prev}" if prev > start else f"{start}")
    return "、".join(out)


class KbStudy:
    def __init__(self, kb_dir: Path, minutes: float):
        self.kb_dir = kb_dir
        self.minutes = minutes
        self.files = required_files(kb_dir)
        self.total: dict[str, int] = {}
        for rel in self.files:
            text = (kb_dir / rel).read_text(encoding="utf-8")
            self.total[rel] = max(1, len(text.splitlines()))
        self.read: dict[str, set[int]] = {rel: set() for rel in self.files}
        self.started = time.monotonic()
        self.finished: float | None = None   # 读完的时刻
        self.forced = False                   # 准备时间用完、强制开放游戏工具
        self.has_reference = any(rel.startswith("reference/") for rel in self.files)

    # ---------------- 输出流

    def note_result(self, event: dict) -> bool:
        """处理一条 user 事件；返回这一条是否让知识库刚好读完。"""
        result = event.get("tool_use_result")
        if not isinstance(result, dict):
            return False
        info = result.get("file")
        if not isinstance(info, dict):
            return False
        path = str(info.get("filePath") or "")
        prefix = CONTAINER_KB + "/"
        if not path.startswith(prefix):
            return False
        rel = path[len(prefix):]
        if rel not in self.read:
            return False
        start = int(info.get("startLine") or 1)
        count = int(info.get("numLines") or 0)
        if info.get("totalLines"):
            self.total[rel] = max(1, int(info["totalLines"]))
        self.read[rel].update(range(start, start + count))
        if self.finished is None and self.complete():
            self.finished = time.monotonic()
            return True
        return False

    # ---------------- 状态

    def unread(self, rel: str) -> list[int]:
        seen = self.read[rel]
        return [n for n in range(1, self.total[rel] + 1) if n not in seen]

    def complete(self) -> bool:
        return all(not self.unread(rel) for rel in self.files)

    def ready(self) -> bool:
        """游戏工具是否开放：读完了，或准备时间已用完。"""
        return self.forced or self.finished is not None

    def deadline(self) -> float:
        return self.started + self.minutes * 60.0

    def force_open(self) -> None:
        self.forced = True

    def progress(self) -> float:
        total = sum(self.total.values())
        done = sum(min(len(self.read[rel]), self.total[rel]) for rel in self.files)
        return done / total if total else 1.0

    def container_paths(self) -> list[str]:
        return [f"{CONTAINER_KB}/{rel}" for rel in self.files]

    def reminder(self) -> str:
        lines = ["知识库还没有读完，游戏工具暂不执行（没有消耗任何游戏帧，也没有开始计时）。还没读到的部分："]
        for rel in self.files:
            missing = self.unread(rel)
            if missing:
                lines.append(f"- {CONTAINER_KB}/{rel}：第 {_ranges(missing)} 行（共 {self.total[rel]} 行）")
        lines.append("请用 Read 读完这些行再开始（文件长时 Read 会截断，用 offset 与 limit 接着读，直到最后一行）。")
        return "\n".join(lines)

    def summary(self) -> dict:
        end = self.finished or time.monotonic()
        return {
            "complete": self.complete(),
            "forced": self.forced,
            "progress": round(self.progress(), 4),
            "read_s": round(end - self.started, 1) if self.finished else None,
            "files": {rel: [len(self.read[rel] & set(range(1, self.total[rel] + 1))), self.total[rel]]
                      for rel in self.files},
        }
