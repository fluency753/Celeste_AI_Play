"""智能体容器隔离验收：逐项检查并打印 通过 / 不通过。

用法（仓库根目录，需要 Docker Desktop 已启动）：
    python -m orchestrator.verify --agent opus

检查项：
1. 认证：容器里的 Claude Code 使用订阅令牌（oauth_token），没有 API 密钥。
2. 防火墙（容器里 agent 用户的普通进程，相当于 Claude Code 本身能连到哪里）：
   只有 Anthropic 官方域名与宿主机主控端口能连通，其余（任意网站、直连 IP、外部 DNS、宿主机其他端口，
   包括游戏桥接端口）全部失败。
3. Bash 沙箱（让 Claude Code 用它的 Bash 工具运行同一套检查）：全部失败，包括 Anthropic 与主控端口；
   命令环境里看不到登录令牌；只能写工作区与 /tmp。
4. 权限：agent 用户改不了防火墙与托管配置。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading

from .config import RunConfig, find_docker_exe, load_dotenv, REPO_ROOT
from .container import docker, ensure_container

MUST_FAIL = ["dns", "ip_direct", "dns_external", "google", "github", "bridge_port", "host_other"]


def parse_netcheck(text: str) -> dict[str, str]:
    for line in text.splitlines():
        if line.startswith("NETCHECK-JSON "):
            return {item["key"]: item["result"] for item in json.loads(line[len("NETCHECK-JSON "):])}
    return {}


class Report:
    def __init__(self):
        self.failed = 0

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        if not ok:
            self.failed += 1
        print(f"  [{'通过' if ok else '不通过'}] {name}{('：' + detail) if detail else ''}", flush=True)


def listen(port: int) -> socket.socket:
    """在宿主机临时监听一个端口，用来确认容器能否连到它。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)

    def accept_loop():
        while True:
            try:
                conn, _ = s.accept()
                conn.close()
            except OSError:
                return
    threading.Thread(target=accept_loop, daemon=True).start()
    return s


def sandbox_netcheck(container: str, token: str, mcp_port: int, bridge_port: int) -> tuple[dict[str, str], str]:
    """让容器里的 Claude Code 用 Bash 工具运行检查脚本，从输出流里取工具的原始输出（不依赖模型转述）。"""
    command = (f"python3 /opt/netcheck.py {mcp_port} {bridge_port} --json; "
               "echo TOKEN-LEN=${#CLAUDE_CODE_OAUTH_TOKEN}; "
               "touch /workspace/.verify_probe && echo WS-WRITE=ok || echo WS-WRITE=fail; "
               "touch /tmp/.verify_probe && echo TMP-WRITE=ok || echo TMP-WRITE=fail; "
               "touch /home/agent/.verify_probe 2>/dev/null && echo HOME-WRITE=ok || echo HOME-WRITE=fail")
    prompt = f"Use the Bash tool exactly once to run this command, then reply DONE:\n{command}"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    proc = subprocess.run(
        [find_docker_exe(), "exec", "-i", "-u", "agent", "-w", "/workspace", "-e", "CLAUDE_CODE_OAUTH_TOKEN",
         container, "claude", "-p", prompt, "--model", "haiku", "--effort", "low", "--restricted",
         "--tools", "Bash", "--allowedTools", "Bash", "--permission-mode", "dontAsk",
         "--output-format", "stream-json", "--verbose"],
        input="", capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    outputs = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "user":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                outputs.append(str(content))
    text = "\n".join(outputs)
    return parse_netcheck(text), text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="智能体标签（决定检查哪个容器）")
    ap.add_argument("--mcp-port", type=int, default=47780)
    ap.add_argument("--bridge-port", type=int, default=47774)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or load_dotenv(REPO_ROOT / ".env").get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        raise SystemExit("缺少 CLAUDE_CODE_OAUTH_TOKEN（见使用说明 2.1 节）")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token  # docker exec -e CLAUDE_CODE_OAUTH_TOKEN 从本进程环境取值
    cfg = RunConfig(agent=args.agent, save="-", model="-", mcp_port=args.mcp_port)
    container = ensure_container(cfg)
    report = Report()

    # 宿主机上临时监听主控端口与桥接端口（若游戏或主控正在运行，端口已被占用，就直接用现有服务）
    listeners = []
    for port in (args.mcp_port, args.bridge_port):
        try:
            listeners.append(listen(port))
        except OSError:
            pass

    try:
        print("1. 认证", flush=True)
        status = docker("exec", "-u", "agent", "-e", "CLAUDE_CODE_OAUTH_TOKEN", container, "claude", "auth", "status",
                        check=False)
        try:
            info = json.loads(status.stdout)
        except ValueError:
            info = {}
        report.check(info.get("authMethod") == "oauth_token", "Claude Code 使用订阅令牌", str(info.get("authMethod")))
        env_dump = docker("exec", "-u", "agent", container, "env", check=False).stdout
        report.check("ANTHROPIC_API_KEY" not in env_dump, "容器环境里没有 ANTHROPIC_API_KEY")

        print("2. 防火墙（Claude Code 本身能连到哪里）", flush=True)
        out = docker("exec", "-u", "agent", container, "python3", "/opt/netcheck.py",
                     str(args.mcp_port), str(args.bridge_port), "--json", check=False).stdout
        results = parse_netcheck(out)
        for key in MUST_FAIL:
            report.check(results.get(key, "").startswith("失败"), f"应失败：{key}", results.get(key, "无结果"))
        report.check(results.get("anthropic", "").startswith("连通"), "应连通：Anthropic API", results.get("anthropic", ""))
        report.check(results.get("mcp_port", "").startswith("连通"), "应连通：宿主机主控端口", results.get("mcp_port", ""))

        print("3. Bash 沙箱（智能体自写程序能连到哪里）", flush=True)
        results, raw = sandbox_netcheck(container, token, args.mcp_port, args.bridge_port)
        if not results:
            report.check(False, "沙箱检查没有输出", raw[:300])
        for key in MUST_FAIL + ["anthropic", "mcp_port"]:
            report.check(results.get(key, "").startswith("失败"), f"应失败：{key}", results.get(key, "无结果"))
        report.check("TOKEN-LEN=0" in raw, "Bash 环境里看不到登录令牌")
        report.check("WS-WRITE=ok" in raw, "可以写工作区 /workspace")
        report.check("TMP-WRITE=ok" in raw, "可以写 /tmp")
        report.check("HOME-WRITE=fail" in raw, "不能写家目录（Claude Code 配置所在处）")

        print("4. 权限", flush=True)
        r = docker("exec", "-u", "agent", container, "iptables", "-F", "OUTPUT", check=False)
        report.check(r.returncode != 0, "agent 用户改不了防火墙")
        r = docker("exec", "-u", "agent", container, "sh", "-c",
                   "echo '{}' > /etc/claude-code/managed-settings.json", check=False)
        report.check(r.returncode != 0, "agent 用户改不了托管配置")
        r = docker("exec", "-u", "agent", container, "sh", "-c", "echo '1.2.3.4 example.com' >> /etc/hosts", check=False)
        report.check(r.returncode != 0, "agent 用户改不了 /etc/hosts")
        settings = docker("exec", container, "cat", "/etc/claude-code/managed-settings.json", check=False).stdout
        report.check('"WebSearch"' in settings and '"allowUnsandboxedCommands": false' in settings,
                     "托管配置拒绝联网工具、禁止退出沙箱")
    finally:
        for s in listeners:
            s.close()
        docker("exec", container, "rm", "-f", "/workspace/.verify_probe", check=False)

    print(f"\n结果：{'全部通过' if report.failed == 0 else f'{report.failed} 项不通过'}", flush=True)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
