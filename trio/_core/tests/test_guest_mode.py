import pytest
import asyncio
import traceback
import queue
from functools import partial
from math import inf
import signal
import socket

import trio
import trio.testing
from .tutil import gc_collect_harder

# The simplest possible "host" loop.
# Nice features:
# - we can run code "outside" of trio using the schedule function passed to
#   our main
# - final result is returned
# - any unhandled exceptions cause an immediate crash
def trivial_guest_run(trio_fn, **start_guest_run_kwargs):
    todo = queue.Queue()

    def run_sync_soon_threadsafe(fn):
        todo.put(("run", fn))

    def done_callback(outcome):
        todo.put(("unwrap", outcome))

    trio.lowlevel.start_guest_run(
        trio_fn,
        run_sync_soon_threadsafe,
        run_sync_soon_threadsafe=run_sync_soon_threadsafe,
        done_callback=done_callback,
        **start_guest_run_kwargs,
    )

    try:
        while True:
            op, obj = todo.get()
            if op == "run":
                obj()
            elif op == "unwrap":
                return obj.unwrap()
            else:  # pragma: no cover
                assert False
    finally:
        # Make sure that exceptions raised here don't capture these, so that
        # if an exception does cause us to abandon a run then the Trio state
        # has a chance to be GC'ed and warn about it.
        del todo, run_sync_soon_threadsafe, done_callback


def test_guest_trivial():
    async def trio_return(in_host):
        await trio.sleep(0)
        return "ok"

    assert trivial_guest_run(trio_return) == "ok"

    async def trio_fail(in_host):
        raise KeyError("whoopsiedaisy")

    with pytest.raises(KeyError, match="whoopsiedaisy"):
        trivial_guest_run(trio_fail)


def test_guest_can_do_io():
    async def trio_main(in_host):
        record = []
        a, b = trio.socket.socketpair()
        with a, b:
            async with trio.open_nursery() as nursery:

                async def do_receive():
                    record.append(await a.recv(1))

                nursery.start_soon(do_receive)
                await trio.testing.wait_all_tasks_blocked()

                await b.send(b"x")

        assert record == [b"x"]

    trivial_guest_run(trio_main)


def test_host_can_directly_wake_trio_task():
    async def trio_main(in_host):
        ev = trio.Event()
        in_host(ev.set)
        await ev.wait()
        return "ok"

    assert trivial_guest_run(trio_main) == "ok"


def test_host_altering_deadlines_wakes_trio_up():
    def set_deadline(cscope, new_deadline):
        cscope.deadline = new_deadline

    async def trio_main(in_host):
        with trio.CancelScope() as cscope:
            in_host(lambda: set_deadline(cscope, -inf))
            await trio.sleep_forever()
        assert cscope.cancelled_caught

        with trio.CancelScope() as cscope:
            in_host(lambda: set_deadline(cscope, -inf))
            await trio.sleep(999)
        assert cscope.cancelled_caught

        return "ok"

    assert trivial_guest_run(trio_main) == "ok"


def test_warn_set_wakeup_fd_overwrite():
    assert signal.set_wakeup_fd(-1) == -1

    async def trio_main(in_host):
        return "ok"

    a, b = socket.socketpair()
    with a, b:
        a.setblocking(False)

        # Warn if there's already a wakeup fd
        signal.set_wakeup_fd(a.fileno())
        try:
            with pytest.warns(RuntimeWarning, match="signal handling code.*collided"):
                assert trivial_guest_run(trio_main) == "ok"
        finally:
            assert signal.set_wakeup_fd(-1) == a.fileno()

        signal.set_wakeup_fd(a.fileno())
        try:
            with pytest.warns(RuntimeWarning, match="signal handling code.*collided"):
                assert (
                    trivial_guest_run(
                        trio_main, trust_host_loop_to_wake_on_signals=False
                    )
                    == "ok"
                )
        finally:
            assert signal.set_wakeup_fd(-1) == a.fileno()

        # Don't warn if there isn't already a wakeup fd
        with pytest.warns(None) as record:
            assert trivial_guest_run(trio_main) == "ok"
        # Apparently this is how you assert 'there were no RuntimeWarnings'
        with pytest.raises(AssertionError):
            record.pop(RuntimeWarning)

        with pytest.warns(None) as record:
            assert (
                trivial_guest_run(trio_main, trust_host_loop_to_wake_on_signals=True)
                == "ok"
            )
        with pytest.raises(AssertionError):
            record.pop(RuntimeWarning)

        # If there's already a wakeup fd, but we've been told to trust it,
        # then it's left alone and there's no warning
        signal.set_wakeup_fd(a.fileno())
        try:

            async def trio_check_wakeup_fd_unaltered(in_host):
                fd = signal.set_wakeup_fd(-1)
                assert fd == a.fileno()
                signal.set_wakeup_fd(fd)
                return "ok"

            with pytest.warns(None) as record:
                assert (
                    trivial_guest_run(
                        trio_check_wakeup_fd_unaltered,
                        trust_host_loop_to_wake_on_signals=True,
                    )
                    == "ok"
                )
            with pytest.raises(AssertionError):
                record.pop(RuntimeWarning)
        finally:
            assert signal.set_wakeup_fd(-1) == a.fileno()


def test_host_wakeup_doesnt_trigger_wait_all_tasks_blocked():
    # This is designed to hit the branch in unrolled_run where:
    #   idle_primed=True
    #   runner.runq is empty
    #   events is Truth-y
    # ...and confirm that in this case, wait_all_tasks_blocked does not get
    # triggered.
    def set_deadline(cscope, new_deadline):
        print(f"setting deadline {new_deadline}")
        cscope.deadline = new_deadline

    async def trio_main(in_host):
        async def sit_in_wait_all_tasks_blocked(watb_cscope):
            with watb_cscope:
                # Overall point of this test is that this
                # wait_all_tasks_blocked should *not* return normally, but
                # only by cancellation.
                await trio.testing.wait_all_tasks_blocked(cushion=9999)
                assert False  # pragma: no cover
            assert watb_cscope.cancelled_caught

        async def get_woken_by_host_deadline(watb_cscope):
            with trio.CancelScope() as cscope:
                print("scheduling stuff to happen")
                # Altering the deadline from the host, to something in the
                # future, will cause the run loop to wake up, but then
                # discover that there is nothing to do and go back to sleep.
                # This should *not* trigger wait_all_tasks_blocked.
                #
                # So the 'before_io_wait' here will wait until we're blocking
                # with the wait_all_tasks_blocked primed, and then schedule a
                # deadline change. The critical test is that this should *not*
                # wake up 'sit_in_wait_all_tasks_blocked'.
                #
                # The after we've had a chance to wake up
                # 'sit_in_wait_all_tasks_blocked', we want the test to
                # actually end. So in after_io_wait we schedule a second host
                # call to tear things down.
                class InstrumentHelper:
                    def __init__(self):
                        self.primed = False

                    def before_io_wait(self, timeout):
                        print(f"before_io_wait({timeout})")
                        if timeout == 9999:
                            assert not self.primed
                            in_host(lambda: set_deadline(cscope, 1e9))
                            self.primed = True

                    def after_io_wait(self, timeout):
                        if self.primed:
                            print("instrument triggered")
                            in_host(lambda: cscope.cancel())
                            trio.lowlevel.remove_instrument(self)

                trio.lowlevel.add_instrument(InstrumentHelper())
                await trio.sleep_forever()
            assert cscope.cancelled_caught
            watb_cscope.cancel()

        async with trio.open_nursery() as nursery:
            watb_cscope = trio.CancelScope()
            nursery.start_soon(sit_in_wait_all_tasks_blocked, watb_cscope)
            await trio.testing.wait_all_tasks_blocked()
            nursery.start_soon(get_woken_by_host_deadline, watb_cscope)

        return "ok"

    assert trivial_guest_run(trio_main) == "ok"


def test_guest_warns_if_abandoned():
    # This warning is emitted from the garbage collector. So we have to make
    # sure that our abandoned run is garbage. The easiest way to do this is to
    # put it into a function, so that we're sure all the local state,
    # traceback frames, etc. are garbage once it returns.
    def do_abandoned_guest_run():
        async def abandoned_main(in_host):
            in_host(lambda: 1 / 0)
            while True:
                await trio.sleep(0)

        with pytest.raises(ZeroDivisionError):
            trivial_guest_run(abandoned_main)

    with pytest.warns(RuntimeWarning, match="Trio guest run got abandoned"):
        do_abandoned_guest_run()
        gc_collect_harder()

        # If you have problems some day figuring out what's holding onto a
        # reference to the unrolled_run generator and making this test fail,
        # then this might be useful to help track it down. (It assumes you
        # also hack start_guest_run so that it does 'global W; W =
        # weakref(unrolled_run_gen)'.)
        #
        # import gc
        # print(trio._core._run.W)
        # targets = [trio._core._run.W()]
        # for i in range(15):
        #     new_targets = []
        #     for target in targets:
        #         new_targets += gc.get_referrers(target)
        #         new_targets.remove(targets)
        #     print("#####################")
        #     print(f"depth {i}: {len(new_targets)}")
        #     print(new_targets)
        #     targets = new_targets

        with pytest.raises(RuntimeError):
            trio.current_time()


def aiotrio_run(trio_fn, **start_guest_run_kwargs):
    loop = asyncio.new_event_loop()

    async def aio_main():
        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):
            print(f"trio_fn finished: {main_outcome!r}")
            trio_done_fut.set_result(main_outcome)

        trio.lowlevel.start_guest_run(
            trio_fn,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
            **start_guest_run_kwargs,
        )

        return (await trio_done_fut).unwrap()

    try:
        return loop.run_until_complete(aio_main())
    finally:
        loop.close()


def test_guest_mode_on_asyncio():
    async def trio_main():
        print("trio_main!")

        to_trio, from_aio = trio.open_memory_channel(float("inf"))
        from_trio = asyncio.Queue()

        aio_task = asyncio.ensure_future(aio_pingpong(from_trio, to_trio))

        from_trio.put_nowait(0)

        async for n in from_aio:
            print(f"trio got: {n}")
            from_trio.put_nowait(n + 1)
            if n >= 10:
                aio_task.cancel()
                return "trio-main-done"

    async def aio_pingpong(from_trio, to_trio):
        print("aio_pingpong!")

        try:
            while True:
                n = await from_trio.get()
                print(f"aio got: {n}")
                to_trio.send_nowait(n + 1)
        except asyncio.CancelledError:
            raise
        except:  # pragma: no cover
            traceback.print_exc()
            raise

    assert (
        aiotrio_run(
            trio_main,
            # Not all versions of asyncio we test on can actually be trusted,
            # but this test doesn't care about signal handling, and it's
            # easier to just avoid the warnings.
            trust_host_loop_to_wake_on_signals=True,
        )
        == "trio-main-done"
    )
