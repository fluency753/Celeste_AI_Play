#!/usr/bin/env python3
"""PreToolUse 钩子（托管配置里登记，智能体改不了）：每次调用工具之前问主控，这次调用是否还在
规划轮次的预算之内。主控说拒绝就把理由交给 Claude Code，这次调用不执行；主控回复里带 context 时（实时展示的
半分钟提醒），把它作为 additionalContext 交给 Claude Code，出现在这次工具结果旁边。

主控不在（例如单独测试容器）、网络出错或超时时放行：这个钩子只管节奏，不承担隔离。
"""
import json
import os
import sys
import urllib.request


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    port = os.environ.get("MCP_PORT", "47780")
    body = json.dumps({"tool_name": data.get("tool_name"), "tool_use_id": data.get("tool_use_id"),
                       "session_id": data.get("session_id")}).encode("utf-8")
    request = urllib.request.Request(f"http://host.docker.internal:{port}/gate", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            decision = json.load(response)
    except Exception:  # noqa: BLE001
        return 0
    if decision.get("decision") == "deny":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": decision.get("reason") or "这次工具调用不在本次暂停的预算之内"}},
            ensure_ascii=False))
    elif decision.get("context"):
        # 实时展示的提醒（半分钟计时器）：不改变放行与否，只把一句话放到这次工具结果旁边给模型看
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": str(decision["context"])}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
