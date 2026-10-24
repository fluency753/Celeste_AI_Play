"""直接试玩客户端的本机 MCP 集成检查，无需游戏或模型。python -m calibration.game_tool_test"""
import asyncio
import base64
import contextlib
import io
import socket
import tempfile
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import Image, MCPServer

from tools.game_tool import call_tool

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


async def check():
    mock = MCPServer(name="game-tool-test")

    @mock.tool(structured_output=False)
    def observe() -> list:
        return ["frozen frame", Image(data=PNG, format="png")]

    @mock.tool(structured_output=False)
    def act() -> list:
        raise ValueError("invalid action")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mock.streamable_http_app(streamable_http_path="/mcp"), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("test server did not start")
            await asyncio.sleep(0.01)
        with tempfile.TemporaryDirectory() as directory:
            shot = Path(directory) / "shot.png"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert await call_tool(port, "observe", {}, shot) == 0
                assert await call_tool(port, "act", {}, None) == 1
            assert shot.read_bytes() == PNG
            assert "frozen frame" in output.getvalue()
            assert "Error executing tool act" in output.getvalue()
            assert base64.b64encode(PNG).decode() not in output.getvalue()
    finally:
        server.should_exit = True
        await task
        sock.close()
    print("PASS: MCP connection, text, PNG output, tool error status, no base64 in stdout")


if __name__ == "__main__":
    asyncio.run(check())
