"""Fake subtitle providers: enough of two APIs to run the fetcher against.

`subtitle_fetcher.py` is the only tool here that reaches the internet, and the
code that decides *what to do with what came back* — retry or give up, spend a
download reservation or defer to tomorrow, write the sidecar or refuse it — can
only be reached through an HTTP response. Mocking the client out tests the
planners and skips the run.

So this stands in one layer lower, at `urllib.request.urlopen`: the real client
builds the real request, sets its real headers, and gets a response object back.
A test says what the provider returns; everything between that and the file on
disk is the tool's own code.

OpenSubtitles routes served by :class:`FakeOpenSubtitles`:

* ``POST /login``      -> a JWT, or whatever the test asked for
* ``GET  /subtitles``  -> a search payload (hash searches and title/year
  searches are answered separately, because the fetcher treats them
  differently)
* ``POST /download``   -> a link to the subtitle file
* the download link    -> the SRT bytes

SubDL routes served by :class:`FakeSubdl`:

* ``GET /files/search``      -> the release-aware match route, with a
  per-subtitle ``match_score`` (the fetcher requires >= 0.80 to treat a hit
  as a release match)
* ``GET /subtitles/search``  -> the weaker title route
* ``GET /subtitles/<id>/download?format=file`` -> the SRT bytes

:class:`FakeSites` stands in for the seven scraped sources — plain HTML
pages, no API and no key — and :func:`subf2m_pages` builds one complete walk
through Subf2m (search list, movie page, download page, zipped SRT).

:class:`FakeProviders` routes by host so a run can be offered all of them at
once, which is the only way to test that the tiers are pooled and failed over
the way the fetcher documents.

Any route can be made to fail with a status code, a body, or a burst of
failures followed by success.
"""

from __future__ import annotations

import email.message
import io
import json
import re
import urllib.error
import urllib.parse
import zipfile
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


# ---------------------------------------------------------------------------
# SubDL
# ---------------------------------------------------------------------------

SUBDL_API_BASE = "https://api.subdl.com/api/v2"


def subdl_subtitle(
    n_id: str,
    release: str,
    *,
    language: str = "en",
    downloads: int = 400,
    match_score: float | None = 0.95,
    media_format: str = "srt",
    hearing_impaired: bool = False,
) -> dict[str, Any]:
    """One entry of a SubDL search response, in the v2 shape."""
    entry: dict[str, Any] = {
        "n_id": n_id,
        "release_name": release,
        "name": f"{release}.srt",
        "language": language,
        "format": media_format,
        "downloads": downloads,
        "hi": hearing_impaired,
    }
    if match_score is not None:
        entry["match_score"] = match_score
    return entry


class FakeSubdl:
    """A programmable SubDL: the two search routes and the download.

    Both search routes are *per movie* — the release route is answered from
    the filename the client sent, the title route from the title it sent — so
    a library of several movies gets several answers, the way it would from
    the real service.
    """

    def __init__(
        self,
        *,
        release_results: list[dict[str, Any]] | None = None,
        title_results: list[dict[str, Any]] | None = None,
        title: str = "The Dark Knight",
        year: int = 2008,
        imdb_id: str = "tt0468569",
        srt_text: str = SRT_TEXT,
    ) -> None:
        self.srt_text = srt_text
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, list[Any]] = {}
        self.movies: dict[str, dict[str, Any]] = {}
        self.add_movie(title, year, imdb_id=imdb_id,
                       release_results=release_results, title_results=title_results)

    # -- test-facing helpers ----------------------------------------------

    def add_movie(
        self,
        title: str,
        year: int,
        *,
        imdb_id: str = "tt0000000",
        release_results: list[dict[str, Any]] | None = None,
        title_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.movies[_key(title)] = {
            "title": title, "year": year, "imdb_id": imdb_id,
            "release": list(release_results or []),
            "titled": list(title_results or []),
        }

    @property
    def release_results(self) -> list[dict[str, Any]]:
        return next(iter(self.movies.values()))["release"]

    @release_results.setter
    def release_results(self, value: list[dict[str, Any]]) -> None:
        next(iter(self.movies.values()))["release"] = list(value)

    @property
    def title(self) -> str:
        return next(iter(self.movies.values()))["title"]

    @title.setter
    def title(self, value: str) -> None:
        entry = self.movies.pop(next(iter(self.movies)))
        entry["title"] = value
        self.movies[_key(value)] = entry

    @property
    def year(self) -> int:
        return next(iter(self.movies.values()))["year"]

    @year.setter
    def year(self, value: int) -> None:
        next(iter(self.movies.values()))["year"] = value

    def fail(self, fragment: str, *responses: Any) -> None:
        self.failures.setdefault(fragment, []).extend(responses)

    def count(self, fragment: str) -> int:
        return sum(1 for _method, path in self.calls if fragment in path)

    # -- the urlopen replacement -------------------------------------------

    def _lookup(self, text: str) -> dict[str, Any] | None:
        wanted = _key(text)
        for key, entry in self.movies.items():
            if key and key in wanted:
                return entry
        return None

    @staticmethod
    def _feature(entry: dict[str, Any]) -> dict[str, Any]:
        return {"name": entry["title"], "type": "movie", "year": entry["year"],
                "imdb_id": entry["imdb_id"]}

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        path = url[len(SUBDL_API_BASE):] if url.startswith(SUBDL_API_BASE) else url
        self.calls.append((request.get_method(), path))

        for fragment, queued in self.failures.items():
            if fragment in url and queued:
                item = queued.pop(0)
                status, body = item if isinstance(item, tuple) else (item, "provider error")
                if status == "garbage":
                    return FakeResponse(b"<html>not json</html>")
                if status == "rejected":
                    return _json_response({"status": False, "error": str(body)})
                if status == "urlerror":
                    raise urllib.error.URLError(body)
                raise urllib.error.HTTPError(
                    url, int(status), str(body), _Headers({"Retry-After": "0"}),
                    io.BytesIO(str(body).encode("utf-8")),
                )

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if path.startswith("/files/search"):
            entry = self._lookup(query.get("filename", [""])[0])
            if entry is None:
                return _json_response({"status": True, "subtitles": []})
            return _json_response({"status": True, "match": self._feature(entry),
                                   "subtitles": entry["release"]})
        if path.startswith("/subtitles/search"):
            entry = self._lookup(query.get("film_name", [""])[0])
            if entry is None:
                return _json_response({"status": True, "results": [], "subtitles": []})
            return _json_response({"status": True, "results": [self._feature(entry)],
                                   "subtitles": entry["titled"]})
        if path.startswith("/subtitles/") and "download" in path:
            data = self.srt_text.encode("utf-8")
            return FakeResponse(data, headers={"Content-Length": str(len(data))})
        raise AssertionError(f"the fetcher asked SubDL for an unexpected URL: {url}")


def _key(text: str) -> str:
    """A loose comparison key: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", str(text).casefold())


SUBF2M_BASE = "https://subf2m.co"


class FakeSites:
    """The scraped tier: every site at once, behind one `urlopen`.

    The scraping sources have no API and no keys — they are HTML pages the
    fetcher parses. A test hands this a mapping of URL path prefix to page
    body; anything not in the mapping answers 404, which is what a source
    with nothing for this movie looks like from the outside. Every request
    is recorded, so a test can also assert on *which* sites were consulted
    and in what order.
    """

    def __init__(self, pages: dict[str, bytes] | None = None) -> None:
        self.pages = dict(pages or {})
        self.calls: list[tuple[str, str]] = []

    def hosts(self) -> list[str]:
        """The sites consulted, in the order they were first asked."""
        seen: list[str] = []
        for host, _path in self.calls:
            if host not in seen:
                seen.append(host)
        return seen

    def count(self, fragment: str) -> int:
        return sum(1 for _host, path in self.calls if fragment in path)

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        split = urllib.parse.urlsplit(request.full_url)
        self.calls.append((split.netloc, split.path))
        matches = [key for key in self.pages if split.path.startswith(key)]
        if not matches:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", _Headers({}), io.BytesIO(b""))
        return FakeResponse(self.pages[max(matches, key=len)])


def subf2m_pages(
    *,
    title: str = "The Dark Knight",
    year: int = 2008,
    slug: str = "the-dark-knight",
    srt_text: str = SRT_TEXT,
) -> dict[str, bytes]:
    """The four pages one Subf2m download walks through.

    Search result list -> movie page (download rows) -> download page ->
    the zipped SRT, mirroring the structure the adapter documents.
    """
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{slug}.utf8.srt", srt_text)
    return {
        "/subtitles/searchbytitle": (
            '<div class="search-result"><ul>'
            f'<li><a href="/subtitles/{slug}">{title} ({year})</a></li>'
            "</ul></div>"
        ).encode(),
        f"/subtitles/{slug}/en": (
            '<ul><li class="item">'
            f'<a href="/subtitles/{slug}/english/12345" class="download icon-download">'
            f"{title} 1080p BluRay</a></li></ul>"
        ).encode(),
        f"/subtitles/{slug}/english/12345": (
            b'<div class="download"><a href="/dl/12345">Download</a></div>'
        ),
        "/dl/12345": archive.getvalue(),
    }


class FakeProviders:
    """Both APIs and the scraped sites behind one `urlopen`, routed by host."""

    def __init__(self, opensubtitles: FakeOpenSubtitles, subdl: FakeSubdl,
                 sites: FakeSites | None = None) -> None:
        self.opensubtitles = opensubtitles
        self.subdl = subdl
        self.sites = sites if sites is not None else FakeSites()

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        if url.startswith(SUBDL_API_BASE):
            target: Any = self.subdl
        elif url.startswith((API_BASE, DOWNLOAD_HOST)):
            target = self.opensubtitles
        else:
            target = self.sites
        return target(request, timeout=timeout)


def _json_response(payload: dict[str, Any]) -> FakeResponse:
    body = json.dumps(payload).encode("utf-8")
    return FakeResponse(body, headers={"Content-Type": "application/json",
                                       "Content-Length": str(len(body))})
