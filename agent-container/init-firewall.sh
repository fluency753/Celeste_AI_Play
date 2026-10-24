#!/bin/bash
# 出站防火墙（容器启动时以 root 执行一次）。
# 只放行：模型服务的 443 端口（默认 Anthropic 官方域名；第三方提供方的容器只放行它自己的域名，见
# configs/providers.json）、宿主机上主控的游戏工具端口，以及第三方提供方要用的宿主机端口（PROVIDER_HOST_PORTS，
# 例如本机把 Anthropic 接口转成 OpenAI 接口的网关）。其余出站一律拒绝，包括 DNS：
# 允许的域名在这里预先解析并写入 /etc/hosts，之后容器内任何程序都无法再解析其他域名。
set -euo pipefail

MCP_PORT="${MCP_PORT:-47780}"
if [ "${ALLOW_ANTHROPIC:-1}" = "1" ]; then
    ALLOWED_DOMAINS="api.anthropic.com claude.ai claude.com platform.claude.com"
    ANTHROPIC_CIDRS="160.79.104.0/23"   # Anthropic 公布的入站地址段（Claude API 与 Console）
else
    ALLOWED_DOMAINS=""
    ANTHROPIC_CIDRS=""
fi
ALLOWED_DOMAINS="$(echo "$ALLOWED_DOMAINS ${PROVIDER_DOMAINS:-}" | xargs)"
HOST_PORTS="$(echo "${PROVIDER_HOST_PORTS:-}" | xargs)"
if [ -z "$ALLOWED_DOMAINS" ] && [ -z "$HOST_PORTS" ]; then
    echo "[firewall] 没有任何允许的模型服务域名" >&2
    exit 1
fi

resolve_v4() { getent ahostsv4 "$1" | awk '{print $1}' | sort -u; }

HOST_IP="$(resolve_v4 host.docker.internal | head -n1)"
if [ -z "$HOST_IP" ]; then
    echo "[firewall] 无法解析 host.docker.internal" >&2
    exit 1
fi

declare -A RESOLVED
for domain in $ALLOWED_DOMAINS; do
    [ -n "$domain" ] || continue
    ips="$(resolve_v4 "$domain")"
    if [ -z "$ips" ]; then
        echo "[firewall] 无法解析 $domain" >&2
        exit 1
    fi
    RESOLVED[$domain]="$ips"
done

# 固定解析结果（DNS 随后被封）
{
    echo "# --- celeste-agent firewall pins ---"
    echo "$HOST_IP host.docker.internal"
    for domain in $ALLOWED_DOMAINS; do
        echo "$(echo "${RESOLVED[$domain]}" | head -n1) $domain"
    done
} >> /etc/hosts

iptables -F OUTPUT
iptables -P OUTPUT DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p tcp -d "$HOST_IP" --dport "$MCP_PORT" -j ACCEPT
for port in $HOST_PORTS; do
    iptables -A OUTPUT -p tcp -d "$HOST_IP" --dport "$port" -j ACCEPT
done
for cidr in $ANTHROPIC_CIDRS; do
    iptables -A OUTPUT -p tcp -d "$cidr" --dport 443 -j ACCEPT
done
for domain in $ALLOWED_DOMAINS; do
    for ip in ${RESOLVED[$domain]}; do
        iptables -A OUTPUT -p tcp -d "$ip" --dport 443 -j ACCEPT
    done
done
# 其余一律立即拒绝（而不是静默丢弃），让失败马上可见
iptables -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset
iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable

# IPv6 全部禁止
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -F OUTPUT 2>/dev/null || true
    ip6tables -P OUTPUT DROP 2>/dev/null || true
    ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
fi

echo "[firewall] 已生效：host=$HOST_IP:$MCP_PORT${HOST_PORTS:+（另放行宿主机端口 $HOST_PORTS）}，允许 ${ALLOWED_DOMAINS:-（无域名）}"
iptables -S OUTPUT
