# Use SSLObject to make a generic wrapper around Stream

# SSL shutdown:
# - call unwrap() on the SSLSocket/SSLObject
# - this sends the "all done here" SSL message
# - but in many practical applications this is neither sent nor checked for,
#   e.g. HTTPS usually ignores it:
#   https://security.stackexchange.com/questions/82028/ssl-tls-is-a-server-always-required-to-respond-to-a-close-notify
#   BUT it is important in some cases, so should be possible to handle
#   properly.
#
# I think the answer is: close is synchronous, and the TLS Stream also has an
# async def unwrap() which sends the close_notify message.
# Possibly we should also default suppress_ragged_eofs to False, unlike the
# stdlib? not sure.

# XX how closely should we match the stdlib API?
# - maybe suppress_ragged_eofs=False is a better default?
# - maybe check crypto folks for advice?
# - this is also interesting: https://bugs.python.org/issue8108#msg102867

# Definitely keep an eye on Cory's TLS API ideas on security-sig etc.

# XX document behavior on cancellation/error (i.e.: all is lost abandon
# stream)

import ssl as _stdlib_ssl

from . import _core
from . import _streams
from . import _sync

__all__ = ["SSLStream"]

def _reexport(name):
    globals()[name] = getattr(_stdlib_ssl, name)
    __all__.append(name)

for _name in [
        "SSLError", "SSLZeroReturnError", "SSLSyscallError", "SSLEOFError",
        "CertificateError", "create_default_context", "match_hostname",
        "cert_time_to_seconds", "DER_cert_to_PEM_cert", "PEM_cert_to_DER_cert",
        "get_default_verify_paths", "SSLContext", "Purpose",
]:
    _reexport(_name)


# Windows only
try:
    for _name in ["enum_certificates", "enum_crls"]:
        _reexport(_name)
except AttributeError:
    pass

try:
    # 3.6+ only:
    for _name in [
            "SSLSession", "VerifyMode", "VerifyFlags", "Options",
            "AlertDescription", "SSLErrorNumber",
    ]:
        _reexport(_name)
except AttributeError:
    pass

for _name in _stdlib_ssl.__dict__.keys():
    if _name == _name.upper():
        _reexport(_name)

# XX add suppress_ragged_eofs option?
# or maybe actually make an option that means "I want the variant of the
# protocol that doesn't do EOFs", so it ignores lack from the other side and
# also doesn't send them.

class _Once:
    def __init__(self, afn, *args):
        self._afn = afn
        self._args = args
        self._started = False
        self._done = _sync.Event()

    async def ensure(self, *, checkpoint):
        if not self._started:
            self._started = True
            await self._afn(*self._args)
            self._done.set()
        elif not checkpoint and self._done.is_set():
            return
        else:
            await self._done.wait()


class SSLStream(_streams.Stream):
    def __init__(
            self, wrapped_stream, sslcontext, *, bufsize=32 * 1024, **kwargs):
        self.wrapped_stream = wrapped_stream
        self._bufsize = bufsize
        self._outgoing = _stdlib_ssl.MemoryBIO()
        self._incoming = _stdlib_ssl.MemoryBIO()
        self._ssl_object = sslcontext.wrap_bio(
            self._incoming, self._outgoing, **kwargs)
        self._send_lock = _sync.Lock()
        self._recv_count = 0
        self._recv_lock = _sync.Lock()
        self._handshook = _Once(self._do_handshake)

    _forwarded = {
        "context", "server_side", "server_hostname", "session",
        "session_reused", "getpeercert", "selected_npn_protocol", "cipher",
        "shared_ciphers", "compression", "pending", "get_channel_binding",
        "selected_alpn_protocol", "version",
    }
    def __getattr__(self, name):
        if name in self._forwarded:
            return getattr(self._ssl_object, name)
        else:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self._forwarded:
            setattr(self._ssl_object, name, value)
        else:
            super().__setattr__(name, value)

    def __dir__(self):
        return super().__dir__() + list(self._forwarded)

    can_send_eof = False

    async def send_eof(self):
        raise RuntimeError("the TLS protocol does not support send_eof")

    async def wait_writable(self):
        await self.wrapped_stream.wait_writable()

    async def _retry(self, fn, *args):
        await _core.yield_if_cancelled()
        yielded = False
        try:
            finished = False
            while not finished:
                want_read = False
                try:
                    ret = fn(*args)
                except _stdlib_ssl.SSLWantReadError:
                    want_read = True
                # SSLWantWriteError can't happen – "Writes to memory BIOs will
                # always succeed if memory is available: that is their size
                # can grow indefinitely."
                # https://wiki.openssl.org/index.php/Manual:BIO_s_mem(3)
                else:
                    finished = True
                recv_count = self._recv_count
                if self._outgoing.pending:
                    # We pull the data out eagerly, so that in the common case
                    # of simultaneous sendall() and recv(), sendall() doesn't
                    # leave data in self._outgoing over a schedule point and
                    # trick recv() into thinking that it has data to
                    # send. This relies on the fairness of send_lock for
                    # correctness, to make sure that 'data' chunks don't get
                    # re-ordered.
                    data = self._outgoing.read()
                    async with self._send_lock:
                        await self.wrapped_stream.sendall(data)
                        yielded = True
                if want_read and recv_count == self._recv_count:
                    async with self._recv_lock:
                        if recv_count == self._recv_count:
                            data = await self.wrapped_stream.recv(self._bufsize)
                            yielded = True
                            if not data:
                                self._incoming.write_eof()
                            else:
                                self._incoming.write(data)
                            recv_count += 1

            return ret
        finally:
            if not yielded:
                await _core.yield_briefly_no_cancel()

    async def _do_handshake(self):
        await self._retry(self._ssl_object.do_handshake)

    async def do_handshake(self):
        await self._handshook.ensure(checkpoint=True)

    async def recv(self, bufsize):
        await self._handshook.ensure(checkpoint=False)
        return await self._retry(self._ssl_object.read, bufsize)

    async def sendall(self, data):
        await self._handshook.ensure(checkpoint=False)
        return await self._retry(self._ssl_object.write, data)

    # This doesn't work right because it loops one time too many...
    # and what happens if more legitimate data arrives after we send the
    # shutdown request? do we really support send_eof after all? or does
    # openssl stop us from sending stuff after receiving a shutdown request?
    # XX need to experiment.
    # See here:
    #    https://wiki.openssl.org/index.php/Manual:SSL_shutdown(3)
    # it sounds like the rule is it should be called exactly twice (or maybe
    # exactly once if the other side already send a close_notify?), and that
    # once a close_notify is sent/received then openssl makes it impossible to
    # send/receive anything else.
    async def unwrap(self):
        await self._handshook.ensure(checkpoint=False)
        await self._retry(self._ssl_object.unwrap)
        return self.wrapped_stream

    def forceful_close(self):
        self.wrapped_stream.forceful_close()

    async def graceful_close(self):
        try:
            await self.unwrap()
            await self.wrapped_stream.graceful_close()
        except:
            self.forceful_close()
            raise
