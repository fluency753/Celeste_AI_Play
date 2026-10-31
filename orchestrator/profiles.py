"""运行配置（控制台顶部的"配置"）：一套可以保存、切换、新建、删除的本机设置。

一份配置包括：新运行表单的全部设置（执行器、模型、模式、开关、更多设置、可调参数、章节与存档选择）、
两种模式的知识库阅读权限、对知识库文件的修改、对提示词文件的修改（含启动提示词与按钮消息的模板）、个人追加要求。
游戏目录、端口以外的本机环境（提供方与密钥、录制配置）不属于配置。

三类配置：
- 默认（default）：就是仓库现在的样子：默认设置、仓库知识库、仓库提示词、没有个人追加要求。只读。
- 本机原有设置（local）：沿用旧版本的本机文件：local/prompts/（提示词修改）、local/personal_prompts.json
  （个人追加要求）、configs/local.console.json 里的知识库权限；知识库修改放在 local/kb_edits/。
  命令行不写 --profile、以及没记配置的旧运行续跑都用它，所以这些情况与之前完全相同。
- 新建的配置：local/profiles/<编号>/ 下的 profile.json（名字、表单设置、知识库权限）、prompts/、
  personal_prompts.json、kb/（知识库修改，目录结构与仓库的 kb/ 相同）。

知识库修改不改仓库里的文件：保存在配置自己的目录，挂给智能体的 /kb 是"仓库知识库 + 这份配置的修改"
（orchestrator/kb_access.prepare_view）。

当前进程用哪份配置：主控（orchestrator/run.py）与唤起的会话（orchestrator/chat.py）开始时 activate；
控制台的请求在 using() 里按控制台选中的配置读写（HUD 服务可能跑在主控进程里，不能改主控用着的那份）。
"""
from __future__ import annotations

import contextvars
import json
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

DEFAULT = "default"
LEGACY = "local"
BUILTIN = {DEFAULT: "默认（仓库）", LEGACY: "本机原有设置"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
MAX_NAME = 40
MAX_FORM_BYTES = 200_000
MODES = ("speedrun", "explore")

_active = LEGACY
_override: contextvars.ContextVar[str | None] = contextvars.ContextVar("celeste_profile", default=None)


# ---------------------------------------------------------------- 位置（按 config.REPO_ROOT 现算，测试换了仓库根目录时跟着换）

def root() -> Path:
    return config.REPO_ROOT / "local" / "profiles"


def selected_file() -> Path:
    return root() / "selected.json"


def legacy_kb_edits() -> Path:
    return config.REPO_ROOT / "local" / "kb_edits"


# ---------------------------------------------------------------- 当前用哪份

def active() -> str:
    return _override.get() or _active


def activate(pid: str) -> str:
    """这个进程（主控或唤起的会话）用哪份配置。"""
    global _active
    _active = check(pid)
    return _active


@contextmanager
def using(pid: str):
    """只在这个线程（控制台的一次请求）里按这份配置读写。"""
    token = _override.set(check(pid))
    try:
        yield pid
    finally:
        _override.reset(token)


# ---------------------------------------------------------------- 编号与路径

def check(pid) -> str:
    if pid in BUILTIN:
        return pid
    if not isinstance(pid, str) or not ID_PATTERN.fullmatch(pid):
        raise ValueError("配置编号无效")
    if not (root() / pid / "profile.json").is_file():
        raise ValueError(f"找不到配置 {pid}（local/profiles/{pid}/）：可能已被删除")
    return pid


def readonly(pid: str) -> bool:
    return pid == DEFAULT


def _dir(pid: str) -> Path:
    return root() / pid


def prompts_dir(pid: str | None = None) -> Path:
    """提示词修改的目录：有同名文件时用它代替仓库 orchestrator/prompts/ 里的文件。"""
    pid = pid or active()
    if pid == LEGACY:
        from . import prompting
        return prompting.OVERRIDES
    return _dir(pid) / "prompts"          # default 的目录从不创建：就是仓库原样


def personal_store(pid: str | None = None) -> Path:
    pid = pid or active()
    if pid == LEGACY:
        from . import personal_prompts
        return Path(personal_prompts.STORE)
    return _dir(pid) / "personal_prompts.json"


def kb_edits_dir(pid: str | None = None) -> Path:
    """知识库修改的目录（结构与仓库的 kb/ 相同）。"""
    pid = pid or active()
    if pid == LEGACY:
        return legacy_kb_edits()
    return _dir(pid) / "kb"


def _files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))


def kb_overlay_for(pid: str | None = None) -> str:
    """这份配置有知识库修改时返回修改目录（挂给智能体的 /kb 要合并它），没有时返回空字符串。"""
    edits = kb_edits_dir(pid)
    return str(edits) if _files(edits) else ""


# ---------------------------------------------------------------- profile.json

def _meta_path(pid: str) -> Path:
    return _dir(pid) / "profile.json"


def _empty_kb() -> dict:
    return {"kb_deny": {m: [] for m in MODES}, "kb_allow": {"explore": []}}


def meta(pid: str) -> dict:
    """名字、表单设置与（新建配置的）知识库权限。default 没有文件；local 只存名字与表单设置。"""
    pid = check(pid)
    data = {}
    if pid != DEFAULT:
        try:
            data = json.loads(_meta_path(pid).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    out = {"id": pid, "name": BUILTIN.get(pid) or str(data.get("name") or pid),
           "form": data.get("form") if isinstance(data.get("form"), dict) else None,
           "created": data.get("created"), "updated": data.get("updated")}
    kb = _empty_kb()
    deny = data.get("kb_deny") if isinstance(data.get("kb_deny"), dict) else {}
    allow = data.get("kb_allow") if isinstance(data.get("kb_allow"), dict) else {}
    kb["kb_deny"] = {m: [x for x in deny.get(m) or [] if isinstance(x, str)] for m in MODES}
    kb["kb_allow"] = {"explore": [x for x in allow.get("explore") or [] if isinstance(x, str)]}
    out.update(kb)
    return out


def _write_meta(pid: str, data: dict) -> None:
    path = _meta_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: data[k] for k in ("name", "form", "kb_deny", "kb_allow", "created", "updated") if k in data}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, **keep}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _raw(pid: str) -> dict:
    try:
        data = json.loads(_meta_path(pid).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _writable(pid: str) -> str:
    pid = check(pid)
    if readonly(pid):
        raise ValueError("默认配置就是仓库原样，不能修改：先在顶部的“配置”里新建一份再改")
    return pid


def _clean_name(name) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME or any(c in name for c in "\r\n\0"):
        raise ValueError(f"配置名须为 1～{MAX_NAME} 个字符")
    return name.strip()


def clean_form(form) -> dict | None:
    """表单设置：控制台运行页的全部选项（不含知识库权限：那部分单独存）。只做形状检查，开跑时再按运行设置逐项校验。"""
    if form is None:
        return None
    if not isinstance(form, dict):
        raise ValueError("表单设置须为对象")
    form = {k: v for k, v in form.items() if k not in ("kb_deny", "kb_allow", "profile")}
    if len(json.dumps(form, ensure_ascii=False).encode("utf-8")) > MAX_FORM_BYTES:
        raise ValueError("表单设置太大")
    return form


def save_form(pid: str, form) -> dict:
    pid = _writable(pid)
    data = _raw(pid)
    data["name"] = data.get("name") or BUILTIN.get(pid, pid)
    data["form"] = clean_form(form)
    data["updated"] = time.time()
    data.setdefault("created", data["updated"])
    _write_meta(pid, data)
    return meta(pid)


def set_kb_permissions(pid: str, kb_deny: dict | None = None, kb_allow: dict | None = None) -> dict:
    """新建配置的知识库权限（已经由调用方按知识库校验过）。本机原有设置的权限在 configs/local.console.json。"""
    pid = _writable(pid)
    if pid == LEGACY:
        raise ValueError("本机原有设置的知识库权限保存在 configs/local.console.json")
    data = _raw(pid)
    current = meta(pid)
    if kb_deny is not None:
        data["kb_deny"] = {**current["kb_deny"], **kb_deny}
    if kb_allow is not None:
        data["kb_allow"] = {**current["kb_allow"], **kb_allow}
    data["updated"] = time.time()
    _write_meta(pid, data)
    return meta(pid)


# ---------------------------------------------------------------- 列表、新建、改名、删除、选中

def ids() -> list[str]:
    found = []
    if root().is_dir():
        for path in sorted(root().iterdir()):
            if path.is_dir() and path.name not in BUILTIN and ID_PATTERN.fullmatch(path.name) and (path / "profile.json").is_file():
                found.append(path.name)
    return [DEFAULT, LEGACY] + found


def summary(pid: str) -> dict:
    """这份配置改了哪些：提示词文件几个、个人追加要求几个范围、知识库文件几个、有没有存表单设置。"""
    from .prompting import prompt_files
    known = set(prompt_files())
    prompts = sorted(p.name for p in prompts_dir(pid).glob("*.md") if p.name in known) if prompts_dir(pid).is_dir() else []
    personal = 0
    try:
        data = json.loads(personal_store(pid).read_text(encoding="utf-8-sig"))
        personal = sum(1 for v in (data.get("scopes") or {}).values() if isinstance(v, str) and v.strip())
    except (OSError, ValueError, AttributeError):
        personal = 0
    return {"prompts": prompts, "personal": personal, "kb": len(_files(kb_edits_dir(pid))),
            "form": meta(pid)["form"] is not None}


def listing() -> dict:
    rows = []
    for pid in ids():
        m = meta(pid)
        rows.append({"id": pid, "name": m["name"], "readonly": readonly(pid), "builtin": pid in BUILTIN,
                     "updated": m["updated"], **summary(pid)})
    stored = _stored_selection()
    current = selected()
    return {"profiles": rows, "selected": current, "stored": stored == current}


def _new_id() -> str:
    base = "p" + time.strftime("%Y%m%d-%H%M%S")
    pid, n = base, 1
    while (root() / pid).exists():
        n += 1
        pid = f"{base}-{n}"
    return pid


def _copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.tmp", "*.lock"), dirs_exist_ok=True)


def create(name, base: str = DEFAULT, form=None, kb: dict | None = None) -> str:
    """新建一份配置：base 是复制来源（default = 从仓库原样开始；其他 = 复制它的提示词修改、个人追加要求与知识库修改）。
    form 是表单设置（None = 默认设置），kb 是知识库权限（{"kb_deny": ..., "kb_allow": ...}，None = 全部默认）。"""
    name = _clean_name(name)
    base = check(base)
    form = clean_form(form)
    pid = _new_id()
    target = _dir(pid)
    target.mkdir(parents=True)
    try:
        if base != DEFAULT:
            _copy_tree(prompts_dir(base), target / "prompts")
            _copy_tree(kb_edits_dir(base), target / "kb")
            store = personal_store(base)
            if store.is_file():
                shutil.copy2(store, target / "personal_prompts.json")
        now = time.time()
        perms = _empty_kb()
        if isinstance(kb, dict):
            perms["kb_deny"].update({m: list(v) for m, v in (kb.get("kb_deny") or {}).items() if m in MODES})
            perms["kb_allow"].update({m: list(v) for m, v in (kb.get("kb_allow") or {}).items() if m == "explore"})
        _write_meta(pid, {"name": name, "form": form, **perms, "created": now, "updated": now})
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return pid


def rename(pid: str, name) -> dict:
    pid = check(pid)
    if pid in BUILTIN:
        raise ValueError("内置的配置不能改名")
    data = _raw(pid)
    data["name"] = _clean_name(name)
    data["updated"] = time.time()
    _write_meta(pid, data)
    return meta(pid)


def delete(pid: str) -> None:
    pid = check(pid)
    if pid in BUILTIN:
        raise ValueError("内置的配置不能删除")
    shutil.rmtree(_dir(pid))
    if _stored_selection() == pid:
        selected_file().unlink(missing_ok=True)


def _stored_selection() -> str | None:
    try:
        value = json.loads(selected_file().read_text(encoding="utf-8-sig")).get("profile")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def legacy_customized() -> bool:
    """本机原有设置里有没有东西（提示词修改、个人追加要求、知识库修改或权限、存过的表单设置）。"""
    s = summary(LEGACY)
    if s["prompts"] or s["personal"] or s["kb"] or s["form"]:
        return True
    try:
        prefs = json.loads((config.REPO_ROOT / "configs" / "local.console.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    deny = prefs.get("kb_deny") if isinstance(prefs, dict) and isinstance(prefs.get("kb_deny"), dict) else {}
    allow = prefs.get("kb_allow") if isinstance(prefs, dict) and isinstance(prefs.get("kb_allow"), dict) else {}
    return any(deny.get(m) for m in MODES) or bool(allow.get("explore"))


def selected() -> str:
    """控制台选中的配置：记在 local/profiles/selected.json；没选过（或选的被删了）时，本机原有设置里有东西就用它，否则用默认。"""
    value = _stored_selection()
    if value is not None:
        try:
            return check(value)
        except ValueError:
            pass
    return LEGACY if legacy_customized() else DEFAULT


def select(pid: str) -> str:
    pid = check(pid)
    path = selected_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"profile": pid}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return pid


def state_profile(state: dict | None) -> str:
    """运行记录里的配置：没记的旧运行是本机原有设置。"""
    value = (state or {}).get("profile") or LEGACY
    return value


def runs_using(pid: str, runs_dir: Path | None = None) -> int:
    """有几次运行记着用了这份配置（续跑时要用它）。"""
    base = runs_dir or config.REPO_ROOT / "runs"
    n = 0
    if not base.is_dir():
        return 0
    for state in base.glob("*/*/run_state.json"):
        try:
            if json.loads(state.read_text(encoding="utf-8-sig")).get("profile") == pid:
                n += 1
        except (OSError, ValueError, AttributeError):
            continue
    return n

