import select
from .. import _core, _subprocess


async def wait_child_exiting(process: "_subprocess.Process") -> None:
    kqueue = _core.current_kqueue()
    try:
        from select import KQ_NOTE_EXIT
    except ImportError:  # pragma: no cover
        # pypy doesn't define KQ_NOTE_EXIT:
        # https://bitbucket.org/pypy/pypy/issues/2921/
        # I verified this value against both Darwin and FreeBSD
        KQ_NOTE_EXIT = 0x80000000

    make_event = lambda flags: select.kevent(
        process.pid,
        filter=select.KQ_FILTER_PROC,
        flags=flags,
        fflags=KQ_NOTE_EXIT
    )

    try:
        kqueue.control(
            [make_event(select.KQ_EV_ADD | select.KQ_EV_ONESHOT)], 0
        )
    except ProcessLookupError:  # pragma: no cover
        # This can supposedly happen if the process is in the process
        # of exiting, and it can even be the case that kqueue says the
        # process doesn't exist before waitpid(WNOHANG) says it hasn't
        # exited yet. See the discussion in https://chromium.googlesource.com/
        # chromium/src/base/+/master/process/kill_mac.cc .
        # We haven't actually seen this error occur since we added
        # locking to prevent multiple calls to wait_child_exiting()
        # for the same process simultaneously, but given the explanation
        # in Chromium it seems we should still keep the check.
        return

    def abort(_):
        kqueue.control([make_event(select.KQ_EV_DELETE)], 0)
        return _core.Abort.SUCCEEDED

    await _core.wait_kevent(process.pid, select.KQ_FILTER_PROC, abort)
