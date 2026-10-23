# 智能体容器：固定版本的 Claude Code + 离线科学计算环境 + 出站防火墙 + 托管配置。
# 智能体以普通用户 agent 运行；防火墙与托管配置归 root 所有，智能体无权修改。
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.309
ARG SANDBOX_RUNTIME_VERSION=0.0.81

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates iptables iproute2 dnsutils procps less jq git \
        bubblewrap socat \
        python3 python3-venv python3-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

# 离线科学计算库：智能体运行时没有网络，只能用这里预装的
RUN python3 -m venv /opt/py \
    && /opt/py/bin/pip install --no-cache-dir \
        numpy==2.2.6 scipy==1.15.3 numba==0.61.2 pillow==11.2.1 networkx==3.4.2
ENV PATH=/opt/py/bin:$PATH

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @anthropic-ai/sandbox-runtime@${SANDBOX_RUNTIME_VERSION} \
    && npm cache clean --force

# 托管配置（最高优先级，归 root 所有）
RUN mkdir -p /etc/claude-code
COPY managed-settings.json /etc/claude-code/managed-settings.json
COPY init-firewall.sh /usr/local/sbin/init-firewall.sh
COPY entrypoint.sh /usr/local/sbin/entrypoint.sh
COPY netcheck.py /opt/netcheck.py
RUN chmod 644 /etc/claude-code/managed-settings.json \
    && chmod 755 /usr/local/sbin/init-firewall.sh /usr/local/sbin/entrypoint.sh

# 普通用户：工作区 /workspace（可写，挂载宿主目录），知识库 /kb（只读挂载）
RUN useradd -m -s /bin/bash -u 1500 agent \
    && mkdir -p /workspace /kb /home/agent/.claude \
    && chown agent:agent /workspace /home/agent/.claude

ENV CLAUDE_CONFIG_DIR=/home/agent/.claude \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    ENABLE_CLAUDEAI_MCP_SERVERS=false \
    CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace
ENTRYPOINT ["/usr/local/sbin/entrypoint.sh"]
