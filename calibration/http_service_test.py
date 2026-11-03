"""Local HTTP lifecycle ownership without models, game, Docker or user settings."""
import asyncio
from contextlib import asynccontextmanager
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from orchestrator.http_service import HttpServiceStopError, http_service


async def get(port):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        await writer.wait_closed()


def app(lifespan=None):
    async def endpoint(request):
        return PlainTextResponse('HTTP_FIXTURE_OK')
    return Starlette(routes=[Route('/', endpoint)], lifespan=lifespan)


class HttpServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_port_zero_and_lifespan_close(self):
        events = []
        @asynccontextmanager
        async def lifespan(application):
            events.append('started')
            try:
                yield
            finally:
                events.append('stopped')
        async with http_service(app(lifespan), port=0, label='fixture') as service:
            self.assertGreater(service.port, 0)
            self.assertEqual(service.actual_port, service.port)
            self.assertEqual(events, ['started'])
            self.assertIn(b'HTTP_FIXTURE_OK', await get(service.port))
            self.assertFalse(service.task.done())
        self.assertTrue(service.task.done())
        self.assertEqual(events, ['started', 'stopped'])
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', service.port)

    async def test_body_error_is_preserved_and_listener_closes(self):
        expected = ValueError('body failed')
        with self.assertRaises(ValueError) as caught:
            async with http_service(app(), port=0) as service:
                self.assertIn(b'HTTP_FIXTURE_OK', await get(service.port))
                raise expected
        self.assertIs(caught.exception, expected)
        self.assertTrue(service.task.done())

    async def test_cancel_running_body_preserves_cancellation_and_closes_lifespan(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        handles = []
        @asynccontextmanager
        async def lifespan(application):
            try:
                yield
            finally:
                stopped.set()
        async def consume():
            async with http_service(app(lifespan), port=0) as service:
                handles.append(service)
                entered.set()
                await asyncio.Event().wait()
        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertTrue(stopped.is_set())
        self.assertTrue(handles[0].task.done())

    async def test_cancel_startup_disposes_separate_lifespan_task(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                stopped.set()
        async def consume():
            async with http_service(app(lifespan), port=0, stop_timeout=.02):
                self.fail('A blocked startup must never yield')
        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertTrue(stopped.is_set())

    async def test_startup_timeout_disposes_lifespan_task(self):
        stopped = asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            try:
                await asyncio.Event().wait()
                yield
            finally:
                stopped.set()
        with self.assertRaisesRegex(TimeoutError, '未就绪'):
            async with http_service(app(lifespan), port=0, start_timeout=.02, stop_timeout=.02):
                self.fail('A timed out startup must never yield')
        self.assertTrue(stopped.is_set())

    async def test_original_startup_error_is_not_replaced_by_service_cleanup(self):
        for expected in (ValueError('ASGI startup fixture failed'), TimeoutError('application timeout')):
            @asynccontextmanager
            async def lifespan(application):
                raise expected
                yield
            with self.subTest(expected=expected), self.assertRaises(type(expected)) as caught:
                async with http_service(app(lifespan), port=0):
                    self.fail('Failed startup must never yield')
            self.assertIs(caught.exception, expected)

    async def test_bound_port_raises_ordinary_error_without_ending_loop(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, '启动失败'):
                async with http_service(app(), port=port, start_timeout=1):
                    self.fail('An occupied port must never yield')
        # A SystemExit from serve must not kill the IsolatedAsyncio event loop.
        async with http_service(app(), port=0) as service:
            self.assertIn(b'HTTP_FIXTURE_OK', await get(service.port))

    async def test_hanging_real_shutdown_is_cancelled_and_awaited(self):
        shutdown_started, stopped = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            try:
                yield
                shutdown_started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async with http_service(app(lifespan), port=0, stop_timeout=.4) as service:
            pass
        self.assertTrue(shutdown_started.is_set())
        self.assertTrue(stopped.is_set())
        self.assertTrue(service.task.done())

    async def test_early_serve_return_or_error_never_yields(self):
        for error in (None, RuntimeError('serve startup failed')):
            class FailedServer:
                started = False
                async def serve(self):
                    if error:
                        raise error
            with self.subTest(error=error), patch('orchestrator.http_service.uvicorn.Server', return_value=FailedServer()):
                with self.assertRaises(RuntimeError) as caught:
                    async with http_service(app(), start_timeout=1):
                        self.fail('An exited startup must never yield')
                if error:
                    self.assertIs(caught.exception, error)

    async def test_stop_timeout_only_cancels_service_owned_tasks(self):
        cancelled, request_cancelled = asyncio.Event(), asyncio.Event()
        class Listener:
            sockets = [SimpleNamespace(getsockname=lambda: ('127.0.0.1', 12345))]
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass
        listener = Listener()
        class HungServer:
            started = False
            servers = [listener]
            server_state = SimpleNamespace(connections=set(), tasks=set())
            async def serve(self):
                async def request():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        request_cancelled.set()
                self.server_state.tasks.add(asyncio.create_task(request()))
                self.started = True
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        unrelated = asyncio.create_task(asyncio.Event().wait())
        try:
            with patch('orchestrator.http_service.uvicorn.Server', return_value=HungServer()):
                async with http_service(app(), port=12345, stop_timeout=.02) as service:
                    pass
            self.assertTrue(cancelled.is_set())
            self.assertTrue(request_cancelled.is_set())
            self.assertTrue(listener.closed)
            self.assertTrue(service.task.done())
            self.assertFalse(unrelated.done())
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated, return_exceptions=True)

    async def test_cleanup_failure_cannot_mask_body_error(self):
        class BrokenListener:
            sockets = [SimpleNamespace(getsockname=lambda: ('127.0.0.1', 12345))]
            def close(self):
                raise OSError('close failed')
            async def wait_closed(self):
                pass
        class BrokenServer:
            started = True
            servers = [BrokenListener()]
            async def serve(self):
                return
        expected = ValueError('body owner failed')
        server = BrokenServer()
        async def serve():
            while not getattr(server, 'should_exit', False):
                await asyncio.sleep(.01)
        server.serve = serve
        with patch('orchestrator.http_service.uvicorn.Server', return_value=server):
            with self.assertRaises(ValueError) as caught:
                async with http_service(app(), port=12345):
                    raise expected
        self.assertIs(caught.exception, expected)

    async def test_application_shutdown_error_is_reported_unless_body_already_failed(self):
        for body_error in (None, ValueError('primary body failure')):
            shutdown_error = RuntimeError('application shutdown failure')
            @asynccontextmanager
            async def lifespan(application):
                try:
                    yield
                finally:
                    raise shutdown_error
            expected = body_error or shutdown_error
            with self.subTest(body_error=body_error), self.assertRaises(type(expected)) as caught:
                async with http_service(app(lifespan), port=0) as service:
                    if body_error:
                        raise body_error
            self.assertIs(caught.exception, expected)
            self.assertTrue(service.task.done())

    async def test_real_lifespan_delayed_cancel_cleanup_has_its_own_deadline(self):
        cleanup_started, release_cleanup, cleanup_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            try:
                yield
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleanup_done.set()
        async def consume():
            async with http_service(app(lifespan), port=0, stop_timeout=.04, label='delayed-cleanup'):
                pass
        pending = ()
        try:
            with self.assertRaises(HttpServiceStopError) as caught:
                await asyncio.wait_for(consume(), 2)
            self.assertTrue(cleanup_started.is_set())
            self.assertFalse(cleanup_done.is_set())
            self.assertIn('尚未确认全部资源释放', str(caught.exception))
            pending = caught.exception.pending_tasks
            self.assertTrue(any(not task.done() for task in pending))
        finally:
            # This is application-owned test cleanup, not another helper cancel.
            release_cleanup.set()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2)
        self.assertTrue(cleanup_done.is_set())
        self.assertTrue(all(task.done() for task in pending))

    async def test_unfinished_cleanup_is_reported_without_replacing_body_error(self):
        release_cleanup = asyncio.Event()
        expected = ValueError('original body failure')
        @asynccontextmanager
        async def lifespan(application):
            try:
                yield
            finally:
                await release_cleanup.wait()
        async def consume():
            async with http_service(app(lifespan), port=0, stop_timeout=.04):
                raise expected
        pending = ()
        try:
            with self.assertRaises(ValueError) as caught:
                await asyncio.wait_for(consume(), 2)
            self.assertIs(caught.exception, expected)
            cleanup_error = caught.exception.http_service_cleanup_error
            self.assertIsInstance(cleanup_error, HttpServiceStopError)
            pending = cleanup_error.pending_tasks
            self.assertTrue(pending)
            self.assertTrue(any('尚未确认全部资源释放' in note for note in caught.exception.__notes__))
        finally:
            release_cleanup.set()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2)

    async def test_caller_cancel_during_delayed_cleanup_keeps_cancel_and_pending_report(self):
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            try:
                yield
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
        async def consume():
            async with http_service(app(lifespan), port=0, stop_timeout=.1):
                pass
        consumer = asyncio.create_task(consume())
        pending = ()
        try:
            await asyncio.wait_for(cleanup_started.wait(), 2)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(consumer, 2)
            self.assertIsInstance(caught.exception.__cause__, HttpServiceStopError)
            pending = caught.exception.__cause__.pending_tasks
            self.assertTrue(pending)
        finally:
            release_cleanup.set()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2)


if __name__ == '__main__':
    unittest.main()
