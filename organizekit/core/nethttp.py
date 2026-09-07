"""Connection reuse for the one tool in this toolkit that talks to the internet.

Every provider call the subtitle fetcher makes goes through
``urllib.request.urlopen``, and urllib closes the socket after each response —
it sends ``Connection: close`` and does not keep a pool. A run that asks
OpenSubtitles for a hash match, then SubDL for a release match, then a scraped
site for a page, pays a fresh TCP handshake *and* a fresh TLS handshake every
single time. On a 400-movie pass that is roughly 1,200 handshakes nobody
needed: the second request to a host arrives about a second after the first
(the per-host rate limiter sees to that), which is well inside every one of
these servers' keep-alive windows.

This module is that pool, and nothing else. It plugs in as a pair of urllib
handlers, so **no call site changes**: `urlopen` keeps being `urlopen`, the
request objects keep being request objects, an HTTP error is still an
`HTTPError` with a readable body, and every test that installs a fake at
`urlopen` still installs it in the same place.

What it is careful about, in the order the care matters:

* **A connection goes back in the pool only when it is provably clean** — the
  body was read to the end, the server did not say ``Connection: close``, and
  the socket is still there. A bounded read that stopped early (the fetcher
  refuses oversized payloads without reading them) closes the connection
  instead, because the rest of that body is still on the wire.
* **A pooled connection can be dead on arrival.** Servers close idle sockets
  whenever they like, and the close races with the next request. When a request
  on a *reused* connection fails before any response arrives, it is retried
  once on a fresh connection; a first attempt on a fresh connection is never
  retried, because that is a real error and hiding it would be worse than
  failing.
* **Idle connections expire.** One that has been sitting for longer than
  ``max_idle_seconds`` is dropped unopened rather than gambled on.
* **TLS verification is not negotiable.** The HTTPS handler builds a default
  ``ssl`` context — certificates and hostnames checked — and a test asserts it,
  because a connection pool is exactly the sort of plumbing where a stray
  ``check_hostname = False`` would never be noticed.

Set ``ORGANIZE_NO_KEEPALIVE=1`` to switch the whole thing off and get stock
urllib behaviour back.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

__all__ = [
    "KEEPALIVE_ENV",
    "ConnectionPool",
    "PoolStats",
    "PooledHTTPHandler",
    "PooledHTTPSHandler",
    "build_pooled_opener",
    "ensure_pooled_opener",
    "keepalive_disabled",
    "shared_pool",
]

KEEPALIVE_ENV = "ORGANIZE_NO_KEEPALIVE"

# How long a connection may sit unused before it is treated as probably dead.
# Servers commonly close idle keep-alive sockets after 5-75 s; the fetcher's
# own per-host gap is about a second, so anything beyond half a minute is a
# connection this run has no plan for anyway.
DEFAULT_MAX_IDLE_SECONDS = 30.0
# One live connection per host is all a serial, rate-limited client can use.
DEFAULT_PER_HOST = 1

# Failures that mean "the server hung up before answering". Retrying one of
# these on a fresh connection cannot duplicate work, because the request was
# never processed.
_STALE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.CannotSendRequest,
    ConnectionResetError,
    BrokenPipeError,
)


class PoolStats:
    """Counters worth reporting: how many handshakes were avoided."""

    __slots__ = ("opened", "reused", "discarded")

    def __init__(self) -> None:
        self.opened = 0
        self.reused = 0
        self.discarded = 0

    def as_dict(self) -> dict[str, int]:
        return {"opened": self.opened, "reused": self.reused, "discarded": self.discarded}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PoolStats(opened={self.opened}, reused={self.reused}, discarded={self.discarded})"


class ConnectionPool:
    """Idle ``http.client`` connections, keyed by scheme and host.

    Threads share one pool safely: a connection is *removed* when it is taken,
    so two callers can never write to the same socket.
    """

    def __init__(
        self,
        *,
        max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS,
        per_host: int = DEFAULT_PER_HOST,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_idle_seconds = float(max_idle_seconds)
        self.per_host = max(0, int(per_host))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str], list[tuple[Any, float]]] = {}
        self.stats = PoolStats()

    # -- borrowing ---------------------------------------------------------

    def take(self, key: tuple[str, str]) -> Any | None:
        """Return a connection that is worth trying, or None."""
        now = self._clock()
        with self._lock:
            waiting = self._idle.get(key) or []
            while waiting:
                conn, stored_at = waiting.pop()
                if now - stored_at > self.max_idle_seconds or conn.sock is None:
                    self.stats.discarded += 1
                    _close_quietly(conn)
                    continue
                self.stats.reused += 1
                if not waiting:
                    self._idle.pop(key, None)
                return conn
            self._idle.pop(key, None)
        return None

    def give(self, key: tuple[str, str], conn: Any) -> None:
        """Offer a clean connection back. The pool may decline and close it."""
        if conn.sock is None:
            self.stats.discarded += 1
            _close_quietly(conn)
            return
        with self._lock:
            waiting = self._idle.setdefault(key, [])
            if len(waiting) >= self.per_host:
                self.stats.discarded += 1
                _close_quietly(conn)
                if not waiting:
                    self._idle.pop(key, None)
                return
            waiting.append((conn, self._clock()))

    def note_opened(self) -> None:
        self.stats.opened += 1

    # -- housekeeping ------------------------------------------------------

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(waiting) for waiting in self._idle.values())

    def close_all(self) -> None:
        with self._lock:
            waiting = [conn for entries in self._idle.values() for conn, _stored in entries]
            self._idle.clear()
        for conn in waiting:
            _close_quietly(conn)


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except OSError:  # a socket already gone is the state we wanted anyway
        pass


class _PooledOpenerMixin:
    """The half of ``AbstractHTTPHandler.do_open`` that reuses the socket."""

    _pool: ConnectionPool
    _scheme: str

    def _new_connection(self, req: urllib.request.Request) -> Any:
        raise NotImplementedError  # pragma: no cover - provided by the handlers

    def _open_pooled(self, req: urllib.request.Request) -> Any:
        key = (self._scheme, req.host)
        conn = self._pool.take(key)
        if conn is not None:
            _retime(conn, _timeout_of(req))
        reused = conn is not None
        if conn is None:
            conn = self._new_connection(req)
        try:
            response = _exchange(conn, req)
        except _STALE_ERRORS as exc:
            _close_quietly(conn)
            if not reused:
                # A fresh connection that fails is a real failure; retrying it
                # would just make the report later and less honest.
                raise urllib.error.URLError(exc) from exc
            conn = self._new_connection(req)
            try:
                response = _exchange(conn, req)
            except (OSError, http.client.HTTPException) as retry_exc:
                _close_quietly(conn)
                raise urllib.error.URLError(retry_exc) from retry_exc
        except (OSError, http.client.HTTPException) as exc:
            _close_quietly(conn)
            raise urllib.error.URLError(exc) from exc
        # urllib's callers (and its own error processor) expect these two.
        response.url = req.get_full_url()
        response.msg = response.reason
        _release_on_close(response, conn, key, self._pool)
        return response


def _exchange(conn: Any, req: urllib.request.Request) -> Any:
    headers = dict(req.unredirected_hdrs)
    headers.update({name: value for name, value in req.headers.items() if name not in headers})
    # The one line this whole module exists for.
    headers["Connection"] = "keep-alive"
    conn.request(req.get_method(), req.selector, req.data, headers)
    return conn.getresponse()


def _timeout_of(req: urllib.request.Request) -> Any:
    """A request built by hand has no ``timeout``; urlopen always sets one."""
    return getattr(req, "timeout", socket._GLOBAL_DEFAULT_TIMEOUT)  # noqa: SLF001


def _retime(conn: Any, timeout: Any) -> None:
    """Apply this request's timeout to a connection opened for an earlier one."""
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:  # noqa: SLF001 - urllib's own sentinel
        return
    conn.timeout = timeout
    if conn.sock is not None:
        try:
            conn.sock.settimeout(timeout)
        except OSError:
            pass


def _release_on_close(response: Any, conn: Any, key: tuple[str, str], pool: ConnectionPool) -> None:
    """Decide, at close time, whether this connection may be used again."""
    original_close = response.close
    released = False

    def close() -> None:
        # Closing twice must not offer the same connection to the pool twice:
        # ``HTTPConnection.close()`` closes the response it is holding, so the
        # second call really does happen.
        nonlocal released
        if released:
            original_close()
            return
        released = True
        # ``isclosed()`` is true once http.client has seen the end of the body
        # and detached it from the socket; anything else means bytes are still
        # in flight and the connection can only be thrown away.
        clean = bool(response.isclosed()) and not response.will_close and conn.sock is not None
        original_close()
        if clean:
            pool.give(key, conn)
        else:
            pool.stats.discarded += 1
            _close_quietly(conn)

    response.close = close


class PooledHTTPHandler(_PooledOpenerMixin, urllib.request.HTTPHandler):
    """Plain HTTP with connection reuse (used by the tests and by loopback)."""

    _scheme = "http"

    def __init__(self, pool: ConnectionPool, debuglevel: int = 0) -> None:
        urllib.request.HTTPHandler.__init__(self, debuglevel=debuglevel)
        self._pool = pool

    def _new_connection(self, req: urllib.request.Request) -> Any:
        self._pool.note_opened()
        return http.client.HTTPConnection(req.host, timeout=_timeout_of(req))

    def http_open(self, req: urllib.request.Request) -> Any:
        return self._open_pooled(req)


class PooledHTTPSHandler(_PooledOpenerMixin, urllib.request.HTTPSHandler):
    """HTTPS with connection reuse, and certificate verification left on."""

    _scheme = "https"

    def __init__(self, pool: ConnectionPool, context: ssl.SSLContext | None = None,
                 debuglevel: int = 0) -> None:
        # ``create_default_context`` is the verifying one: hostnames checked,
        # certificates required. Nothing here may weaken it.
        self._context = context or ssl.create_default_context()
        urllib.request.HTTPSHandler.__init__(self, debuglevel=debuglevel, context=self._context)
        self._pool = pool

    def _new_connection(self, req: urllib.request.Request) -> Any:
        self._pool.note_opened()
        return http.client.HTTPSConnection(req.host, timeout=_timeout_of(req),
                                           context=self._context)

    def https_open(self, req: urllib.request.Request) -> Any:
        return self._open_pooled(req)


_SHARED_POOL = ConnectionPool()
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def shared_pool() -> ConnectionPool:
    """The pool the installed opener uses (one per process)."""
    return _SHARED_POOL


def keepalive_disabled(env: dict[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(KEEPALIVE_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_pooled_opener(pool: ConnectionPool | None = None,
                        context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    """An opener identical to urllib's default except that it reuses sockets."""
    target = pool or _SHARED_POOL
    return urllib.request.build_opener(
        PooledHTTPHandler(target),
        PooledHTTPSHandler(target, context=context),
    )


def ensure_pooled_opener(*, force: bool = False) -> bool:
    """Install the pooled opener once per process. Returns whether it is on.

    Safe to call from anywhere that is about to make a request: the second and
    later calls do nothing. ``ORGANIZE_NO_KEEPALIVE=1`` makes it a no-op, which
    is the escape hatch if a proxy or a middlebox ever disagrees with reuse.
    """
    global _INSTALLED
    if keepalive_disabled():
        return False
    with _INSTALL_LOCK:
        if _INSTALLED and not force:
            return True
        urllib.request.install_opener(build_pooled_opener())
        _INSTALLED = True
    return True


def reset_pooled_opener() -> None:
    """Undo :func:`ensure_pooled_opener` (tests, and only tests)."""
    global _INSTALLED
    with _INSTALL_LOCK:
        _INSTALLED = False
    urllib.request.install_opener(urllib.request.build_opener())
    _SHARED_POOL.close_all()
