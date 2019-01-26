import errno
import select
from typing import Tuple
import os

import pytest

from .._core.tests.tutil import gc_collect_harder
from .. import _core, move_on_after
from ..testing import wait_all_tasks_blocked, check_one_way_stream

posix = os.name == "posix"
pytestmark = pytest.mark.skipif(not posix, reason="posix only")
if posix:
    from .._unix_pipes import PipeSendStream, PipeReceiveStream


async def make_pipe() -> Tuple[PipeSendStream, PipeReceiveStream]:
    """Makes a new pair of pipes."""
    (r, w) = os.pipe()
    return PipeSendStream(w), PipeReceiveStream(r)


async def make_clogged_pipe():
    s, r = await make_pipe()
    try:
        while True:
            # We want to totally fill up the pipe buffer.
            # This requires working around a weird feature that POSIX pipes
            # have.
            # If you do a write of <= PIPE_BUF bytes, then it's guaranteed
            # to either complete entirely, or not at all. So if we tried to
            # write PIPE_BUF bytes, and the buffer's free space is only
            # PIPE_BUF/2, then the write will raise BlockingIOError... even
            # though a smaller write could still succeed! To avoid this,
            # make sure to write >PIPE_BUF bytes each time, which disables
            # the special behavior.
            # For details, search for PIPE_BUF here:
            #   http://pubs.opengroup.org/onlinepubs/9699919799/functions/write.html

            # for the getattr:
            # https://bitbucket.org/pypy/pypy/issues/2876/selectpipe_buf-is-missing-on-pypy3
            buf_size = getattr(select, "PIPE_BUF", 8192)
            os.write(s.fileno(), b"x" * buf_size * 2)
    except BlockingIOError:
        pass
    return s, r


async def test_send_pipe():
    r, w = os.pipe()
    async with PipeSendStream(w) as send:
        assert send.fileno() == w
        await send.send_all(b"123")
        assert (os.read(r, 8)) == b"123"

        os.close(r)


async def test_receive_pipe():
    r, w = os.pipe()
    async with PipeReceiveStream(r) as recv:
        assert (recv.fileno()) == r
        os.write(w, b"123")
        assert (await recv.receive_some(8)) == b"123"

        os.close(w)


async def test_pipes_combined():
    write, read = await make_pipe()
    count = 2**20

    async def sender():
        big = bytearray(count)
        await write.send_all(big)

    async def reader():
        await wait_all_tasks_blocked()
        received = 0
        while received < count:
            received += len(await read.receive_some(4096))

        assert received == count

    async with _core.open_nursery() as n:
        n.start_soon(sender)
        n.start_soon(reader)

    await read.aclose()
    await write.aclose()


async def test_pipe_errors():
    with pytest.raises(TypeError):
        PipeReceiveStream(None)

    with pytest.raises(ValueError):
        await PipeReceiveStream(0).receive_some(0)


async def test_del():
    w, r = await make_pipe()
    f1, f2 = w.fileno(), r.fileno()
    del w, r
    gc_collect_harder()

    with pytest.raises(OSError) as excinfo:
        os.close(f1)
    assert excinfo.value.errno == errno.EBADF

    with pytest.raises(OSError) as excinfo:
        os.close(f2)
    assert excinfo.value.errno == errno.EBADF


async def test_async_with():
    w, r = await make_pipe()
    async with w, r:
        pass

    assert w.fileno() == -1
    assert r.fileno() == -1

    with pytest.raises(OSError) as excinfo:
        os.close(w.fileno())
    assert excinfo.value.errno == errno.EBADF

    with pytest.raises(OSError) as excinfo:
        os.close(r.fileno())
    assert excinfo.value.errno == errno.EBADF


async def test_misdirected_aclose_regression():
    # https://github.com/python-trio/trio/issues/661#issuecomment-456582356
    w, r = await make_pipe()
    old_r_fd = r.fileno()

    # Close the original objects
    await w.aclose()
    await r.aclose()

    # Do a little dance to get a new pipe whose receive handle matches the old
    # receive handle.
    r2_fd, w2_fd = os.pipe()
    if r2_fd != old_r_fd:  # pragma: no cover
        os.dup2(r2_fd, old_r_fd)
        os.close(r2_fd)
    async with PipeReceiveStream(old_r_fd) as r2:
        assert r2.fileno() == old_r_fd

        # And now set up a background task that's working on the new receive
        # handle
        async def expect_eof():
            assert await r2.receive_some(10) == b""

        async with _core.open_nursery() as nursery:
            nursery.start_soon(expect_eof)
            await wait_all_tasks_blocked()

            # Here's the key test: does calling aclose() again on the *old*
            # handle, cause the task blocked on the *new* handle to raise
            # ClosedResourceError?
            await r.aclose()
            await wait_all_tasks_blocked()

            # Guess we survived! Close the new write handle so that the task
            # gets an EOF and can exit cleanly.
            os.close(w2_fd)


async def test_close_at_bad_time_for_receive_some(monkeypatch):
    # We used to have race conditions where if one task was using the pipe,
    # and another closed it at *just* the wrong moment, it would give an
    # unexpected error instead of ClosedResourceError:
    # https://github.com/python-trio/trio/issues/661
    #
    # This tests what happens if the pipe gets closed in the moment *between*
    # when receive_some wakes up, and when it tries to call os.read
    async def expect_closedresourceerror():
        with pytest.raises(_core.ClosedResourceError):
            await r.receive_some(10)

    orig_wait_readable = _core._run.TheIOManager.wait_readable

    async def patched_wait_readable(*args, **kwargs):
        await orig_wait_readable(*args, **kwargs)
        await r.aclose()

    monkeypatch.setattr(
        _core._run.TheIOManager, "wait_readable", patched_wait_readable
    )
    s, r = await make_pipe()
    async with s, r:
        async with _core.open_nursery() as nursery:
            nursery.start_soon(expect_closedresourceerror)
            await wait_all_tasks_blocked()
            # Trigger everything by waking up the receiver
            await s.send_all(b"x")


async def test_close_at_bad_time_for_send_all(monkeypatch):
    # We used to have race conditions where if one task was using the pipe,
    # and another closed it at *just* the wrong moment, it would give an
    # unexpected error instead of ClosedResourceError:
    # https://github.com/python-trio/trio/issues/661
    #
    # This tests what happens if the pipe gets closed in the moment *between*
    # when send_all wakes up, and when it tries to call os.write
    async def expect_closedresourceerror():
        with pytest.raises(_core.ClosedResourceError):
            await s.send_all(b"x" * 100)

    orig_wait_writable = _core._run.TheIOManager.wait_writable

    async def patched_wait_writable(*args, **kwargs):
        await orig_wait_writable(*args, **kwargs)
        await s.aclose()

    monkeypatch.setattr(
        _core._run.TheIOManager, "wait_writable", patched_wait_writable
    )
    s, r = await make_clogged_pipe()
    async with s, r:
        async with _core.open_nursery() as nursery:
            nursery.start_soon(expect_closedresourceerror)
            await wait_all_tasks_blocked()
            # Trigger everything by waking up the sender
            await r.receive_some(10000)


async def test_pipe_fully():
    await check_one_way_stream(make_pipe, make_clogged_pipe)
