import logging
import types
import attr
from typing import Any, Callable, Dict, List, Sequence, Iterator, TypeVar

from .._abc import Instrument

# Used to log exceptions in instruments
INSTRUMENT_LOGGER = logging.getLogger("trio.abc.Instrument")


F = TypeVar("F", bound=Callable[..., Any])

# Decorator to mark methods public. This does nothing by itself, but
# trio/_tools/gen_exports.py looks for it.
def _public(fn: F) -> F:
    return fn


HookImpl = Callable[..., Any]


class Hook(Dict[Instrument, HookImpl]):
    """Manages installed instruments for a single hook such as before_run().

    The base dictionary maps each instrument to the method of that
    instrument that will be called when the hook is invoked. We use
    inheritance so that 'if hook:' is fast (no Python-level function
    calls needed).

    """

    __slots__ = ("_name", "_parent", "_in_call")

    def __init__(self, name: str, parent: "Instruments"):
        self._name = name  # "before_run" or similar
        self._parent = parent
        self._in_call = 0

    def __call__(self, *args: Any):
        """Invoke the instrumentation hook with the given arguments."""
        self._in_call += 1
        try:
            for instrument, method in self.items():
                try:
                    method(*args)
                except:
                    self._parent.remove_instrument(instrument)
                    INSTRUMENT_LOGGER.exception(
                        "Exception raised when calling %r on instrument %r. "
                        "Instrument has been disabled.",
                        self._name,
                        instrument,
                    )
        finally:
            self._in_call -= 1

    def as_mutable(self) -> "Hook":
        """Return a Hook object to which any desired modifications should be made.

        If this Hook is not in the middle of a call, it can be safely
        mutated, and as_mutable() just returns self. If this Hook is
        in the middle of a call, though, any mutation will cause the
        call to raise a concurrent modification error. To handle the
        latter case, we replace this Hook with a copy in our parent
        Instruments collection, and return that copy.
        """

        if self._in_call:
            # We're in the middle of a call on this hook, so
            # we must replace it with a copy in order to avoid
            # a "dict changed size during iteration" error.
            replacement = Hook(self._name, self._parent)
            replacement.update(self)
            setattr(self._parent, self._name, replacement)
            return replacement
        return self


class Instruments(Instrument):
    """A collection of `trio.abc.Instrument` with some optimizations.

    Instrumentation calls are rather expensive, and we don't want a
    rarely-used instrument (like before_run()) to slow down hot
    operations (like before_task_step()). Thus, we cache the set of
    handlers to be called for each hook, and skip the instrumentation
    call if there's nothing currently installed for that hook.

    This inherits from `trio.abc.Instrument` for the benefit of
    static type checking (to make sure you pass the right arguments
    when calling an instrument). All of the class-level function
    definitions are shadowed by instance-level Hooks.
    """

    # One Hook per instrument, with its same name
    __slots__ = [name for name in Instrument.__dict__ if not name.startswith("_")]

    # Maps each installed instrument to the list of hook names that it implements.
    _instruments: Dict[Instrument, List[str]]
    __slots__.append("_instruments")

    def __init__(self, incoming: Sequence[Instrument]):
        self._instruments = {}
        for name in Instruments.__slots__:
            if not hasattr(self, name):
                setattr(self, name, Hook(name, self))
        for instrument in incoming:
            self.add_instrument(instrument)

    def __bool__(self) -> bool:
        return bool(self._instruments)

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self._instruments)

    @_public
    def add_instrument(self, instrument: Instrument) -> None:
        """Start instrumenting the current run loop with the given instrument.

        Args:
          instrument (trio.abc.Instrument): The instrument to activate.

        If ``instrument`` is already active, does nothing.

        """
        if instrument in self._instruments:
            return
        hooknames = self._instruments[instrument] = []
        try:
            for name in dir(instrument):
                if name.startswith("_"):
                    continue
                try:
                    prototype = getattr(Instrument, name)
                except AttributeError:
                    continue
                impl: HookImpl = getattr(instrument, name)
                if isinstance(impl, types.MethodType) and impl.__func__ is prototype:
                    # Inherited unchanged from _abc.Instrument
                    continue
                hook: Hook = getattr(self, name).as_mutable()
                hook[instrument] = impl
                hooknames.append(name)
        except:
            self.remove_instrument(instrument)
            raise

    @_public
    def remove_instrument(self, instrument: Instrument) -> None:
        """Stop instrumenting the current run loop with the given instrument.

        Args:
          instrument (trio.abc.Instrument): The instrument to de-activate.

        Raises:
          KeyError: if the instrument is not currently active. This could
              occur either because you never added it, or because you added it
              and then it raised an unhandled exception and was automatically
              deactivated.

        """
        # If instrument isn't present, the KeyError propagates out
        hooknames = self._instruments.pop(instrument)
        for name in hooknames:
            hook: Hook = getattr(self, name).as_mutable()
            del hook[instrument]
