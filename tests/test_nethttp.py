"""Connection reuse, proved against a real server rather than a mock.

A connection pool is the kind of code that looks right and is wrong: the
interesting states are "the server hung up while the socket sat idle", "the
caller stopped reading half way down the body", and "two threads want the same
host at once". None of those are visible to a test that fakes the socket, so
these tests run a real HTTP server on the loopback interface and *count the
connections it accepts*. That number is the whole point of the feature, and it
is not something you can talk your way into.

The rule being pinned everywhere below is that a connection goes back into the
pool only when it is provably finished with — and when the pool is unsure, it
throws the connection away and pays for a new one. A pool that guesses wrong in
the other direction sends the next request into a socket with somebody else's
half-read response still coming down it.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from organizekit.core import nethttp


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this the server's header and body writes wait on each other's
    # ACKs and a reused connection looks 40 ms slower than a fresh one. Real
    # servers set TCP_NODELAY; a test server that forgets to is slow for a
    # reason that has nothing to do with the code under test.
    disable_nagle_algorithm = True  # keep-alive is an HTTP/1.1 default

    def log_message(self, *_args: object) -> None:  # keep the test output clean
        return

    def _send(self, code: int, body: bytes, *, close: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path == "/big":
            self._send(200, b"x" * 50_000)
        elif self.path == "/liar":
            # Answers as if the connection stays open, then drops it anyway:
            # this is what an idle keep-alive socket timing out looks like from
            # the client side, and it is the one race a pool cannot design out.
            self._send(200, b"see you")
            self.close_connection = True
        elif self.path == "/close":
            self._send(200, b"goodbye", close=True)
        elif self.path == "/404":
            self._send(404, b"no such subtitle")
        else:
            self._send(200, b"hello")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        self._send(200, self.rfile.read(length).upper())


class FakeSocket:
    """Just enough socket for the pool's timeout bookkeeping."""

    def settimeout(self, _timeout: float | None) -> None:
        return

    def close(self) -> None:
        return


class CountingServer(ThreadingHTTPServer):
    """A server that remembers how many TCP connections it accepted."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.connections = 0
        self._count_lock = threading.Lock()

    def get_request(self) -> tuple[socket.socket, object]:
        result = super().get_request()
        with self._count_lock:
            self.connections += 1
        return result


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = CountingServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.pool = nethttp.ConnectionPool()
        self.addCleanup(self.pool.close_all)
        self.opener = nethttp.build_pooled_opener(self.pool)

    def get(self, path: str = "/ok", *, read: int | None = None,
            opener: urllib.request.OpenerDirector | None = None) -> bytes:
        with (opener or self.opener).open(self.base + path, timeout=5) as response:
            return response.read() if read is None else response.read(read)

    def post(self, body: bytes, path: str = "/echo") -> bytes:
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        with self.opener.open(request, timeout=5) as response:
            return response.read()


class ReusingTheConnectionTests(ServerCase):
    def test_five_requests_used_to_be_five_connections(self) -> None:
        """The measurement the feature exists for."""
        stock = urllib.request.build_opener()
        for _ in range(5):
            self.assertEqual(self.get(opener=stock), b"hello")
        self.assertEqual(self.server.connections, 5)

    def test_five_requests_are_now_one_connection(self) -> None:
        for _ in range(5):
            self.assertEqual(self.get(), b"hello")
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(self.pool.stats.opened, 1)
        self.assertEqual(self.pool.stats.reused, 4)

    def test_a_post_body_survives_the_pooling(self) -> None:
        self.assertEqual(self.post(b"dune"), b"DUNE")
        self.assertEqual(self.post(b"heat"), b"HEAT")
        self.assertEqual(self.server.connections, 1)

    def test_a_reused_connection_takes_this_request_s_timeout(self) -> None:
        """A socket opened for a 5 s call must not sit on a 1 s call forever."""
        self.get()  # opened with timeout=5
        with self.opener.open(self.base + "/ok", timeout=1) as response:
            response.read()
        conn = self.pool.take(("http", f"127.0.0.1:{self.server.server_address[1]}"))
        assert conn is not None
        self.addCleanup(conn.close)
        self.assertEqual(self.server.connections, 1, "it really was the same socket")
        self.assertEqual(conn.timeout, 1)
        self.assertEqual(conn.sock.gettimeout(), 1)

    def test_the_response_is_still_an_ordinary_urllib_response(self) -> None:
        with self.opener.open(self.base + "/ok", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "text/plain")
            self.assertEqual(response.geturl(), self.base + "/ok")

    def test_an_error_is_still_an_http_error_with_a_readable_body(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/404")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(caught.exception.read(), b"no such subtitle")


class WhenAConnectionMustNotBeReusedTests(ServerCase):
    def test_a_body_the_caller_stopped_reading_is_not_pooled(self) -> None:
        """The rest of that response is still on the wire; the socket is spent."""
        self.assertEqual(len(self.get("/big", read=10)), 10)
        self.assertEqual(self.pool.idle_count(), 0)
        self.get()
        self.assertEqual(self.server.connections, 2)

    def test_a_server_that_says_close_is_believed(self) -> None:
        self.assertEqual(self.get("/close"), b"goodbye")
        self.assertEqual(self.pool.idle_count(), 0)
        self.get()
        self.assertEqual(self.server.connections, 2)

    def test_a_connection_the_server_hung_up_on_is_retried_once(self) -> None:
        """The race this pool cannot avoid: closed while idle, used anyway."""
        self.assertEqual(self.get("/liar"), b"see you")
        self.assertEqual(self.pool.idle_count(), 1, "the server gave no warning")
        time.sleep(0.2)  # let the server actually drop it
        self.assertEqual(self.get(), b"hello", "the retry answered the caller")
        self.assertEqual(self.server.connections, 2)

    def test_the_retry_is_only_for_a_connection_that_was_reused(self) -> None:
        """A first attempt is never repeated, whatever it failed with."""
        attempts: list[str] = []

        class Hangup:
            sock = FakeSocket()
            timeout = 5

            def request(self, *_args: object, **_kwargs: object) -> None:
                attempts.append("sent")
                raise http.client.RemoteDisconnected("closed")

            def close(self) -> None:
                return

        handler = nethttp.PooledHTTPHandler(self.pool)
        handler._new_connection = lambda _req: Hangup()  # type: ignore[assignment]
        request = urllib.request.Request(self.base + "/ok")
        request.timeout = 5
        with self.assertRaises(urllib.error.URLError):
            handler.http_open(request)
        self.assertEqual(attempts, ["sent"], "no retry on a fresh connection")

        self.pool.give(("http", request.host), Hangup())
        with self.assertRaises(urllib.error.URLError):
            handler.http_open(request)
        self.assertEqual(attempts, ["sent", "sent", "sent"], "one retry, then give up")

    def test_the_windows_name_for_a_dropped_socket_is_also_retried(self) -> None:
        """WinError 10053 is the same event as a reset, under another name.

        Linux reports a keep-alive socket the peer dropped as a reset or a
        remote disconnection; Windows raises ConnectionAbortedError instead.
        A pool that did not know that would turn every Windows race into a
        failed request.
        """
        attempts: list[str] = []

        class Aborted:
            sock = FakeSocket()
            timeout = 5

            def request(self, *_args: object, **_kwargs: object) -> None:
                attempts.append("sent")
                raise ConnectionAbortedError(
                    10053, "An established connection was aborted")

            def close(self) -> None:
                return

        handler = nethttp.PooledHTTPHandler(self.pool)
        request = urllib.request.Request(self.base + "/ok")
        request.timeout = 5
        self.pool.give(("http", request.host), Aborted())
        with handler.http_open(request) as response:
            self.assertEqual(response.read(), b"hello", "the retry answered the caller")
        self.assertEqual(attempts, ["sent"], "the pooled connection was tried once")
        self.assertEqual(self.server.connections, 1, "then a fresh one carried the request")

    def test_a_timeout_on_a_reused_connection_is_not_retried(self) -> None:
        """Retrying a timeout would double the wait and report it later."""
        attempts: list[str] = []

        class Slow:
            sock = FakeSocket()
            timeout = 5

            def request(self, *_args: object, **_kwargs: object) -> None:
                attempts.append("sent")
                raise TimeoutError("timed out")

            def close(self) -> None:
                return

        handler = nethttp.PooledHTTPHandler(self.pool)
        handler._new_connection = lambda _req: Slow()  # type: ignore[assignment]
        request = urllib.request.Request(self.base + "/ok")
        request.timeout = 5
        self.pool.give(("http", request.host), Slow())
        with self.assertRaises(urllib.error.URLError):
            handler.http_open(request)
        self.assertEqual(attempts, ["sent"])

    def test_a_first_attempt_that_fails_is_reported_not_retried(self) -> None:
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        with self.assertRaises(urllib.error.URLError):
            self.opener.open(f"http://127.0.0.1:{port}/ok", timeout=5)
        self.assertEqual(self.pool.stats.opened, 1, "one attempt, not two")

    def test_an_idle_connection_that_sat_too_long_is_dropped_unopened(self) -> None:
        clock = [1000.0]
        pool = nethttp.ConnectionPool(max_idle_seconds=30.0, clock=lambda: clock[0])
        opener = nethttp.build_pooled_opener(pool)
        self.addCleanup(pool.close_all)
        self.get(opener=opener)
        clock[0] += 31.0
        self.get(opener=opener)
        self.assertEqual(self.server.connections, 2)
        self.assertEqual(pool.stats.discarded, 1)

    def test_an_idle_connection_inside_the_window_is_used(self) -> None:
        clock = [1000.0]
        pool = nethttp.ConnectionPool(max_idle_seconds=30.0, clock=lambda: clock[0])
        opener = nethttp.build_pooled_opener(pool)
        self.addCleanup(pool.close_all)
        self.get(opener=opener)
        clock[0] += 29.0
        self.get(opener=opener)
        self.assertEqual(self.server.connections, 1)


class ThePoolItselfTests(unittest.TestCase):
    """The bookkeeping, without a server in the way."""

    class FakeConn:
        def __init__(self, alive: bool = True) -> None:
            self.sock: object | None = object() if alive else None
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self.sock = None

    def pool(self, **kwargs: object) -> nethttp.ConnectionPool:
        return nethttp.ConnectionPool(**kwargs)  # type: ignore[arg-type]

    def test_an_empty_pool_hands_back_nothing(self) -> None:
        self.assertIsNone(self.pool().take(("https", "example.test")))

    def test_a_connection_comes_back_out_again(self) -> None:
        pool = self.pool()
        conn = self.FakeConn()
        pool.give(("https", "a"), conn)
        self.assertIs(pool.take(("https", "a")), conn)
        self.assertEqual(pool.stats.reused, 1)

    def test_a_connection_is_only_handed_to_one_caller(self) -> None:
        pool = self.pool()
        pool.give(("https", "a"), self.FakeConn())
        self.assertIsNotNone(pool.take(("https", "a")))
        self.assertIsNone(pool.take(("https", "a")))

    def test_hosts_do_not_share_connections(self) -> None:
        pool = self.pool()
        pool.give(("https", "a"), self.FakeConn())
        self.assertIsNone(pool.take(("https", "b")))
        self.assertIsNone(pool.take(("http", "a")), "the scheme is part of the key")

    def test_a_dead_connection_is_never_stored(self) -> None:
        pool = self.pool()
        pool.give(("https", "a"), self.FakeConn(alive=False))
        self.assertEqual(pool.idle_count(), 0)
        self.assertEqual(pool.stats.discarded, 1)

    def test_a_connection_that_died_while_it_waited_is_not_handed_out(self) -> None:
        """Alive when stored, dead when wanted: the interesting case."""
        pool = self.pool()
        conn = self.FakeConn()
        pool.give(("https", "a"), conn)
        conn.sock = None
        self.assertIsNone(pool.take(("https", "a")))
        self.assertEqual(pool.stats.discarded, 1)
        self.assertEqual(pool.stats.reused, 0)

    def test_more_connections_than_the_host_needs_are_closed(self) -> None:
        pool = self.pool(per_host=1)
        first, second = self.FakeConn(), self.FakeConn()
        pool.give(("https", "a"), first)
        pool.give(("https", "a"), second)
        self.assertTrue(second.closed)
        self.assertEqual(pool.idle_count(), 1)

    def test_closing_the_pool_closes_what_is_in_it(self) -> None:
        pool = self.pool()
        conn = self.FakeConn()
        pool.give(("https", "a"), conn)
        pool.close_all()
        self.assertTrue(conn.closed)
        self.assertEqual(pool.idle_count(), 0)

    def test_a_socket_that_will_not_close_does_not_break_the_pool(self) -> None:
        class Stubborn(ThePoolItselfTests.FakeConn):
            def close(self) -> None:
                raise OSError("already gone")

        pool = self.pool()
        pool.give(("https", "a"), Stubborn())
        pool.close_all()

    def test_a_pool_configured_to_hold_nothing_holds_nothing(self) -> None:
        pool = self.pool(per_host=0)
        conn = self.FakeConn()
        pool.give(("https", "a"), conn)
        self.assertTrue(conn.closed)
        self.assertEqual(pool.idle_count(), 0)
        self.assertIsNone(pool.take(("https", "a")))

    def test_the_counters_are_reportable(self) -> None:
        pool = self.pool()
        pool.note_opened()
        self.assertEqual(pool.stats.as_dict(), {"opened": 1, "reused": 0, "discarded": 0})


class TheReleaseRuleTests(unittest.TestCase):
    """The truth table for "may this connection be used again?".

    Each clause is checked on its own, because in a live run two of them
    usually fire together and a missing one would never show.
    """

    class Conn:
        def __init__(self, alive: bool = True) -> None:
            self.sock: object | None = FakeSocket() if alive else None
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self.sock = None

    class Response:
        def __init__(self, *, drained: bool = True, will_close: bool = False) -> None:
            self.drained = drained
            self.will_close = will_close
            self.closes = 0

        def isclosed(self) -> bool:
            return self.drained

        def close(self) -> None:
            self.closes += 1

    def release(self, *, drained: bool = True, will_close: bool = False,
                alive: bool = True) -> tuple[nethttp.ConnectionPool, Conn, Response]:
        pool = nethttp.ConnectionPool()
        conn = self.Conn(alive=alive)
        response = self.Response(drained=drained, will_close=will_close)
        nethttp._release_on_close(response, conn, ("https", "example.test"), pool)
        response.close()
        return pool, conn, response

    def test_a_drained_response_on_a_live_socket_is_pooled(self) -> None:
        pool, conn, response = self.release()
        self.assertEqual(pool.idle_count(), 1)
        self.assertFalse(conn.closed)
        self.assertEqual(response.closes, 1, "the real close still happened")

    def test_a_response_still_arriving_is_not_pooled(self) -> None:
        pool, conn, _response = self.release(drained=False)
        self.assertEqual(pool.idle_count(), 0)
        self.assertTrue(conn.closed)

    def test_a_server_that_announced_a_close_is_not_pooled(self) -> None:
        """Even when the socket still looks usable from here."""
        pool, conn, _response = self.release(will_close=True)
        self.assertEqual(pool.idle_count(), 0)
        self.assertTrue(conn.closed)

    def test_a_connection_without_a_socket_is_not_pooled(self) -> None:
        pool, _conn, _response = self.release(alive=False)
        self.assertEqual(pool.idle_count(), 0)
        self.assertEqual(pool.stats.discarded, 1)

    def test_closing_the_response_twice_pools_one_connection(self) -> None:
        pool, _conn, response = self.release()
        response.close()
        self.assertEqual(pool.idle_count(), 1)


class SafetyOfTheHandlersTests(unittest.TestCase):
    def test_https_verifies_certificates_and_hostnames(self) -> None:
        """A pool is exactly where a disabled check would go unnoticed."""
        handler = nethttp.PooledHTTPSHandler(nethttp.ConnectionPool())
        context = handler._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_a_caller_supplied_context_is_the_one_used(self) -> None:
        context = ssl.create_default_context()
        handler = nethttp.PooledHTTPSHandler(nethttp.ConnectionPool(), context=context)
        self.assertIs(handler._context, context)

    def test_the_https_handler_builds_https_connections(self) -> None:
        pool = nethttp.ConnectionPool()
        handler = nethttp.PooledHTTPSHandler(pool)
        conn = handler._new_connection(urllib.request.Request("https://example.test/x"))
        self.addCleanup(conn.close)
        self.assertIsInstance(conn, http.client.HTTPSConnection)
        self.assertEqual(pool.stats.opened, 1)

    def test_the_request_asks_for_keep_alive(self) -> None:
        sent: dict[str, object] = {}

        class Recorder:
            def request(self, method: str, selector: str, body: object,
                        headers: dict[str, str]) -> None:
                sent.update({"method": method, "selector": selector, "headers": headers})

            def getresponse(self) -> object:
                return object()

        request = urllib.request.Request("https://example.test/search?q=1",
                                         headers={"Api-Key": "secret"})
        nethttp._exchange(Recorder(), request)
        self.assertEqual(sent["method"], "GET")
        self.assertEqual(sent["selector"], "/search?q=1")
        headers = sent["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["Connection"], "keep-alive")
        self.assertEqual(headers["Api-key"], "secret", "the caller's headers survive")


class TimeoutsTests(unittest.TestCase):
    class Conn:
        def __init__(self, sock: object | None) -> None:
            self.sock = sock
            self.timeout = 99.0

    def test_a_request_without_a_timeout_leaves_the_connection_alone(self) -> None:
        conn = self.Conn(FakeSocket())
        nethttp._retime(conn, socket._GLOBAL_DEFAULT_TIMEOUT)
        self.assertEqual(conn.timeout, 99.0)

    def test_this_request_s_timeout_replaces_the_last_one_s(self) -> None:
        applied: list[float] = []

        class Recorder(FakeSocket):
            def settimeout(self, timeout: float | None) -> None:
                applied.append(timeout)  # type: ignore[arg-type]

        conn = self.Conn(Recorder())
        nethttp._retime(conn, 7.0)
        self.assertEqual(conn.timeout, 7.0)
        self.assertEqual(applied, [7.0])

    def test_a_socket_that_refuses_the_timeout_is_not_fatal(self) -> None:
        class Refuser(FakeSocket):
            def settimeout(self, _timeout: float | None) -> None:
                raise OSError("socket already gone")

        conn = self.Conn(Refuser())
        nethttp._retime(conn, 7.0)  # the request below will fail honestly instead
        self.assertEqual(conn.timeout, 7.0)

    def test_a_request_built_by_hand_still_has_a_timeout(self) -> None:
        request = urllib.request.Request("https://example.test/x")
        self.assertIs(nethttp._timeout_of(request), socket._GLOBAL_DEFAULT_TIMEOUT)
        request.timeout = 3
        self.assertEqual(nethttp._timeout_of(request), 3)


class BothSchemesTests(unittest.TestCase):
    """https_open and http_open are the same code path, and must stay so."""

    class Response:
        will_close = False
        reason = "OK"

        def isclosed(self) -> bool:
            return True

        def close(self) -> None:
            return

    def fake_connection(self, response: object) -> object:
        class Conn:
            sock = FakeSocket()
            timeout = 5

            def request(self, *_args: object, **_kwargs: object) -> None:
                return

            def getresponse(self) -> object:
                return response

            def close(self) -> None:
                return

        return Conn()

    def test_https_open_pools_like_http_open(self) -> None:
        pool = nethttp.ConnectionPool()
        handler = nethttp.PooledHTTPSHandler(pool)
        response = self.Response()
        handler._new_connection = lambda _req: self.fake_connection(response)  # type: ignore[assignment]
        request = urllib.request.Request("https://example.test/search")
        request.timeout = 5
        returned = handler.https_open(request)
        self.assertIs(returned, response)
        self.assertEqual(returned.url, "https://example.test/search")
        self.assertEqual(returned.msg, "OK")
        returned.close()
        self.assertEqual(pool.idle_count(), 1, "an https connection is reusable too")

    def test_the_installed_opener_uses_the_shared_pool(self) -> None:
        self.addCleanup(nethttp.reset_pooled_opener)
        nethttp.ensure_pooled_opener()
        handlers = [h for h in urllib.request._opener.handlers
                    if isinstance(h, nethttp.PooledHTTPHandler | nethttp.PooledHTTPSHandler)]
        self.assertEqual(len(handlers), 2)
        for handler in handlers:
            self.assertIs(handler._pool, nethttp.shared_pool())


class TurningItOffTests(unittest.TestCase):
    def tearDown(self) -> None:
        nethttp.reset_pooled_opener()

    def test_the_environment_switch(self) -> None:
        self.assertTrue(nethttp.keepalive_disabled({"ORGANIZE_NO_KEEPALIVE": "1"}))
        self.assertTrue(nethttp.keepalive_disabled({"ORGANIZE_NO_KEEPALIVE": "TRUE"}))
        self.assertFalse(nethttp.keepalive_disabled({"ORGANIZE_NO_KEEPALIVE": "0"}))
        self.assertFalse(nethttp.keepalive_disabled({}))

    def test_installing_is_idempotent(self) -> None:
        self.assertTrue(nethttp.ensure_pooled_opener())
        installed = urllib.request._opener
        self.assertTrue(nethttp.ensure_pooled_opener())
        self.assertIs(urllib.request._opener, installed, "the second call changed nothing")

    def test_the_switch_leaves_urllib_alone(self) -> None:
        import os
        from unittest import mock

        nethttp.reset_pooled_opener()
        before = urllib.request._opener
        with mock.patch.dict(os.environ, {"ORGANIZE_NO_KEEPALIVE": "1"}):
            self.assertFalse(nethttp.ensure_pooled_opener())
        self.assertIs(urllib.request._opener, before)


class ManyThreadsOneHostTests(ServerCase):
    def test_eight_threads_get_eight_correct_answers(self) -> None:
        """Concurrency must not cross two callers' bytes over one socket."""
        answers: list[bytes] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def work() -> None:
            try:
                for _ in range(4):
                    body = self.get()
                    with lock:
                        answers.append(body)
            except BaseException as exc:  # noqa: BLE001 - reported below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(answers, [b"hello"] * 32)
        self.assertGreater(self.pool.stats.reused, 0, "at least some sockets were reused")
        self.assertLessEqual(self.server.connections, 32)


class TheFetcherAsksForItTests(unittest.TestCase):
    """The only tool that makes requests turns this on for itself."""

    def test_every_client_installs_the_pooled_opener(self) -> None:
        from unittest import mock

        import subtitle_fetcher as sf

        for build in (lambda: sf.ScrapeTransport(gap=0.0),
                      lambda: sf.SubdlClient("key"),
                      lambda: sf.OpenSubtitlesClient(sf.Config())):
            with mock.patch.object(sf, "ensure_pooled_opener") as ensure:
                build()
            ensure.assert_called_once_with()


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
