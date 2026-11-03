"""Game-free runtime hosting for preparation, naming and idle chat."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from .runtimes import runtime


@asynccontextmanager
async def host_runner(cfg, directory, metrics, container, study=None, **options):
    adapter = runtime(cfg.engine)
    adapter.workspace(cfg).validate(preparation=options.get('prepare_only', False))
    if not adapter.mcp:
        yield adapter.runner(cfg, directory, None, metrics, container, study, **options)
        return
    # This context has no BridgeClient and cannot enter a game or start recording.
    session = SimpleNamespace(cfg=cfg, metrics=metrics, workspace=cfg.workspace_dir,
                              done=False, done_reason='')
    from .server import build_server, TRANSPORT_SECURITY
    from .tool_gate import add_gate_route
    from .http_service import http_service
    server = build_server(session, study)
    runner = adapter.runner(cfg, directory, session, metrics, container, study, **options)
    primary = None
    try:
        app = server.streamable_http_app(streamable_http_path='/mcp', session_idle_timeout=None,
                                        host='127.0.0.1', transport_security=TRANSPORT_SECURITY)
        add_gate_route(app, session=session)
        async with http_service(app, port=cfg.mcp_port, label='会话工具服务') as service:
            cfg.mcp_port = service.port
            yield runner
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            runner.close_events()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f'会话事件日志清理失败：{error}')
