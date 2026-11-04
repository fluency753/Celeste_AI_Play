#!/bin/bash
# 容器入口（root）：先设置防火墙，成功后写就绪标记，然后常驻。
# Claude Code 由主控用 docker exec -u agent 启动，智能体没有 root 权限，改不了防火墙。
set -e
# Mask the real file in the player-login layout so the workspace symlink
# resolves to the masked file. Codex login uses the shared directory as its
# home, so its temporary executables there must stay readable.
if [ "${CELESTE_CODEX_PLAYER_LOGIN:-}" = 1 ]; then
    sed -i 's|"/home/agent/\.codex/auth\.json"|"/var/lib/celeste-codex-login/auth.json", "/var/lib/celeste-codex-login/log"|' /etc/codex/requirements.toml
fi
rm -f /run/firewall-ready /run/firewall-failed
if /usr/local/sbin/init-firewall.sh; then
    touch /run/firewall-ready
else
    touch /run/firewall-failed
    echo "[entrypoint] 防火墙设置失败，容器保持运行以便查看日志，但主控不会在其中启动智能体" >&2
fi
exec sleep infinity
