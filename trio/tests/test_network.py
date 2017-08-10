import pytest

import socket as stdlib_socket
import errno

from .. import _core
from ..testing import (
    check_half_closeable_stream, wait_all_tasks_blocked, assert_yields
)
from .._streams import ClosedListenerError
from .._network import *
from .. import socket as tsocket


async def test_SocketStream_basics():
    # stdlib socket bad (even if connected)
    a, b = stdlib_socket.socketpair()
    with a, b:
        with pytest.raises(TypeError):
            SocketStream(a)

    # DGRAM socket bad
    with tsocket.socket(type=tsocket.SOCK_DGRAM) as sock:
        with pytest.raises(ValueError):
            SocketStream(sock)

    # disconnected socket bad
    with tsocket.socket() as sock:
        with pytest.raises(ValueError):
            SocketStream(sock)

    a, b = tsocket.socketpair()
    with a, b:
        s = SocketStream(a)
        assert s.socket is a

    # Use a real, connected socket to test socket options, because
    # socketpair() might give us a unix socket that doesn't support any of
    # these options
    with tsocket.socket() as listen_sock:
        listen_sock.bind(("127.0.0.1", 0))
        listen_sock.listen(1)
        with tsocket.socket() as client_sock:
            await client_sock.connect(listen_sock.getsockname())

            s = SocketStream(client_sock)

            # TCP_NODELAY enabled by default
            assert s.getsockopt(tsocket.IPPROTO_TCP, tsocket.TCP_NODELAY)
            # We can disable it though
            s.setsockopt(tsocket.IPPROTO_TCP, tsocket.TCP_NODELAY, False)
            assert not s.getsockopt(tsocket.IPPROTO_TCP, tsocket.TCP_NODELAY)

            b = s.getsockopt(tsocket.IPPROTO_TCP, tsocket.TCP_NODELAY, 1)
            assert isinstance(b, bytes)


async def fill_stream(s):
    async def sender():
        while True:
            await s.send_all(b"x" * 10000)

    async def waiter(nursery):
        await wait_all_tasks_blocked()
        nursery.cancel_scope.cancel()

    async with _core.open_nursery() as nursery:
        nursery.spawn(sender)
        nursery.spawn(waiter, nursery)


async def test_SocketStream_generic():
    async def stream_maker():
        left, right = tsocket.socketpair()
        return SocketStream(left), SocketStream(right)

    async def clogged_stream_maker():
        left, right = await stream_maker()
        await fill_stream(left)
        await fill_stream(right)
        return left, right

    await check_half_closeable_stream(stream_maker, clogged_stream_maker)


async def test_SocketListener():
    # Not a trio socket
    with stdlib_socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(10)
        with pytest.raises(TypeError):
            SocketListener(s)

    # Not a SOCK_STREAM
    with tsocket.socket(type=tsocket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        with pytest.raises(ValueError) as excinfo:
            SocketListener(s)
        excinfo.match(r".*SOCK_STREAM")

    # Didn't call .listen()
    with tsocket.socket() as s:
        s.bind(("127.0.0.1", 0))
        with pytest.raises(ValueError) as excinfo:
            SocketListener(s)
        excinfo.match(r".*listen")

    listen_sock = tsocket.socket()
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(10)
    listener = SocketListener(listen_sock)

    assert listener.socket is listen_sock

    client_sock = tsocket.socket()
    await client_sock.connect(listen_sock.getsockname())
    with assert_yields():
        server_stream = await listener.accept()
    assert isinstance(server_stream, SocketStream)
    assert server_stream.socket.getsockname() == listen_sock.getsockname()
    assert server_stream.socket.getpeername() == client_sock.getsockname()

    with assert_yields():
        await listener.aclose()

    with assert_yields():
        await listener.aclose()

    with assert_yields():
        with pytest.raises(ClosedListenerError):
            await listener.accept()

    client_sock.close()
    await server_stream.aclose()


async def test_SocketListener_socket_closed_underfoot():
    listen_sock = tsocket.socket()
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(10)
    listener = SocketListener(listen_sock)

    # Close the socket, not the listener
    listen_sock.close()

    # SocketListener gives correct error
    with assert_yields():
        with pytest.raises(ClosedListenerError):
            await listener.accept()


async def test_SocketListener_accept_errors():
    class FakeSocket:
        def __init__(self, events):
            self._events = iter(events)

        type = tsocket.SOCK_STREAM

        # Fool the check for SO_ACCEPTCONN in SocketListener.__init__
        def getsockopt(self, level, opt):
            return True

        def setsockopt(self, level, opt, value):
            pass

        # Fool the check for connection in SocketStream.__init__
        def getpeername(self):
            pass

        async def accept(self):
            await _core.yield_briefly()
            event = next(self._events)
            if isinstance(event, BaseException):
                raise event
            else:
                return event, None

    class FakeSocketFactory:
        def is_trio_socket(self, obj):
            return isinstance(obj, FakeSocket)

    tsocket.set_custom_socket_factory(FakeSocketFactory())

    fake_server_sock = FakeSocket([])

    fake_listen_sock = FakeSocket([
        OSError(errno.ECONNABORTED, "Connection aborted"),
        OSError(errno.EPERM, "Permission denied"),
        OSError(errno.EPROTO, "Bad protocol"),
        fake_server_sock,
        OSError(errno.EMFILE, "Out of file descriptors"),
        OSError(errno.EFAULT, "attempt to write to read-only memory"),
        OSError(errno.ENOBUFS, "out of buffers"),
        fake_server_sock,
    ])

    l = SocketListener(fake_listen_sock)

    with assert_yields():
        s = await l.accept()
        assert s.socket is fake_server_sock

    for code in [errno.EMFILE, errno.EFAULT, errno.ENOBUFS]:
        with assert_yields():
            with pytest.raises(OSError) as excinfo:
                await l.accept()
            assert excinfo.value.errno == code

    with assert_yields():
        s = await l.accept()
        assert s.socket is fake_server_sock
