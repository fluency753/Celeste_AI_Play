"""智能体容器的生命周期：每个智能体标签一个常驻容器，按需创建、启动与校验。

容器里：/workspace 挂载宿主机的 workspaces/<标签>/（可写），/kb 挂载仓库的 kb/（只读），
/home/agent/.claude 是独立的数据卷（会话记录，便于中断后续跑）。游戏目录、存档、OBS 都不挂载。
开放预置模拟器（--sim）时，本机的 sim/dist 只读挂到 /sim。
配置变了要重建容器时，旧容器 /tmp 里的东西（智能体常把程序和中间结果放在 Claude Code 的 /tmp/claude-<uid>）
先打包到本机 local/container_tmp/，新容器建好后原样放回（保留属主与权限）。
"""
from __future__ import annotations

import json
import re
import subprocess
import tarfile
import time
from pathlib import Path

from . import config as _config
from .config import REPO_ROOT, RunConfig, find_docker_exe

LABEL = "celeste-ai-play"


def slug(agent: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", agent)


def container_name(agent: str) -> str:
    return f"celeste-agent-{slug(agent)}"


def docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    result = subprocess.run([find_docker_exe(), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} 失败：{(result.stderr or result.stdout).strip()}")
    return result


def _labels(cfg: RunConfig) -> dict[str, str]:
    return {f"{LABEL}.image": cfg.image, f"{LABEL}.mcp_port": str(cfg.mcp_port),
            f"{LABEL}.seccomp": "unconfined" if cfg.seccomp_unconfined else "default", f"{LABEL}.init": "1",
            f"{LABEL}.sim": "1" if cfg.sim else "0", f"{LABEL}.provider": cfg.provider,
            f"{LABEL}.knowledge": cfg.knowledge_profile,
            f"{LABEL}.workspace_tag": cfg.workspace_dir.name,
            f"{LABEL}.domains": ",".join(cfg.provider_domains),
            f"{LABEL}.host_ports": ",".join(str(p) for p in cfg.provider_host_ports),
            # /kb 挂的是知识库本身（0）还是同步出来的副本（1：有不给看的文件或本机追加资料，见 kb_access.py）；
            # 之前建的容器没有这个标签，按 0 算
            f"{LABEL}.kb_view": "1" if cfg.kb_view else "0"}


LABEL_DEFAULTS = {f"{LABEL}.kb_view": "0"}


def _create(cfg: RunConfig, name: str) -> None:
    workspace = cfg.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "kb").mkdir(exist_ok=True)
    cfg.kb_dir.mkdir(parents=True, exist_ok=True)
    # --init：用 tini 作 1 号进程，回收沙箱留下的子进程，避免僵尸进程堆积
    args = ["run", "-d", "--init", "--name", name, "--hostname", "celeste-agent",
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
            "-e", f"MCP_PORT={cfg.mcp_port}",
            # 防火墙放行哪些模型服务：Anthropic 订阅只放行 Anthropic；第三方提供方只放行它自己的域名，不放行 Anthropic
            "-e", f"ALLOW_ANTHROPIC={'0' if cfg.third_party else '1'}",
            "-e", f"PROVIDER_DOMAINS={' '.join(cfg.provider_domains)}",
            # 第三方提供方经宿主机上的网关接入时（例如 GPT），另外放行宿主机上网关的端口（需要 2026-10-24 之后构建的镜像）
            "-e", f"PROVIDER_HOST_PORTS={' '.join(str(p) for p in cfg.provider_host_ports)}",
            "-v", f"{workspace}:/workspace",
            "-v", f"{cfg.kb_dir}:/kb:ro",
            "-v", f"celeste-claude-{slug(cfg.workspace_dir.name)}:/home/agent/.claude",
            "--label", f"{LABEL}=agent"]
    if cfg.sim:
        args += ["-v", f"{cfg.sim_dir}:/sim:ro"]
    if cfg.seccomp_unconfined:
        args += ["--security-opt", "seccomp=unconfined"]
    for key, value in _labels(cfg).items():
        args += ["--label", f"{key}={value}"]
    docker(*args, cfg.image)


def _save_tmp(name: str) -> Path:
    """重建前把旧容器的 /tmp 打包到本机（docker cp 的 tar 保留属主与权限，套接字会被跳过）。失败就抛错、不重建。"""
    folder = _config.REPO_ROOT / "local" / "container_tmp"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.tar"
    with open(archive, "wb") as out:
        result = subprocess.run([find_docker_exe(), "cp", f"{name}:/tmp", "-"], stdout=out, stderr=subprocess.PIPE,
                                timeout=900)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"要重建容器 {name}，但没能先保存它的 /tmp（{detail}）；容器没有动")
    return archive


def _regular_files(archive: Path) -> int:
    with tarfile.open(archive) as tar:
        return sum(1 for member in tar if member.isfile())


def _restore_tmp(name: str, archive: Path, log=print) -> None:
    """把旧容器的 /tmp 放回新容器（-a：按打包里的属主写入，智能体用户照样能改）。核对普通文件数后删掉本机打包。"""
    hint = f"旧 /tmp 的打包保留在 {archive}，可以手动放回：docker cp -a - {name}:/ < 打包文件"
    with open(archive, "rb") as source:
        result = subprocess.run([find_docker_exe(), "cp", "-a", "-", f"{name}:/"], stdin=source,
                                stderr=subprocess.PIPE, timeout=900)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"容器 {name} 已重建，但没能放回旧的 /tmp（{detail}）。{hint}")
    expected = _regular_files(archive)
    found = docker("exec", name, "sh", "-c", "find /tmp -type f | wc -l", check=False)
    try:
        count = int(found.stdout.strip())
    except ValueError:
        count = -1
    if count < expected:
        raise RuntimeError(f"容器 {name} 已重建，但新 /tmp 里只有 {count} 个文件（打包里有 {expected} 个）。{hint}")
    archive.unlink()
    log(f"[container] 旧容器 /tmp 的 {expected} 个文件已放回新容器")


def apply_cpu_limit(cfg: RunConfig, name: str, log=print) -> None:
    """探索模式：按可调参数 agent_cpus 限制容器能用的 CPU 核（cpuset，容器里 nproc 显示的就是这个数）；为空时用 Docker 的全部核。
    每次运行都重新设置（docker update 对运行中的容器生效），复用的容器也跟着这次的设置。"""
    info = docker("info", "--format", "{{.NCPU}}", check=False)
    try:
        total = max(1, int(info.stdout.strip()))
    except ValueError:
        log("[container] 读不到 Docker 的 CPU 核数，不限制容器的核数")
        return
    want = total if cfg.agent_cpus is None else min(int(cfg.agent_cpus), total)
    if cfg.agent_cpus is not None and cfg.agent_cpus > total:
        log(f"[container] Docker 只能用 {total} 个核，按 {total} 个限制（设置的是 {cfg.agent_cpus}）")
    result = docker("update", "--cpuset-cpus", "0" if want == 1 else f"0-{want - 1}", name, check=False)
    if result.returncode != 0:
        log(f"[container] 没能限制容器的 CPU 核数：{(result.stderr or result.stdout).strip()[:200]}")
        cfg.agent_cpus_effective = total
    else:
        cfg.agent_cpus_effective = want          # 提示词里写的最多可用核数
        log(f"[container] 智能体容器最多用 {want} 个 CPU 核")


def ensure_container(cfg: RunConfig, log=print) -> str:
    """返回已就绪（防火墙生效）的容器名。配置变了（镜像、端口）就重建容器；工作区与会话数据卷保留。"""
    image = docker("image", "inspect", "-f", "{{.Id}}", cfg.image, check=False)
    if image.returncode != 0:
        raise RuntimeError(f"找不到镜像 {cfg.image}，请先在仓库根目录执行：docker build -t {cfg.image} agent-container")
    image_id = image.stdout.strip()
    name = container_name(cfg.agent + ("-explore" if cfg.knowledge_profile == "explore" else ""))
    inspect = docker("inspect", "-f", "{{.State.Running}}|{{.Image}}|{{json .Config.Labels}}", name, check=False)
    if inspect.returncode == 0:
        running, container_image, labels = inspect.stdout.strip().split("|", 2)
        current = json.loads(labels or "{}") or {}
        stale = container_image != image_id or any(current.get(k, LABEL_DEFAULTS.get(k)) != v
                                                   for k, v in _labels(cfg).items())
        if stale:
            log(f"[container] {name} 的镜像或配置已变化，重建（工作区、会话与 /tmp 保留）")
            archive = _save_tmp(name)
            docker("rm", "-f", name)
            try:
                _create(cfg, name)
            except Exception as exc:
                raise RuntimeError(f"重建容器 {name} 失败：{exc}；旧 /tmp 的打包保留在 {archive}") from exc
            _restore_tmp(name, archive, log)
        elif running != "true":
            log(f"[container] 启动 {name}")
            docker("start", name)
    else:
        log(f"[container] 创建 {name}（工作区 {cfg.workspace_dir}）")
        _create(cfg, name)

    if cfg.knowledge_profile == "explore" or getattr(cfg, "mode", None) == "explore":
        apply_cpu_limit(cfg, name, log)       # 只在探索模式（提示词里写明最多可以用几个核）
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if docker("exec", name, "test", "-f", "/run/firewall-ready", check=False).returncode == 0:
            return name
        if docker("exec", name, "test", "-f", "/run/firewall-failed", check=False).returncode == 0:
            logs = docker("logs", "--tail", "30", name, check=False)
            raise RuntimeError(f"容器防火墙设置失败：\n{logs.stdout}{logs.stderr}")
        time.sleep(0.5)
    raise RuntimeError(f"容器 {name} 在 60 秒内没有就绪")
