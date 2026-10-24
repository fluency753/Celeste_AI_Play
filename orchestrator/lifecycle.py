"""Game-free runtime hosting for preparation, naming and idle chat."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from .runtimes import runtime


@asynccontextmanager
async def host_runner(cfg, directory, metrics, container, study=None, **options):
    adapter = runtime(cfg.engine)
    if not adapter.mcp:
        yield adapter.runner(cfg, directory, None, metrics, container, study, **options)
        return
    # This context has no BridgeClient and cannot enter a game or start recording.
    session = SimpleNamespace(cfg=cfg, metrics=metrics, workspace=cfg.workspace_dir,
                              done=False, done_reason='')
    from .server import build_server, TRANSPORT_SECURITY
    from .pausegate import PauseGate
    from .run import add_gate_route
    import uvicorn
    server = build_server(session, study)
    runner = adapter.runner(cfg, directory, session, metrics, container, study, **options)
    app = server.streamable_http_app(streamable_http_path='/mcp', session_idle_timeout=None,
                                    host='127.0.0.1', transport_security=TRANSPORT_SECURITY)
    add_gate_route(app, PauseGate(metrics, enabled=False), session=session)
    web = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=cfg.mcp_port, log_level='warning'))
    task = asyncio.create_task(web.serve())
    try:
        async with asyncio.timeout(15):
            while not web.started:
                if task.done():
                    await task
                    raise RuntimeError('会话工具服务启动失败')
                await asyncio.sleep(.05)
        if cfg.mcp_port == 0:
            cfg.mcp_port = web.servers[0].sockets[0].getsockname()[1]
        yield runner
    finally:
        runner.close_events()
        web.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
