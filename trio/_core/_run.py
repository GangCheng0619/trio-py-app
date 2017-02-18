import abc
import inspect
import enum
from collections import deque
import threading
from time import monotonic
import os
import random
from contextlib import contextmanager, closing
import select
import sys
from math import inf

import attr
from sortedcontainers import SortedDict
from async_generator import async_generator, yield_

from .._util import acontextmanager

from .. import _core
from ._exceptions import (
    TrioInternalError, RunFinishedError, MultiError, Cancelled, WouldBlock
)
from ._result import Result, Error, Value
from ._traps import (
    yield_briefly_no_cancel, Abort, yield_indefinitely,
)
from ._ki import (
    LOCALS_KEY_KI_PROTECTION_ENABLED, ki_protected, ki_manager,
    enable_ki_protection
)
from ._wakeup_socketpair import WakeupSocketpair
from . import _public, _hazmat

# At the bottom of this file there's also some "clever" code that generates
# wrapper functions for runner and io manager methods, and adds them to
# __all__. These are all re-exported as part of the 'trio' or 'trio.hazmat'
# namespaces.
__all__ = ["Clock", "Task", "run", "open_nursery", "open_cancel_scope",
           "yield_briefly", "current_task", "yield_if_cancelled"]

GLOBAL_RUN_CONTEXT = threading.local()


if os.name == "nt":
    from ._io_windows import WindowsIOManager as TheIOManager
elif hasattr(select, "epoll"):
    from ._io_epoll import EpollIOManager as TheIOManager
elif hasattr(select, "kqueue"):
    from ._io_kqueue import KqueueIOManager as TheIOManager
else:  # pragma: no cover
    raise NotImplementedError("unsupported platform")


class Clock(abc.ABC):
    @abc.abstractmethod
    def current_time(self):  # pragma: no cover
        pass

    @abc.abstractmethod
    def deadline_to_sleep_time(self, deadline):  # pragma: no cover
        pass

_r = random.Random()
@attr.s(slots=True, frozen=True)
class SystemClock(Clock):
    # Add a large random offset to our clock to ensure that if people
    # accidentally call time.monotonic() directly or start comparing clocks
    # between different runs, then they'll notice the bug quickly:
    offset = attr.ib(default=attr.Factory(lambda: _r.uniform(10000, 200000)))

    def current_time(self):
        return self.offset + monotonic()

    def deadline_to_sleep_time(self, deadline):
        return deadline - self.current_time()


################################################################
# CancelScope and friends
################################################################

@attr.s(slots=True, cmp=False, hash=False)
class CancelScope:
    _tasks = attr.ib(default=attr.Factory(set))
    _effective_deadline = attr.ib(default=inf)
    _deadline = attr.ib(default=inf)
    _shield = attr.ib(default=False)
    # We want to re-use the same exception object within a given task, to get
    # complete tracebacks. This maps {task: exception} for active tasks.
    _excs = attr.ib(default=attr.Factory(dict))
    cancel_called = attr.ib(default=False)
    cancel_caught = attr.ib(default=False)

    @contextmanager
    @enable_ki_protection
    def _might_change_effective_deadline(self):
        try:
            yield
        finally:
            old = self._effective_deadline
            if self.cancel_called or not self._tasks:
                new = inf
            else:
                new = self._deadline
            if old != new:
                self._effective_deadline = new
                runner = GLOBAL_RUN_CONTEXT.runner
                if old != inf:
                    del runner.deadlines[old, id(self)]
                if new != inf:
                    runner.deadlines[new, id(self)] = self

    @property
    def deadline(self):
        return self._deadline

    @deadline.setter
    def deadline(self, new_deadline):
        with self._might_change_effective_deadline():
            self._deadline = float(new_deadline)

    @property
    def shield(self):
        return self._shield

    @shield.setter
    def shield(self, new_value):
        if not isinstance(new_value, bool):
            raise TypeError("shield must be a bool")
        self._shield = new_value
        if not self._shield:
            for task in self._tasks:
                task._attempt_delivery_of_any_pending_cancel()

    def _cancel_no_notify(self):
        # returns the affected tasks
        if not self.cancel_called:
            with self._might_change_effective_deadline():
                self.cancel_called = True
            return self._tasks
        else:
            return set()

    @enable_ki_protection
    def cancel(self):
        for task in self._cancel_no_notify():
            task._attempt_delivery_of_any_pending_cancel()

    def _add_task(self, task):
        self._tasks.add(task)
        task._cancel_stack.append(self)

    def _remove_task(self, task):
        with self._might_change_effective_deadline():
            self._tasks.remove(task)
        if task in self._excs:
            del self._excs[task]
        assert task._cancel_stack[-1] is self
        task._cancel_stack.pop()

    def _make_exc(self, task):
        if task not in self._excs:
            exc = Cancelled()
            exc._scope = self
            self._excs[task] = exc
        return self._excs[task]

    def _filter_exception(self, exc):
        if isinstance(exc, Cancelled) and exc._scope is self:
            self.cancel_caught = True
            return None
        elif isinstance(exc, MultiError):
            new_exceptions = []
            for sub_exc in exc.exceptions:
                if isinstance(sub_exc, Cancelled) and sub_exc._scope is self:
                    self.cancel_caught = True
                else:
                    new_exceptions.append(sub_exc)
            if len(new_exceptions) == 0:
                return None
            elif len(new_exceptions) == 1:
                return new_exceptions[0]
            else:
                exc.exceptions = new_exceptions
                return exc
        else:
            return exc

@contextmanager
@enable_ki_protection
def open_cancel_scope(*, deadline=inf, shield=False):
    task = _core.current_task()
    scope = CancelScope()
    scope._add_task(task)
    scope.deadline = deadline
    scope.shield = shield
    try:
        yield scope
    except (Cancelled, MultiError) as exc:
        new_exc = scope._filter_exception(exc)
        if new_exc is not None:
            raise new_exc
    finally:
        scope._remove_task(task)


################################################################
# Nursery and friends
################################################################

@acontextmanager
@async_generator
@enable_ki_protection
async def open_nursery():
    assert ki_protected()
    with open_cancel_scope() as scope:
        nursery = Nursery(current_task(), scope)
        try:
            await yield_(nursery)
        finally:
            assert ki_protected()
            exceptions = []
            _, exc, _ = sys.exc_info()
            if exc is not None:
                exceptions.append(exc)
            await nursery._clean_up(exceptions)

class Nursery:
    def __init__(self, parent, cancel_scope):
        # the parent task -- only used for introspection, to implement
        # task.parent_task
        self._parent = parent
        # the cancel stack that children inherit
        self._cancel_stack = list(parent._cancel_stack)
        # the cancel scope that directly surrounds us; used for cancelling all
        # children.
        self.cancel_scope = cancel_scope
        assert self.cancel_scope is self._cancel_stack[-1]
        self._children = set()
        self._zombies = set()
        self.monitor = _core.UnboundedQueue()
        self._closed = False

    @property
    def children(self):
        return frozenset(self._children)

    @property
    def zombies(self):
        return frozenset(self._zombies)

    def _child_finished(self, task):
        self._children.remove(task)
        self._zombies.add(task)
        self.monitor.put_nowait(task)

    def spawn(self, fn, *args):
        return GLOBAL_RUN_CONTEXT.runner.spawn_impl(fn, args, self)

    def reap(self, task):
        try:
            self._zombies.remove(task)
        except KeyError:
            raise ValueError("{} is not a zombie in this nursery".format(task))

    def reap_and_unwrap(self, task):
        self.reap(task)
        return task.result.unwrap()

    async def _clean_up(self, exceptions):
        cancelled_children = False
        # Careful - the logic in this loop is deceptively subtle, because of
        # all the different possible states that we have to handle. (Entering
        # with/out an error, with/out unreaped zombies, with/out children
        # living, with/out an error that occurs after we enter, ...)
        with open_cancel_scope() as scope:
            while self._children or self._zombies:
                # First, reap any zombies. They may or may not still be in the
                # monitor queue, and they may or may not trigger cancellation
                # of remaining tasks, so we have to check first before
                # blocking on the monitor queue.
                for task in list(self._zombies):
                    if type(task.result) is Error:
                        exceptions.append(task.result.error)
                    self.reap(task)

                if exceptions and not cancelled_children:
                    self.cancel_scope.cancel()
                    cancelled_children = True

                if self.children:
                    try:
                        # We ignore the return value here, and will pick up
                        # the actual tasks from the zombies set after looping
                        # around. (E.g. it's possible there are tasks in the
                        # queue that were already reaped.)
                        await self.monitor.get_all()
                    except Cancelled as exc:
                        exceptions.append(exc)
                        # After we catch Cancelled once, mute it
                        scope.shield = True
                    except KeyboardInterrupt as exc:
                        exceptions.append(exc)
                    except BaseException as exc:  # pragma: no cover
                        raise TrioInternalError from exc

        self._closed = True
        if exceptions:
            raise MultiError(exceptions)

    def __del__(self):
        assert not self.children and not self.zombies


################################################################
# Task and friends
################################################################

@attr.s(slots=True, cmp=False, hash=False, repr=False)
class Task:
    _nursery = attr.ib()
    coro = attr.ib()
    _runner = attr.ib()
    result = attr.ib(default=None)
    # tasks start out unscheduled, and unscheduled tasks have None here
    _next_send = attr.ib(default=None)
    _abort_func = attr.ib(default=None)

    def __repr__(self):
        return "<Task with coro={!r}>".format(self.coro)

    # For debugging and visualization:
    @property
    def parent_task(self):
        return self._nursery._parent

    ################
    # Monitoring task exit
    ################

    _monitors = attr.ib(default=attr.Factory(set))

    def add_monitor(self, queue):
        # Rationale: (a) don't particularly want to create a
        # callback-in-disguise API by allowing people to stick in some
        # arbitrary object with a put_nowait method, (b) don't want to have to
        # figure out how to deal with errors from a user-provided object; if
        # UnboundedQueue.put_nowait raises then that's legitimately a bug in
        # trio so raising InternalError is justified.
        if type(queue) is not _core.UnboundedQueue:
            raise TypeError("monitor must be an UnboundedQueue object")
        if queue in self._monitors:
            raise ValueError("can't add same monitor twice")
        if self.result is not None:
            queue.put_nowait(self)
        else:
            self._monitors.add(queue)

    def discard_monitor(self, queue):
        self._monitors.discard(queue)

    async def wait(self):
        q = _core.UnboundedQueue()
        self.add_monitor(q)
        try:
            await q.get_all()
        finally:
            self.discard_monitor(q)

    ################
    # Cancellation
    ################

    _cancel_stack = attr.ib(default=attr.Factory(list), repr=False)

    def _pending_cancel_scope(self):
        # Return the outermost exception that is is not outside a shield.
        pending_scope = None
        for scope in self._cancel_stack:
            # Check shield before _exc, because shield should not block
            # processing of *this* scope's exception
            if scope.shield:
                pending_scope = None
            if pending_scope is None and scope.cancel_called:
                pending_scope = scope
        return pending_scope

    def _attempt_abort(self, raise_cancel):
        # Either the abort succeeds, in which case we will reschedule the
        # task, or else it fails, in which case it will worry about
        # rescheduling itself (hopefully eventually calling reraise to raise
        # the given exception, but not necessarily).
        success = self._abort_func(raise_cancel)
        if type(success) is not _core.Abort:
            raise TrioInternalError("abort function must return Abort enum")
        # We only attempt to abort once per blocking call, regardless of
        # whether we succeeded or failed.
        self._abort_func = None
        if success is Abort.SUCCEEDED:
            self._runner.reschedule(self, Result.capture(raise_cancel))

    def _attempt_delivery_of_any_pending_cancel(self):
        if self._abort_func is None:
            return
        pending_scope = self._pending_cancel_scope()
        if pending_scope is None:
            return
        exc = pending_scope._make_exc(self)
        def raise_cancel():
            raise exc
        self._attempt_abort(raise_cancel)

    def _attempt_delivery_of_pending_ki(self):
        assert self._runner.ki_pending
        if self._abort_func is None:
            return
        def raise_cancel():
            self._runner.ki_pending = False
            raise KeyboardInterrupt
        self._attempt_abort(raise_cancel)

################################################################
# The central Runner object
################################################################

@attr.s(frozen=True)
class _RunStatistics:
    tasks_living = attr.ib()
    tasks_runnable = attr.ib()
    seconds_to_next_deadline = attr.ib()
    io_statistics = attr.ib()
    call_soon_queue_size = attr.ib()

@attr.s(cmp=False, hash=False)
class Runner:
    clock = attr.ib()
    instruments = attr.ib()
    io_manager = attr.ib()

    runq = attr.ib(default=attr.Factory(deque))
    tasks = attr.ib(default=attr.Factory(set))
    r = attr.ib(default=attr.Factory(random.Random))

    # {(deadline, id(CancelScope)): CancelScope}
    # only contains scopes with non-infinite deadlines that are currently
    # attached to at least one task
    deadlines = attr.ib(default=attr.Factory(SortedDict))

    init_task = attr.ib(default=None)
    main_task = attr.ib(default=None)
    system_nursery = attr.ib(default=None)

    def close(self):
        self.io_manager.close()
        self.call_soon_wakeup.close()
        self.instrument("after_run")

    # Methods marked with @_public get converted into functions exported by
    # trio.hazmat:
    @_public
    def current_statistics(self):
        if self.deadlines:
            next_deadline, _ = self.deadlines.keys()[0]
            seconds_to_next_deadline = next_deadline - self.current_time()
        else:
            seconds_to_next_deadline = float("inf")
        return _RunStatistics(
            tasks_living=len(self.tasks),
            tasks_runnable=len(self.runq),
            seconds_to_next_deadline=seconds_to_next_deadline,
            io_statistics=self.io_manager.statistics(),
            call_soon_queue_size=
              len(self.call_soon_queue) + len(self.call_soon_idempotent_queue),
        )

    @_public
    def current_time(self):
        return self.clock.current_time()

    ################
    # Core task handling primitives
    ################

    @_public
    @_hazmat
    def reschedule(self, task, next_send=Value(None)):
        assert task._runner is self
        assert task._next_send is None
        task._next_send = next_send
        task._abort_func = None
        self.runq.append(task)
        self.instrument("task_scheduled", task)

    def spawn_impl(self, fn, args, nursery, *, ki_protection_enabled=False):
        # This sorta feels like it should be a method on nursery, except it
        # has to handle nursery=None for init. And it touches the internals of
        # all kinds of objects.
        if nursery is not None and nursery._closed:
            raise RuntimeError("Nursery is closed to new arrivals")
        if nursery is None:
            assert self.init_task is None
        coro = fn(*args)
        if not inspect.iscoroutine(coro):
            raise TypeError("spawn expected an async function")
        task = Task(coro=coro, nursery=nursery, runner=self)
        self.tasks.add(task)
        if nursery is not None:
            nursery._children.add(task)
            for scope in nursery._cancel_stack:
                scope._add_task(task)
        coro.cr_frame.f_locals.setdefault(
            LOCALS_KEY_KI_PROTECTION_ENABLED, ki_protection_enabled)
        # Special case: normally next_send should be a Result, but for the
        # very first send we have to send a literal unboxed None.
        self.reschedule(task, None)
        return task

    def task_finished(self, task, result):
        task.result = result
        while task._cancel_stack:
            task._cancel_stack[-1]._remove_task(task)
        self.tasks.remove(task)
        if task._nursery is None:
            # the init task should be the last task to exit
            assert not self.tasks
        else:
            task._nursery._child_finished(task)
        for monitor in task._monitors:
            monitor.put_nowait(task)
        task._monitors.clear()

    ################
    # System tasks and init
    ################

    @_public
    @_hazmat
    def spawn_system_task(self, fn, *args):
        async def system_task_wrapper(fn, args):
            try:
                await fn(*args)
            # XX: this is not really correct in the presence of MultiError
            # (basically we should do this filtering on each error inside the
            # MultiError)
            except (Cancelled, KeyboardInterrupt, GeneratorExit,
                    TrioInternalError):
                raise
            except BaseException as exc:
                raise TrioInternalError from exc
        return self.spawn_impl(
            system_task_wrapper, (fn, args), self.system_nursery,
            ki_protection_enabled=True)

    async def init(self, fn, args):
        async with open_nursery() as system_nursery:
            self.system_nursery = system_nursery
            self.spawn_system_task(self.call_soon_task)
            self.main_task = system_nursery.spawn(fn, *args)
            async for task_batch in system_nursery.monitor:
                for task in task_batch:
                    if task is self.main_task:
                        system_nursery.cancel_scope.cancel()
                        return system_nursery.reap_and_unwrap(task)
                    else:
                        system_nursery.reap_and_unwrap(task)

    ################
    # Outside Context Problems
    ################

    # XX factor this chunk into another file

    # This used to use a queue.Queue. but that was broken, because Queues are
    # implemented in Python, and not reentrant -- so it was thread-safe, but
    # not signal-safe. deque is implemented in C, so each operation is atomic
    # WRT threads (and this is guaranteed in the docs), AND each operation is
    # atomic WRT signal delivery (signal handlers can run on either side, but
    # not *during* a deque operation). dict makes similar guarantees - and on
    # CPython 3.6 and PyPy, it's even ordered!
    call_soon_wakeup = attr.ib(default=attr.Factory(WakeupSocketpair))
    call_soon_queue = attr.ib(default=attr.Factory(deque))
    call_soon_idempotent_queue = attr.ib(default=attr.Factory(dict))
    call_soon_done = attr.ib(default=False)
    # Must be a reentrant lock, because it's acquired from signal
    # handlers. RLock is signal-safe as of cpython 3.2.
    # NB that this does mean that the lock is effectively *disabled* when we
    # enter from signal context. The way we use the lock this is OK though,
    # because when call_soon_thread_and_signal_safe is called from a signal
    # it's atomic WRT the main thread -- it just might happen at some
    # inconvenient place. But if you look at the one place where the main
    # thread holds the lock, it's just to make 1 assignment, so that's atomic
    # WRT a signal anyway.
    call_soon_lock = attr.ib(default=attr.Factory(threading.RLock))

    def call_soon_thread_and_signal_safe(self, fn, *args, idempotent=False):
        with self.call_soon_lock:
            if self.call_soon_done:
                raise RunFinishedError("run() has exited")
            # We have to hold the lock all the way through here, because
            # otherwise the main thread might exit *while* we're doing these
            # calls, and then our queue item might not be processed, or the
            # wakeup call might trigger an OSError b/c the IO manager has
            # already been shut down.
            if idempotent:
                self.call_soon_idempotent_queue[(fn, args)] = None
            else:
                self.call_soon_queue.append((fn, args))
            self.call_soon_wakeup.wakeup_thread_and_signal_safe()

    @_public
    @_hazmat
    def current_call_soon_thread_and_signal_safe(self):
        return self.call_soon_thread_and_signal_safe

    async def call_soon_task(self):
        assert ki_protected()
        # RLock has two implementations: a signal-safe version in _thread, and
        # and signal-UNsafe version in threading. We need the signal safe
        # version. Python 3.2 and later should always use this anyway, but,
        # since the symptoms if this goes wrong are just "weird rare
        # deadlocks", then let's make a little check.
        # See:
        #     https://bugs.python.org/issue13697#msg237140
        assert self.call_soon_lock.__class__.__module__ == "_thread"

        def run_cb(job):
            # We run this with KI protection enabled; it's the callbacks
            # job to disable it if it wants it disabled. Exceptions are
            # treated like system task exceptions (i.e., converted into
            # TrioInternalError and cause everything to shut down).
            fn, args = job
            try:
                fn(*args)
            except BaseException as exc:
                async def kill_everything(exc):
                    raise exc
                self.spawn_system_task(kill_everything, exc)
            return True

        # This has to be carefully written to be safe in the face of new items
        # being queued while we iterate, and to do a bounded amount of work on
        # each pass:
        def run_all_bounded():
            for _ in range(len(self.call_soon_queue)):
                run_cb(self.call_soon_queue.popleft())
            for job in list(self.call_soon_idempotent_queue):
                del self.call_soon_idempotent_queue[job]
                run_cb(job)

        try:
            while True:
                run_all_bounded()
                if (not self.call_soon_queue
                      and not self.call_soon_idempotent_queue):
                    await self.call_soon_wakeup.wait_woken()
                else:
                    await yield_briefly()
        except Cancelled:
            # Keep the work done with this lock held as minimal as possible,
            # because it doesn't protect us against concurrent signal delivery
            # (see the comment above). Notice that this could would still be
            # correct if written like:
            #   self.call_soon_done = True
            #   with self.call_soon_lock:
            #       pass
            # because all we want is to force call_soon_thread_and_signal_safe
            # to either be completely before or completely after the write to
            # call_soon_done. That's why we don't need the lock to protect
            # against signal handlers.
            with self.call_soon_lock:
                self.call_soon_done = True
            # No more jobs will be submitted, so just clear out any residual
            # ones:
            run_all_bounded()
            assert not self.call_soon_queue
            assert not self.call_soon_idempotent_queue

    ################
    # KI handling
    ################

    ki_pending = attr.ib(default=False)

    # This gets called from signal context
    def deliver_ki(self):
        self.ki_pending = True
        try:
            self.call_soon_thread_and_signal_safe(self._deliver_ki_cb)
        except RunFinishedError:
            pass

    def _deliver_ki_cb(self):
        if not self.ki_pending:
            return
        # Can't happen because main_task and call_soon_task are created at the
        # same time -- so even if KI arrives before main_task is created, we
        # won't get here until afterwards.
        assert self.main_task is not None
        if self.main_task.result is not None:
            # We're already in the process of exiting -- leave ki_pending set
            # and we'll check it again on our way out of run().
            return
        self.main_task._attempt_delivery_of_pending_ki()

    ################
    # Quiescing
    ################

    waiting_for_idle = attr.ib(default=attr.Factory(set))

    @_public
    @_hazmat
    async def wait_run_loop_idle(self):
        task = current_task()
        self.waiting_for_idle.add(task)
        def abort(_):
            self.waiting_for_idle.remove(task)
            return Abort.SUCCEEDED
        await yield_indefinitely(abort)

    ################
    # Instrumentation
    ################

    def instrument(self, method_name, *args):
        for instrument in list(self.instruments):
            try:
                method = getattr(instrument, method_name)
            except AttributeError:
                continue
            try:
                method(*args)
            except:
                self.instruments.remove(instrument)
                sys.stderr.write(
                    "Exception raised when calling {!r} on instrument {!r}\n"
                    .format(method_name, instrument))
                sys.excepthook(*sys.exc_info())
                sys.stderr.write("Instrument has been disabled.\n")

    @_public
    def current_instruments(self):
        return self.instruments


################################################################
# run
################################################################

def run(fn, *args, clock=None, instruments=[]):
    # Do error-checking up front, before we enter the TrioInternalError
    # try/catch
    #
    # It wouldn't be *hard* to support nested calls to run(), but I can't
    # think of a single good reason for it, so let's be conservative for
    # now:
    if hasattr(GLOBAL_RUN_CONTEXT, "runner"):
        raise RuntimeError("Attempted to call run() from inside a run()")

    if clock is None:
        clock = SystemClock()
    instruments = list(instruments)
    io_manager = TheIOManager()
    runner = Runner(clock=clock, instruments=instruments, io_manager=io_manager)
    GLOBAL_RUN_CONTEXT.runner = runner
    locals()[LOCALS_KEY_KI_PROTECTION_ENABLED] = True

    # KI handling goes outside the core try/except/finally to avoid a window
    # where KeyboardInterrupt would be allowed and converted into an
    # TrioInternalError:
    try:
        with ki_manager(runner.deliver_ki):
            try:
                with closing(runner):
                    # The main reason this is split off into its own function
                    # is just to get rid of this extra indentation.
                    result = run_impl(runner, fn, args)
            except BaseException as exc:
                raise TrioInternalError(
                    "internal error in trio - please file a bug!") from exc
            finally:
                GLOBAL_RUN_CONTEXT.__dict__.clear()
            return result.unwrap()
    finally:
        # To guarantee that we never swallow a KeyboardInterrupt, we have to
        # check for pending ones once more after leaving the context manager:
        if runner.ki_pending:
            # Implicitly chains with any exception from result.unwrap():
            raise KeyboardInterrupt

# 24 hours is arbitrary, but it avoids issues like people setting timeouts of
# 10**20 and then getting integer overflows in the underlying system calls.
_MAX_TIMEOUT = 24 * 60 * 60

def run_impl(runner, fn, args):
    runner.instrument("before_run")
    runner.init_task = runner.spawn_impl(
        runner.init, (fn, args), None, ki_protection_enabled=True)

    while runner.tasks:
        if runner.runq or runner.waiting_for_idle:
            timeout = 0
        elif runner.deadlines:
            deadline, _ = runner.deadlines.keys()[0]
            timeout = runner.clock.deadline_to_sleep_time(deadline)
        else:
            timeout = _MAX_TIMEOUT
        timeout = min(max(0, timeout), _MAX_TIMEOUT)

        runner.instrument("before_io_wait", timeout)
        runner.io_manager.handle_io(timeout)
        runner.instrument("after_io_wait", timeout)

        now = runner.clock.current_time()
        # We process all timeouts in a batch and then notify tasks at the end
        # to ensure that if multiple timeouts occur at once, then it's the
        # outermost one that gets delivered.
        cancelled_tasks = set()
        while runner.deadlines:
            (deadline, _), cancel_scope = runner.deadlines.peekitem(0)
            if deadline <= now:
                # This removes the given scope from runner.deadlines:
                cancelled_tasks.update(cancel_scope._cancel_no_notify())
            else:
                break
        for task in cancelled_tasks:
            task._attempt_delivery_of_any_pending_cancel()

        if not runner.runq:
            while runner.waiting_for_idle:
                runner.reschedule(runner.waiting_for_idle.pop())

        # Process all runnable tasks, but only the ones that are already
        # runnable now. Anything that becomes runnable during this cycle needs
        # to wait until the next pass. This avoids various starvation issues
        # by ensuring that there's never an unbounded delay between successive
        # checks for I/O.
        #
        # Also, we randomize the order of each batch to avoid assumptions
        # about scheduling order sneaking in. In the long run, I suspect we'll
        # either (a) use strict FIFO ordering and document that for
        # predictability/determinism, or (b) implement a more sophisticated
        # scheduler (e.g. some variant of fair queueing), for better behavior
        # under load. For now, this is the worst of both worlds - but it keeps
        # our options open. (If we do decide to go all in on deterministic
        # scheduling, then there are other things that will probably need to
        # change too, like the deadlines tie-breaker and the non-deterministic
        # ordering of task._notify_queues.)
        batch = list(runner.runq)
        runner.runq.clear()
        runner.r.shuffle(batch)
        while batch:
            task = batch.pop()
            GLOBAL_RUN_CONTEXT.task = task
            runner.instrument("before_task_step", task)

            next_send = task._next_send
            task._next_send = None
            final_result = None
            try:
                # We used to unwrap the Result object here and send/throw its
                # contents in directly, but it turns out that .throw() is
                # buggy, at least on CPython 3.6 and earlier:
                #   https://bugs.python.org/issue29587
                #   https://bugs.python.org/issue29590
                # So now we send in the Result object and unwrap it on the
                # other side.
                msg = task.coro.send(next_send)
            except StopIteration as stop_iteration:
                final_result = Value(stop_iteration.value)
            except BaseException as task_exc:
                final_result = Error(task_exc)

            if final_result is not None:
                # We can't call this directly inside the except: blocks above,
                # because then the exceptions end up attaching themselves to
                # other exceptions as __context__ in unwanted ways.
                runner.task_finished(task, final_result)
            else:
                yield_fn, *args = msg
                if yield_fn is yield_briefly_no_cancel:
                    runner.reschedule(task)
                else:
                    assert yield_fn is yield_indefinitely
                    task._abort_func, = args
                    task._attempt_delivery_of_any_pending_cancel()
                    if runner.ki_pending and task is runner.main_task:
                        task._attempt_delivery_of_pending_ki()

            runner.instrument("after_task_step", task)
            del GLOBAL_RUN_CONTEXT.task

    return runner.init_task.result


################################################################
# Other public API functions
################################################################

def current_task():
    return GLOBAL_RUN_CONTEXT.task

@_hazmat
async def yield_briefly():
    with open_cancel_scope(deadline=-inf) as scope:
        await _core.yield_indefinitely(lambda _: _core.Abort.SUCCEEDED)

@_hazmat
async def yield_if_cancelled():
    task = current_task()
    if (task._pending_cancel_scope() is not None
          or (task is task._runner.main_task and task._runner.ki_pending)):
        await _core.yield_briefly()
        assert False  # pragma: no cover


_WRAPPER_TEMPLATE = """
def wrapper(*args, **kwargs):
    locals()[LOCALS_KEY_KI_PROTECTION_ENABLED] = True
    try:
        meth = GLOBAL_RUN_CONTEXT.{}.{}
    except AttributeError:
        raise RuntimeError("must be called from async context")
    return meth(*args, **kwargs)
"""

def _generate_method_wrappers(cls, path_to_instance):
    for methname, fn in cls.__dict__.items():
        if callable(fn) and getattr(fn, "_public", False):
            # Create a wrapper function that looks up this method in the
            # current thread-local context version of this object, and calls
            # it. exec() is a bit ugly but the resulting code is faster and
            # simpler than doing some loop over getattr.
            ns = {"GLOBAL_RUN_CONTEXT": GLOBAL_RUN_CONTEXT,
                  "LOCALS_KEY_KI_PROTECTION_ENABLED":
                      LOCALS_KEY_KI_PROTECTION_ENABLED}
            exec(_WRAPPER_TEMPLATE.format(path_to_instance, methname), ns)
            wrapper = ns["wrapper"]
            # 'fn' is the *unbound* version of the method, but our exported
            # function has the same API as the *bound* version of the
            # method. So create a dummy bound method object:
            from types import MethodType
            bound_fn = MethodType(fn, object())
            # Then set exported function's metadata to match it:
            from functools import update_wrapper
            update_wrapper(wrapper, bound_fn)
            # And finally export it:
            globals()[methname] = wrapper
            __all__.append(methname)

_generate_method_wrappers(Runner, "runner")
_generate_method_wrappers(TheIOManager, "runner.io_manager")
