"""The SubDL client: the request, the retry, and the bytes that come back.

SubDL is the second API provider, and unlike the scraped sites it is spoken to
with a key. That changes what the tests have to be about. A scraped page can
only waste a request; a provider request is *metered*, is authenticated, and
its answer is turned straight into a file on the user's disk.

So the three things pinned here are the three things that cost something when
they are wrong. **The request**: the key travels in a header and never in a
URL, a retry only happens for the statuses that are worth retrying, and the
provider's own `Retry-After` is honoured but capped. **The answer**: a body is
bounded before it is read, not after, and every shape that is not a subtitle —
an error document, a list, HTML, a truncated archive — is refused with a
sentence that says which one it was. **The file**: bytes become a sidecar only
after they have been decoded, validated as an SRT *and* checked against the
movie that was looked up, and never on top of a subtitle that appeared while
the download was in flight.

The parsing and scoring of a search result is tested in
`test_subtitle_fetcher.py`; this file is everything either side of it.
"""

from __future__ import annotations

import email.message
import gzip
import io
import json
import unittest
import urllib.error
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import subtitle_fetcher as sf

SRT = ("1\n00:00:01,000 --> 00:00:04,000\nA beginning is a very delicate time.\n\n"
       "2\n00:00:05,000 --> 00:00:08,000\nKnow then, that it is the year 10191.\n")

IDENTITY = sf.MovieIdentity(title="Dune", year=1984, normalized_title="dune")


def http_error(code: int, body: bytes = b"", *, retry_after: str | None = None,
               headers: bool = True) -> urllib.error.HTTPError:
    hdrs: email.message.Message | None = None
    if headers:
        hdrs = email.message.Message()
        if retry_after is not None:
            hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.subdl.com/api/v2/x", code, "err",
                                  hdrs, io.BytesIO(body))


class Reply:
    """One canned HTTP response, read the way the client reads it."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class FakeNet:
    """Routes requests by URL fragment; a list of replies is a sequence."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = {key: (list(value) if isinstance(value, list) else value)
                       for key, value in routes.items()}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        for fragment, reply in self.routes.items():
            if fragment in request.full_url:
                item = reply.pop(0) if isinstance(reply, list) else reply
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"nothing routed for {request.full_url}")

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]


class RecordingBuckets(sf.BucketRegistry):
    """The real pacing arithmetic with the sleeping taken out."""

    def __init__(self) -> None:
        super().__init__(gap=0.0, sleep=lambda _s: None, clock=lambda: 0.0)
        self.penalties: list[tuple[str, float]] = []

    def penalize(self, key: str, seconds: float) -> float:
        self.penalties.append((key, seconds))
        return super().penalize(key, seconds)


class SubdlClientCase(unittest.TestCase):
    def client(self, key: str = "secret-key", **kwargs: Any) -> sf.SubdlClient:
        client = sf.SubdlClient(key, **kwargs)
        self.buckets = RecordingBuckets()
        client.buckets = self.buckets
        return client

    def net(self, routes: dict[str, Any]) -> Any:
        fake = FakeNet(routes)
        patcher = mock.patch.object(sf.urllib.request, "urlopen", new=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(sf.time, "sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        return fake


class AskingSubdlTests(SubdlClientCase):
    """`_request_json`: one search, however many attempts it takes."""

    OK = json.dumps({"status": True, "results": [], "subtitles": []}).encode("utf-8")

    def search(self, client: sf.SubdlClient) -> dict[str, Any]:
        return client._request_json("/subtitles/search", {"film_name": "Dune"})

    def test_no_key_means_no_request(self) -> None:
        client = self.client("")
        with self.assertRaisesRegex(RuntimeError, "API key is required"):
            self.search(client)

    def test_the_key_travels_in_a_header_never_in_the_url(self) -> None:
        """A key in a query string ends up in every proxy and access log."""
        net = self.net({"/subtitles/search": Reply(self.OK)})
        self.search(self.client("secret-key"))
        request = net.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("secret-key", request.full_url)

    def test_a_rate_limit_is_retried_and_holds_the_whole_host_back(self) -> None:
        net = self.net({"/subtitles/search": [http_error(429, b"slow down", retry_after="5"),
                                              Reply(self.OK)]})
        client = self.client()
        self.assertEqual(self.search(client)["status"], True)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual([seconds for _host, seconds in self.buckets.penalties], [5.0])

    def test_an_unreasonable_retry_after_is_capped(self) -> None:
        """A provider asking for an hour does not get an hour of this run."""
        self.net({"/subtitles/search": [http_error(503, retry_after="3600"), Reply(self.OK)]})
        self.search(self.client())
        self.assertEqual([seconds for _host, seconds in self.buckets.penalties], [30.0])

    def test_a_retry_after_that_is_not_a_number_falls_back_to_the_schedule(self) -> None:
        self.net({"/subtitles/search": [http_error(429, retry_after="tomorrow"), Reply(self.OK)]})
        self.search(self.client())
        self.assertEqual([seconds for _host, seconds in self.buckets.penalties], [2.0])

    def test_an_error_with_no_headers_at_all_still_retries(self) -> None:
        self.net({"/subtitles/search": [http_error(500, headers=False), Reply(self.OK)]})
        self.search(self.client())
        self.assertEqual([seconds for _host, seconds in self.buckets.penalties], [2.0])

    def test_the_backoff_grows_with_each_attempt(self) -> None:
        self.net({"/subtitles/search": [http_error(500), http_error(500), http_error(500),
                                        Reply(self.OK)]})
        self.search(self.client())
        self.assertEqual([seconds for _host, seconds in self.buckets.penalties], [2.0, 4.0, 6.0])

    def test_a_provider_that_never_recovers_reports_its_last_answer(self) -> None:
        net = self.net({"/subtitles/search": [http_error(503, b"maintenance") for _ in range(4)]})
        with self.assertRaisesRegex(RuntimeError, "SubDL API HTTP 503: maintenance"):
            self.search(self.client())
        self.assertEqual(len(net.requests), 4, "four attempts, then it stops")

    def test_a_refusal_is_not_retried(self) -> None:
        """401 is an answer, not a hiccup: asking again spends the quota twice."""
        net = self.net({"/subtitles/search": [http_error(401, b"bad key"), Reply(self.OK)]})
        with self.assertRaisesRegex(RuntimeError, "SubDL API HTTP 401: bad key"):
            self.search(self.client())
        self.assertEqual(len(net.requests), 1)

    def test_a_network_error_is_retried_then_reported(self) -> None:
        net = self.net({"/subtitles/search": [urllib.error.URLError("unreachable")] * 4})
        with self.assertRaisesRegex(RuntimeError, "SubDL API network error: unreachable"):
            self.search(self.client())
        self.assertEqual(len(net.requests), 4)

    def test_a_network_error_that_clears_costs_nothing_but_time(self) -> None:
        self.net({"/subtitles/search": [urllib.error.URLError("dns"), Reply(self.OK)]})
        self.assertEqual(self.search(self.client())["status"], True)

    def test_a_declared_length_over_the_limit_is_refused_before_the_read(self) -> None:
        oversize = {"Content-Length": str(sf.SUBDL_MAX_RESPONSE_BYTES + 1)}
        self.net({"/subtitles/search": Reply(self.OK, headers=oversize)})
        with self.assertRaisesRegex(RuntimeError, "SubDL API response exceeds"):
            self.search(self.client())

    def test_a_declared_length_that_is_not_a_number_is_refused(self) -> None:
        self.net({"/subtitles/search": Reply(self.OK, headers={"Content-Length": "lots"})})
        with self.assertRaisesRegex(RuntimeError, "invalid SubDL API response content length"):
            self.search(self.client())

    def test_a_body_that_lies_about_its_length_is_still_bounded(self) -> None:
        """The header is a hint; the limit is enforced on what actually arrives."""
        body = b"x" * (sf.SUBDL_MAX_RESPONSE_BYTES + 1)
        self.net({"/subtitles/search": Reply(body, headers={"Content-Length": "12"})})
        with self.assertRaisesRegex(RuntimeError, "SubDL API response exceeds"):
            self.search(self.client())

    def test_an_answer_that_is_not_json_is_refused(self) -> None:
        self.net({"/subtitles/search": Reply(b"<html>maintenance</html>")})
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            self.search(self.client())

    def test_an_answer_that_is_not_an_object_is_refused(self) -> None:
        self.net({"/subtitles/search": Reply(b"[1, 2, 3]")})
        with self.assertRaisesRegex(RuntimeError, "unexpected JSON document"):
            self.search(self.client())

    def test_a_documented_error_object_is_quoted_back(self) -> None:
        body = json.dumps({"status": False, "error": {"message": "daily limit reached"}})
        self.net({"/subtitles/search": Reply(body.encode("utf-8"))})
        with self.assertRaisesRegex(RuntimeError, "rejected the search: daily limit reached"):
            self.search(self.client())

    def test_an_error_that_is_just_a_string_is_quoted_too(self) -> None:
        self.net({"/subtitles/search": Reply(json.dumps({"error": "no such film"}).encode())})
        with self.assertRaisesRegex(RuntimeError, "rejected the search: no such film"):
            self.search(self.client())

    def test_a_status_false_with_nothing_to_say(self) -> None:
        self.net({"/subtitles/search": Reply(json.dumps({"status": False}).encode())})
        with self.assertRaisesRegex(RuntimeError, "rejected the search$"):
            self.search(self.client())


class TheRedirectShapeTests(unittest.TestCase):
    """Some deployments answer the download endpoint with a URL, not a file."""

    def test_a_subtitle_is_not_a_redirect(self) -> None:
        self.assertIsNone(sf.subdl_download_redirect_url(SRT.encode("utf-8")))

    def test_a_zip_is_not_a_redirect(self) -> None:
        self.assertIsNone(sf.subdl_download_redirect_url(b"PK\x03\x04rest"))

    def test_json_that_is_not_an_object_is_not_a_redirect(self) -> None:
        self.assertIsNone(sf.subdl_download_redirect_url(b'["/subtitle/x.srt"]'))

    def test_the_documented_download_url_field(self) -> None:
        body = json.dumps({"download_url": "/subtitle/dune.zip"}).encode()
        self.assertEqual(sf.subdl_download_redirect_url(body),
                         "https://dl.subdl.com/subtitle/dune.zip")

    def test_a_url_nested_under_data(self) -> None:
        body = json.dumps({"data": {"url": "https://dl.subdl.com/subtitle/dune.zip"}}).encode()
        self.assertEqual(sf.subdl_download_redirect_url(body),
                         "https://dl.subdl.com/subtitle/dune.zip")

    def test_a_link_nested_under_download(self) -> None:
        body = json.dumps({"download": {"link": "/subtitle/dune.srt"}}).encode()
        self.assertEqual(sf.subdl_download_redirect_url(body),
                         "https://dl.subdl.com/subtitle/dune.srt")

    def test_a_url_somewhere_else_entirely_is_refused(self) -> None:
        """Following a provider-supplied host would make this an SSRF hop."""
        body = json.dumps({"download_url": "https://evil.example/subtitle/x.srt"}).encode()
        with self.assertRaisesRegex(RuntimeError, "unsafe download URL"):
            sf.subdl_download_redirect_url(body)

    def test_an_error_document_is_the_download_failing(self) -> None:
        body = json.dumps({"error": {"message": "download limit reached"}}).encode()
        with self.assertRaisesRegex(RuntimeError, "download failed: download limit reached"):
            sf.subdl_download_redirect_url(body)

    def test_an_error_with_no_message(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SubDL download failed$"):
            sf.subdl_download_redirect_url(json.dumps({"error": {"code": 5}}).encode())


def zipped(*members: tuple[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members:
            archive.writestr(name, text)
    return buffer.getvalue()


class ReadingWhatWasDownloadedTests(unittest.TestCase):
    """`decode_subdl_srt_payload`: one SRT out of whatever arrived."""

    def decode(self, data: bytes, max_bytes: int = sf.MAX_SUBTITLE_BYTES) -> str:
        return sf.decode_subdl_srt_payload(data, max_bytes)

    def test_a_plain_srt(self) -> None:
        self.assertIn("delicate time", self.decode(SRT.encode("utf-8")))

    def test_windows_newlines_are_normalized(self) -> None:
        self.assertNotIn("\r", self.decode(SRT.replace("\n", "\r\n").encode("utf-8")))

    def test_a_payload_over_the_limit_never_gets_decoded(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "safety limit"):
            self.decode(SRT.encode("utf-8"), max_bytes=10)

    def test_one_srt_in_a_zip(self) -> None:
        self.assertIn("delicate time", self.decode(zipped(("dune.srt", SRT))))

    def test_the_library_release_is_preferred_inside_a_zip(self) -> None:
        """1080p is what this library holds, whatever order the names sort in."""
        payload = zipped(("Dune.BluRay.2160p.srt", "1\n00:00:01,000 --> 00:00:02,000\nfour k\n"),
                         ("Dune.WEB.1080p.srt", "1\n00:00:01,000 --> 00:00:02,000\nten eighty\n"))
        self.assertIn("ten eighty", self.decode(payload))

    def test_a_hearing_impaired_member_is_the_last_resort(self) -> None:
        payload = zipped(("Dune.1080p.sdh.srt", "1\n00:00:01,000 --> 00:00:02,000\nsdh\n"),
                         ("Dune.1080p.srt", "1\n00:00:01,000 --> 00:00:02,000\nplain\n"))
        self.assertIn("plain", self.decode(payload))

    def test_an_hi_member_is_the_last_resort_too(self) -> None:
        """"HI" is how half the uploaders spell it; the pattern missed it."""
        payload = zipped(("Dune.1080p.HI.srt", "1\n00:00:01,000 --> 00:00:02,000\nhi\n"),
                         ("Dune.1080p.srt", "1\n00:00:01,000 --> 00:00:02,000\nplain\n"))
        self.assertIn("plain", self.decode(payload))

    def test_a_title_that_merely_starts_with_hi_is_not_a_hearing_impaired_copy(self) -> None:
        payload = zipped(("High.Fidelity.1080p.srt", "1\n00:00:01,000 --> 00:00:02,000\nfine\n"))
        self.assertIn("fine", self.decode(payload))

    def test_a_zip_with_no_subtitle_in_it(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no usable .srt"):
            self.decode(zipped(("readme.txt", "visit our site")))

    def test_a_zip_whose_only_subtitle_is_too_big(self) -> None:
        """A small archive can hold a large file; the member size is what counts."""
        payload = zipped(("dune.srt", SRT * 2_000))
        self.assertLess(len(payload), 50_000)
        with self.assertRaisesRegex(RuntimeError, "no usable .srt"):
            self.decode(payload, max_bytes=50_000)

    def test_a_zip_that_is_damaged(self) -> None:
        payload = bytearray(zipped(("dune.srt", SRT)))
        payload[40:80] = b"\x00" * 40
        with self.assertRaisesRegex(RuntimeError, "could not be read safely"):
            self.decode(bytes(payload))

    def test_a_gzipped_subtitle(self) -> None:
        self.assertIn("delicate time", self.decode(gzip.compress(SRT.encode("utf-8"))))

    def test_a_gzip_that_is_damaged(self) -> None:
        broken = gzip.compress(SRT.encode("utf-8"))[:-6] + b"\x00\x00\x00\x00\x00\x00"
        with self.assertRaisesRegex(RuntimeError, "gzip subtitle could not be read safely"):
            self.decode(broken)

    def test_a_gzip_bomb_is_stopped_at_the_limit(self) -> None:
        """The compressed size says nothing about what it expands to."""
        payload = gzip.compress(b"x" * 200_000)
        with self.assertRaisesRegex(RuntimeError, "safety limit"):
            self.decode(payload, max_bytes=1_000)

    def test_an_html_error_page_is_not_a_subtitle(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not a valid SRT"):
            self.decode(b"<html><body>404</body></html>")


class DownloadingOneSubtitleTests(SubdlClientCase):
    """`download_srt`: the whole leg, ending in a file or in nothing."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.video = self.dir / "Dune (1984).mkv"
        self.video.write_bytes(b"movie bytes")
        self.dest = self.dir / "Dune (1984).eng.srt"

    def download(self, routes: dict[str, Any], download: sf.SubdlDownload, **kwargs: Any) -> Any:
        net = self.net(routes)
        self.client_ = self.client()
        self.client_.download_srt(download, self.dest, **kwargs)
        return net

    def test_a_documented_relative_url_is_resolved_against_the_download_host(self) -> None:
        net = self.download({"dl.subdl.com": Reply(SRT.encode("utf-8"))},
                            sf.SubdlDownload(url="/subtitle/dune.srt"))
        self.assertEqual(net.urls, ["https://dl.subdl.com/subtitle/dune.srt"])
        self.assertIn("delicate time", self.dest.read_text(encoding="utf-8"))

    def test_an_identifier_builds_the_v2_endpoint_locally(self) -> None:
        """With no vetted URL the client asks the API, never a URL it was handed."""
        net = self.download({"api.subdl.com": Reply(SRT.encode("utf-8"))},
                            sf.SubdlDownload(n_id="subtitle-123"))
        self.assertEqual(
            net.urls,
            ["https://api.subdl.com/api/v2/subtitles/subtitle-123/download?format=file"],
        )
        self.assertTrue(self.dest.exists())

    def test_an_identifier_that_is_not_one_is_refused(self) -> None:
        self.net({})
        with self.assertRaisesRegex(RuntimeError, "invalid subtitle identifier"):
            self.client().download_srt(sf.SubdlDownload(n_id="../../etc/passwd"), self.dest)
        self.assertFalse(self.dest.exists())

    def test_a_candidate_with_neither_reference_is_refused(self) -> None:
        self.net({})
        with self.assertRaisesRegex(RuntimeError, "no safe download reference"):
            self.client().download_srt(sf.SubdlDownload(), self.dest)

    def test_an_answer_that_is_a_url_is_followed_once(self) -> None:
        redirect = json.dumps({"download_url": "/subtitle/dune-file.srt"}).encode()
        net = self.download(
            {"api.subdl.com": Reply(redirect), "dl.subdl.com": Reply(SRT.encode("utf-8"))},
            sf.SubdlDownload(n_id="subtitle-123"),
        )
        self.assertEqual(len(net.urls), 2)
        self.assertTrue(net.urls[1].startswith("https://dl.subdl.com/subtitle/"))
        self.assertIn("delicate time", self.dest.read_text(encoding="utf-8"))

    def test_a_download_that_fails_leaves_nothing_behind(self) -> None:
        self.net({"dl.subdl.com": http_error(500)})
        with self.assertRaisesRegex(RuntimeError, "download HTTP 500"):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)
        self.assertEqual(list(self.dir.glob("*.srt")), [])

    def test_a_download_that_cannot_connect(self) -> None:
        self.net({"dl.subdl.com": urllib.error.URLError("unreachable")})
        with self.assertRaisesRegex(RuntimeError, "download network error: unreachable"):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)

    def test_a_download_is_not_retried(self) -> None:
        """A search is idempotent; a download is metered separately and is not."""
        net = self.net({"dl.subdl.com": [http_error(503), Reply(SRT.encode("utf-8"))]})
        with self.assertRaises(RuntimeError):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)
        self.assertEqual(len(net.requests), 1)

    def test_bytes_that_are_not_a_subtitle_never_become_a_sidecar(self) -> None:
        self.net({"dl.subdl.com": Reply(b"<html>rate limited</html>")})
        with self.assertRaisesRegex(RuntimeError, "not a valid SRT"):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)
        self.assertEqual(list(self.dir.glob("*.srt")), [])

    def test_a_subtitle_over_the_limit_is_refused(self) -> None:
        self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        with self.assertRaisesRegex(RuntimeError, "subtitle exceeds"):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"),
                                       self.dest, max_bytes=16)
        self.assertEqual(list(self.dir.glob("*.srt")), [])

    def test_a_movie_that_changed_during_the_lookup_does_not_get_the_subtitle(self) -> None:
        """The subtitle was chosen for the file that was there when it started."""
        snapshot = sf.video_snapshot(self.video)
        self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        self.video.write_bytes(b"a different, longer movie entirely")
        with self.assertRaisesRegex(RuntimeError, "movie changed during subtitle lookup"):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest,
                                       video=self.video, expected_video=snapshot)
        self.assertEqual(list(self.dir.glob("*.srt")), [])

    def test_an_unchanged_movie_gets_its_subtitle(self) -> None:
        snapshot = sf.video_snapshot(self.video)
        self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest,
                                   video=self.video, expected_video=snapshot)
        self.assertIn("delicate time", self.dest.read_text(encoding="utf-8"))

    def test_a_sidecar_that_appeared_in_the_meantime_is_kept(self) -> None:
        self.dest.write_text("someone else got there first\n", encoding="utf-8")
        self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        with self.assertRaises(sf.ConcurrentSidecarError):
            self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "someone else got there first\n")

    def test_nothing_temporary_is_left_in_the_movie_folder(self) -> None:
        self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        self.client().download_srt(sf.SubdlDownload(url="/subtitle/dune.srt"), self.dest)
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()),
                         ["Dune (1984).eng.srt", "Dune (1984).mkv"])

    def test_the_raw_url_helper_still_works(self) -> None:
        """`download_subdl_srt` is the pre-client entry point and is still used."""
        net = self.net({"dl.subdl.com": Reply(SRT.encode("utf-8"))})
        sf.download_subdl_srt("/subtitle/dune.srt", self.dest, sf.MAX_SUBTITLE_BYTES)
        self.assertEqual(net.urls, ["https://dl.subdl.com/subtitle/dune.srt"])
        self.assertIn("delicate time", self.dest.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
