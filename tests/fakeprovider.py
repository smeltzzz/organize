"""Canned OpenSubtitles answers, shared by every suite that speaks to the tier.

The extractor's provider tier is walked by two kinds of test: the decision-level
suite (``tests/test_subtitle_extract.py``) drives it in-process, and the
end-to-end suite (``tests/test_subtitle_extract_e2e.py``) reaches it from a real
run against a fake toolchain. Both need the same three things - a search
response, a download response, and the bytes a CDN would serve - so they are
defined once here rather than twice, exactly like ``fake_mkvmerge.py`` and
``fake_ffprobe.py`` are shared by the tool end-to-end suites.

Nothing in here touches the network: ``FakeTransport`` is a callable standing in
for ``urlopen``, and it records every request it was handed so a test can assert
what was asked for as well as what came back.
"""

from __future__ import annotations

import io
import json

__all__ = [
    "FULL_SRT_PAYLOAD",
    "SRT_PAYLOAD",
    "FakeHttpResponse",
    "FakeTransport",
    "download_answer",
    "hash_search_transport",
    "http_error",
    "provider_entry",
    "search_answer",
]

#: The bytes a CDN would serve for a downloaded subtitle: real, English, and
#: long enough to pass the cue floor the end-to-end tests run with.
SRT_PAYLOAD = (
    b"1\n00:00:01,000 --> 00:00:02,000\nA line of English dialogue\n\n"
    b"2\n00:00:03,000 --> 00:00:04,000\nAnd another line after it\n"
)


#: A payload that clears the default ``--download-min-cues`` floor of 10, for
#: tests that want the *default* gate exercised rather than a lowered one.
FULL_SRT_PAYLOAD = "\n\n".join(
    f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},500\nLine number {index} of dialogue"
    for index in range(1, 13)
).encode("utf-8") + b"\n"


def search_answer(*entries: dict) -> bytes:
    """A canned ``GET /subtitles`` body."""
    return json.dumps({
        "total_pages": 1, "total_count": len(entries), "per_page": 50, "page": 1,
        "data": list(entries),
    }).encode("utf-8")


def provider_entry(
    file_id: int,
    *,
    language: str = "en",
    hash_match: bool = True,
    hearing_impaired: bool = False,
    foreign_parts_only: bool = False,
    machine_translated: bool = False,
    ai_translated: bool = False,
    included: bool = True,
    downloads: int = 10,
    release: str = "Fake.2021.1080p.WEB-DL",
    subtitle_id: str = "555",
    trusted: bool = True,
) -> dict:
    return {
        "id": subtitle_id,
        "type": "subtitle",
        "attributes": {
            "subtitle_id": subtitle_id,
            "language": language,
            "download_count": downloads,
            "hearing_impaired": hearing_impaired,
            "foreign_parts_only": foreign_parts_only,
            "machine_translated": machine_translated,
            "ai_translated": ai_translated,
            "from_trusted": trusted,
            "release": release,
            "moviehash_match": hash_match,
            "feature_details": {"title": "Fake", "year": 2021, "movie_name": "Fake"},
            "files": ([{"file_id": file_id, "cd_number": 1, "file_name": "Fake.2021.srt"}]
                      if included else []),
        },
    }


def download_answer(link: str = "https://dl.opensubtitles.com/download/abc/Fake.2021.srt",
                    remaining: int = 4) -> bytes:
    """A canned ``POST /download`` body."""
    return json.dumps({
        "link": link, "file_name": "Fake.2021.srt", "requests": 1, "remaining": remaining,
        "message": "", "reset_time": "03 hours", "reset_time_utc": "2026-01-01 00:00:00",
    }).encode("utf-8")


class FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, _limit: int = -1) -> bytes:
        return self._payload

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class FakeTransport:
    """Stands in for ``urlopen``: records every request, serves canned answers."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None) -> FakeHttpResponse:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("the test transport ran out of canned answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, bytes)
        return FakeHttpResponse(answer)

    def urls(self) -> list[str]:
        return [str(getattr(request, "full_url", request)) for request in self.requests]

    def headers(self, index: int) -> dict:
        """The request's headers, keyed case-insensitively (urllib capitalizes)."""
        request = self.requests[index]
        return {str(key).lower(): str(value)
                for key, value in getattr(request, "headers", {}).items()}


def hash_search_transport(
    *,
    file_id: int = 9001,
    link: str = "https://dl.opensubtitles.com/download/abc/Fake.2021.srt",
    payload: bytes = SRT_PAYLOAD,
    entry: dict | None = None,
) -> FakeTransport:
    """The three answers a successful exact-hash download takes, in order.

    Search, then the download request, then the CDN fetch - the same sequence a
    live run makes, so a test that only wants "a download happened" does not
    have to spell it out again.
    """
    return FakeTransport(
        search_answer(entry if entry is not None else provider_entry(file_id)),
        download_answer(link),
        payload,
    )


def http_error(code: int, body: bytes = b"", headers: dict | None = None) -> Exception:
    from urllib.error import HTTPError

    return HTTPError("https://api.opensubtitles.com/api/v1/download", code, "error",
                     headers or {}, io.BytesIO(body))
