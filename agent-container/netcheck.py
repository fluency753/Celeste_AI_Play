"""容器网络自检：在容器里运行，逐项尝试连接并打印结果（不依赖 curl）。

用法：python3 /opt/netcheck.py [主控端口] [桥接端口]
期望：只有 Anthropic 官方域名的 443 与宿主机主控端口能连通，其余全部失败。
"""
import socket
import ssl
import sys
import urllib.request

MCP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47780
BRIDGE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 47774


def tcp(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "连通"
    except Exception as exc:  # noqa: BLE001
        return f"失败（{type(exc).__name__}: {exc}）"


def https(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ssl.create_default_context()) as resp:
            return f"连通（HTTP {resp.status}）"
    except urllib.error.HTTPError as exc:
        return f"连通（HTTP {exc.code}）"
    except Exception as exc:  # noqa: BLE001
        return f"失败（{type(exc).__name__}: {exc}）"


CHECKS = [
    ("应失败", "DNS 解析 example.com", lambda: tcp("example.com", 443)),
    ("应失败", "直连 1.1.1.1:443（绕过 DNS）", lambda: tcp("1.1.1.1", 443)),
    ("应失败", "直连 8.8.8.8:53（外部 DNS）", lambda: tcp("8.8.8.8", 53)),
    ("应失败", "https://www.google.com", lambda: https("https://www.google.com")),
    ("应失败", "https://github.com", lambda: https("https://github.com")),
    ("应失败", f"宿主机桥接端口 host.docker.internal:{BRIDGE_PORT}", lambda: tcp("host.docker.internal", BRIDGE_PORT)),
    ("应失败", "宿主机其他端口 host.docker.internal:445", lambda: tcp("host.docker.internal", 445)),
    ("视情况", "https://api.anthropic.com（只有 Claude Code 本身需要）", lambda: https("https://api.anthropic.com")),
    ("视情况", f"宿主机主控端口 host.docker.internal:{MCP_PORT}", lambda: tcp("host.docker.internal", MCP_PORT)),
]

for expect, name, check in CHECKS:
    print(f"[{expect}] {name}：{check()}", flush=True)
