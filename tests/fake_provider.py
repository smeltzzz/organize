"""A fake OpenSubtitles: enough of the API to run the fetcher against.

`subtitle_fetcher.py` is the only tool here that reaches the internet, and the
code that decides *what to do with what came back* — retry or give up, spend a
download reservation or defer to tomorrow, write the sidecar or refuse it — can
only be reached through an HTTP response. Mocking the client out tests the
planners and skips the run.

So this stands in one layer lower, at `urllib.request.urlopen`: the real client
builds the real request, sets its real headers, and gets a response object back.
A test says what the provider returns; everything between that and the file on
disk is the tool's own code.

Routes served:

* ``POST /login``      -> a JWT, or whatever the test asked for
* ``GET  /subtitles``  -> a search payload (hash searches and title/year
  searches are answered separately, because the fetcher treats them
  differently)
* ``POST /download``   -> a link to the subtitle file
* the download link    -> the SRT bytes

Any route can be made to fail with a status code, a body, or a burst of
failures followed by success.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from typing import Any

API_BASE = "https://api.opensubtitles.com/api/v1"
DOWNLOAD_HOST = "https://dl.opensubtitles.example"
DOWNLOAD_LINK = f"{DOWNLOAD_HOST}/subtitle.srt"

SRT_TEXT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "You either die a hero, or you live long enough\n"
    "to see yourself become the villain.\n"
    "\n"
    "2\n"
    "00:00:05,500 --> 00:00:09,000\n"
    "Why do we fall? So we can learn to pick ourselves up.\n"
)


def subtitle(
    file_id: int,
    release: str,
    *,
    moviehash_match: bool = True,
    downloads: int = 500,
    language: str = "en",
    title: str = "",
    year: int = 0,
    machine_translated: bool = False,
    ai_translated: bool = False,
    hearing_impaired: bool = False,
    trusted: bool = True,
) -> dict[str, Any]:
    """One entry of a `/subtitles` response, in the provider's shape."""
    return {
        "id": str(file_id),
        "type": "subtitle",
        "attributes": {
            "language": language,
            "download_count": downloads,
            "votes": 12,
            "ratings": 8.5,
            "from_trusted": trusted,
            "hearing_impaired": hearing_impaired,
            "machine_translated": machine_translated,
            "ai_translated": ai_translated,
            "foreign_parts_only": False,
            "moviehash_match": moviehash_match,
            "release": release,
            "feature_details": {"title": title, "year": year, "feature_type": "Movie"},
            "files": [{"file_id": file_id, "file_name": release}],
        },
    }


class _Headers(email.message.Message):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        super().__init__()
        for key, value in (values or {}).items():
            self[key] = value


class FakeResponse:
    """What `urlopen` hands back: a context manager that can be read once."""

    def __init__(self, body: bytes, status: int = 200,
                 headers: dict[str, str] | None = None) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = _Headers(headers)

    def read(self, amt: int | None = None) -> bytes:
        return self._body.read() if amt is None else self._body.read(amt)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class FakeOpenSubtitles:
    """A programmable provider. Call it where `urlopen` would be called."""

    def __init__(
        self,
        *,
        hash_results: list[dict[str, Any]] | None = None,
        identity_results: list[dict[str, Any]] | None = None,
        srt_text: str = SRT_TEXT,
        download_link: str = DOWNLOAD_LINK,
        token: str = "fake-jwt-token",
    ) -> None:
        self.hash_results = list(hash_results or [])
        self.identity_results = list(identity_results or [])
        self.srt_text = srt_text
        self.download_link = download_link
        self.token = token
        # Fired just before the subtitle bytes are served, so a test can change
        # the library from inside a live fetch.
        self.on_download: Any = None
        # (method, path) for every request that reached the provider.
        self.calls: list[tuple[str, str]] = []
        # path fragment -> list of responses to give before behaving normally.
        # Each item is either an int status code or (status, body).
        self.failures: dict[str, list[Any]] = {}

    # -- test-facing helpers ----------------------------------------------

    def fail(self, fragment: str, *responses: Any) -> None:
        """Queue failures for the next requests whose URL contains `fragment`."""
        self.failures.setdefault(fragment, []).extend(responses)

    def count(self, fragment: str) -> int:
        return sum(1 for _method, path in self.calls if fragment in path)

    # -- the urlopen replacement -------------------------------------------

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        method = request.get_method()
        path = url[len(API_BASE):] if url.startswith(API_BASE) else url
        self.calls.append((method, path))

        for fragment, queued in self.failures.items():
            if fragment in url and queued:
                item = queued.pop(0)
                status, body = item if isinstance(item, tuple) else (item, "provider error")
                if status == "garbage":
                    return FakeResponse(b"<html>not json</html>")
                if status == "urlerror":
                    raise urllib.error.URLError(body)
                raise urllib.error.HTTPError(
                    url, int(status), str(body), _Headers({"Retry-After": "0"}),
                    io.BytesIO(str(body).encode("utf-8")),
                )

        if url == self.download_link or url.startswith(DOWNLOAD_HOST):
            if self.on_download is not None:
                self.on_download()
            data = self.srt_text.encode("utf-8")
            return FakeResponse(data, headers={"Content-Length": str(len(data))})
        if path.startswith("/login"):
            return self._json({"token": self.token, "status": 200})
        if path.startswith("/download"):
            return self._json({"link": self.download_link, "remaining": 90,
                               "requests": 10, "file_name": "subtitle.srt"})
        if path.startswith("/subtitles"):
            hashed = "moviehash=" in url
            data = self.hash_results if hashed else self.identity_results
            return self._json({"total_count": len(data), "page": 1, "data": data})
        raise AssertionError(f"the fetcher asked for an unexpected URL: {url}")

    @staticmethod
    def _json(payload: dict[str, Any]) -> FakeResponse:
        return FakeResponse(json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"})
