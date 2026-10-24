"""从当前聊天或命令行调用主控的游戏工具，不启动独立模型。

先运行主控（存档名按实际填写）：
    python -m orchestrator.run --agent codex --save test --no-agent --no-sim --keep-obs-open
再调用工具：
    python tools/game_tool.py observe --out runs/observe.png
    python tools/game_tool.py narrate --args '{"thought":"先观察出口和落脚点。"}'
    python tools/game_tool.py act --args-file local/action.json --out runs/after.png

act 的 JSON 例如 {"thought":"跳到右侧平台。", "inputs":["10,R","1,R,J","20,R"]}。
截图单独保存，标准输出只含文字结果，避免打印 base64。默认只连接本机。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def call_tool(port: int, tool: str, arguments: dict, output: Path | None) -> int:
    # 本机游戏工具不经过系统代理，避免 SOCKS 代理配置干扰回环连接。
    async with (
        httpx2.AsyncClient(trust_env=False, timeout=httpx2.Timeout(30, read=900)) as http,
        streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http,
                               max_sse_event_size=32 * 1024 * 1024) as (read, write),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments, read_timeout_seconds=900)
            images = 0
            for block in result.content:
                if block.type == "text":
                    print(block.text)
                elif block.type == "image":
                    images += 1
                    if output is None:
                        print("[image] 用 --out 指定截图保存路径")
                        continue
                    target = output if images == 1 else output.with_name(f"{output.stem}-{images}{output.suffix}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(block.data))
                    print(f"[image] {target.resolve()}")
            return 1 if result.is_error else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", choices=["observe", "act", "narrate", "exit_level", "cutscene"])
    parser.add_argument("--port", type=int, default=47780)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--args", default="{}", help="工具参数 JSON 对象")
    source.add_argument("--args-file", type=Path, help="UTF-8 JSON 参数文件；在 PowerShell 中推荐使用")
    parser.add_argument("--out", type=Path, help="保存返回截图的路径（PNG）")
    args = parser.parse_args()
    try:
        arguments = json.loads(args.args_file.read_text(encoding="utf-8-sig") if args.args_file else args.args)
    except (OSError, ValueError) as exc:
        parser.error(f"无法读取工具参数：{exc}")
    if not isinstance(arguments, dict):
        parser.error("工具参数必须是 JSON 对象")
    return asyncio.run(call_tool(args.port, args.tool, arguments, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
