"""知识库的阅读权限与编辑（控制台的"知识库"页）。

每种游玩模式给智能体挂进容器 /kb 的目录不同：速通模式是整个 kb/（含本机的 kb/reference/ 社区原文），
探索模式只有 kb/explore/。用户可以取消勾选某些文件（RunConfig.kb_deny，命令行 --kb-deny），
也可以给某个系列赛追加本机资料（RunConfig.kb_extra，命令行 --kb-extra，目录放在 local/ 下、不入库；
同名文件覆盖默认版本）。这两种情况下主控把最终的文件同步到 local/kb_views/<工作区标签>/，挂进容器的是这个副本，
准备阶段也只要求读这些文件。全部可读且没有追加（默认）时直接挂知识库本身，与之前完全相同。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import REPO_ROOT
from .kb_study import required_files
from .modes import EXPLORE, SPEEDRUN

KB_ROOT = REPO_ROOT / "kb"
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv"}
MAX_EDIT_BYTES = 2 * 1024 * 1024


def root_of(mode: str) -> Path:
    if mode not in (SPEEDRUN, EXPLORE):
        raise ValueError("游玩模式须为速通或探索")
    return KB_ROOT / "explore" if mode == EXPLORE else KB_ROOT


def _files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith(".") and not name.endswith(".zip"):
                out.append(Path(dirpath) / name)
    return out


def _group(rel: str, mode: str = SPEEDRUN) -> str:
    if mode == EXPLORE:
        return "操作提示（选读）" if rel.startswith("help/") else "入门资料"
    if rel.startswith("reference/community/"):
        return "社区资料"
    if rel.startswith("reference/celeste_wiki/"):
        return "Celeste Wiki"
    if rel.startswith("reference/strawberry_jam/"):
        return "草莓酱训练场与机制库"
    if rel.startswith("reference/"):
        return "参考资料"
    if rel.startswith("explore/"):
        return "探索模式入门资料"
    if rel.startswith("help/"):
        return "操作提示（选读）"
    return "知识库正文"


def documents(mode: str) -> list[dict]:
    """这个模式下智能体能读到的全部文件（知识库内的相对路径）、是否要求在准备阶段读完、能否在控制台编辑。"""
    root = root_of(mode)
    required = set(required_files(root)) if root.is_dir() else set()
    rows = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        rows.append({"path": rel, "size": path.stat().st_size, "group": _group(rel, mode), "required": rel in required,
                     "editable": path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= MAX_EDIT_BYTES,
                     "local_only": rel.startswith("reference/")})
    return rows


def normalize_deny(mode: str, deny) -> list[str]:
    """校验不给看的文件列表：只能是这个模式下存在的文件。"""
    if deny is None:
        return []
    if not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
        raise ValueError("知识库权限须为文件路径列表")
    known = {row["path"] for row in documents(mode)}
    out = sorted({x.strip().replace("\\", "/") for x in deny if x.strip()})
    unknown = [x for x in out if x not in known]
    if unknown:
        raise ValueError("知识库里没有这些文件：" + "、".join(unknown[:5]))
    return out


def _resolve(mode: str, rel: str) -> Path:
    root = root_of(mode).resolve()
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise ValueError("文件路径无效")
    path = (root / rel).resolve()
    if root not in path.parents:
        raise ValueError("只能访问知识库里的文件")
    return path


def read_document(mode: str, rel: str) -> dict:
    path = _resolve(mode, rel)
    if not path.is_file():
        raise ValueError(f"找不到 {rel}")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return {"path": rel, "editable": False, "text": None, "size": path.stat().st_size}
    data = path.read_bytes()
    text = (data[:200000] if len(data) > MAX_EDIT_BYTES else data).decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(data) > MAX_EDIT_BYTES:
        return {"path": rel, "editable": False, "text": text,
                "size": len(data), "truncated": True}
    return {"path": rel, "editable": True, "text": text, "size": len(data),
            "mtime": path.stat().st_mtime}


def write_document(mode: str, rel: str, text: str, expected_mtime: float | None = None) -> dict:
    """保存编辑：只允许文本文件；文件在打开之后被别处改过时拒绝（expected_mtime 对不上）。"""
    path = _resolve(mode, rel)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("只能编辑文本文件（.md、.txt、.json、.csv）")
    if not isinstance(text, str):
        raise ValueError("内容须为文本")
    data = text.replace("\r\n", "\n").encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        raise ValueError("文件太大（最多 2 MB）")
    if not path.is_file():
        raise ValueError(f"找不到 {rel}（控制台只编辑已有的文件）")
    if expected_mtime is not None and abs(path.stat().st_mtime - float(expected_mtime)) > 1e-3:
        raise ValueError("这个文件在你打开之后被改过，请重新打开再编辑")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return {"path": rel, "size": len(data), "mtime": path.stat().st_mtime}


def prepare_view(cfg, log=print) -> Path:
    """有不给看的文件或本机追加资料时，把最终的文件同步到 cfg.kb_dir（local/kb_views/<标签>/）并返回它；
    否则返回知识库本身。追加目录里与知识库同名的文件覆盖默认版本；不给看的文件对追加的文件同样生效。"""
    source = cfg.kb_source
    if not cfg.kb_view:
        return source
    view = cfg.kb_dir
    deny = set(cfg.kb_deny)
    files = {path.relative_to(source).as_posix(): path for path in _files(source)}
    extra_dir = cfg.kb_extra_dir
    extra = {}
    if extra_dir is not None:
        if not extra_dir.is_dir():
            raise RuntimeError(f"找不到本机追加资料的目录：{extra_dir}")
        extra = {path.relative_to(extra_dir).as_posix(): path for path in _files(extra_dir)}
        files.update(extra)
    allowed = {rel: path for rel, path in files.items() if rel not in deny}
    missing = sorted(deny - set(files))
    if missing:
        log(f"[kb] 提示：这些不给看的文件在知识库里不存在：{'、'.join(missing)}")
    view.mkdir(parents=True, exist_ok=True)
    for rel, src in allowed.items():
        dst = view / rel
        st = src.stat()
        if dst.is_file():
            dt = dst.stat()
            if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for path in _files(view):
        rel = path.relative_to(view).as_posix()
        if rel not in allowed:
            path.unlink()
    for dirpath in sorted((d for d, _, _ in os.walk(view)), key=len, reverse=True):
        if Path(dirpath) != view and not any(Path(dirpath).iterdir()):
            Path(dirpath).rmdir()
    if deny:
        log(f"[kb] 知识库有 {len(deny)} 个文件不给智能体看，挂进容器的是只含允许文件的副本（{view}）")
    if extra:
        log(f"[kb] 追加本机资料 {len(extra)} 个（{extra_dir}）：{'、'.join(sorted(extra))}；挂进容器的是合并后的副本（{view}）")
    return view
