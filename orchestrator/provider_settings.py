"""控制台"模型提供方"表单的后端：Claude Code 执行器经 Anthropic 兼容接口接入的第三方提供方。

- 入库的 configs/providers.json 只读、不改。内置提供方（例如 deepseek）在表单里改的设置写进本机覆盖文件
  configs/local.providers.json（configs/local* 不入库）的同名项，只写与入库默认值不同的字段；"恢复默认"就是删掉
  本机覆盖里的这一项（可以选择保留密钥设置）。主控照旧用 config.load_provider 合并两份文件。
- 本机覆盖文件先读出原内容再合并：其他提供方、说明字段与表单不认识的字段原样保留；写入先写 .tmp 再替换。
  文件不是有效的 JSON 时拒绝写入，免得覆盖里面的内容。
- 密钥绝不回显：接口只返回是否已配置、来源（环境变量 / 文件 / 控制台保存）与末尾 4 位，不返回全文，
  也不写进错误信息。粘贴的密钥存到 configs/local.keys/<标识>.key，key_file 指向它，并尽量收紧文件权限。
- 本机网关：http://localhost:端口、http://127.0.0.1:端口 换成容器能连的 http://host.docker.internal:端口，
  并写 allowed_host_ports——容器创建时作为 PROVIDER_HOST_PORTS 传进去（container.py），防火墙
  （agent-container/init-firewall.sh）只放行宿主机上的这些端口；https 地址的主机名写进 allowed_domains，
  防火墙只放行这些域名的 443 端口，所以 https 地址不能带别的端口。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config as _config
from .efforts import valid_effort

RESERVED = ("anthropic", "codex")          # Anthropic 订阅与原生 Codex 执行器，不能用作第三方提供方的标识
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_ENV = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}", "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model_base}", "CLAUDE_CODE_SUBAGENT_MODEL": "{model_base}",
               "API_TIMEOUT_MS": "600000"}
UNTESTED = "未实测"                         # 与部署向导相同：status 不是 ready 时主控开跑前打印"注意：模型提供方 X 未实测"
# 表单管理的字段；其余字段（auth、说明、以后加的字段）读出来原样写回
FORM_FIELDS = ("label", "status", "base_url", "allowed_domains", "allowed_host_ports", "key_env", "key_file",
               "model_suffix", "images", "autocompact", "env", "gateway_check", "models")
MODEL_FIELDS = ("id", "label", "efforts", "default_effort")
REQUEST_FIELDS = {"id", "create", "version", "label", "base_url", "extra_domains", "key", "key_env", "models",
                  "model_suffix", "autocompact", "env", "images", "ready", "gateway_check"}

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
WINDOWS_DEVICES = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.I)   # 不能用作 Windows 文件名
MODEL_ID = re.compile(r"[A-Za-z0-9._:/-]{1,100}")          # 与 runtimes.Runtime.models 一致
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)")
URL_PATH = re.compile(r"(?:/[A-Za-z0-9._~%-]+)+")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}
CONTAINER_HOST = "host.docker.internal"
# 这些环境变量由主控自己设置（接口地址、密钥），或者是凭据：不能写进 env（会被当成明文存进 JSON，并在表单里回显）
# （ANTHROPIC_CUSTOM_HEADERS 这类请求头变量里常带认证头，也算）
SECRET_ENV = re.compile(r"(?:^|_)(?:API_KEY|KEY|TOKEN|OAUTH|SECRET|PASSWORD|PASSWD|ACCESS_KEY|CREDENTIALS?|HEADERS|COOKIE)(?:$|_)")
OWN_ENV = {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
URL_HINT = ("接口地址须为 https://（Anthropic 兼容接口的 base_url，例如 https://api.deepseek.com/anthropic）；"
            "本机网关可用 http://localhost:端口、http://127.0.0.1:端口 或 http://host.docker.internal:端口")

_lock = threading.Lock()


# ---------------------------------------------------------------- 文件位置与读写

def repo_path() -> Path:
    return _config.PROVIDERS


def local_path() -> Path:
    return _config.LOCAL_PROVIDERS


def keys_dir() -> Path:
    """控制台保存的密钥：与本机覆盖文件同目录的 local.keys/（configs/local* 不入库）。"""
    return local_path().parent / "local.keys"


def _display(path: Path) -> str:
    """给人看的路径：仓库里的写相对路径。"""
    try:
        return path.resolve().relative_to(_config.REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_repo() -> dict:
    try:
        data = json.loads(repo_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_local() -> tuple[bytes | None, dict]:
    """本机覆盖文件的原始内容与解析结果；没有文件时是 (None, {})。不是有效的 JSON 对象时抛 ValueError。"""
    path = local_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{_display(path)} 不是有效的 JSON：先手动修好或删掉它，免得覆盖里面的内容") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{_display(path)} 的最外层须为 JSON 对象：先手动修好或删掉它")
    return raw, data


def _version(raw: bytes | None) -> str:
    return "missing" if raw is None else hashlib.sha256(raw).hexdigest()[:16]


def _write_local(data: dict) -> None:
    """原子写：先写同目录的 .tmp，再替换。"""
    path = local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _provider_ids(repo: dict, local: dict) -> list[str]:
    names = {k for source in (repo, local) for k, v in source.items()
             if isinstance(v, dict) and not k.startswith("_") and k not in RESERVED}
    return sorted(names, key=lambda n: (n not in repo, n.lower()))


def _builtin(repo: dict, ident: str) -> bool:
    return isinstance(repo.get(ident), dict) and not ident.startswith("_") and ident not in RESERVED


# ---------------------------------------------------------------- 规范化（读出来的值可能是手改的，宽松处理）

def _str(value, default: str = "") -> str:
    return value if isinstance(value, str) else default if value is None else str(value)


def _str_list(value) -> list[str]:
    return [str(v) for v in value if isinstance(v, (str, int))] if isinstance(value, list) else []


def _ports(value) -> list[int]:
    out = []
    for item in value if isinstance(value, list) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _model_rows(value) -> list[dict]:
    rows = []
    for row in value if isinstance(value, list) else []:
        if isinstance(row, dict):
            rows.append({"id": _str(row.get("id")), "label": _str(row.get("label")),
                         "efforts": _str_list(row.get("efforts")), "default_effort": _str(row.get("default_effort"))})
    return rows


def _normalized(entry: dict | None, ident: str) -> dict:
    """表单字段的规范值（缺省按主控的默认含义：status 缺省是 ready，images 缺省是开）。"""
    e = entry if isinstance(entry, dict) else {}
    models = []
    for row in e.get("models") if isinstance(e.get("models"), list) else []:
        if isinstance(row, dict):
            models.append(row)
    return {"label": _str(e.get("label")) or ident, "status": _str(e.get("status")) or "ready",
            "base_url": _str(e.get("base_url")), "allowed_domains": _str_list(e.get("allowed_domains")),
            "allowed_host_ports": _ports(e.get("allowed_host_ports")), "key_env": _str(e.get("key_env")),
            "key_file": _str(e.get("key_file")), "model_suffix": _str(e.get("model_suffix")),
            "images": bool(e.get("images", True)), "autocompact": _str(e.get("autocompact")),
            "env": dict(e.get("env")) if isinstance(e.get("env"), dict) else {},
            "gateway_check": _str(e.get("gateway_check")), "models": models}


def _effective(repo: dict, local: dict, ident: str) -> dict:
    """与 config.load_provider 相同：入库的设置，被本机同名项按顶层字段覆盖。"""
    merged: dict = {}
    for source in (repo, local):
        if isinstance(source.get(ident), dict):
            merged.update(source[ident])
    return merged


# ---------------------------------------------------------------- 接口地址与放行域名

def parse_base_url(value) -> tuple[str, str | None, list[int]]:
    """校验接口地址，返回 (容器里用的地址, 自动放行的域名或 None, 要放行的宿主机端口)。"""
    if not isinstance(value, str):
        raise ValueError("接口地址须为文本")
    text = value.strip()
    if not text:
        raise ValueError("请填写接口地址（Anthropic 兼容接口的 base_url）")
    if len(text) > 300 or re.search(r"[\s\x00-\x1f\x7f]", text):
        raise ValueError("接口地址格式无效：不能有空格或换行，最多 300 个字符")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(URL_HINT)
    if "@" in parts.netloc:
        raise ValueError("接口地址里不能带用户名或密码：密钥请填在下面的密钥栏")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError("接口地址不能带 ? 或 # 及后面的部分")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("接口地址的端口无效（1～65535）") from exc
    path = parts.path.rstrip("/")
    if path and not URL_PATH.fullmatch(path):
        raise ValueError("接口地址的路径里有不支持的字符")
    if re.search(r"/(?:v1|messages|v1/messages)$", path, re.I):
        raise ValueError("接口地址只写到 /v1 之前：Claude Code 会自己在后面加 /v1/messages")
    if scheme == "http":
        if host not in LOCAL_HOSTS:
            raise ValueError(URL_HINT)
        if not port:
            raise ValueError("本机网关的地址要写端口，例如 http://localhost:4000")
        # 容器里的 localhost 是容器自己：换成宿主机在容器里的名字，防火墙按端口放行（PROVIDER_HOST_PORTS）
        return f"http://{CONTAINER_HOST}:{port}{path}", None, [port]
    if host in LOCAL_HOSTS or host.startswith("127."):
        raise ValueError("本机网关请用 http://（例如 http://localhost:4000）：容器经宿主机端口连接网关")
    if port not in (None, 443):
        raise ValueError("容器防火墙只放行接口域名的 443 端口：https 地址不能带其他端口")
    if not (DOMAIN.fullmatch(host) or IPV4.fullmatch(host)):
        raise ValueError("接口地址的主机名无效")
    return f"https://{host}{path}", host, []


def host_url(base_url: str) -> str:
    """主机上（连通测试）用的地址：容器里的 host.docker.internal 在主机上就是 127.0.0.1（手写的 localhost 也一样）。"""
    return re.sub(r"^http://(?:host\.docker\.internal|localhost)(?=[:/]|$)", "http://127.0.0.1", base_url.strip(),
                  flags=re.I)


def safe_url(value: str) -> str:
    """显示用：手写的地址里带了用户名或密码时遮住。"""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1****@", value, flags=re.I)


def _auto_domain(base_url: str) -> str | None:
    try:
        return parse_base_url(base_url)[1]
    except ValueError:
        return None


def _domains(value, auto: str | None) -> list[str]:
    if value is None:
        value = []
    if isinstance(value, str):
        value = [v for v in re.split(r"[\s,，;；]+", value) if v]
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("追加的放行域名最多 20 个")
    out = [auto] if auto else []
    for index, item in enumerate(value, 1):
        if not isinstance(item, str):
            raise ValueError("放行域名须为文本")
        domain = item.strip().lower()
        if domain in LOCAL_HOSTS or domain.startswith("127."):
            raise ValueError("本机地址不用写进放行域名：本机网关按接口地址的端口放行")
        if not (DOMAIN.fullmatch(domain) or IPV4.fullmatch(domain)):
            raise ValueError(f"追加的第 {index} 个放行域名无效：只写主机名，例如 api.example.com（不带 https:// 与路径）")
        if domain not in out:
            out.append(domain)
    return out


# ---------------------------------------------------------------- 其他字段的校验

def _plain(value, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"{label}格式无效（最多 {limit} 个字符，不能有换行）")
    return value.strip()


def _key_env(value) -> str:
    name = _plain(value, "环境变量名", 64)
    if not name:
        return ""
    if not ENV_NAME.fullmatch(name):
        raise ValueError("环境变量名只能用大写字母、数字与下划线，不能以数字开头")
    if name.startswith(("CLAUDE", "ANTHROPIC")):
        raise ValueError("环境变量名不能用 CLAUDE 或 ANTHROPIC 开头：避免把 Claude 的凭据发给第三方")
    return name


def default_key_env(ident: str) -> str:
    name = re.sub(r"[^A-Z0-9_]", "_", ident.upper()) + "_API_KEY"
    name = name if not name[0].isdigit() else "P_" + name
    return "" if name.startswith(("CLAUDE", "ANTHROPIC")) else name


def _models(value, previous: list) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("模型列表须有 1～50 个模型")
    old = {row.get("id"): row for row in previous if isinstance(row, dict)}
    out, seen = [], set()
    for index, row in enumerate(value, 1):
        if not isinstance(row, dict) or set(row) - set(MODEL_FIELDS):
            raise ValueError(f"第 {index} 个模型的格式无效")
        ident = row.get("id")
        if not isinstance(ident, str) or not MODEL_ID.fullmatch(ident.strip()):
            raise ValueError(f"第 {index} 个模型的标识无效：只能用字母、数字与 . _ : / -，最多 100 个字符")
        ident = ident.strip()
        if ident in seen:
            raise ValueError(f"模型 {ident} 重复")
        seen.add(ident)
        label = _plain(row.get("label"), f"模型 {ident} 的显示名", 60)
        efforts = row.get("efforts")
        if not isinstance(efforts, list) or not 1 <= len(efforts) <= 12:
            raise ValueError(f"模型 {ident} 须有 1～12 个推理强度")
        clean = []
        for effort in efforts:
            if not valid_effort(effort) or effort == "auto":
                raise ValueError(f"模型 {ident} 的第 {len(clean) + 1} 个推理强度无效：只能用字母、数字与 . _ -，"
                                 "以字母或数字开头（不能用 auto）")
            if effort in clean:
                raise ValueError(f"模型 {ident} 的推理强度 {effort} 重复")
            clean.append(effort)
        default = _plain(row.get("default_effort"), "默认推理强度", 64)
        if default and default not in clean:
            raise ValueError(f"模型 {ident} 的默认推理强度不在它的可选推理强度里")
        entry = {"id": ident, **({"label": label} if label else {}), "efforts": clean,
                 **({"default_effort": default} if default else {})}
        # 这个模型原来有表单不认识的字段（例如 description）：原样保留
        entry.update({k: v for k, v in (old.get(ident) or {}).items() if k not in MODEL_FIELDS})
        out.append(entry)
    return out


def _model_suffix(value) -> str:
    text = _plain(value, "模型名后缀", 12)
    if text and not re.fullmatch(r"\[[0-9A-Za-z]{1,8}\]", text):
        raise ValueError("模型名后缀须为空或形如 [1m]（Claude Code 按 100 万 token 上下文压缩）")
    return text


def _autocompact(value) -> str:
    text = _plain(_str(value) if isinstance(value, int) and not isinstance(value, bool) else value,
                  "自动压缩窗口", 10)
    if text and text != "auto" and not (text.isdigit() and 100000 <= int(text) <= 1000000):
        raise ValueError("自动压缩窗口须为空、auto 或 100000～1000000")
    return text


def _secret_env(name: str) -> bool:
    return name in OWN_ENV or bool(SECRET_ENV.search(name))


def _env(value, hidden: dict) -> dict:
    """模型槽位等传给容器里 Claude Code 的环境变量；hidden 是原来就有、表单里不显示的凭据类变量（原样保留）。"""
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError("env 须为 JSON 对象（最多 30 项），键为环境变量名、值为文本")
    out = {}
    for name, item in value.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ValueError("env 里有无效的环境变量名：只能用大写字母、数字与下划线，不能以数字开头")
        if _secret_env(name):
            raise ValueError(f"env 里不能写 {name}：接口地址与密钥由主控设置，密钥请用上面的密钥栏")
        if isinstance(item, int) and not isinstance(item, bool):
            item = str(item)
        if not isinstance(item, str) or len(item) > 300 or re.search(r"[\x00-\x1f\x7f]", item):
            raise ValueError(f"env 的 {name} 须为文本（最多 300 个字符，不能有换行）")
        out[name] = item
    for name, item in hidden.items():
        out.setdefault(name, item)
    return out


def _gateway_check(value) -> str:
    text = _plain(value, "网关检查地址", 300)
    if not text:
        return ""
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "http" or (parts.hostname or "").lower() not in ("127.0.0.1", "localhost") or not port \
            or "@" in parts.netloc:
        raise ValueError("网关检查地址须为本机地址，例如 http://127.0.0.1:4000/health（主控开跑前在主机上访问）")
    return text


# ---------------------------------------------------------------- 密钥

def _console_saved(key_file: str) -> bool:
    if not key_file:
        return False
    try:
        path = _config.key_file_path(key_file).resolve()
        return os.path.normcase(str(path.parent)) == os.path.normcase(str(keys_dir().resolve()))
    except OSError:
        return False


def _short(text: str, limit: int = 64) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def key_state(info: dict, ident: str) -> dict:
    """密钥是否已配置、来源与末尾 4 位（不足 12 个字符的不显示末尾）。不返回密钥本身。"""
    env_name = _str(info.get("key_env"))
    key_file = _str(info.get("key_file"))
    if env_name and os.environ.get(env_name, "").strip():
        source, text = "env", f"环境变量 {env_name}"
    elif key_file:
        saved = _console_saved(key_file)
        source = "console" if saved else "file"
        text = (f"控制台保存（{_display(_config.key_file_path(key_file))}）" if saved
                else f"文件 {_short(key_file)}")
    else:
        return {"configured": False, "source": None, "text": "未配置" + (f"（也可以设环境变量 {env_name}）" if env_name else ""),
                "tail": ""}
    try:
        key = _config.load_api_key({**info, "name": ident})
    except SystemExit as exc:
        return {"configured": False, "source": source, "text": f"{text}：{exc}", "tail": ""}
    except (OSError, ValueError):
        return {"configured": False, "source": source, "text": f"{text}：读不出密钥文件", "tail": ""}
    tail = key[-4:] if len(key) >= 12 else ""
    del key
    return {"configured": True, "source": source, "text": text, "tail": tail}


def _check_key_value(value) -> str:
    # 错误信息里不带密钥的任何部分
    if not isinstance(value, str):
        raise ValueError("粘贴的密钥须为文本")
    value = value.strip()
    if not re.fullmatch(r"[\x21-\x7e]{8,4096}", value) or '"' in value or "'" in value:
        raise ValueError("粘贴的密钥格式不对：应为 8～4096 个可见 ASCII 字符，不能有空格、换行或引号")
    return value


def _restrict(path: Path) -> None:
    """尽量只让当前用户能读写密钥文件：POSIX 用 0600，Windows 用 icacls 去掉继承的权限、只授予当前用户。"""
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{domain + chr(92) if domain else ''}{user}:F"],
                       capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass     # 收紧不了就算了：文件在不入库的目录里


def _write_key(ident: str, value: str, key_env: str) -> Path:
    folder = keys_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
    target = folder / f"{ident}.key"
    tmp = folder / f".{ident}.key.tmp"
    # 密钥里有 = 时 load_api_key 会按 KEY=VALUE 解析：那就照这种写法写，值原样保留
    text = f"{key_env or 'API_KEY'}={value}\n" if "=" in value else value + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
        handle.write(text)
    _restrict(tmp)
    os.replace(tmp, target)
    return target


def _key_file_text(path: Path) -> str:
    """写进 key_file 的路径：在仓库里时写相对路径（configs/local.keys/<标识>.key，按仓库根目录解析），否则写绝对路径。"""
    try:
        return path.resolve().relative_to(_config.REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _tracked_risk(path: Path) -> bool:
    """文件在仓库里、又没被 .gitignore 排除（可能被提交）。没有 git 时不拦。"""
    root = _config.REPO_ROOT
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    try:
        result = subprocess.run(["git", "check-ignore", "-q", rel.as_posix()], cwd=root, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 1


def _key_file_choice(text, ident: str, key_env: str) -> str:
    path_text = _plain(text, "密钥文件路径", 400).strip('"').strip("'")
    if not path_text:
        return ""                          # 不用文件：只用环境变量
    path = _config.key_file_path(path_text)
    if not path.is_file():
        raise ValueError(f"找不到密钥文件 {_short(path_text)}")
    if path.stat().st_size > 64 * 1024:
        raise ValueError("密钥文件太大：应只有密钥一行，或 KEY=VALUE 形式")
    if _tracked_risk(path):
        raise ValueError("这个密钥文件在仓库目录里、又没有被 .gitignore 排除，可能被提交：移到仓库外，或改名为 .key 结尾")
    try:
        key = _config.load_api_key({"name": ident, "key_env": "", "key_file": path_text})
    except SystemExit as exc:
        raise ValueError("这个文件里没有读到密钥：应只有密钥一行，或 KEY=VALUE 形式") from exc
    except (OSError, ValueError) as exc:
        raise ValueError("读不出这个密钥文件（应为 UTF-8 文本）") from exc
    del key
    return path_text


# ---------------------------------------------------------------- 列表

def _locked(ident: str, in_use) -> bool:
    return in_use is None or ident in in_use


def _describe(repo: dict, local: dict, ident: str, in_use) -> dict:
    info = _effective(repo, local, ident)
    norm = _normalized(info, ident)
    auto = _auto_domain(norm["base_url"])
    env = {k: _str(v) for k, v in norm["env"].items() if isinstance(k, str) and not _secret_env(k)}
    entry = local.get(ident) if isinstance(local.get(ident), dict) else {}
    return {
        "id": ident, "label": norm["label"], "builtin": _builtin(repo, ident), "overridden": bool(entry),
        "local_fields": list(entry), "unknown_fields": [k for k in entry if k not in FORM_FIELDS and k != "auth"],
        "base_url": safe_url(norm["base_url"]), "auto_domain": auto,
        "extra_domains": [d for d in norm["allowed_domains"] if d != auto], "allowed_host_ports": norm["allowed_host_ports"],
        "key_env": norm["key_env"], "key_file": norm["key_file"], "key_saved_by_console": _console_saved(norm["key_file"]),
        "key": key_state(info, ident),
        "models": _model_rows(norm["models"]), "model_suffix": norm["model_suffix"], "autocompact": norm["autocompact"],
        "env": env, "env_hidden": sorted(k for k in norm["env"] if isinstance(k, str) and _secret_env(k)),
        "images": norm["images"], "status": norm["status"], "ready": norm["status"] == "ready",
        "gateway_check": safe_url(norm["gateway_check"]), "in_use": in_use is not None and ident in in_use,
    }


def listing(running: bool = False, in_use=frozenset()) -> dict:
    """in_use：正在运行的主控用着的提供方；None 表示有主控在运行但读不到它用哪个（全部不能改）。"""
    from .runtimes import configured_runtimes
    repo = _read_repo()
    error = None
    try:
        raw, local = _read_local()
    except ValueError as exc:
        raw, local, error = b"", {}, str(exc)
    try:
        runtimes = configured_runtimes()
    except Exception as exc:  # noqa: BLE001（下拉框读不出来时表单照样能用）
        runtimes, error = None, error or f"读不出执行器列表：{exc}"
    return {"providers": [_describe(repo, local, ident, in_use) for ident in _provider_ids(repo, local)],
            "version": _version(raw) if error is None else "invalid", "local_error": error,
            "running": bool(running), "locked_all": in_use is None,
            "local_file": _display(local_path()), "keys_dir": _display(keys_dir()),
            "defaults": {"efforts": DEFAULT_EFFORTS, "env": DEFAULT_ENV, "status": UNTESTED},
            "reserved": list(RESERVED), "runtimes": runtimes}


# ---------------------------------------------------------------- 保存

def _check_new_id(value, repo: dict, local: dict) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value.strip()):
        raise ValueError("标识只能用字母、数字、点、下划线与连字符，以字母或数字开头，最多 40 个字符")
    ident = value.strip()
    if WINDOWS_DEVICES.fullmatch(ident):
        raise ValueError(f"标识不能用 {ident}：这是 Windows 的设备名，密钥文件 <标识>.key 存不了")
    if ident.lower() in RESERVED:
        raise ValueError(f"标识不能用 {ident}：anthropic 与 codex 是内置执行器的名字")
    existing = {k.lower(): k for source in (repo, local) for k in source if isinstance(k, str)}
    if ident.lower() in existing:
        raise ValueError(f"已有名为 {existing[ident.lower()]} 的提供方（标识不区分大小写）")
    return ident


def save(request, in_use=frozenset()) -> str:
    """保存表单（新建或修改），返回提供方的标识。in_use 见 listing。"""
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise ValueError("提供方设置的格式无效")
    with _lock:
        repo = _read_repo()
        raw, local = _read_local()
        if request.get("version") != _version(raw):
            raise ValueError(f"{_display(local_path())} 在你打开之后被改过，请重新读取再修改")
        create = request.get("create") is True
        if create:
            ident = _check_new_id(request.get("id"), repo, local)
            if in_use is None:
                raise ValueError("有主控在运行，读不到它用的提供方：等运行结束再修改提供方")
        else:
            ident = request.get("id")
            if not isinstance(ident, str) or ident not in _provider_ids(repo, local):
                raise ValueError("没有这个提供方，请重新读取")
            if _locked(ident, in_use):
                raise ValueError(f"有主控正在用 {ident} 运行（或读不到它用的提供方）：等运行结束再修改它的设置")
        builtin = _builtin(repo, ident)
        entry = local.get(ident)
        if entry is not None and not isinstance(entry, dict):
            raise ValueError(f"{_display(local_path())} 里 {ident} 这一项不是 JSON 对象：先手动修好")
        current = _normalized(_effective(repo, local, ident), ident) if not create else _normalized({}, ident)

        new: dict = {}
        label = _plain(request.get("label"), "显示名", 40)
        new["label"] = label or (_normalized(repo.get(ident), ident)["label"] if builtin else ident)
        new["base_url"], auto, new["allowed_host_ports"] = parse_base_url(request.get("base_url"))
        new["allowed_domains"] = _domains(request.get("extra_domains"), auto)
        new["key_env"] = _key_env(request["key_env"]) if "key_env" in request else (
            default_key_env(ident) if create else current["key_env"])
        new["models"] = _models(request.get("models"), current["models"])
        new["model_suffix"] = _model_suffix(request.get("model_suffix"))
        new["autocompact"] = _autocompact(request.get("autocompact"))
        hidden = {k: v for k, v in current["env"].items() if isinstance(k, str) and _secret_env(k)}
        shown = {k: v for k, v in current["env"].items() if k not in hidden}
        new["env"] = _env(request.get("env", DEFAULT_ENV if create else shown), hidden)
        if type(request.get("images", True)) is not bool or type(request.get("ready", False)) is not bool:
            raise ValueError("是否支持图片、是否已实测须为开或关")
        new["images"] = request.get("images", True)
        previous_status = current["status"] if current["status"] != "ready" else UNTESTED
        new["status"] = "ready" if request.get("ready") else (previous_status if not create else UNTESTED)
        new["gateway_check"] = _gateway_check(request.get("gateway_check"))
        key = request.get("key") or {"mode": "keep"}
        if not isinstance(key, dict) or set(key) - {"mode", "file", "value"} or key.get("mode") not in ("keep", "file", "paste"):
            raise ValueError("密钥的设置方式无效")
        pasted = _check_key_value(key.get("value")) if key["mode"] == "paste" else None
        if key["mode"] == "file":
            new["key_file"] = _key_file_choice(key.get("file"), ident, new["key_env"])
        elif key["mode"] == "keep":
            new["key_file"] = current["key_file"]

        # 先校验完所有字段，再写密钥文件与本机覆盖
        if pasted is not None:
            new["key_file"] = _key_file_text(_write_key(ident, pasted, new["key_env"]))
            del pasted
        # 在原来那一项上合并：没改的字段一个不动（包括手写的、与默认值相同的字段），改了的才写
        updated = dict(entry or {})
        base = _normalized(repo[ident], ident) if builtin else None
        if create:
            updated = {"label": new["label"], "status": new["status"], "auth": "api_key"}
        for field in FORM_FIELDS:
            if not create and new[field] == current[field]:
                continue
            if builtin and new[field] == base[field]:
                updated.pop(field, None)         # 改回入库默认值：去掉本机覆盖，主控读到的就是默认值
            elif not builtin and field in ("allowed_host_ports", "autocompact", "gateway_check") and not new[field]:
                updated.pop(field, None)         # 自建项的可选字段空着就不写
            else:
                updated[field] = new[field]
        if not builtin:
            updated.setdefault("auth", "api_key")
        if updated:
            local[ident] = updated
        else:
            local.pop(ident, None)
        _write_local(local)
    return ident


# ---------------------------------------------------------------- 删除与恢复默认

def remove(request, in_use=frozenset()) -> str:
    """自建提供方：删掉本机覆盖里的这一项。内置提供方：恢复默认（keep_key 时保留 key_file 与 key_env）。
    delete_key_file：同时删除控制台保存的密钥文件（只限 local.keys/ 里、没有别的提供方用着的）。"""
    if not isinstance(request, dict) or set(request) - {"id", "version", "keep_key", "delete_key_file"}:
        raise ValueError("请求格式无效")
    if any(type(request.get(k, False)) is not bool for k in ("keep_key", "delete_key_file")):
        raise ValueError("请求格式无效")
    with _lock:
        repo = _read_repo()
        raw, local = _read_local()
        if request.get("version") != _version(raw):
            raise ValueError(f"{_display(local_path())} 在你打开之后被改过，请重新读取再操作")
        ident = request.get("id")
        if not isinstance(ident, str) or ident not in _provider_ids(repo, local):
            raise ValueError("没有这个提供方，请重新读取")
        if _locked(ident, in_use):
            raise ValueError(f"有主控正在用 {ident} 运行（或读不到它用的提供方）：等运行结束再修改它的设置")
        builtin = _builtin(repo, ident)
        entry = local.get(ident)
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"{ident} 没有本机修改，已经是默认设置" if builtin else "没有这个提供方，请重新读取")
        keep = builtin and request.get("keep_key", False)
        key_file = _str(_effective(repo, local, ident).get("key_file"))
        if keep:
            kept = {k: entry[k] for k in ("key_env", "key_file") if k in entry}
            if kept:
                local[ident] = kept
            else:
                local.pop(ident)
        else:
            local.pop(ident)
        target = None
        if request.get("delete_key_file"):
            if keep or not _console_saved(key_file):
                raise ValueError("只有不再使用的、控制台保存的密钥文件（configs/local.keys/ 里）可以在这里删除")
            target = _config.key_file_path(key_file).resolve()
            others = [k for k in _provider_ids(repo, local)
                      if _console_saved(_str(_effective(repo, local, k).get("key_file")))
                      and _config.key_file_path(_str(_effective(repo, local, k).get("key_file"))).resolve() == target]
            if others:
                raise ValueError(f"这个密钥文件还有 {'、'.join(others)} 在用，没有删除")
        _write_local(local)
        if target is not None:
            target.unlink(missing_ok=True)
    return ident


# ---------------------------------------------------------------- 连通测试

def _scrub(text: str, key: str) -> str:
    """去掉回复里可能带的密钥：完整的密钥、密钥里任意 8 个以上连续字符的片段、Bearer 后面的内容与长串令牌。
    （有的平台在报错里回显密钥的前几位，例如 "api key sk-12****0000 is invalid"）"""
    if key:
        text = text.replace(key, "****")
        for size in range(min(len(key) - 1, 64), 7, -1):
            for start in range(0, len(key) - size + 1):
                piece = key[start:start + size]
                if piece in text:
                    text = text.replace(piece, "****")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer ****", text)
    return re.sub(r"[A-Za-z0-9_\-]{24,}", "…", text)


def _reason(status: int, body: bytes, key: str) -> str:
    common = {400: "请求被拒绝", 401: "认证失败（检查密钥）", 403: "没有权限", 404: "地址不对，或接口里没有这个模型",
              408: "接口超时", 429: "请求太频繁或余额不足", 529: "服务过载"}
    text = common.get(status) or ("服务端出错" if status >= 500 else "请求失败")
    detail = ""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            error = data.get("error")
            detail = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else data.get("message")
    except ValueError:
        detail = ""
    detail = _scrub(_str(detail), key).strip()[:160]
    return f"{text}：{detail}" if detail else text


def connectivity(request, timeout: float = 20.0) -> dict:
    """在主机上用已保存的接口地址与密钥发一个最小的 Anthropic Messages 请求（max_tokens 很小）。
    只报告成功 / 失败、HTTP 状态与简短原因，不回显密钥。和容器里的 Claude Code 一样用 Authorization: Bearer 认证。"""
    import http.client
    import urllib.error
    import urllib.request
    if not isinstance(request, dict) or set(request) - {"id", "model"}:
        raise ValueError("请求格式无效")
    repo = _read_repo()
    _, local = _read_local()
    ident = request.get("id")
    if not isinstance(ident, str) or ident not in _provider_ids(repo, local):
        raise ValueError("没有这个提供方，请先保存")
    info = _effective(repo, local, ident)
    norm = _normalized(info, ident)
    if not norm["base_url"]:
        raise ValueError("先填写并保存接口地址")
    models = [row.get("id") for row in norm["models"] if isinstance(row, dict)]
    model = request.get("model") or (models[0] if models else None)
    if not isinstance(model, str) or model not in models:
        raise ValueError("先在模型列表里添加并保存要测试的模型")
    url = host_url(norm["base_url"]).rstrip("/") + "/v1/messages"
    if not url.startswith(("https://", "http://127.0.0.1:")):
        raise ValueError("连通测试只支持 https 接口或本机网关（http://localhost:端口）：在表单里重新填写接口地址并保存")
    try:
        key = _config.load_api_key({**info, "name": ident})
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ValueError("读不出密钥文件") from exc
    body = json.dumps({"model": model, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "ping"}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01",
        "authorization": f"Bearer {key}", "user-agent": "celeste-ai-play-console"})
    # 本机网关不走代理；外网接口按系统代理设置
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if url.startswith("http://127.0.0.1:") \
        else urllib.request.build_opener()
    started = time.monotonic()
    result = {"ok": False, "status": None, "model": model, "url": safe_url(url)}
    try:
        with opener.open(req, timeout=timeout) as response:
            result["status"] = response.status
            data = response.read(256 * 1024)
        try:
            message = json.loads(data.decode("utf-8"))
        except ValueError:
            message = None
        if isinstance(message, dict) and (message.get("type") == "message" or "content" in message):
            result.update(ok=True, reason=f"{model} 回复正常")
        else:
            result["reason"] = f"返回的不是 Anthropic Messages 格式（HTTP {result['status']}）：检查接口地址"
    except urllib.error.HTTPError as exc:
        with exc:
            result.update(status=exc.code, reason=_reason(exc.code, exc.read(8192), key))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        result["reason"] = _scrub("连不上接口：" + _str(reason)[:160], key)
    finally:
        del key
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result
