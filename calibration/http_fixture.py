"""A bound HTTP server boundary for tests retaining the real hosting context."""
from types import SimpleNamespace


def bound_server(server, port=47780):
    class Listener:
        sockets = [SimpleNamespace(getsockname=lambda: ('127.0.0.1', port or 12345))]

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    server.servers = [Listener()]
    server.server_state = SimpleNamespace(connections=set(), tasks=set())
    return server
