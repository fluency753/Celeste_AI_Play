"""Own one local ASGI HTTP server from startup through bounded shutdown."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn


@dataclass(frozen=True)
class HttpService:
    port: int
    task: asyncio.Task

    @property
    def actual_port(self):
        return self.port


class HttpServiceStopError(RuntimeError):
    def __init__(self, label, pending_tasks):
        self.pending_tasks = tuple(pending_tasks)
        names = '、'.join(task.get_name() for task in self.pending_tasks)
        super().__init__(f'{label}取消后仍有 {len(self.pending_tasks)} 个自有任务未退出：{names}；'
                         '尚未确认全部资源释放')


class _LifespanOwner:
    """Uvicorn starts lifespan separately; retain its ownership on startup failure."""
    def __init__(self, app):
        self.app = app
        self.tasks = set()
        self.error = None

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'lifespan':
            return await self.app(scope, receive, send)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await self.app(scope, receive, send)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.error = error
            raise
        finally:
            self.tasks.discard(task)


async def _serve(server, label):
    try:
        await server.serve()
    except SystemExit as error:
        # A bind error must be an ordinary service failure, not stop the event loop.
        raise RuntimeError(f'{label}启动失败（服务退出码 {error.code}）') from error


async def _ready(server, task, owner, timeout, label):
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            while not server.started:
                if task.done():
                    if owner.error is not None:
                        raise owner.error
                    task.result()
                    raise RuntimeError(f'{label}在就绪前退出')
                await asyncio.sleep(.01)
    except TimeoutError as error:
        if not deadline.expired():
            raise
        raise TimeoutError(f'{label}在 {timeout:g} 秒内未就绪') from error
    if task.done():
        if owner.error is not None:
            raise owner.error
        task.result()
        raise RuntimeError(f'{label}在就绪时退出')
    sockets = [socket for listener in server.servers for socket in listener.sockets or ()]
    if not sockets:
        raise RuntimeError(f'{label}没有可用的监听端口')
    return sockets[0].getsockname()[1]


async def _stop(server, task, owner, timeout, label):
    server.should_exit = True
    cancelled = False
    try:
        await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        # Finish disposing the resources we own before forwarding caller cancellation.
        cancelled = True
    forced = not task.done()
    cleanup_error = None
    for listener in getattr(server, 'servers', ()):
        try:
            listener.close()
        except Exception as error:
            cleanup_error = cleanup_error or error
    state = getattr(server, 'server_state', None)
    for connection in tuple(getattr(state, 'connections', ())):
        try:
            connection.shutdown()
        except Exception as error:
            cleanup_error = cleanup_error or error
    owned = {task, *owner.tasks, *getattr(state, 'tasks', ())}
    for pending in owned:
        if not pending.done():
            pending.cancel()
    # Waiting for cancellation can itself block when application finally code
    # awaits more work. Give the entire cancellation phase its own deadline.
    close_tasks = set()
    for listener in getattr(server, 'servers', ()):
        close_tasks.add(asyncio.create_task(listener.wait_closed(), name=f'{label}:listener-close'))
    owned.update(close_tasks)
    try:
        done, pending = await asyncio.wait(owned, timeout=timeout)
    except asyncio.CancelledError:
        cancelled = True
        done = {item for item in owned if item.done()}
        pending = owned - done
    results = await asyncio.gather(*done, return_exceptions=True)
    if pending:
        # Never send repeated cancellations into application cleanup. Retain the
        # exact unfinished tasks so the failure cannot imply successful release.
        for close in close_tasks & pending:
            close.cancel()
        error = HttpServiceStopError(label, pending)
        if cancelled:
            raise asyncio.CancelledError from error
        raise error
    if cancelled:
        raise asyncio.CancelledError
    if owner.error is not None:
        raise owner.error
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result
    if cleanup_error is not None:
        raise cleanup_error
    lifespan = getattr(server, 'lifespan', None)
    if not forced and getattr(lifespan, 'shutdown_failed', False):
        raise RuntimeError('HTTP 应用停止失败')


@asynccontextmanager
async def http_service(app, *, host='127.0.0.1', port=0, start_timeout=15, stop_timeout=5,
                       label='HTTP 服务'):
    """Yield only after binding; own listener, serve and lifespan tasks on every exit.

    stop_timeout bounds each of graceful stop and cancellation cleanup. A task
    that keeps running after cancellation is reported through HttpServiceStopError.
    The returned port reports the HTTP listener only. Container firewall and
    advertised endpoint configuration remain the caller's responsibility.
    """
    owner = _LifespanOwner(app)
    server = uvicorn.Server(uvicorn.Config(owner, host=host, port=port, log_level='warning',
                                          lifespan='on', timeout_graceful_shutdown=stop_timeout))
    task = asyncio.create_task(_serve(server, label), name=f'{label}:serve')
    primary = None
    try:
        actual_port = await _ready(server, task, owner, start_timeout, label)
        yield HttpService(actual_port, task)
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            await _stop(server, task, owner, stop_timeout, label)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if primary is None:
                raise
            if error is not primary:
                primary.add_note(f'{label}清理失败：{error}')
                primary.http_service_cleanup_error = error
