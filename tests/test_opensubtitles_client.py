"""The OpenSubtitles client's transport and its download safety rules.

Everything here runs against a fake ``urlopen``: the retry ladder, the
re-login on an expired token, the `Retry-After` handling that now goes into
the host's rate-limit bucket, and — the part that matters most — the checks
between "the provider handed us a link" and "there is a new subtitle beside
your movie". A provider response is remote input, so a bad one must fail the
download, not land on disk.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import subtitle_fetcher as sf

SRT = (
    "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n"
    "2\n00:00:05,000 --> 00:00:08,000\nGoodbye.\n\n"
)


class Response:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


def http_error(code: int, body: bytes = b"{}", headers: dict[str, str] | None = None):
    return urllib.error.HTTPError(
        "https://api.opensubtitles.com/api/v1/subtitles", code, "boom",
        headers or {}, io.BytesIO(body),  # type: ignore[arg-type]
    )


def json_response(payload: object) -> Response:
    return Response(json.dumps(payload).encode("utf-8"))


class _ClientFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = sf.Config(library=Path("/library"), log_file=None,
                             report_file=Path("/logs/r.txt"), api_key="test-key")
        self.client = sf.OpenSubtitlesClient(self.cfg)
        # Every bucket in this module is unlimited: the pacing has its own
        # suite, and a real one-second gap would make these tests sleep.
        self.client.buckets = sf.BucketRegistry(gap=0.0)
        self.slept: list[float] = []
        patcher = mock.patch("subtitle_fetcher.time.sleep", self.slept.append)
        patcher.start()
        self.addCleanup(patcher.stop)


class RequestRetryTests(_ClientFixture):
    def test_a_plain_response_is_parsed(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        return_value=json_response({"data": []})):
            self.assertEqual(self.client._request("GET", "/subtitles"), {"data": []})

    def test_an_empty_body_is_an_empty_document(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        return_value=Response(b"   ")):
            self.assertEqual(self.client._request("GET", "/subtitles"), {})

    def test_invalid_json_is_an_error_not_a_crash(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        return_value=Response(b"<html>maintenance</html>")), \
                self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            self.client._request("GET", "/subtitles")

    def test_a_json_list_is_refused(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        return_value=json_response([1, 2, 3])), \
                self.assertRaisesRegex(RuntimeError, "unexpected JSON"):
            self.client._request("GET", "/subtitles")

    def test_a_server_error_is_retried_and_then_succeeds(self) -> None:
        responses = [http_error(503), http_error(502), json_response({"data": []})]
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=responses) as opened:
            self.assertEqual(self.client._request("GET", "/subtitles"), {"data": []})
        self.assertEqual(opened.call_count, 3)

    def test_the_retry_ladder_is_finite(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[http_error(503)] * 4) as opened, \
                self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            self.client._request("GET", "/subtitles")
        self.assertEqual(opened.call_count, 4, "four attempts, then give up")

    def test_a_client_error_is_not_retried(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[http_error(404, b"no such route")]) as opened, \
                self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            self.client._request("GET", "/subtitles")
        self.assertEqual(opened.call_count, 1, "a 404 will not become a 200")

    def test_retry_after_holds_the_whole_host_back(self) -> None:
        penalties: list[tuple[str, float]] = []
        self.client.buckets = mock.Mock(
            take=lambda key: 0.0,
            penalize=lambda key, seconds: penalties.append((key, seconds)),
        )
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[http_error(429, b"slow down", {"Retry-After": "7"}),
                                     json_response({"data": []})]):
            self.client._request("GET", "/subtitles")
        self.assertEqual(penalties, [("api.opensubtitles.com", 7.0)],
                         "the penalty belongs to the host, not to this request")

    def test_an_absurd_retry_after_is_capped(self) -> None:
        penalties: list[float] = []
        self.client.buckets = mock.Mock(
            take=lambda key: 0.0,
            penalize=lambda key, seconds: penalties.append(seconds),
        )
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[http_error(429, b"", {"Retry-After": "86400"}),
                                     json_response({})]):
            self.client._request("GET", "/subtitles")
        self.assertEqual(penalties, [30.0], "a day-long hold would stall the run")

    def test_a_nonsense_retry_after_falls_back_to_the_ladder(self) -> None:
        penalties: list[float] = []
        self.client.buckets = mock.Mock(
            take=lambda key: 0.0,
            penalize=lambda key, seconds: penalties.append(seconds),
        )
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[http_error(429, b"", {"Retry-After": "soon"}),
                                     json_response({})]):
            self.client._request("GET", "/subtitles")
        self.assertEqual(penalties, [2.0])

    def test_a_network_error_backs_off_and_retries(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=[urllib.error.URLError("dns"), json_response({})]) as opened:
            self.client._request("GET", "/subtitles")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.slept, [1.5])

    def test_a_persistent_network_error_is_reported(self) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("dns")), \
                self.assertRaisesRegex(RuntimeError, "network error"):
            self.client._request("GET", "/subtitles")

    def test_an_expired_token_is_refreshed_once(self) -> None:
        self.cfg.username, self.cfg.password = "user", "pass"
        self.client.token = "stale"
        responses = [
            http_error(401, b"invalid token"),
            json_response({"token": "fresh"}),   # the re-login
            json_response({"link": "ok"}),       # the retried request
        ]
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", side_effect=responses):
            payload = self.client._request("POST", "/download", auth=True)
        self.assertEqual(payload, {"link": "ok"})
        self.assertEqual(self.client.token, "fresh")

    def test_a_second_rejection_is_not_an_infinite_login_loop(self) -> None:
        self.cfg.username, self.cfg.password = "user", "pass"
        self.client.token = "stale"
        responses = [
            http_error(401, b"invalid token"),
            json_response({"token": "fresh"}),
            http_error(401, b"invalid token"),
            json_response({"token": "fresher"}),
        ]
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", side_effect=responses), \
                self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            self.client._request("POST", "/download", auth=True)

    def test_login_without_credentials_says_which_ones(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "OPENSUBTITLES_USERNAME"):
            self.client.login()

    def test_a_login_response_without_a_token_is_an_error(self) -> None:
        self.cfg.username, self.cfg.password = "user", "pass"
        with mock.patch("subtitle_fetcher.urllib.request.urlopen",
                        return_value=json_response({"status": "ok"})), \
                self.assertRaisesRegex(RuntimeError, "login failed"):
            self.client.login()


class DownloadSafetyTests(_ClientFixture):
    """Between the provider's link and a file beside the movie."""

    def setUp(self) -> None:
        super().setUp()
        self._td = tempfile.TemporaryDirectory(prefix="fetch_dl_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.video = self.root / "Film (2020).mkv"
        self.video.write_bytes(b"movie bytes" * 1000)
        self.dest = self.root / "Film (2020).eng.srt"
        self.snapshot = sf.video_snapshot(self.video)

    def _download(self, *responses: object) -> None:
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", side_effect=list(responses)):
            self.client.download_srt(42, self.dest, video=self.video,
                                     expected_video=self.snapshot)

    def test_a_good_download_lands_beside_the_movie(self) -> None:
        self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                       Response(SRT.encode("utf-8")))
        self.assertEqual(self.dest.read_text(encoding="utf-8"), SRT)

    def test_a_missing_link_is_an_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no link"):
            self._download(json_response({"status": "ok"}))
        self.assertFalse(self.dest.exists())

    def test_a_non_https_link_is_never_dereferenced(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-HTTPS"):
            self._download(json_response({"link": "file:///etc/passwd"}))
        self.assertFalse(self.dest.exists())

    def test_a_schemeless_link_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-HTTPS"):
            self._download(json_response({"link": "/f/42.srt"}))

    def test_a_declared_oversize_payload_is_refused_before_reading_it(self) -> None:
        huge = str(sf.MAX_SUBTITLE_BYTES + 1)
        with self.assertRaisesRegex(RuntimeError, "safety limit"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(b"", {"Content-Length": huge}))
        self.assertFalse(self.dest.exists())

    def test_an_undeclared_oversize_payload_is_refused_after_reading_it(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "safety limit"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(b"x" * (sf.MAX_SUBTITLE_BYTES + 1)))

    def test_a_nonsense_content_length_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "content length"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(SRT.encode("utf-8"), {"Content-Length": "many"}))

    def test_an_html_error_page_is_not_a_subtitle(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not a valid SRT"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(b"<html>rate limited</html>"))
        self.assertFalse(self.dest.exists())

    def test_a_download_http_error_is_reported(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "download HTTP 500"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           http_error(500))

    def test_a_movie_that_changed_mid_lookup_gets_no_sidecar(self) -> None:
        stale = self.snapshot
        self.video.write_bytes(b"a different movie entirely")
        self.snapshot = stale
        with self.assertRaisesRegex(RuntimeError, "movie changed"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(SRT.encode("utf-8")))
        self.assertFalse(self.dest.exists(),
                         "the subtitle belonged to bytes that are no longer there")

    def test_a_sidecar_that_appeared_meanwhile_is_preserved(self) -> None:
        self.dest.write_text("someone else got there first\n", encoding="utf-8")
        with self.assertRaises(sf.ConcurrentSidecarError):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}),
                           Response(SRT.encode("utf-8")))
        self.assertEqual(self.dest.read_text(encoding="utf-8"),
                         "someone else got there first\n")

    def test_an_anonymous_rejection_explains_the_fix(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--auth-mode user"):
            self._download(http_error(403, b"anonymous downloads disabled"))

    def test_an_unsupported_auth_mode_is_refused(self) -> None:
        self.cfg.auth_mode = "telepathy"
        with self.assertRaisesRegex(RuntimeError, "unsupported authentication mode"):
            self._download(json_response({"link": "https://dl.opensubtitles.com/f/42.srt"}))


if __name__ == "__main__":
    unittest.main()
