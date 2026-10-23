"""智能体容器的生命周期：每个智能体标签一个常驻容器，按需创建、启动与校验。

容器里：/workspace 挂载宿主机的 workspaces/<标签>/（可写），/kb 挂载仓库的 kb/（只读），
/home/agent/.claude 是独立的数据卷（会话记录，便于中断后续跑）。游戏目录、存档、OBS 都不挂载。
开放预置模拟器（--sim）时，本机的 sim/dist 只读挂到 /sim。
"""
from __future__ import annotations

import re
import subprocess
import time

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
            f"{LABEL}.domains": ",".join(cfg.provider_domains)}


def _create(cfg: RunConfig, name: str) -> None:
    workspace = cfg.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "kb").mkdir(exist_ok=True)
    # --init：用 tini 作 1 号进程，回收沙箱留下的子进程，避免僵尸进程堆积
    args = ["run", "-d", "--init", "--name", name, "--hostname", "celeste-agent",
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
            "-e", f"MCP_PORT={cfg.mcp_port}",
            # 防火墙放行哪些模型服务：Anthropic 订阅只放行 Anthropic；第三方提供方只放行它自己的域名，不放行 Anthropic
            "-e", f"ALLOW_ANTHROPIC={'0' if cfg.third_party else '1'}",
            "-e", f"PROVIDER_DOMAINS={' '.join(cfg.provider_domains)}",
            "-v", f"{workspace}:/workspace",
            "-v", f"{REPO_ROOT / 'kb'}:/kb:ro",
            "-v", f"celeste-claude-{slug(cfg.agent)}:/home/agent/.claude",
            "--label", f"{LABEL}=agent"]
    if cfg.sim:
        args += ["-v", f"{cfg.sim_dir}:/sim:ro"]
    if cfg.seccomp_unconfined:
        args += ["--security-opt", "seccomp=unconfined"]
    for key, value in _labels(cfg).items():
        args += ["--label", f"{key}={value}"]
    docker(*args, cfg.image)


def ensure_container(cfg: RunConfig, log=print) -> str:
    """返回已就绪（防火墙生效）的容器名。配置变了（镜像、端口）就重建容器；工作区与会话数据卷保留。"""
    image = docker("image", "inspect", "-f", "{{.Id}}", cfg.image, check=False)
    if image.returncode != 0:
        raise RuntimeError(f"找不到镜像 {cfg.image}，请先在仓库根目录执行：docker build -t {cfg.image} agent-container")
    image_id = image.stdout.strip()
    name = container_name(cfg.agent)
    inspect = docker("inspect", "-f", "{{.State.Running}}|{{.Image}}|{{json .Config.Labels}}", name, check=False)
    if inspect.returncode == 0:
        running, container_image, labels = inspect.stdout.strip().split("|", 2)
        stale = container_image != image_id or any(f'"{k}":"{v}"' not in labels for k, v in _labels(cfg).items())
        if stale:
            log(f"[container] {name} 的镜像或配置已变化，重建（工作区与会话保留）")
            docker("rm", "-f", name)
            _create(cfg, name)
        elif running != "true":
            log(f"[container] 启动 {name}")
            docker("start", name)
    else:
        log(f"[container] 创建 {name}（工作区 {cfg.workspace_dir}）")
        _create(cfg, name)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if docker("exec", name, "test", "-f", "/run/firewall-ready", check=False).returncode == 0:
            return name
        if docker("exec", name, "test", "-f", "/run/firewall-failed", check=False).returncode == 0:
            logs = docker("logs", "--tail", "30", name, check=False)
            raise RuntimeError(f"容器防火墙设置失败：\n{logs.stdout}{logs.stderr}")
        time.sleep(0.5)
    raise RuntimeError(f"容器 {name} 在 60 秒内没有就绪")
