#!/usr/bin/env python3
"""
English Subtitle Fetcher for Jellyfin Movies
============================================
After ``movie_standardizer.py`` and before ``mkv_track_cleaner.py``: walk the
canonical movie library and create at most one validated external English SRT
sidecar per MKV. This single script owns its persistent UTC request ledger;
there is no separate queue script or launcher to run.

The first thing it tries for any uncovered movie is the movie itself. Most
Jellyfin MKVs already carry an English subtitle as an embedded track, and that
track is exact for this release: it costs no provider request, it cannot be the
wrong cut, and its cues come from the container's own timeline, so the sidecar
needs no timing correction. Text tracks (SRT/SSA/ASS/WebVTT) are extracted with
mkvextract and converted in-process; image tracks (PGS/SUP, VobSub, DVB) are
OCR'd by an external backend (sup2srt + Tesseract, Subtitle Edit, or PgsToSrt)
when one is installed, and are skipped with the exact fix printed when none is.
Forced/signs-only, commentary, non-English, and too-short tracks are refused, so
a movie is never left with a partial "subtitle"; those movies simply fall
through to the providers as before. Extraction is recorded outside the library,
and sync_subtitles.py reads that record so an extracted sidecar - already
frame-accurate - is never handed to ffsubsync. Disable it with --no-extract.

OpenSubtitles and SubDL are treated as equal sources. Both providers'
release-identifying routes are consulted for every movie - the exact
OpenSubtitles moviehash and SubDL's score-gated release-aware filename
match (score >= 0.80) - and the qualifying release with the most downloads
is downloaded, whichever provider it came from. When neither release route
yields a pick, both providers' strict title/year routes are pooled the same
way. A wrong cut or a tie the quality signals cannot break is held for
review rather than downloaded.

When every API source misses, the fetcher does not stop: seven scraping
sources are consulted in a fixed failover order - Subf2me, Podnapisi,
Addic7ed, SubSource, Subsunacs, YIFY Subtitles, and Subs.Sab.BZ - vendored
in the scraping-sources section of this file (Python standard library only,
no keys, no accounts). A scraped candidate is only accepted when it names
the movie, matches its release year, and decodes to a valid English SRT;
each source carries a per-run circuit breaker and a UTC daily search cap so
one dead or hostile site can never stall the library. The product goal is a
validated English SRT beside every movie: movies that still lack one are
listed by name in the report, retried on the next UTC day, and make the
process exit non-zero (override with --allow-missing) until they are
covered.

A candidate is auto-selected only when its release name carries the movie
title, the release year, and an explicit Blu-ray keyword (``BluRay``,
``Blu-ray``, ``BLU RAY``, ...). Among the qualifying candidates the one
with the highest download count wins; the trusted flag, community rating and
votes remain as tiebreakers, and a tie they cannot break is held for manual
review. There is no separate rating/votes quality floor, so popular but
unvoted subtitles for big-name movies are fetched automatically.

The position in the pipeline is deliberate, not cosmetic. The moviehash is the
file size plus the sum of the first and last 64 KiB, and this tool submits it
with ``moviehash_match=only`` so the provider returns only subtitles uploaded
against a byte-identical release. ``mkv_track_cleaner.py`` rewrites those bytes,
so any movie that is remuxed first can never reproduce its release hash again
and is silently reduced to the far weaker title/year fallback. Fetching first
keeps the pristine release hash available while it still exists.

    py -3 subtitle_fetcher.py --dry-run
    py -3 subtitle_fetcher.py
    py -3 subtitle_fetcher.py --self-test

The default policy intentionally downloads only UTF-8 SRT sidecars. SRT is the
most broadly direct-play-safe external subtitle choice across Jellyfin clients;
ASS/SSA, VobSub, PGS, and other formats are never requested or written here.

Configure one or both API providers through environment variables (the
scraping sources need no credentials at all):
    set OPENSUBTITLES_API_KEY=...
    set SUBDL_API_KEY=...

Credentials are read only from environment variables, never command-line
arguments. Development-anonymous mode uses only the OpenSubtitles API key for
consumers that OpenSubtitles currently permits to download anonymously.
Authenticated user mode remains available as an explicit fallback. A run with
no API keys configured still works: every movie is offered to the scraping
sources instead.

OpenSubtitles key: https://www.opensubtitles.com/en/consumers
SubDL key: https://subdl.com/panel/api
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html as _html
import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Shared implementation: everything imported here is defined exactly once,
# in organizekit/core/. See tests/test_shared_core.py for the rule that
# keeps it that way.
from organizekit.core import (
    COVERING_ENGLISH_SRT_SUFFIXES,
    EXTERNAL_SRT_ENCODINGS,
    EXTERNAL_SRT_MAX_BYTES,
    EXTERNAL_SRT_SUFFIX,
    REPORT_WIDTH,
    BucketRegistry,
    CoordinationLock,
    Report,
    atomic_write_text,
    default_tool_dir,
    enable_utf8_stdio,
    exact_external_english_srt_path,
    host_key,
    normalize_srt_newlines,
    path_norm,
    print_text,
    promote_legacy_external_english_srt,
    resolve_library,
    run_field_smoke_test,
    srt_looks_valid,
    validate_srt_sidecar,
)

# ---------------------------------------------------------------------------
# External English SRT sidecar contract
# ---------------------------------------------------------------------------
# Every tool in the pipeline that reasons about an external subtitle agrees on
# the same conservative contract: a plain-text file beside the movie, small,
# non-empty, and carrying at least one well-formed cue.  The content verdict
# lives here so a new tool cannot quietly disagree with the others about
# whether a sidecar is usable.
#
# The cue pattern is the tolerant form: leading whitespace before the cue
# number is accepted, because some muxers and editors indent it.  This is a
# "does it look like a subtitle at all" test, not a full SRT parser.
#
# Canonical language tag is ISO 639-2/B ``eng`` (``.eng.srt``).  The older
# ISO 639-1 ``.en.srt`` form is recognized only as a legacy rename source so a
# library cut over from the previous convention is not stuck in review.


# The single agreed decode order. Every tool that turns subtitle bytes into
# text uses this tuple and nothing else, so a tool cannot quietly accept an
# encoding the others would reject. "utf-8-sig" first so a provider BOM does
# not make an otherwise valid file look binary; "cp1252" last because it
# decodes almost any byte sequence and would mask a genuine encoding problem.


def covering_english_srt_paths(media_path: Path) -> tuple[Path, ...]:
    """Return ``.eng.srt`` then ``.eng.sdh.srt`` beside a movie file."""
    return tuple(
        media_path.with_name(f"{media_path.stem}{suffix}")
        for suffix in COVERING_ENGLISH_SRT_SUFFIXES
    )


def is_covering_english_sidecar(path: Path, media_path: Path) -> bool:
    wanted = {candidate.name.casefold() for candidate in covering_english_srt_paths(media_path)}
    return path.name.casefold() in wanted


def report_banner(
    title: str,
    subtitle: str = "",
    meta: Iterable[tuple[str, object]] = (),
    *,
    width: int = REPORT_WIDTH,
) -> str:
    """The boxed header on its own, for a tool's startup print."""
    report = Report(title, subtitle, width=width)
    report.metas(meta)
    return report.render_header()

# =============================================================================
# CONFIGURATION

# =============================================================================
# SCRAPING FALLBACK SOURCES (vendored)
#
# Tier 3: seven scraping subtitle sources, consulted in fixed failover
# order when the OpenSubtitles/SubDL API tiers miss. Originally developed
# as the standalone module subtitle_sources.py; vendored here so the
# fetcher remains one self-contained file. Standard library only, no keys,
# no accounts. Adapters raise ScrapeSourceError (hard failure) or
# CandidateRejected (soft refusal); ScrapeChain adds the per-run circuit
# breakers, the durable UTC search caps (reserve_cb), and failover.
# =============================================================================

__version__ = "1.0.0"  # overridden below to the real version

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_SUBF2ME = "subf2me"
PROVIDER_PODNAPISI = "podnapisi"
PROVIDER_ADDIC7ED = "addic7ed"
PROVIDER_SUBSOURCE = "subsource"
PROVIDER_SUBSUNACS = "subsunacs"
PROVIDER_YIFY = "yifysubtitles"
PROVIDER_SUBSAB = "subsab"

#: Execution order for the failover chain. API sources (OpenSubtitles, SubDL)
#: run first in subtitle_fetcher.py; this is the order of the scraped chain.
SCRAPE_PROVIDER_ORDER: tuple[str, ...] = (
    PROVIDER_SUBF2ME,
    PROVIDER_PODNAPISI,
    PROVIDER_ADDIC7ED,
    PROVIDER_SUBSOURCE,
    PROVIDER_SUBSUNACS,
    PROVIDER_YIFY,
    PROVIDER_SUBSAB,
)

SCRAPE_PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_SUBF2ME: "Subf2m.co",
    PROVIDER_PODNAPISI: "Podnapisi.NET",
    PROVIDER_ADDIC7ED: "Addic7ed.com",
    PROVIDER_SUBSOURCE: "SubSource.net",
    PROVIDER_SUBSUNACS: "Subsunacs.net",
    PROVIDER_YIFY: "YIFY Subtitles",
    PROVIDER_SUBSAB: "Subs.sab.bz",
}

#: Polite default: search requests per UTC day per scraped source.
DEFAULT_SEARCH_DAILY_CAP = 20

#: A source with this many consecutive hard failures is disabled for the run.
BREAKER_HARD_FAILURES = 3
#: A source whose structure parsing keeps failing is disabled too.
BREAKER_PARSE_FAILURES = 3

SCRAPE_HTTP_TIMEOUT_SEC = 20.0
SCRAPE_REQUEST_GAP_SEC = 1.0
SCRAPE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SCRAPE_MAX_CANDIDATES_PER_SOURCE = 3


SCRAPE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScrapeSourceError(RuntimeError):
    """Hard failure against one source (network, HTTP, structure, archive).

    Counts toward the source's circuit breaker.
    """


class CandidateRejected(RuntimeError):
    """A specific candidate was inspected and refused (soft miss).

    Does not count toward the breaker: the chain simply tries the next
    candidate. Example: a subsunacs subtitle page that turns out to be
    Bulgarian, or a download whose bytes are not an SRT.
    """


class SourceUnavailable(RuntimeError):
    """The chain refused to work a source this run (breaker open or the
    source's UTC daily search cap is exhausted)."""


# ---------------------------------------------------------------------------
# Identity / candidate types (local to avoid a circular import)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceIdentity:
    """Canonical movie identity derived from a ``Title (Year)`` filename."""

    title: str
    year: int
    normalized_title: str = ""


@dataclass
class ScrapeCandidate:
    """One addressable subtitle on one source, before acceptance checks.

    ``file_id`` is the source-specific reference the adapter's ``fetch``
    understands (an id, a URL path, or an attach id). ``downloads`` and
    ``rating`` are best-effort popularity signals used for ordering.
    """

    provider: str
    file_id: str
    release: str = ""
    feature_title: str = ""
    feature_year: int = 0
    downloads: int = 0
    rating: float = 0.0
    hearing_impaired: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transport seam (stdlib urllib by default; tests inject a fake)
# ---------------------------------------------------------------------------


class ScrapeTransport:
    """Small HTTP client seam shared by every adapter.

    ``get``/``post`` return raw bytes and raise :class:`ScrapeSourceError`
    for anything that is not a clean 2xx response within the size limit.

    **The polite gap is per host, not per transport.** One instance drives all
    seven scraped sources, and they are seven different servers: making a
    request to subf2m wait a second because the previous request went to
    podnapisi protected nobody and, over a large library, cost roughly a second
    per source per movie. Each host now has its own token bucket, so every site
    still sees at most one request per ``gap`` seconds and unrelated sites do
    not queue behind each other.
    """

    def __init__(self, *, timeout: float = SCRAPE_HTTP_TIMEOUT_SEC,
                 gap: float = SCRAPE_REQUEST_GAP_SEC,
                 sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.timeout = timeout
        self.gap = gap
        # ``sleep``/``clock`` are the test seam: pacing is arithmetic, and a
        # test should be able to prove it without spending the seconds.
        self.buckets = BucketRegistry(gap=gap, sleep=sleep, clock=clock)

    def _throttle(self, url: str) -> float:
        """Wait until ``url``'s host may be asked again. Returns seconds slept."""
        return self.buckets.take(host_key(url))

    def _open(self, url: str, data: bytes | None, headers: dict[str, str]) -> bytes:
        base = {
            "User-Agent": SCRAPE_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        base.update(headers or {})
        req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                     headers=base)
        try:
            # URLs here are fixed provider endpoints (see the adapter that
            # built them); user-controlled data only ever appears in a
            # percent-encoded query string or POST body.
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                status = getattr(resp, "status", 200)
                if not (200 <= int(status) < 300):
                    raise ScrapeSourceError(f"HTTP {status} for {urllib.parse.urlsplit(url).path}")
                raw = resp.read(SCRAPE_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ScrapeSourceError(f"HTTP {exc.code} for {urllib.parse.urlsplit(url).path}") from exc
        except urllib.error.URLError as exc:
            raise ScrapeSourceError(f"network error for {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ScrapeSourceError(f"transport error for {urllib.parse.urlsplit(url).netloc}: {exc}") from exc
        if len(raw) > SCRAPE_MAX_RESPONSE_BYTES:
            raise ScrapeSourceError("response exceeds the size limit")
        return raw

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        self._throttle(url)
        return self._open(url, None, headers or {})

    def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        self._throttle(url)
        return self._open(url, data, hdrs)


def default_transport() -> ScrapeTransport:
    return ScrapeTransport()


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def unescape(text: str) -> str:
    return _html.unescape(text or "")


def strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", " ", fragment or "")


def scrape_normalize_title(text: str) -> str:
    """Lowercase, de-accent-free token set used for title comparisons."""
    value = unescape(text or "").casefold()
    value = re.sub(r"\(hearing impaired\)|\[hi\]|\(hi\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return re.sub(r"\s+", " ", value)


def title_tokens(text: str) -> frozenset[str]:
    return frozenset(scrape_normalize_title(text).split())


def title_similarity(a: str, b: str) -> float:
    """Token-overlap similarity in [0, 1]; containment scores 1.0."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb or ta <= tb or tb <= ta:
        return 1.0
    return len(ta & tb) / min(len(ta), len(tb))


def titles_match(a: str, b: str, *, threshold: float = 0.6) -> bool:
    if not a or not b:
        return False
    return title_similarity(a, b) >= threshold


def looks_like_srt_text(text: str) -> bool:
    """At least one well-formed cue: index line + ``HH:MM:SS,mmm --> ...``."""
    if not text or len(text) > 4 * 1024 * 1024:
        return False
    return bool(re.search(
        r"(?m)^\s*\d{1,6}\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}",
        text,
    ))


def decode_scrape_subtitle_bytes(raw: bytes) -> str:
    """utf-8-sig, then utf-8, then cp1252 — the shared sidecar contract."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" in text:
            continue
        return text
    raise ScrapeSourceError("subtitle bytes are not decodable text (not a subtitle?)")


def mostly_cyrillic(text: str) -> bool:
    """Heuristic language guard for sources that expose no language metadata.

    True when the letter content is dominated by Cyrillic: a Bulgarian (or
    any Cyrillic) subtitle must never be installed as the English sidecar.
    """
    cyr = lat = 0
    for ch in text:
        if "\u0400" <= ch <= "\u04FF":
            cyr += 1
        elif ch.isalpha() and ord(ch) < 0x0250:
            lat += 1
    if cyr + lat < 8:
        return False
    return cyr > 0.3 * (cyr + lat)


def slugify(title: str) -> str:
    """The SubSource-style slug: lowercase, apostrophes dropped, runs of
    anything non-alphanumeric collapsed to a single hyphen."""
    value = unescape(title or "").casefold().replace("'", "").replace("\u2019", "")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return re.sub(r"-{2,}", "-", value)


def first_bytes_are_zip(raw: bytes) -> bool:
    return raw[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def pick_zip_subtitle(raw: bytes) -> bytes:
    """Extract the SRT payload from a one-file subtitle archive.

    Prefers an entry whose name advertises UTF-8 (Subf2m ships a UTF-8 and a
    non-UTF-8 copy in the same zip), then the first .srt, then the first
    entry. Raises ScrapeSourceError for non-zips and unreadable archives.
    """
    if not first_bytes_are_zip(raw):
        raise ScrapeSourceError("expected a subtitle archive, got a non-zip payload")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if not names:
                raise ScrapeSourceError("subtitle archive is empty")
            utf_entry = next((n for n in names if "utf" in n.casefold()), None)
            srt_entry = next((n for n in names if n.casefold().endswith(".srt")), None)
            chosen = utf_entry or srt_entry or names[0]
            return zf.read(chosen)
    except zipfile.BadZipFile as exc:
        raise ScrapeSourceError(f"unreadable subtitle archive: {exc}") from exc


def valid_srt_bytes(raw: bytes) -> bool:
    if not raw or len(raw) > 4 * 1024 * 1024:
        return False
    try:
        return looks_like_srt_text(decode_scrape_subtitle_bytes(raw))
    except ScrapeSourceError:
        return False


def absolute_url(base: str, value: str) -> str:
    value = (value or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = "/" + value
    return base.rstrip("/") + value


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class BaseSource:
    """One community subtitle source.

    ``search`` returns candidates (metadata only, no download). ``fetch``
    retrieves one candidate's payload bytes; it must raise
    :class:`CandidateRejected` for "wrong subtitle" outcomes and
    :class:`ScrapeSourceError` for "source is broken" outcomes.
    """

    key: str = ""
    label: str = ""

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        raise NotImplementedError

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Subf2m.co
# ---------------------------------------------------------------------------


class Subf2meSource(BaseSource):
    """Subf2m.co: title search (language-scoped), movie page, zipped SRT.

    Verified against the site's own structure (as consumed by the Emby
    Subf2m plugin): ``/subtitles/searchbytitle?query=..&l=en`` returns a
    ``div.search-result`` whose ``ul`` lists ``Title (YYYY)`` links; the
    movie page (``<link>/<lang>``) holds ``li.item`` rows with
    ``a.download.icon-download`` links; the download page carries a
    ``div.download`` link to a zip.
    """

    key = PROVIDER_SUBF2ME
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBF2ME]
    BASE = "https://subf2m.co"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        url = f"{self.BASE}/subtitles/searchbytitle?query={urllib.parse.quote_plus(identity.title)}&l=en"
        page = t.get(url)
        text = page.decode("utf-8", errors="replace")
        marker = re.search(r"<div[^>]*class=[\"'][^\"']*search-result[^\"']*[\"']", text, re.I)
        if not marker:
            return []
        region = text[marker.start():]
        ul = re.search(r"<ul.*?</ul>", region, re.S | re.I)
        if ul:
            region = ul.group(0)
        else:
            region = region[:4000]
        cands: list[ScrapeCandidate] = []
        for href, inner in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", region, re.S | re.I):
            inner_text = unescape(strip_tags(inner))
            year = re.search(r"\((\d{4})\)", inner_text)
            if not year or int(year.group(1)) != identity.year:
                continue
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", inner_text).strip()
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href, release=title,
                feature_title=title, feature_year=int(year.group(1)),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        movie_page_url = absolute_url(self.BASE, candidate.file_id)
        if not movie_page_url.rstrip("/").endswith("/en"):
            movie_page_url = movie_page_url.rstrip("/") + "/en"
        page = t.get(movie_page_url).decode("utf-8", errors="replace")
        download_href: str | None = None
        # Split on the item markers (nested <li> children make a simple
        # (.*?)</li> capture stop at the wrong closing tag); each segment is
        # one row's subtree, which holds its own download anchor.
        segments = re.split(r"<li[^>]*class=[\"'][^\"']*item[^\"']*[\"'][^>]*>", page, flags=re.I)
        for block in segments[1:]:
            m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*class=[\"'][^\"']*download[^\"']*[\"']", block, re.I) \
                or re.search(r"<a[^>]+class=[\"'][^\"']*download[^\"']*[\"'][^>]*href=[\"']([^\"']+)[\"']", block, re.I)
            if m:
                download_href = m.group(1)
                break
        if not download_href:
            raise ScrapeSourceError("no download rows on the movie page")
        dl_page = t.get(absolute_url(self.BASE, download_href)).decode("utf-8", errors="replace")
        dl_div = re.search(r"<div[^>]+class=[\"'][^\"']*download[^\"']*[\"'][^>]*>(.*?)</div>", dl_page, re.S | re.I)
        scope = dl_div.group(1) if dl_div else dl_page
        m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", scope, re.I)
        if not m:
            raise ScrapeSourceError("no download link on the download page")
        raw = t.get(absolute_url(self.BASE, m.group(1)))
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 2. Podnapisi.NET
# ---------------------------------------------------------------------------


class PodnapisiSource(BaseSource):
    """Podnapisi.NET's documented JSON advanced-search (movies only).

    ``GET /subtitles/search/advanced?keywords=..&language=en&movie_type=movie
    &year=..`` returns ``{"data": [{id, releases[], custom_releases[],
    movie:{title, year}}], "page", "all_pages"}``. Download:
    ``GET /subtitles/<id>/download?container=zip`` (single-file zip).
    """

    key = PROVIDER_PODNAPISI
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_PODNAPISI]
    BASE = "https://www.podnapisi.net/subtitles"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        params = {
            "keywords": identity.title,
            "language": "en",
            "movie_type": "movie",
            "year": str(identity.year),
        }
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for page_no in (1, 2):  # the site paginates; two pages are plenty
            params["page"] = str(page_no)
            payload = json.loads(t.get(f"{self.BASE}/search/advanced?{urllib.parse.urlencode(params)}").decode("utf-8", "replace"))
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise ScrapeSourceError("unexpected search payload shape")
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("id")
                if pid is None or str(pid) in seen:
                    continue
                seen.add(str(pid))
                movie = entry.get("movie") or {}
                try:
                    year = int(movie.get("year") or 0)
                except (TypeError, ValueError):
                    year = 0
                if year and year != identity.year:
                    continue
                releases = list(entry.get("releases") or []) + list(entry.get("custom_releases") or [])
                cands.append(ScrapeCandidate(
                    provider=self.key, file_id=str(pid),
                    release=next((str(r) for r in releases if str(r).strip()), ""),
                    feature_title=str(movie.get("title") or ""),
                    feature_year=year,
                ))
                if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                    break
            try:
                if int(payload.get("page") or 1) >= int(payload.get("all_pages") or 1):
                    break
            except (TypeError, ValueError):
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raw = t.get(f"{self.BASE}/{candidate.file_id}/download?container=zip")
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 3. Addic7ed.com
# ---------------------------------------------------------------------------


class Addic7edSource(BaseSource):
    """Addic7ed.com movies: ``srch.php`` search, movie page, gated SRT.

    Verified against the site's current layout (as consumed by the
    addic7ed-api scraper): the search page lists ``href="movie/<id>"`` for
    movie hits; the movie page contains ``Version <release>,`` blocks whose
    rows pair ``td.language`` text with a ``Download``/``most updated``
    anchor (``a.buttonDownload``) and an ``N Downloads`` count. Only
    *Completed* subtitles carry a working download link; the download
    requires a ``Referer`` pointing at the show page.
    """

    key = PROVIDER_ADDIC7ED
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_ADDIC7ED]
    BASE = "https://www.addic7ed.com"

    def _headers(self) -> dict[str, str]:
        return {"Referer": self.BASE}

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        url = f"{self.BASE}/srch.php?search={urllib.parse.quote_plus(identity.title)}&Submit=Search"
        body = t.get(url, headers=self._headers()).decode("utf-8", errors="replace")
        if re.search(r"<b>\s*0\s+results\s+found\s*</b>", body, re.I):
            return []
        movie_links = re.findall(r'href="(movie/\d+)"', body)
        if not movie_links:
            return []
        movie_html = t.get(f"{self.BASE}/{movie_links[0]}", headers=self._headers()).decode("utf-8", errors="replace")
        referer_m = re.search(r"/show/\d+", movie_html)
        referer = f"{self.BASE}{referer_m.group(0)}" if referer_m else f"{self.BASE}/show/1"
        header_m = re.search(r"(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*<small", movie_html, re.S)
        header_title = re.sub(r"\s+", " ", unescape(strip_tags(header_m.group("title")))).strip() if header_m else ""
        try:
            header_year = int(header_m.group("year")) if header_m else 0
        except ValueError:
            header_year = 0
        cands: list[ScrapeCandidate] = []
        version_re = re.compile(r"Version\s+([^,<]+),")
        # Window-based row parsing: layout details (which cells carry
        # anchors, in what order) shift over time, so each language cell is
        # inspected inside its own bounded window instead of with one long
        # all-in-one pattern.
        for lm in re.finditer(r'class="language"[^>]*>', movie_html):
            end = movie_html.find('class="language"', lm.end())
            window = movie_html[lm.end(): end if end != -1 else lm.end() + 3000]
            text = unescape(strip_tags(window))
            # The language name precedes the completion status in the row.
            pre_status = text.split("Completed", 1)[0].strip()
            lang_name = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", pre_status).strip()
            if lang_name.casefold() != "english":
                continue
            if re.search(r"%\s*Completed", text, re.I):
                continue  # "% Completed" rows are not downloadable
            if not re.search(r"Completed", text, re.I):
                continue
            dl = re.search(r'href="([^"]+?)"[^>]*>\s*<strong>\s*(?:most updated|Download)', window, re.I)
            if not dl:
                continue
            dl_count = re.search(r"(\d+)\s*Downloads", text)
            pre_versions = list(version_re.finditer(movie_html[: lm.start()]))
            release = pre_versions[-1].group(1).strip() if pre_versions else ""
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=dl.group(1), release=release,
                feature_title=header_title or identity.title,
                feature_year=header_year or identity.year,
                downloads=int(dl_count.group(1)) if dl_count else 0,
                hearing_impaired="hearing impaired" in pre_status.casefold(),
                extra={"referer": referer},
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        url = absolute_url(self.BASE, candidate.file_id)
        headers = {"Referer": str(candidate.extra.get("referer") or f"{self.BASE}/show/1")}
        raw = t.get(url, headers=headers)
        if not valid_srt_bytes(raw):
            raise CandidateRejected("addic7ed payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# 4. SubSource.net
# ---------------------------------------------------------------------------


class SubSourceSource(BaseSource):
    """SubSource.net (a community subtitle catalog).

    Deterministic slugs (``/subtitles/<slug>-<year>``) are tried first,
    falling back to the public search page (``/search?q=<title>``). The
    movie page lists one row per subtitle file with a language anchor
    (``/subtitle/<slug>/english/<id>``); that file page carries the direct
    API download link (``api.subsource.net/v1/subtitle/download/<hash>``).
    """

    key = PROVIDER_SUBSOURCE
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSOURCE]
    BASE = "https://subsource.net"

    def _movie_page_candidates(self, page: bytes, identity: SourceIdentity) -> list[ScrapeCandidate]:
        text = page.decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for path in re.findall(r'href="(/subtitle/[^"]+/english/(\d+))"', text):
            href = path[0]
            if href in seen:
                continue
            seen.add(href)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href,
                feature_title=identity.title, feature_year=identity.year,
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE:
                break
        return cands

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        slug = slugify(identity.title)
        direct = f"{self.BASE}/subtitles/{slug}-{identity.year}"
        try:
            page = t.get(direct)
        except ScrapeSourceError:
            page = b""
        if page:
            cands = self._movie_page_candidates(page, identity)
            if cands:
                return cands
        search_page = t.get(f"{self.BASE}/search?q={urllib.parse.quote_plus(identity.title)}")
        text = search_page.decode("utf-8", errors="replace")
        slugs = set(re.findall(r'href="(/subtitles/[a-z0-9\-]+)"', text))
        wanted = f"/subtitles/{slug}-{identity.year}"
        cands = []
        if wanted in slugs:
            cands.extend(self._movie_page_candidates(t.get(f"{self.BASE}{wanted}"), identity))
        for other in sorted(s for s in slugs if s.endswith(f"-{identity.year}")):
            if other == wanted or len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE:
                continue
            title_guess = other.rsplit("/", 1)[-1][: -len(f"-{identity.year}")].replace("-", " ")
            if not titles_match(title_guess, identity.title):
                continue
            try:
                cands.extend(self._movie_page_candidates(t.get(f"{self.BASE}{other}"), identity))
            except ScrapeSourceError:
                continue
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(f"{self.BASE}{candidate.file_id}").decode("utf-8", errors="replace")
        m = re.search(r"(https://api\.subsource\.net/v1/subtitle/download/[A-Za-z0-9]+)", page)
        if not m:
            raise ScrapeSourceError("no API download link on the subtitle page")
        raw = t.get(m.group(1))
        if not first_bytes_are_zip(raw):
            if valid_srt_bytes(raw):
                return raw
            raise CandidateRejected("subsource payload is not a valid SRT")
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 5. Subsunacs.net
# ---------------------------------------------------------------------------


class SubsunacsSource(BaseSource):
    """Subsunacs.net: POST search, per-candidate language verification.

    The search form (``search.php``) takes ``m`` (title), ``y`` (year) and
    ``l`` (language: 0 = all). Results are ``/subtitles/<Name>-<id>/`` rows
    with a ``(YYYY)`` year span. Because the search cannot be scoped to
    English reliably, each candidate's subtitle page is re-checked before
    any download: the page states ``Език: <language>`` and repeats the
    title and year, and hosts the direct SRT entry
    (``getentry.php?id=<id>&ei=0``).
    """

    key = PROVIDER_SUBSUNACS
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSUNACS]
    BASE = "https://subsunacs.net"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        form = {"m": identity.title, "y": str(identity.year), "l": "0", "t": "Submit"}
        page = t.post(f"{self.BASE}/search.php", form).decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for href, inner, year in re.findall(
            r'<a[^>]+href="(/subtitles/[^"]+/)"[^>]*>(.*?)</a>\s*(?:<[^>]+>)?\((\d{4})\)',
            page, re.S,
        ):
            if href in seen:
                continue
            seen.add(href)
            title = unescape(strip_tags(inner)).strip()
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href, release=title,
                feature_title=title, feature_year=int(year),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(f"{self.BASE}{candidate.file_id}").decode("utf-8", errors="replace")
        lang_m = re.search(r"Език:\s*([^/]+)", page)
        if lang_m:
            lang_text = unescape(lang_m.group(1)).strip()
            if "англ" not in lang_text.casefold() and "english" not in lang_text.casefold():
                raise CandidateRejected(f"subsunacs subtitle is not English ({lang_text})")
        head_m = re.search(r"<h1[^>]*>(.*?)\s*\((\d{4})\)", page, re.S)
        if head_m:
            head_title = unescape(strip_tags(head_m.group(1))).strip()
            head_year = int(head_m.group(2))
            if candidate.feature_year and head_year != candidate.feature_year:
                raise CandidateRejected("subsunacs page year does not match the search row")
            if candidate.feature_title and not titles_match(head_title, candidate.feature_title):
                raise CandidateRejected("subsunacs page title does not match the search row")
        entry_m = re.search(
            r'href="((?:https://subsunacs\.net)?/getentry\.php\?id=\d+&(?:amp;)?ei=0)"', page)
        if not entry_m:
            raise ScrapeSourceError("no archive entry on the subtitle page")
        raw = t.get(absolute_url(self.BASE, entry_m.group(1)))
        if not valid_srt_bytes(raw):
            raise CandidateRejected("subsunacs payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# 6. YIFY Subtitles
# ---------------------------------------------------------------------------


class YifySubtitlesSource(BaseSource):
    """YIFY Subtitles (yifysubtitles.ch — the current YTS/YIFY domain).

    ``/search?q=<title>`` returns ``div.media-body`` result cards carrying
    an ``h3[itemprop=name]`` title, a ``span.movinfo-section`` year and the
    movie link. The movie page lists subtitle rows (``tr[data-id]``) with
    ``span.sub-lang``, a rating cell and the ``/subtitles/...`` address;
    the download is the same address with ``/subtitles/`` rewritten to
    ``/subtitle/`` plus ``.zip``.
    """

    key = PROVIDER_YIFY
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_YIFY]
    BASE = "https://yifysubtitles.ch"

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        page = t.get(f"{self.BASE}/search?q={urllib.parse.quote_plus(identity.title)}").decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for raw_chunk in re.split(r"<div[^>]+class=[\"']media-body[\"']", page)[1:]:
            chunk = raw_chunk[:4000]
            title_m = re.search(r"<h3[^>]*itemprop=[\"']name[\"'][^>]*>(.*?)</h3>", chunk, re.S)
            year_m = re.search(r"<span[^>]*class=[\"']movinfo-section[\"'][^>]*>\s*(\d{4})", chunk)
            href_m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", chunk)
            if not (title_m and year_m and href_m):
                continue
            href = href_m.group(1)
            if href in seen:
                continue
            seen.add(href)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=href,
                release=unescape(strip_tags(title_m.group(1))).strip(),
                feature_title=unescape(strip_tags(title_m.group(1))).strip(),
                feature_year=int(year_m.group(1)),
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        page = t.get(absolute_url(self.BASE, candidate.file_id)).decode("utf-8", errors="replace")
        best_href: str | None = None
        best_rating = -1.0
        for row in re.findall(r"<tr data-id=[\"'][^\"']*[\"']>(.*?)(?:</tr>|$)", page, re.S):
            lang_m = re.search(r"<span[^>]*class=[\"']sub-lang[\"'][^>]*>([^<]+)</span>", row, re.I)
            if not lang_m or lang_m.group(1).strip().casefold() != "english":
                continue
            cell_m = re.search(r"<td[^>]*class=[\"']rating-cell[\"'][^>]*>(.*?)(?:</td>|$)", row, re.S)
            numbers = re.findall(r"-?\d+(?:\.\d+)?", cell_m.group(1)) if cell_m else []
            try:
                rating = float(numbers[-1]) if numbers else 0.0
            except ValueError:
                rating = 0.0
            if rating < 0:
                continue
            href_m = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"']", row, re.I)
            if not href_m:
                continue
            if rating > best_rating:
                best_rating, best_href = rating, href_m.group(1)
        if not best_href:
            raise CandidateRejected("no English subtitle rows on the YIFY movie page")
        zip_url = absolute_url(self.BASE, best_href).replace("/subtitles/", "/subtitle/") + ".zip"
        raw = t.get(zip_url)
        return pick_zip_subtitle(raw)


# ---------------------------------------------------------------------------
# 7. Subs.sab.bz
# ---------------------------------------------------------------------------


class SubsSabSource(BaseSource):
    """Subs.sab.bz (Bulgarian-era catalog that still carries English subs).

    The search form (``index.php?``) takes ``movie`` + ``yr``; results are
    rows with ``attach_id=<n>`` download links and a ``(YYYY)`` year. The
    site exposes no per-row language metadata we can trust, so every
    downloaded payload is language-guarded (Cyrillic-dominant content is
    rejected) before it may become a sidecar.
    """

    key = PROVIDER_SUBSAB
    label = SCRAPE_PROVIDER_LABELS[PROVIDER_SUBSAB]
    BASE = "http://subs.sab.bz"

    def _headers(self) -> dict[str, str]:
        return {"Referer": f"{self.BASE}/index.php?"}

    def search(self, identity: SourceIdentity, t: ScrapeTransport) -> list[ScrapeCandidate]:
        form = {"movie": identity.title, "act": "search", "select-language": "1",
                "upldr": "", "yr": str(identity.year), "release": ""}
        page = t.post(f"{self.BASE}/index.php?", form, headers=self._headers()).decode("utf-8", errors="replace")
        cands: list[ScrapeCandidate] = []
        seen: set[str] = set()
        for m in re.finditer(r'href="[^"]*attach_id=(\d+)[^"]*"', page):
            attach_id = m.group(1)
            if attach_id in seen:
                continue
            seen.add(attach_id)
            context = page[max(0, m.start() - 300): m.start() + 300]
            year_m = re.search(r"\((\d{4})\)", context)
            title_m = re.search(r"<a[^>]*>([^<]+?)\s*\(\d{4}\)", context)
            cands.append(ScrapeCandidate(
                provider=self.key, file_id=attach_id,
                release=unescape(title_m.group(1)).strip() if title_m else "",
                feature_title=unescape(title_m.group(1)).strip() if title_m else identity.title,
                feature_year=int(year_m.group(1)) if year_m else identity.year,
            ))
            if len(cands) >= SCRAPE_MAX_CANDIDATES_PER_SOURCE * 2:
                break
        return cands

    def fetch(self, candidate: ScrapeCandidate, t: ScrapeTransport) -> bytes:
        raw = t.get(f"{self.BASE}/index.php?act=download&attach_id={candidate.file_id}",
                    headers=self._headers())
        try:
            text = decode_scrape_subtitle_bytes(raw)
        except ScrapeSourceError as exc:
            raise CandidateRejected("subs.sab.bz payload is not text") from exc
        if mostly_cyrillic(text):
            raise CandidateRejected("subs.sab.bz payload is a non-English (Cyrillic) subtitle")
        if not looks_like_srt_text(text):
            raise CandidateRejected("subs.sab.bz payload is not a valid SRT")
        return raw


# ---------------------------------------------------------------------------
# Registry + chain
# ---------------------------------------------------------------------------

SCRAPE_SOURCES: dict[str, BaseSource] = {
    src.key: src
    for src in (
        Subf2meSource(), PodnapisiSource(), Addic7edSource(), SubSourceSource(),
        SubsunacsSource(), YifySubtitlesSource(), SubsSabSource(),
    )
}


def scrape_provider_keys() -> tuple[str, ...]:
    return tuple(key for key in SCRAPE_PROVIDER_ORDER if key in SCRAPE_SOURCES)


def is_scrape_provider(key: str) -> bool:
    return key in SCRAPE_SOURCES


def scrape_provider_label(key: str) -> str:
    return SCRAPE_PROVIDER_LABELS.get(key, key)


@dataclass
class SourceHealth:
    """Circuit-breaker state for one source within one run."""

    hard_failures: int = 0
    parse_failures: int = 0
    disabled_reason: str = ""

    @property
    def disabled(self) -> bool:
        return bool(self.disabled_reason)


def pick_candidates(identity: SourceIdentity, candidates: Iterable[ScrapeCandidate],
                    *, limit: int = SCRAPE_MAX_CANDIDATES_PER_SOURCE) -> list[ScrapeCandidate]:
    """Order a source's candidates by how confidently they name the movie.

    Requires a real title match (and the source's year, when the source
    states one). Ties break on popularity signals.
    """
    scored: list[tuple[float, float, float, ScrapeCandidate]] = []
    for cand in candidates:
        sim = title_similarity(cand.feature_title or cand.release, identity.title)
        if sim < 0.6:
            continue
        if cand.feature_year and cand.feature_year != identity.year:
            continue
        year_penalty = 0.0 if (not cand.feature_year or cand.feature_year == identity.year) else 0.25
        scored.append((sim - year_penalty, float(cand.downloads or 0), float(cand.rating or 0.0), cand))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored[:max(0, limit)]]


class ScrapeChain:
    """Runs the failover chain with per-source breakers and daily caps.

    ``reserve_cb(source)`` is invoked before each search leaves this
    process so an interrupted request still counts in the durable ledger
    (the fetcher passes a callback that persists the ledger).
    """

    def __init__(self, *, keys: tuple[str, ...] = scrape_provider_keys(),
                 transport: ScrapeTransport | None = None,
                 search_caps: dict[str, int] | None = None,
                 reserved: dict[str, int] | None = None,
                 reserve_cb: Callable[[str], None] | None = None) -> None:
        self.keys = tuple(keys)
        self.transport = transport or default_transport()
        self.search_caps = dict(search_caps or {})
        self.reserved = dict(reserved or {})
        self.reserve_cb = reserve_cb
        self.health: dict[str, SourceHealth] = {key: SourceHealth() for key in self.keys}
        self.notes: dict[str, list[str]] = {key: [] for key in self.keys}

    # -- status -----------------------------------------------------------

    def enabled_keys(self) -> list[str]:
        return [key for key in self.keys if not self.health[key].disabled]

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in self.keys:
            health = self.health[key]
            if health.disabled:
                out[key] = f"disabled: {health.disabled_reason}"
                continue
            cap = self.search_caps.get(key, DEFAULT_SEARCH_DAILY_CAP)
            used = self.reserved.get(key, 0)
            out[key] = f"ok (searches used {used}/{cap})"
        return out

    # -- breaker ----------------------------------------------------------

    def _note_hard_failure(self, key: str, reason: str) -> None:
        health = self.health[key]
        health.hard_failures += 1
        self.notes[key].append(f"hard failure ({health.hard_failures}): {reason}")
        if health.hard_failures >= BREAKER_HARD_FAILURES:
            health.disabled_reason = f"{health.hard_failures} consecutive hard failures (last: {reason})"

    def _note_parse_failure(self, key: str, reason: str) -> None:
        health = self.health[key]
        health.parse_failures += 1
        self.notes[key].append(f"parse failure ({health.parse_failures}): {reason}")
        if health.parse_failures >= BREAKER_PARSE_FAILURES:
            health.disabled_reason = f"{health.parse_failures} repeated parse failures (last: {reason})"

    def _note_success(self, key: str) -> None:
        health = self.health[key]
        health.hard_failures = 0
        health.parse_failures = 0

    # -- operations ---------------------------------------------------------

    def search(self, key: str, identity: SourceIdentity) -> list[ScrapeCandidate]:
        source = SCRAPE_SOURCES.get(key)
        if source is None:
            raise ValueError(f"unknown scraped source: {key}")
        health = self.health[key]
        if health.disabled:
            raise SourceUnavailable(f"source disabled this run: {health.disabled_reason}")
        cap = self.search_caps.get(key, DEFAULT_SEARCH_DAILY_CAP)
        if self.reserved.get(key, 0) >= cap:
            raise SourceUnavailable(f"UTC daily search cap reached ({cap})")
        self.reserved[key] = self.reserved.get(key, 0) + 1
        if self.reserve_cb is not None:
            self.reserve_cb(key)
        try:
            cands = source.search(identity, self.transport)
        except ScrapeSourceError as exc:
            self._note_hard_failure(key, str(exc))
            raise SourceUnavailable(str(exc)) from exc
        except Exception as exc:  # structural surprises must not kill the run
            self._note_parse_failure(key, f"{type(exc).__name__}: {exc}")
            raise SourceUnavailable(f"unparseable response ({exc})") from exc
        self._note_success(key)
        return cands

    def fetch(self, key: str, candidate: ScrapeCandidate) -> bytes:
        source = SCRAPE_SOURCES.get(key)
        if source is None:
            raise ValueError(f"unknown scraped source: {key}")
        health = self.health[key]
        if health.disabled:
            raise SourceUnavailable(f"source disabled this run: {health.disabled_reason}")
        try:
            raw = source.fetch(candidate, self.transport)
        except CandidateRejected:
            raise
        except ScrapeSourceError as exc:
            self._note_hard_failure(key, str(exc))
            raise SourceUnavailable(str(exc)) from exc
        except Exception as exc:
            self._note_parse_failure(key, f"{type(exc).__name__}: {exc}")
            raise SourceUnavailable(f"unparseable response ({exc})") from exc
        self._note_success(key)
        if not valid_srt_bytes(raw):
            raise CandidateRejected("payload is not a valid SRT")
        return raw


def run_scrape_chain(
    identity: SourceIdentity,
    *,
    keys: tuple[str, ...],
    chain: ScrapeChain,
    on_reason: Callable[[str, str], None] | None = None,
) -> tuple[ScrapeCandidate | None, str, bytes | None]:
    """Offer the movie to every enabled source in order.

    Returns ``(candidate, provider, raw_bytes)`` on the first accepted
    subtitle, else ``(None, "", None)`` with every source's verdict
    appended through ``on_reason(source, reason)`` so the fetcher can fold
    them into the movie's review detail.
    """
    for key in keys:
        if key not in chain.health or chain.health[key].disabled:
            reason = (f"disabled: {chain.health[key].disabled_reason}" if key in chain.health
                      else "not enabled")
            if on_reason:
                on_reason(key, reason)
            continue
        try:
            cands = chain.search(key, identity)
        except SourceUnavailable as exc:
            if on_reason:
                on_reason(key, str(exc))
            continue
        cands = pick_candidates(identity, cands)
        if not cands:
            if on_reason:
                on_reason(key, "no matching English subtitle")
            continue
        for cand in cands:
            try:
                raw = chain.fetch(key, cand)
            except CandidateRejected as exc:
                if on_reason:
                    on_reason(key, f"candidate refused: {exc}")
                continue
            except SourceUnavailable as exc:
                if on_reason:
                    on_reason(key, str(exc))
                break
            return cand, key, raw
        else:
            if on_reason:
                on_reason(key, "candidates were checked but none produced a valid English SRT")
    return None, "", None


# =============================================================================

LIBRARY_DIR = str(resolve_library())
# Logs and reports live under tools\ReportsAndLogs so the root of E:\torrents
# stays media-only.
LOG_FILE = str(default_tool_dir("subtitle_fetcher") / "subtitle_fetcher.log")  # Appended every run; this is also the durable quota/retry ledger.
REPORT_FILE = str(default_tool_dir("subtitle_fetcher") / "subtitle_fetcher_report.txt")
# The append-only log is the durable quota ledger; no state/cache file is created.
LEDGER_EVENT = "SUBTITLE_LEDGER"
# OpenSubtitles free-tier download allowance (24 hours; the provider resets
# download counters at midnight UTC, which is why the local ledger is keyed by
# UTC day): a plain signed-up user gets 20/day (VIP ranks go up to 1000), and
# an API consumer flagged "Under Development" may download up to 100/day
# without user authentication. Verified against the official OpenSubtitles API
# documentation (opensubtitles.stoplight.io, "Getting started" > Authentication)
# on 2026-09-01. NOTE: a consumer that is NOT "Under Development" may only
# download 5 subtitles per IP per 24 hours without a user, so the default
# development-anonymous mode requires the consumer to have Under Development
# and Allow anonymous enabled at opensubtitles.com/consumers.
USER_DAILY_CAP = 20
DEVELOPMENT_ANONYMOUS_DAILY_CAP = 100
AUTH_MODE_DEVELOPMENT_ANONYMOUS = "development-anonymous"
AUTH_MODE_USER = "user"
DEFAULT_AUTH_MODE = AUTH_MODE_DEVELOPMENT_ANONYMOUS

# Leave blank to use environment variables instead. In development-anonymous
# mode only the API key is used; username/password are intentionally ignored.
OPENSUBTITLES_API_KEY = ""
OPENSUBTITLES_USERNAME = ""
OPENSUBTITLES_PASSWORD = ""
SUBDL_API_KEY = ""
SUBDL_API_BASE = "https://api.subdl.com/api/v2"
SUBDL_DOWNLOAD_HOST = "dl.subdl.com"
# SubDL's current v2 developer docs publish separate free-tier allowances:
# 2,000 searches and 50 downloads per day. Keep conservative local guards for
# both; users on a paid plan can explicitly raise either cap with the matching
# --subdl-*-daily-cap flag.
SUBDL_DEFAULT_SEARCH_DAILY_CAP = 2_000
# Scrape-chain searches: the scraped sources publish no official API quota, so
# this is a courtesy self-limit (requests per source per UTC day), not a
# provider-published allowance.
SCRAPE_DEFAULT_SEARCH_DAILY_CAP = DEFAULT_SEARCH_DAILY_CAP

SUBDL_DEFAULT_DAILY_CAP = 50
SUBDL_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

__version__ = "2.11.1"
APP_USER_AGENT = "JellyfinMovieSubtitleFetcher v2.9"
API_BASE = "https://api.opensubtitles.com/api/v1"

# The preceding standardizer emits canonical MKV movies only. Limiting the
# fetcher to that exact contract prevents unrelated videos or media variants
# from receiving sidecars.
VIDEO_EXTENSIONS = {".mkv"}
DIRECT_PLAY_SUBTITLE_EXTENSION = ".srt"
DOWNLOAD_SUBTITLE_FORMAT = "srt"
MIN_MOVIE_SIZE_MB = 300
REQUEST_GAP_SEC = 1.1  # stay under the documented per-second limit
# Bound to the one shared limit (vendored below), not a second copy of the number.
MAX_SUBTITLE_BYTES = EXTERNAL_SRT_MAX_BYTES
LANGUAGES = "en"

PROVIDER_OPENSUBTITLES = "opensubtitles"
PROVIDER_SUBDL = "subdl"

# =============================================================================
# CONSTANTS
# =============================================================================

HASH_CHUNK = 65536  # 64 KiB
MIN_HASH_SIZE = HASH_CHUNK * 2

EXTRA_DIR_NAMES = frozenset({
    "featurettes", "extras", "specials", "shorts", "bonus",
    "behind the scenes", "deleted scenes", "interviews", "scenes",
    "trailers", "other", "samples", "sample", "clips",
    "bdmv", "certificate", "video_ts", "audio_ts",
    "subs", "sub", "subtitles",
})
DISC_DIR_NAMES = frozenset({"bdmv", "certificate", "video_ts", "audio_ts", "hvdvd_ts"})
SAMPLE_NAME_RE = re.compile(
    r"(?i)(?:^|[._\-\s\[(])(sample|trailer|teaser)(?:[.)\]\-\s_]|$)"
)
ENGLISH_LANGUAGE_TOKENS = frozenset({"en", "eng", "english"})
MOVIE_IDENTITY_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:18|19|20)\d{2})\)\s*$")
# Without an original release name, edition-labelled subtitles are too uncertain
# for automatic selection. They remain visible in the report for manual review.
EDITION_MARKERS = frozenset({
    "extended", "unrated", "directors cut", "director s cut", "theatrical",
    "ultimate", "special edition", "collectors edition", "anniversary",
    "remastered", "redux", "final cut", "alternate cut",
})
# Automatic selection requires *any one* library keyword in the release
# name: a Blu-ray spelling (BluRay / Blu-ray / BD / BDRip / ...), 1080p,
# qXR, or Tigole. Candidates do not need to carry every keyword at once.
# Bare WEB / WebRip / WebDL / HDTV without those tokens still fail the gate.
BLURAY_TOKEN = r"(?:blu[\s._-]*ray|bd(?:[\s._-]*rip)?|br[\s._-]*rip|bdmv)"
BLURAY_RELEASE_RE = re.compile(rf"(?<![a-z0-9]){BLURAY_TOKEN}(?![a-z0-9])", re.IGNORECASE)
LIBRARY_1080P_RE = re.compile(r"(?<![0-9])1080p(?![0-9a-z])", re.IGNORECASE)
LIBRARY_QXR_RE = re.compile(r"(?<![a-z0-9])qxr(?![a-z0-9])", re.IGNORECASE)
LIBRARY_TIGOLE_RE = re.compile(r"(?<![a-z0-9])tigole(?![a-z0-9])", re.IGNORECASE)
LIBRARY_SOURCE_RELEASE_RE = re.compile(
    rf"(?<![a-z0-9]){BLURAY_TOKEN}(?![a-z0-9])|(?<![0-9])1080p(?![0-9a-z])|(?<![a-z0-9])qxr(?![a-z0-9])|(?<![a-z0-9])tigole(?![a-z0-9])",
    re.IGNORECASE,
)
# One sentence for the banner and the report: what automatic selection does.
SELECTION_POLICY_TEXT = (
    "auto-selects, across OpenSubtitles and SubDL as equal sources, "
    "the release that names the movie and its release year and carries any one "
    "of Blu-ray (any spelling, including BD), 1080p, qXR, or Tigole, preferring 1080p then qXR then Tigole"
)
# SubDL documents this as a confident release-level filename match. It applies
# only to its /files/search endpoint; title-only fallback retains its separate
# strict identity and provider-quality policy.
MIN_SUBDL_RELEASE_MATCH_SCORE = 0.80

# Official OSHash test: first+last 64KiB of a synthetic pattern is tested in --self-test.

@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(LIBRARY_DIR))
    log_file: Path | None = field(default_factory=lambda: Path(LOG_FILE) if LOG_FILE else None)
    report_file: Path = field(default_factory=lambda: Path(REPORT_FILE))
    api_key: str = ""
    subdl_api_key: str = ""
    username: str = ""
    password: str = ""
    dry_run: bool = False
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    limit: int = 0
    identity_fallback: bool = False
    auth_mode: str = DEFAULT_AUTH_MODE

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

    @property
    def sidecar_suffix(self) -> str:
        """The sole output sidecar: a normal English UTF-8 SRT (``.eng.srt``)."""
        return EXTERNAL_SRT_SUFFIX

@dataclass
class Candidate:
    # OpenSubtitles uses a numeric ``file_id`` while SubDL exposes opaque
    # ``n_id`` values. Keep the common selection model without throwing away
    # the provider's stable identifier.
    file_id: int | str
    release: str
    moviehash_match: bool
    downloads: int
    votes: int
    rating: float
    trusted: bool
    hearing_impaired: bool
    machine_translated: bool
    ai_translated: bool
    foreign_parts_only: bool
    language: str
    feature_title: str = ""
    feature_year: int = 0
    feature_imdb_id: int = 0
    # /api/v2/files/search provides this release-name similarity in [0, 1].
    # ``None`` means the provider did not offer a filename-match score.
    subdl_match_score: float | None = None

@dataclass(frozen=True)
class SubdlDownload:
    """A vetted SubDL download reference kept out of human-facing logs.

    ``url`` is an optional raw-file URL returned for an unpacked SRT. ``n_id``
    is the documented v2 API download identifier and is used when no raw URL
    is available. Neither value is ever printed because a provider may attach
    short-lived query credentials to a URL.
    """

    n_id: str = ""
    url: str = ""

class SubdlSearchQuotaExhausted(RuntimeError):
    """Raised before a SubDL search that would exceed the durable local cap."""

@dataclass(frozen=True)
class MovieIdentity:
    """Canonical identity inferred only from a standardized ``Title (Year)`` name."""
    title: str
    year: int
    normalized_title: str

# Every result carries a machine-readable reason alongside its human detail so
# the report groups movies by what the user has to *do*, instead of guessing
# that grouping back out of a prose sentence.
REASON_COVERED = "covered"
REASON_DOWNLOADED = "downloaded"
REASON_EXTRACTED = "extracted"
REASON_DRY_RUN = "dry_run"
REASON_NO_MATCH = "no_match"
REASON_SIDECAR_UNUSABLE = "sidecar_unusable"
REASON_SIDECAR_NAME = "sidecar_name"
REASON_REVIEW = "review"
REASON_QUOTA = "quota"
REASON_LAYOUT = "layout"
REASON_ERROR = "error"

@dataclass
class JobResult:
    video: Path
    status: str  # have, skip, download, dry-run, review, error
    detail: str
    dest: Path | None = None
    reason: str = ""

@dataclass(frozen=True)
class VideoSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int

# =============================================================================
# LOGGING / HTTP
# =============================================================================

class ConcurrentSidecarError(RuntimeError):
    """Raised when another actor safely created the requested sidecar first."""

def log(msg: str, level: str = "INFO", log_file: Path | None = None) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    # Never let a console encoding abort a run: the progress lines carry an em
    # dash and the report carries box-drawing characters.
    print_text(line)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

def video_snapshot(path: Path) -> VideoSnapshot:
    """Capture a no-follow video identity before an external-provider transaction."""
    file_stat = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise OSError(f"not a regular non-symlink movie file: {path}")
    return VideoSnapshot(
        device=int(file_stat.st_dev), inode=int(file_stat.st_ino),
        size=int(file_stat.st_size), mtime_ns=int(file_stat.st_mtime_ns),
    )

def video_snapshot_matches(path: Path, expected: VideoSnapshot) -> bool:
    try:
        return video_snapshot(path) == expected
    except OSError:
        return False

def _sum_u64_le(fh, nbytes: int) -> int:
    fmt = "<Q"
    n = nbytes // 8
    total = 0
    for _ in range(n):
        chunk = fh.read(8)
        if len(chunk) != 8:
            raise ValueError("short read while hashing")
        total = (total + struct.unpack(fmt, chunk)[0]) & 0xFFFFFFFFFFFFFFFF
    return total

def moviehash(path: Path) -> str:
    """OpenSubtitles OSHash: size + uint64le sum of first/last 64 KiB."""
    size = path.stat().st_size
    if size < MIN_HASH_SIZE:
        raise ValueError(f"file too small to hash ({size} bytes)")
    with path.open("rb") as fh:
        total = size & 0xFFFFFFFFFFFFFFFF
        total = (total + _sum_u64_le(fh, HASH_CHUNK)) & 0xFFFFFFFFFFFFFFFF
        fh.seek(size - HASH_CHUNK)
        total = (total + _sum_u64_le(fh, HASH_CHUNK)) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"

def moviehash_bytes(data: bytes) -> str:
    """Same algorithm over an in-memory blob (tests)."""
    size = len(data)
    if size < MIN_HASH_SIZE:
        raise ValueError("too small")
    fmt = "<Q"
    n = HASH_CHUNK // 8
    total = size & 0xFFFFFFFFFFFFFFFF
    for i in range(n):
        total = (total + struct.unpack_from(fmt, data, i * 8)[0]) & 0xFFFFFFFFFFFFFFFF
    tail = size - HASH_CHUNK
    for i in range(n):
        total = (total + struct.unpack_from(fmt, data, tail + i * 8)[0]) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"

class OpenSubtitlesClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token: str | None = None
        # One bucket per host: the API and the download host it hands back are
        # different servers with separate limits, and a 429 from one of them
        # says nothing about the other.
        self.buckets = BucketRegistry(gap=REQUEST_GAP_SEC)

    def _headers(self, *, auth: bool = False) -> dict[str, str]:
        h = {
            "Api-Key": self.cfg.api_key,
            "User-Agent": APP_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _throttle(self, url: str) -> float:
        """Wait until ``url``'s host may be asked again. Returns seconds slept."""
        return self.buckets.take(host_key(url))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        _retried_auth: bool = False,
    ) -> dict[str, Any]:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle(url)
            req = urllib.request.Request(
                url, data=data, method=method, headers=self._headers(auth=auth),
            )
            try:
                # API_BASE is a fixed HTTPS provider endpoint.
                with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                    raw = resp.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")[:400]
                invalid_token = auth and "invalid" in err_body.casefold()
                if (exc.code == 401 or (exc.code == 500 and invalid_token)) and auth and not _retried_auth:
                    self.token = None
                    self.login()
                    return self._request(
                        method, path, params=params, body=body, auth=True, _retried_auth=True,
                    )
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if retryable and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, float(retry_after)) if retry_after else 2.0 * (attempt + 1)
                    except ValueError:
                        delay = 2.0 * (attempt + 1)
                    # "Slow down" is about this host, not about this request:
                    # hold the whole bucket back so the retry - and anything
                    # else aimed at that host - waits it out exactly once.
                    self.buckets.penalize(host_key(url), delay)
                    last_err = RuntimeError(f"HTTP {exc.code} {path}: {err_body}")
                    continue
                raise RuntimeError(f"HTTP {exc.code} {path}: {err_body}") from exc
            except urllib.error.URLError as exc:
                last_err = RuntimeError(f"network error {path}: {exc}")
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err from exc
        else:
            raise last_err or RuntimeError(f"request failed {path}")

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON from {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"unexpected JSON from {path}")
        return parsed

    def login(self) -> None:
        if not self.cfg.username or not self.cfg.password:
            raise RuntimeError(
                "OpenSubtitles username/password required for downloads. "
                "Set OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD."
            )
        payload = self._request(
            "POST",
            "/login",
            body={"username": self.cfg.username, "password": self.cfg.password},
        )
        token = payload.get("token")
        if not token:
            raise RuntimeError(f"login failed: {payload}")
        self.token = str(token)

    def search(self, *, movie_hash: str, query: str) -> list[Candidate]:
        # OpenSubtitles recommends submitting the moviehash and filename query
        # together. Explicit filters reduce unsafe/irrelevant candidates before
        # local ranking; no provider-side ordering is requested.
        params = {
            "moviehash": movie_hash,
            "moviehash_match": "only",
            "query": query,
            "languages": LANGUAGES,
            "type": "movie",
            "machine_translated": "exclude",
            "ai_translated": "exclude",
        }
        params["foreign_parts_only"] = "exclude"
        payload = self._request("GET", "/subtitles", params=params)
        return parse_candidates(payload)

    def search_identity(self, identity: MovieIdentity) -> list[Candidate]:
        """Search only a normalized title/year identity after a hash search fails.

        This method deliberately has no moviehash parameter. Its results are never
        accepted by the strict picker; they must pass ``pick_identity_candidate``.
        """
        params = {
            "query": f"{identity.title} {identity.year}",
            "languages": LANGUAGES,
            "type": "movie",
            "machine_translated": "exclude",
            "ai_translated": "exclude",
            "foreign_parts_only": "exclude",
        }
        payload = self._request("GET", "/subtitles", params=params)
        return parse_candidates(payload)

    def download_srt(self, file_id: int, dest: Path, *, video: Path, expected_video: VideoSnapshot) -> None:
        """Download exactly one provider-rendered UTF-8 SRT and activate it atomically.

        The development-anonymous mode sends the consumer API key and no JWT,
        matching OpenSubtitles' temporary Under Development allowance. User mode
        retains the previous login/JWT path.
        """
        if self.cfg.auth_mode == AUTH_MODE_USER:
            if not self.token:
                self.login()
            use_user_token = True
        elif self.cfg.auth_mode == AUTH_MODE_DEVELOPMENT_ANONYMOUS:
            use_user_token = False
        else:
            raise RuntimeError(f"unsupported authentication mode: {self.cfg.auth_mode}")
        try:
            payload = self._request(
                "POST", "/download", body={"file_id": file_id, "sub_format": DOWNLOAD_SUBTITLE_FORMAT}, auth=use_user_token,
            )
        except RuntimeError as exc:
            message = str(exc)
            if self.cfg.auth_mode == AUTH_MODE_DEVELOPMENT_ANONYMOUS and ("HTTP 401" in message or "HTTP 403" in message):
                raise RuntimeError(
                    "OpenSubtitles rejected the development-anonymous download. Confirm this API consumer still has "
                    "Under Development and Allow anonymous enabled, then retry. If the temporary allowance has ended, "
                    "run with --auth-mode user and configured username/password."
                ) from exc
            raise
        link = payload.get("link")
        if not link:
            raise RuntimeError(f"download endpoint returned no link: {payload}")
        download_url = str(link)
        parsed_link = urllib.parse.urlsplit(download_url)
        # The download URL is provider-supplied data, not a trusted local path.
        # Restrict it to an absolute HTTPS URL so file:, data:, ftp:, malformed,
        # and downgrade links cannot be dereferenced by urllib.
        if parsed_link.scheme.lower() != "https" or not parsed_link.netloc:
            raise RuntimeError("download endpoint returned an invalid non-HTTPS subtitle link")
        self._throttle(download_url)
        req = urllib.request.Request(
            download_url, method="GET", headers={"User-Agent": APP_USER_AGENT, "Accept": "text/plain, */*;q=0.1"},
        )
        try:
            # urlsplit above requires an absolute HTTPS provider link.
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_SUBTITLE_BYTES:
                    raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
                data = resp.read(MAX_SUBTITLE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"subtitle download HTTP {exc.code}") from exc
        except ValueError as exc:
            raise RuntimeError("invalid subtitle content length") from exc
        if len(data) > MAX_SUBTITLE_BYTES:
            raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
        try:
            text = decode_subtitle_bytes(data)
        except (OSError, EOFError, ValueError) as exc:
            raise RuntimeError("downloaded subtitle could not be decompressed") from exc
        text = normalize_srt_newlines(text)
        if not looks_like_srt(text):
            raise RuntimeError("downloaded payload is not a valid SRT subtitle")
        if not video_snapshot_matches(video, expected_video):
            raise RuntimeError("movie changed during subtitle lookup; downloaded SRT was not activated")
        try:
            atomic_write_text(dest, text, replace=False)
        except FileExistsError as exc:
            raise ConcurrentSidecarError("English SRT appeared during download; preserved the existing sidecar") from exc

def _subdl_text(value: Any) -> str:
    """Return a bounded, stripped API scalar without treating containers as text."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()

def _subdl_identifier(value: Any) -> str:
    """Accept only a compact identifier that is safe in a v2 URL path segment."""
    identifier = _subdl_text(value)
    if not identifier or len(identifier) > 256:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
        return ""
    return identifier

def _subdl_match_score(value: Any) -> float | None:
    """Parse SubDL's documented [0, 1] release-match score fail-closed."""
    text = _subdl_text(value)
    if not text:
        return None
    try:
        score = float(text)
    except ValueError:
        return None
    return score if 0.0 <= score <= 1.0 else None

def normalize_subdl_download_url(value: Any) -> str:
    """Validate a SubDL raw-file URL before ``urllib`` can dereference it.

    Search responses are remote input. SubDL documents relative ``/subtitle/``
    URLs and ``dl.subdl.com`` raw URLs; accepting an arbitrary absolute URL
    here would turn a subtitle lookup into an SSRF primitive. The v2 API
    download endpoint is built locally instead and therefore needs no URL from
    the response.
    """
    raw = _subdl_text(value)
    if not raw:
        raise ValueError("SubDL returned an empty download URL")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme.casefold() != "https" or hostname != SUBDL_DOWNLOAD_HOST:
            raise ValueError("SubDL returned a download URL outside dl.subdl.com")
        try:
            unsafe_port = parsed.port not in (None, 443)
        except ValueError as exc:
            raise ValueError("SubDL returned an unsafe download URL") from exc
        if parsed.username or parsed.password or unsafe_port:
            raise ValueError("SubDL returned an unsafe download URL")
        normalized = raw
    else:
        # Network-path URLs (//host/path) are absolute URLs in disguise.
        if not raw.startswith("/") or raw.startswith("//"):
            raise ValueError("SubDL returned an invalid relative download URL")
        normalized = f"https://{SUBDL_DOWNLOAD_HOST}{raw}"

    final = urllib.parse.urlsplit(normalized)
    decoded_parts = urllib.parse.unquote(final.path).split("/")
    if not final.path.startswith("/subtitle/") or any(part in {".", ".."} for part in decoded_parts):
        raise ValueError("SubDL returned an invalid subtitle download path")
    return normalized

def _subdl_exact_feature_record(
    feature: dict[str, Any], identity: MovieIdentity,
) -> tuple[str, int, int] | None:
    """Validate one provider-supplied movie identity record."""
    titles = [
        _subdl_text(feature.get(field_name))
        for field_name in ("name", "title", "original_name")
    ]
    matched_title = next(
        (title for title in titles if normalize_title(title) == identity.normalized_title), ""
    )
    year = _nonnegative_int(feature.get("year"))
    media_type = _subdl_text(feature.get("type")).casefold()
    if media_type != "movie" or year != identity.year or not matched_title:
        return None

    imdb_text = _subdl_text(feature.get("imdb_id"))
    imdb_match = re.search(r"(\d+)$", imdb_text)
    return matched_title, year, int(imdb_match.group(1)) if imdb_match else 0

def _subdl_exact_feature(
    payload: dict[str, Any], identity: MovieIdentity, *, require_match: bool = False,
) -> tuple[str, int, int] | None:
    """Confirm the provider says these subtitles belong to this exact movie.

    ``/files/search`` returns a ``match`` record that is specifically bound to
    the filename supplied by this client, so it is mandatory for that route.
    The title-search endpoint documents its subtitle array as belonging to the
    first entry in ``results``, which remains its authoritative identity.
    """
    match = payload.get("match")
    if require_match:
        # The documented filename endpoint attaches ``subtitles`` to this
        # parsed-release record. Do not substitute a generic search result if
        # it is absent or disagrees; that would turn release matching into a
        # weaker title search without the caller's knowledge.
        return _subdl_exact_feature_record(match, identity) if isinstance(match, dict) else None

    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return _subdl_exact_feature_record(results[0], identity)
    if isinstance(match, dict):
        return _subdl_exact_feature_record(match, identity)
    return None

def _subdl_value(child: dict[str, Any], parent: dict[str, Any], *names: str) -> Any:
    """Read an unpacked-file field first, then its parent subtitle record."""
    for name in names:
        if name in child and child[name] is not None:
            return child[name]
    for name in names:
        if name in parent and parent[name] is not None:
            return parent[name]
    return None

def _subdl_is_srt_or_archive(child: dict[str, Any], parent: dict[str, Any]) -> bool:
    """Reject an explicitly non-SRT SubDL result before it reaches download."""
    media_format = _subdl_text(_subdl_value(child, parent, "format")).casefold().lstrip(".")
    if media_format and media_format not in {"srt", "zip"}:
        return False
    name = _subdl_text(_subdl_value(child, parent, "name", "file_name"))
    if not name:
        return True
    lower_name = name.casefold().split("?", 1)[0]
    # A provider may call an archive simply "subtitle"; accept an unknown
    # extension only when no explicit format says otherwise, then validate the
    # bytes after download. Known non-SRT formats are never candidates.
    known_non_srt = (".ass", ".ssa", ".sub", ".idx", ".vtt", ".ttml", ".dfxp")
    return not lower_name.endswith(known_non_srt)

def _subdl_candidate_reference(
    child: dict[str, Any], parent: dict[str, Any],
) -> tuple[str, SubdlDownload] | None:
    """Build a stable, non-secret candidate key and safe download reference."""
    n_id = _subdl_identifier(_subdl_value(child, parent, "n_id", "nId"))
    file_n_id = _subdl_identifier(_subdl_value(child, parent, "file_n_id", "fileNId"))
    raw_url = _subdl_value(child, parent, "url", "download_link")
    url = ""
    if raw_url:
        try:
            url = normalize_subdl_download_url(raw_url)
        except ValueError:
            # An authenticated v2 n_id gives us a safer locally constructed
            # endpoint, so an unexpected response URL is not fatal in that
            # case. Without an n_id there is nothing safe to download.
            if not n_id:
                return None
    if not n_id and not url:
        return None

    if n_id:
        candidate_id = f"subdl:{n_id}"
        if file_n_id:
            candidate_id += f":{file_n_id}"
        elif url:
            candidate_id += ":" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    else:
        # A legacy v1-shaped response may expose only a raw URL. A deterministic
        # digest is stable across processes, unlike Python's randomized hash().
        candidate_id = "subdl:url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return candidate_id, SubdlDownload(n_id=n_id, url=url)

def _identity_candidate_basics(cands: Sequence[Candidate], identity: MovieIdentity) -> list[Candidate]:
    """Return title/year-exact candidates before provider-specific quality rules."""
    return [
        candidate for candidate in cands
        if _is_normal_english_human_candidate(candidate)
        and candidate.feature_year == identity.year
        and normalize_title(candidate.feature_title) == identity.normalized_title
        and not release_has_edition_marker(candidate.release)
    ]

def pick_subdl_identity_candidate(
    cands: Sequence[Candidate],
    identity: MovieIdentity,
    *,
    require_release_match_score: bool = False,
) -> tuple[Candidate | None, str]:
    """Choose a conservative SubDL fallback candidate.

    The generic title/year route has no documented release-similarity signal, so
    it retains the existing strict provider-quality policy (or one uniquely
    normal English SRT when the v2 response omits quality metadata). In contrast,
    ``/files/search`` explicitly ranks exact-release candidates by
    ``match_score``. There, choose only the single highest valid score at or
    above SubDL's documented confident threshold; provider vote metadata must
    not accidentally outrank the release match, and a tied top score is review
    rather than a guess.
    """
    if require_release_match_score:
        scored: list[tuple[Candidate, float]] = []
        for candidate in _identity_candidate_basics(cands, identity):
            score = _subdl_match_score(candidate.subdl_match_score)
            if score is not None and score >= MIN_SUBDL_RELEASE_MATCH_SCORE:
                scored.append((candidate, score))
        if not scored:
            return (
                None,
                "SubDL did not return a confident release match "
                f"(requires match_score >= {MIN_SUBDL_RELEASE_MATCH_SCORE:.2f})",
            )
        highest = max(score for _candidate, score in scored)
        top = [(candidate, score) for candidate, score in scored if score == highest]
        if len(top) != 1:
            return None, "multiple equally scored confident SubDL release matches require review"
        candidate, score = top[0]
        return candidate, f"title/year exact; SubDL highest release match {score:.2f}"

    pick, reason = pick_identity_candidate(cands, identity)
    if pick is not None:
        return pick, reason

    usable = _identity_candidate_basics(cands, identity)
    if len(usable) != 1:
        return None, "SubDL did not return one unambiguous title/year-exact normal English SRT"
    candidate = usable[0]
    if candidate.downloads or candidate.votes or candidate.rating:
        return None, reason
    return candidate, "title/year exact; one normal English SubDL SRT (no provider vote metadata)"

def subdl_download_redirect_url(data: bytes) -> str | None:
    """Return a vetted raw-file URL when the v2 download endpoint returns JSON.

    Some SubDL deployments respond with the file directly while others return a
    short-lived raw download URL. Supporting both shapes keeps the client on
    the documented v2 endpoint without trusting a URL outside the provider.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error:
        message = _subdl_text(error.get("message") if isinstance(error, dict) else error)
        raise RuntimeError(f"SubDL download failed{': ' + message if message else ''}")
    containers = (payload, payload.get("data"), payload.get("download"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("download_url", "url", "link"):
            value = container.get(key)
            if value:
                try:
                    return normalize_subdl_download_url(value)
                except ValueError as exc:
                    raise RuntimeError("SubDL returned an unsafe download URL") from exc
    return None

def decode_subdl_srt_payload(data: bytes, max_bytes: int) -> str:
    """Decode a raw SRT or exactly one SRT member from a bounded archive."""
    if len(data) > max_bytes:
        raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                candidates = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and not (info.flag_bits & 0x1)  # encrypted archives cannot be safely inspected
                    and info.filename.casefold().endswith(".srt")
                    and info.file_size <= max_bytes
                ]
                if not candidates:
                    raise RuntimeError("no usable .srt file found in SubDL zip archive")
                candidates.sort(
                    key=lambda info: (
                        0 if LIBRARY_1080P_RE.search(info.filename or "") else 1,
                        0 if LIBRARY_QXR_RE.search(info.filename or "") else 1,
                        0 if LIBRARY_TIGOLE_RE.search(info.filename or "") else 1,
                        1 if re.search(r"(?i)sdh|hi\\b|hearing", info.filename or "") else 0,
                        info.filename.casefold(),
                    ),
                )
                selected = candidates[0]
                with archive.open(selected, "r") as member:
                    raw_srt = member.read(max_bytes + 1)
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            if isinstance(exc, RuntimeError) and (
                str(exc).startswith("no usable .srt")
                or str(exc).startswith("SubDL zip archive contains multiple")
            ):
                raise
            raise RuntimeError("SubDL zip archive could not be read safely") from exc
        if len(raw_srt) > max_bytes:
            raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
        data = raw_srt
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as archive:
                data = archive.read(max_bytes + 1)
        except (OSError, EOFError) as exc:
            raise RuntimeError("SubDL gzip subtitle could not be read safely") from exc
        if len(data) > max_bytes:
            raise RuntimeError(f"subtitle exceeds {max_bytes} byte safety limit")
    text = normalize_srt_newlines(decode_subtitle_bytes(data))
    if not looks_like_srt(text):
        raise RuntimeError("downloaded payload from SubDL is not a valid SRT subtitle")
    return text

class SubdlClient:
    """Small stdlib-only client for SubDL's authenticated v2 API."""

    def __init__(
        self,
        api_key: str,
        *,
        before_search_request: Callable[[], None] | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        # Queue mode supplies a durable reservation callback. Keep it optional
        # so this small client remains usable on its own and in focused tests.
        self._before_search_request = before_search_request
        # Per host, for the same reason as OpenSubtitles: the API lives on one
        # server and the subtitle payloads on another.
        self.buckets = BucketRegistry(gap=REQUEST_GAP_SEC)

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"User-Agent": APP_USER_AGENT, "Accept": accept}
        if self.api_key:
            # v2 documents Bearer authentication. Keeping credentials out of
            # query strings prevents a key from leaking into proxy/access logs.
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _throttle(self, url: str) -> float:
        """Wait until ``url``'s host may be asked again. Returns seconds slept."""
        return self.buckets.take(host_key(url))

    @staticmethod
    def _read_limited(response: Any, max_bytes: int, label: str) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    raise RuntimeError(f"{label} exceeds {max_bytes} byte safety limit")
            except ValueError as exc:
                raise RuntimeError(f"invalid {label} content length") from exc
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(f"{label} exceeds {max_bytes} byte safety limit")
        return data

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("SubDL API key is required")
        url = SUBDL_API_BASE + path + "?" + urllib.parse.urlencode(params)
        last_error: RuntimeError | None = None
        for attempt in range(4):
            # A retry is another HTTP search request and can count against the
            # provider quota, so reserve it before each outbound attempt. Do
            # this before throttling too: an exhausted cap must not sleep only
            # to reject a request that will never be sent.
            if self._before_search_request is not None:
                self._before_search_request()
            self._throttle(url)
            request = urllib.request.Request(url, headers=self._headers("application/json"))
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - fixed provider API endpoint
                    raw = self._read_limited(response, SUBDL_MAX_RESPONSE_BYTES, "SubDL API response")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read(400).decode("utf-8", errors="replace").strip()
                last_error = RuntimeError(f"SubDL API HTTP {exc.code}: {body}".rstrip())
                if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, float(retry_after)) if retry_after else 2.0 * (attempt + 1)
                    except ValueError:
                        delay = 2.0 * (attempt + 1)
                    # Hold the host back rather than this one request (see the
                    # OpenSubtitles client for why).
                    self.buckets.penalize(host_key(url), delay)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"SubDL API network error: {exc.reason}")
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc
        else:
            raise last_error or RuntimeError("SubDL API request failed")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("SubDL API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("SubDL API returned an unexpected JSON document")
        error = payload.get("error")
        if payload.get("status") is False or error:
            message = _subdl_text(error.get("message") if isinstance(error, dict) else error)
            message = message or _subdl_text(payload.get("message"))
            raise RuntimeError(f"SubDL API rejected the search{': ' + message if message else ''}")
        return payload

    def _candidate(
        self,
        parent: dict[str, Any],
        child: dict[str, Any],
        feature_title: str,
        feature_year: int,
        feature_imdb_id: int,
    ) -> tuple[Candidate, str, SubdlDownload] | None:
        language = _subdl_text(_subdl_value(child, parent, "language", "lang")).casefold()
        if language not in ENGLISH_LANGUAGE_TOKENS or not _subdl_is_srt_or_archive(child, parent):
            return None
        reference = _subdl_candidate_reference(child, parent)
        if reference is None:
            return None
        candidate_id, download = reference
        release = _subdl_text(_subdl_value(child, parent, "release_name", "release", "name", "file_name"))
        return (
            Candidate(
                file_id=candidate_id,
                release=release,
                moviehash_match=False,
                downloads=_nonnegative_int(_subdl_value(child, parent, "downloads", "download_count")),
                votes=_nonnegative_int(_subdl_value(child, parent, "votes", "vote_count")),
                rating=_nonnegative_float(_subdl_value(child, parent, "ratings", "rating")),
                trusted=as_bool(_subdl_value(child, parent, "trusted", "from_trusted")),
                hearing_impaired=as_bool(_subdl_value(child, parent, "hi", "hearing_impaired")),
                machine_translated=as_bool(_subdl_value(child, parent, "machine_translated", "machine_translation")),
                ai_translated=as_bool(_subdl_value(child, parent, "ai_translated", "ai_translation")),
                foreign_parts_only=as_bool(_subdl_value(child, parent, "foreign_parts_only", "forced")),
                language=language,
                feature_title=feature_title,
                feature_year=feature_year,
                feature_imdb_id=feature_imdb_id,
                subdl_match_score=_subdl_match_score(_subdl_value(child, parent, "match_score")),
            ),
            candidate_id,
            download,
        )

    def _parse_search_payload(
        self,
        payload: dict[str, Any],
        identity: MovieIdentity,
        *,
        require_match: bool = False,
    ) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Turn one vetted SubDL search response into downloadable candidates."""
        feature = _subdl_exact_feature(payload, identity, require_match=require_match)
        if feature is None:
            return [], {}
        feature_title, feature_year, feature_imdb_id = feature

        candidates: list[Candidate] = []
        downloads: dict[str, SubdlDownload] = {}
        subtitles = payload.get("subtitles")
        if not isinstance(subtitles, list):
            return candidates, downloads
        for parent in subtitles:
            if not isinstance(parent, dict):
                continue
            # ``unpack_files`` is the documented subtitle-search shape. Accept
            # the two equivalent spellings defensively because v2 is evolving,
            # but never infer a file reference from a non-object value.
            unpacked = parent.get("unpack_files")
            if not isinstance(unpacked, list):
                unpacked = parent.get("unpacked_files") or parent.get("files")
            entries: list[dict[str, Any]]
            if isinstance(unpacked, list) and unpacked:
                entries = [entry for entry in unpacked if isinstance(entry, dict)]
            else:
                entries = [parent]
            for child in entries:
                built = self._candidate(parent, child, feature_title, feature_year, feature_imdb_id)
                if built is None:
                    continue
                candidate, candidate_id, download = built
                # A duplicate key is the same provider record; preserving the
                # first result maintains provider ordering without ambiguity.
                if candidate_id not in downloads:
                    candidates.append(candidate)
                    downloads[candidate_id] = download
        return candidates, downloads

    def search_filename(
        self,
        filename: str,
        identity: MovieIdentity,
    ) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Use SubDL's release-aware v2 media-manager search endpoint.

        The API documents ``/files/search`` as the route for library scanners:
        it returns the movie identity plus a per-subtitle ``match_score`` that
        measures release-name similarity. Only the basename is sent, never a
        local directory path.
        """
        if not self.api_key:
            return [], {}
        # ``Path.name`` on Linux does not split a Windows backslash path, so
        # normalize both separators before taking the basename.
        name = str(filename).replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or "\x00" in name or len(name) > 512:
            return [], {}
        payload = self._request_json(
            "/files/search",
            {
                "filename": name,
                "type": "movie",
                "languages": "en",
                "hi": "1",
                "subs_per_page": "30",
            },
        )
        return self._parse_search_payload(payload, identity, require_match=True)

    def search_identity(self, identity: MovieIdentity) -> tuple[list[Candidate], dict[str, SubdlDownload]]:
        """Use documented title search only when filename matching found nothing."""
        if not self.api_key:
            return [], {}
        payload = self._request_json(
            "/subtitles/search",
            {
                "film_name": identity.title,
                "type": "movie",
                "languages": "en",
                "unpack": "1",
            },
        )
        return self._parse_search_payload(payload, identity)

    def _download_bytes(self, url: str, max_bytes: int) -> bytes:
        self._throttle(url)
        request = urllib.request.Request(url, headers=self._headers("application/octet-stream, */*;q=0.1"))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - URL is provider-host validated or locally built
                return self._read_limited(response, max_bytes, "subtitle")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"SubDL subtitle download HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"SubDL subtitle download network error: {exc.reason}") from exc

    def download_srt(
        self,
        download: SubdlDownload,
        dest: Path,
        *,
        video: Path | None = None,
        expected_video: VideoSnapshot | None = None,
        max_bytes: int = MAX_SUBTITLE_BYTES,
    ) -> None:
        """Download, validate, snapshot-check, and atomically publish one SRT."""
        if download.url:
            url = normalize_subdl_download_url(download.url)
        elif download.n_id:
            subtitle_id = _subdl_identifier(download.n_id)
            if not subtitle_id:
                raise RuntimeError("SubDL candidate has an invalid subtitle identifier")
            # The documented ``format=file`` mode returns a non-ZIP payload
            # only when SubDL can identify one obvious file. That is safer than
            # silently choosing from an archive; unexpected ZIP responses still
            # pass through the one-SRT-only validator below.
            url = (
                f"{SUBDL_API_BASE}/subtitles/{urllib.parse.quote(subtitle_id, safe='')}/download?"
                "format=file"
            )
        else:
            raise RuntimeError("SubDL candidate has no safe download reference")

        data = self._download_bytes(url, max_bytes)
        redirected_url = subdl_download_redirect_url(data)
        if redirected_url is not None:
            data = self._download_bytes(redirected_url, max_bytes)
        text = decode_subdl_srt_payload(data, max_bytes)
        if video is not None and expected_video is not None and not video_snapshot_matches(video, expected_video):
            raise RuntimeError("movie changed during subtitle lookup; downloaded SRT was not activated")
        try:
            atomic_write_text(dest, text, replace=False)
        except FileExistsError as exc:
            raise ConcurrentSidecarError("English SRT appeared during download; preserved the existing sidecar") from exc

def download_subdl_srt(
    url: str,
    dest: Path,
    max_bytes: int,
    *,
    api_key: str = "",
    video: Path | None = None,
    expected_video: VideoSnapshot | None = None,
) -> None:
    """Backward-compatible raw-URL helper; new queue code uses ``SubdlClient``."""
    client = SubdlClient(api_key)
    client.download_srt(
        SubdlDownload(url=normalize_subdl_download_url(url)),
        dest,
        video=video,
        expected_video=expected_video,
        max_bytes=max_bytes,
    )


def as_bool(value: Any) -> bool:
    """API fields arrive as true/false, 0/1, or the strings \"0\"/\"true\"."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def decode_subtitle_bytes(data: bytes) -> str:
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as archive:
            data = archive.read(MAX_SUBTITLE_BYTES + 1)
        if len(data) > MAX_SUBTITLE_BYTES:
            raise ValueError("decompressed subtitle exceeds safety limit")
    for enc in EXTERNAL_SRT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Unlike the shared decode_srt_bytes helper this must return a string:
    # the caller inspects a rejected download to explain why it was rejected.
    return data.decode("utf-8", errors="replace")

def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

def _nonnegative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0

def normalize_language(value: str) -> str:
    return value.strip().casefold()

def normalize_title(value: str) -> str:
    """Return a punctuation/diacritic-insensitive title key for exact comparison."""
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()

def movie_identity_from_video(video: Path) -> MovieIdentity | None:
    """Accept only a canonical ``Title (YYYY).mkv`` name as fallback input."""
    match = MOVIE_IDENTITY_RE.fullmatch(video.stem.strip())
    if not match:
        return None
    title = match.group("title").strip()
    normalized = normalize_title(title)
    if not normalized:
        return None
    return MovieIdentity(title=title, year=int(match.group("year")), normalized_title=normalized)

def release_has_edition_marker(release: str) -> bool:
    normalized = normalize_title(release)
    return any(marker in normalized for marker in EDITION_MARKERS)

def release_has_bluray_keyword(release: str) -> bool:
    """True when the release name carries an explicit Blu-ray keyword."""
    return bool(BLURAY_RELEASE_RE.search(release or ""))


def release_has_library_source_keyword(release: str) -> bool:
    """True when the release names Blu-ray, 1080p, qXR, or Tigole."""
    return bool(LIBRARY_SOURCE_RELEASE_RE.search(release or ""))


def library_release_rank_key(release: str) -> tuple:
    """Prefer 1080p, then qXR, then Tigole, then non-SDH as a last tiebreak."""
    name = release or ""
    return (
        0 if LIBRARY_1080P_RE.search(name) else 1,
        0 if LIBRARY_QXR_RE.search(name) else 1,
        0 if LIBRARY_TIGOLE_RE.search(name) else 1,
        1 if re.search(r"(?i)(?:\bsd h\b|\bsdh\b|hearing.?impaired|\bhi\b)", name) else 0,
    )

def release_matches_movie_title(release: str, title: str) -> bool:
    """True when ``title`` appears as a whole phrase inside the release name.

    Both sides go through ``normalize_title`` (case-, punctuation- and
    diacritic-insensitive) and the match is phrase-boundary aware, so the
    title "Alien" does not match an "Aliens" release and vice versa.
    """
    normalized_title = normalize_title(title)
    if not normalized_title:
        return False
    normalized_release = normalize_title(release or "")
    return re.search(rf"(?<!\w){re.escape(normalized_title)}(?!\w)", normalized_release) is not None

def release_contains_year(release: str, year: int) -> bool:
    """True when the release name carries ``year`` as a standalone number.

    Digit boundaries keep "2009" from matching "20091" and let the check run
    on the normalized name, where ``.``/``-``/``_`` separators are spaces.
    """
    if not 1000 <= int(year) <= 9999:
        return False
    normalized_release = normalize_title(release or "")
    return re.search(rf"(?<!\d){int(year)}(?!\d)", normalized_release) is not None

def release_matches_movie_identity(release: str, identity: MovieIdentity) -> bool:
    """True when the release name names the movie: title and release year."""
    return (
        release_matches_movie_title(release, identity.title)
        and release_contains_year(release, identity.year)
    )

def candidate_rank_key(candidate: Candidate) -> tuple:
    """Library-fit first: 1080p, then qXR, then downloads.

    Trusted/rating/votes remain only as later tiebreakers. Hearing-impaired
    is a last-resort tiebreak so a non-SDH file of equal fit wins.
    """
    return (
        *library_release_rank_key(candidate.release),
        int(candidate.hearing_impaired),
        -candidate.downloads,
        -int(candidate.trusted),
        -candidate.rating,
        -candidate.votes,
    )

def parse_candidates(payload: dict[str, Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        feature = attrs.get("feature_details") or {}
        if not isinstance(feature, dict):
            feature = {}
        files = attrs.get("files") or []
        if not files or not isinstance(files[0], dict):
            continue
        file_id = files[0].get("file_id")
        if file_id is None:
            continue
        out.append(
            Candidate(
                file_id=int(file_id),
                release=str(files[0].get("file_name") or attrs.get("release") or ""),
                moviehash_match=as_bool(attrs.get("moviehash_match")),
                downloads=_nonnegative_int(attrs.get("download_count")),
                votes=_nonnegative_int(attrs.get("votes")),
                rating=_nonnegative_float(attrs.get("ratings")),
                trusted=as_bool(attrs.get("from_trusted")),
                hearing_impaired=as_bool(attrs.get("hearing_impaired")),
                machine_translated=as_bool(attrs.get("machine_translated")),
                ai_translated=as_bool(attrs.get("ai_translated")),
                foreign_parts_only=as_bool(attrs.get("foreign_parts_only")),
                language=str(attrs.get("language") or "en"),
                feature_title=str(feature.get("title") or ""),
                feature_year=_nonnegative_int(feature.get("year")),
                feature_imdb_id=_nonnegative_int(feature.get("imdb_id")),
            )
        )
    return out

def _is_normal_english_human_candidate(candidate: Candidate) -> bool:
    return (
        normalize_language(candidate.language) in ENGLISH_LANGUAGE_TOKENS
        and not candidate.machine_translated
        and not candidate.ai_translated
        and not candidate.foreign_parts_only
    )
def pick_candidate(
    cands: Sequence[Candidate], cfg: Config, *, identity: MovieIdentity | None = None,
    exclude_ids: Iterable[int | str] = (),
) -> Candidate | None:
    """Return one strict best candidate for the requested English subtitle mode.

    A candidate must be a moviehash match on a normal English human SRT whose
    release name carries an explicit Blu-ray keyword and, when ``identity`` is
    given, names the movie (title and release year). Among the qualifying
    candidates the highest download count wins; the trusted flag, rating and
    votes remain as tiebreakers. This yields one deterministic SRT.
    """
    usable = [
        candidate for candidate in cands
        if candidate.moviehash_match
        and _is_normal_english_human_candidate(candidate)
        and release_has_library_source_keyword(candidate.release)
        and (identity is None or release_matches_movie_identity(candidate.release, identity))
        and str(candidate.file_id) not in {str(item) for item in exclude_ids}
    ]
    if not usable:
        return None
    usable.sort(
        key=lambda candidate: (
            *candidate_rank_key(candidate),
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    return usable[0]

def pick_identity_candidate(
    cands: Sequence[Candidate], identity: MovieIdentity, *, exclude_ids: Iterable[int | str] = (),
) -> tuple[Candidate | None, str]:
    """Choose one non-hash candidate when identity and release name agree.

    Title/year must exactly match provider feature metadata, and the release
    name must carry the movie title, the release year, and an explicit
    Blu-ray keyword. Among the qualifying candidates the highest download
    count wins, with the trusted flag, rating and votes as tiebreakers - no
    separate quality floor, so popular but unvoted subtitles still fetch.
    Edition-labelled releases are deliberately not auto-selected because a
    canonical local name contains no reliable edition/cut marker to compare
    against.
    """
    usable = [
        candidate for candidate in cands
        if _is_normal_english_human_candidate(candidate)
        and candidate.feature_year == identity.year
        and normalize_title(candidate.feature_title) == identity.normalized_title
        and not release_has_edition_marker(candidate.release)
        and release_has_library_source_keyword(candidate.release)
        and release_matches_movie_identity(candidate.release, identity)
        and str(candidate.file_id) not in {str(item) for item in exclude_ids}
    ]
    if not usable:
        return None, "no title/year-exact Blu-ray/1080p/qXR/Tigole release naming the movie and its release year"
    usable.sort(
        key=lambda candidate: (
            *candidate_rank_key(candidate),
            str(candidate.file_id), candidate.release.casefold(),
        ),
    )
    top = usable[0]
    top_key = candidate_rank_key(top)
    tied = [candidate for candidate in usable if candidate_rank_key(candidate) == top_key]
    if len(tied) != 1:
        return None, "multiple equally ranked title/year-exact library-source SRT candidates require review"
    return top, "title/year exact; Blu-ray/1080p/qXR/Tigole release naming the movie; 1080p then qXR then Tigole then downloads"

def pick_pooled_candidates(
    entries: list[tuple[Candidate, str, str, str]],
    identity: MovieIdentity | None,
) -> tuple[Candidate | None, str, str, str]:
    """Rank same-tier candidates from different providers as equal sources.

    ``entries`` are ``(candidate, provider, method, provider_reason)`` tuples,
    at most one per provider. A lone entry stands exactly as its provider
    selected it. When both providers contribute, every contributor must also
    carry the release-name policy - movie title, release year and an explicit
    Blu-ray keyword - and the highest download count wins regardless of
    provider; an unbroken tie is held for manual review rather than resolved
    by a provider default.
    """
    if not entries:
        return None, "", "", ""
    if len(entries) == 1:
        candidate, provider, method, reason = entries[0]
        return candidate, provider, method, reason
    conforming: list[tuple[Candidate, str, str, str]] = []
    rejected: list[str] = []
    for candidate, provider, method, reason in entries:
        if release_has_library_source_keyword(candidate.release) and (
            identity is None or release_matches_movie_identity(candidate.release, identity)
        ):
            conforming.append((candidate, provider, method, reason))
        else:
            rejected.append(provider_label(provider))
    if not conforming:
        return (
            None, "", "",
            f"no release met the selection policy on either provider ({'; '.join(rejected)} rejected)",
        )
    if len(conforming) == 1:
        candidate, provider, method, reason = conforming[0]
        return candidate, provider, method, f"{reason}; {'; '.join(rejected)} release did not meet the selection policy"
    conforming.sort(
        key=lambda entry: (
            *candidate_rank_key(entry[0]),
            entry[1], str(entry[0].file_id), entry[0].release.casefold(),
        ),
    )
    top_key = candidate_rank_key(conforming[0][0])
    tied = [entry for entry in conforming if candidate_rank_key(entry[0]) == top_key]
    if len(tied) != 1:
        return None, "", "", "multiple equally ranked candidates across providers require review"
    candidate, provider, method, reason = tied[0]
    loser = next(entry for entry in conforming if entry[1] != provider)
    return (
        candidate, provider, method,
        f"{reason}; best across both providers (beats {provider_label(loser[1])})",
    )

def looks_like_srt(text: str) -> bool:
    """The shared verdict on whether text contains a well-formed SRT cue.

    This used to be a private copy of the cue pattern, and it had drifted: it
    anchored the cue number at column 0 while the other four tools allowed
    leading whitespace. A subtitle with an indented cue number was therefore
    rejected here at download time ("downloaded payload is not a valid SRT
    subtitle") yet accepted as canonical by library_auditor, movie_standardizer
    and mkv_track_cleaner. Delegating to the shared helper makes that
    disagreement impossible.
    """
    return srt_looks_valid(text)

def is_english_srt_sidecar(path: Path, video_stem: str) -> bool:
    """Return true only for an English SRT attached to this exact movie stem."""
    if not path.is_file() or path.is_symlink() or path.suffix.lower() != DIRECT_PLAY_SUBTITLE_EXTENSION:
        return False
    prefix = video_stem.casefold() + "."
    stem = path.stem.casefold()
    if not stem.startswith(prefix):
        return False
    tokens = [token for token in stem[len(prefix):].split(".") if token]
    # Jellyfin permits descriptive title fields, so only require that one token
    # is English. The filename prefix check keeps a neighboring movie's SRT from
    # blocking this fetch.
    return any(token in ENGLISH_LANGUAGE_TOKENS for token in tokens)

def has_english_sidecar(folder: Path, video_stem: str) -> Path | None:
    """Return the first direct-play-safe English SRT for this exact movie file."""
    try:
        names = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return None
    return next((path for path in names if is_english_srt_sidecar(path, video_stem)), None)

def discover_videos(root: Path, min_bytes: int) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.strip().lower() not in DISC_DIR_NAMES
            and d.strip().lower() not in EXTRA_DIR_NAMES
            and not (Path(dirpath) / d).is_symlink()
        ]
        current = Path(dirpath)
        for name in filenames:
            if SAMPLE_NAME_RE.search(Path(name).stem):
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            path = current / name
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size < min_bytes:
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).casefold())
    return found

def canonical_movie_layout_issue(video: Path, library: Path) -> str | None:
    """Return a reason when a file violates the one-movie-per-folder contract."""
    parent = video.parent
    if parent == library:
        return "noncanonical layout: movie MKV is directly under the library root"
    if parent.is_symlink() or video.is_symlink() or not video.is_file():
        return "noncanonical layout: movie is not a regular non-symlink file in a regular folder"
    if video.stem.casefold() != parent.name.casefold():
        return "noncanonical layout: MKV stem does not match its movie-folder name"
    try:
        sibling_mkvs = [
            item for item in parent.iterdir()
            if item.suffix.lower() == ".mkv" and item.is_file() and not item.is_symlink()
        ]
    except OSError as exc:
        return f"noncanonical layout: could not inspect movie folder ({exc})"
    if len(sibling_mkvs) != 1:
        return f"noncanonical layout: expected one regular MKV in movie folder, found {len(sibling_mkvs)}"
    return None

def dest_for(video: Path, cfg: Config) -> Path:
    # Plex/Jellyfin: file next to the video, same stem + language suffix.
    # cfg.sidecar_suffix is always EXTERNAL_SRT_SUFFIX (".eng.srt"); keep the
    # Config hook so a future override stays one call site away.
    _ = cfg.sidecar_suffix
    return exact_external_english_srt_path(video)


def refetch_english_srt(
    video: Path,
    dest: Path,
    *,
    exclude_ids: Sequence[int | str] = (),
    log_file: Path | None = None,
) -> tuple[bool, str, str]:
    """Replace ``dest`` with the next qualifying English SRT from the APIs.

    Used by ``sync_subtitles.py`` when ffsubsync cannot trust the current
    sidecar. Tried ``file_id`` values in ``exclude_ids`` are skipped so a
    retry does not re-download the same upload. Returns
    ``(ok, file_id, detail)``.
    """
    api_key = (os.environ.get("OPENSUBTITLES_API_KEY") or OPENSUBTITLES_API_KEY).strip()
    subdl_key = (os.environ.get("SUBDL_API_KEY") or SUBDL_API_KEY).strip()
    if not api_key and not subdl_key:
        return False, "", "no subtitle API key configured for a replacement fetch"
    cfg = Config(
        library=video.parent,
        log_file=log_file,
        report_file=video.parent / "unused-report.txt",
        api_key=api_key,
        subdl_api_key=subdl_key,
        username=(os.environ.get("OPENSUBTITLES_USERNAME") or OPENSUBTITLES_USERNAME).strip(),
        password=(os.environ.get("OPENSUBTITLES_PASSWORD") or OPENSUBTITLES_PASSWORD).strip(),
        identity_fallback=True,
    )
    identity = movie_identity_from_video(video)
    snapshot = video_snapshot(video)
    excluded = {str(item) for item in exclude_ids}
    pick: Candidate | None = None
    subdl_downloads: dict[str, SubdlDownload] = {}
    selected_provider = PROVIDER_OPENSUBTITLES
    if api_key:
        client = OpenSubtitlesClient(cfg)
        try:
            digest = moviehash(video)
            cands = client.search(movie_hash=digest, query=video.stem)
            pick = pick_candidate(cands, cfg, identity=identity, exclude_ids=excluded)
        except (RuntimeError, ValueError, OSError) as exc:
            log(f"replacement hash search failed: {exc}", log_file=log_file)
        if pick is None and identity is not None:
            try:
                cands = client.search_identity(identity)
                pick, _reason = pick_identity_candidate(cands, identity, exclude_ids=excluded)
            except (RuntimeError, ValueError, OSError) as exc:
                log(f"replacement identity search failed: {exc}", log_file=log_file)
        if pick is not None:
            selected_provider = PROVIDER_OPENSUBTITLES
    if pick is None and subdl_key and identity is not None:
        subdl = SubdlClient(subdl_key)
        try:
            cands, subdl_downloads = subdl.search_filename(video.name, identity)
            pick, _reason = pick_subdl_identity_candidate(
                cands, identity, require_release_match_score=True,
            )
            if pick is not None and str(pick.file_id) in excluded:
                pick = None
        except (RuntimeError, ValueError, OSError) as exc:
            log(f"replacement SubDL search failed: {exc}", log_file=log_file)
        if pick is None:
            try:
                cands, subdl_downloads = subdl.search_identity(identity)
                pick, _reason = pick_identity_candidate(cands, identity, exclude_ids=excluded)
            except (RuntimeError, ValueError, OSError) as exc:
                log(f"replacement SubDL title search failed: {exc}", log_file=log_file)
        if pick is not None:
            selected_provider = PROVIDER_SUBDL
    if pick is None:
        return False, "", "no unused qualifying English SRT for a replacement fetch"

    # Never remove or download over the live sidecar.  API quota errors,
    # interrupted transfers, validation failures, and disk errors must leave
    # the exact file that the caller gave us in place.  The sync caller may
    # try several candidates, so each successful candidate is published with
    # one atomic swap only after the downloader has completely written it.
    if dest.is_symlink():
        return False, str(pick.file_id), "refusing to replace a symlink sidecar"
    staging = dest.with_name(f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex}.refetch.tmp")
    try:
        if selected_provider == PROVIDER_SUBDL:
            download = subdl_downloads.get(str(pick.file_id))
            if download is None:
                return False, str(pick.file_id), "SubDL candidate download reference is missing"
            SubdlClient(subdl_key).download_srt(download, staging, video=video, expected_video=snapshot)
        else:
            if not isinstance(pick.file_id, int):
                return False, str(pick.file_id), "OpenSubtitles candidate has an invalid file identifier"
            OpenSubtitlesClient(cfg).download_srt(pick.file_id, staging, video=video, expected_video=snapshot)
        usable, reason = validate_srt_sidecar(staging)
        if not usable:
            return False, str(pick.file_id), f"downloaded replacement is unusable ({reason})"
        os.replace(staging, dest)
    except (RuntimeError, ValueError, OSError, ConcurrentSidecarError) as exc:
        return False, str(pick.file_id), str(exc)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
    return True, str(pick.file_id), pick.release or "unnamed release"


# =============================================================================
# EMBEDDED SUBTITLE EXTRACTION
#
# Before spending a provider request, look inside the movie. A Jellyfin MKV
# very often already carries the English subtitle as an embedded track, and
# that track is byte-exact for this release, needs no timing correction, and
# cannot be the wrong cut. Extracting it into the canonical external sidecar
# is therefore strictly better than downloading anything:
#
#   * no provider request is spent, so the UTC caps are untouched;
#   * the cues carry the container's own timestamps, so the sidecar is
#     frame-accurate for this exact file and must NOT be re-synced;
#   * mkv_track_cleaner.py still strips every embedded subtitle afterwards,
#     so the external sidecar remains the sole subtitle option.
#
# Text tracks (SRT/SSA/ASS/WebVTT) are converted to SRT in-process with the
# standard library. Image tracks (PGS/SUP, VobSub, DVB) need OCR, which a
# stdlib-only script cannot vendor: they are handed to an external OCR
# backend (sup2srt + Tesseract, Subtitle Edit, or PgsToSrt) when one is
# installed, and are skipped with the exact fix printed when none is.
#
# Extraction never rewrites or deletes the movie: mkvextract reads it and
# writes temporary files outside the library, and the moviehash (size plus
# the first and last 64 KiB) is untouched.
# =============================================================================

# Embedded subtitle codecs this tool can turn into an external SRT. The value
# is the extension mkvextract must write; PGS becomes a .sup stream, VobSub
# becomes the .idx/.sub pair Subtitle Edit reads.
EXTRACT_TEXT_CODECS: dict[str, str] = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
}

EXTRACT_IMAGE_CODECS: dict[str, str] = {
    "S_HDMV/PGS": ".sup",
    "S_VOBSUB": ".idx",
    "S_DVBSUB": ".sub",
}

# Preference inside each class: an already-tagged plain SRT needs no
# conversion; ASS/SSA keep styling that is dropped on the way to SRT; WebVTT
# and USF are rare and converted best-effort. PGS beats DVD-era VobSub.
TEXT_CODEC_RANK: dict[str, int] = {
    "S_TEXT/UTF8": 0,
    "S_TEXT/ASCII": 0,
    "S_TEXT/ASS": 1,
    "S_TEXT/SSA": 2,
    "S_TEXT/WEBVTT": 3,
    "S_TEXT/USF": 4,
}

IMAGE_CODEC_RANK: dict[str, int] = {
    "S_HDMV/PGS": 0,
    "S_VOBSUB": 1,
    "S_DVBSUB": 2,
}

USF_XML_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.IGNORECASE | re.DOTALL)
USF_TAG_RE = re.compile(r"<[^>]+>")

# A full movie track carries hundreds of cues. A handful means the track is
# signs/songs-only or a foreign-language-forced stream, and writing it as the
# movie's English subtitle would be a silent downgrade.
DEFAULT_EXTRACT_MIN_CUES = 10
# How many candidates to try per movie before giving up and letting the
# provider tiers run. Text extraction is cheap; OCR is not, so each class is
# capped separately and the whole run can cap OCR jobs (--ocr-limit).
DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT = 3
DEFAULT_EXTRACT_IMAGE_CANDIDATE_LIMIT = 2
DEFAULT_MKVEXTRACT_TIMEOUT_SEC = 900.0
DEFAULT_OCR_TIMEOUT_SEC = 1_800.0

# Durable, outside-the-library record of which sidecars this tool created from
# the movie's own tracks. sync_subtitles.py reads it so it never spends an
# ffsubsync run "correcting" a subtitle that is frame-accurate by
# construction. It lives beside the other ReportsAndLogs artefacts.
EXTRACTED_LEDGER_NAME = "subtitle_fetcher_extracted.json"
EXTRACTED_LEDGER_ENV = "SUBTITLE_EXTRACTED_LEDGER"
EXTRACTED_LEDGER_VERSION = 1

OCR_BACKEND_AUTO = "auto"
OCR_BACKEND_NONE = "none"
OCR_BACKEND_CUSTOM = "custom"
OCR_BACKEND_PGSRIP = "pgsrip"
OCR_BACKEND_SUP2SRT = "sup2srt"
OCR_BACKEND_SUBTITLEEDIT = "subtitleedit"
OCR_BACKEND_PGSTOSRT = "pgstosrt"
OCR_BACKEND_CHOICES: tuple[str, ...] = (
    OCR_BACKEND_AUTO,
    OCR_BACKEND_PGSRIP,
    OCR_BACKEND_SUP2SRT,
    OCR_BACKEND_SUBTITLEEDIT,
    OCR_BACKEND_PGSTOSRT,
    OCR_BACKEND_CUSTOM,
    OCR_BACKEND_NONE,
)
# Auto-detection order. pgsrip comes first: it is the actively maintained
# option, it filters by language itself, and it reads both a .sup stream and
# an .mkv, so it needs the least help from us.
OCR_BACKEND_AUTO_ORDER: tuple[str, ...] = (
    OCR_BACKEND_PGSRIP,
    OCR_BACKEND_SUP2SRT,
    OCR_BACKEND_SUBTITLEEDIT,
    OCR_BACKEND_PGSTOSRT,
)

# Tesseract language codes are ISO 639-2/T (``eng``), while PgsToSrt is
# usually called with the same three-letter codes; Subtitle Edit and sup2srt
# accept either. Keep one place that normalizes what the tools are given.
OCR_TESSERACT_LANGUAGES: dict[str, str] = {"en": "eng", "eng": "eng", "english": "eng"}

# pgsrip parses languages with babelfish, which wants ISO 639-1/2B tags
# (``en``, ``pt-BR``); the container gives us ISO 639-2/T (``eng``).
OCR_PGSRIP_LANGUAGES: dict[str, str] = {"eng": "en", "en": "en", "english": "en"}

# Very common English function words. Their share of a real dialogue track is
# far above this floor; OCR noise and wrong-language tracks fall below it.
ENGLISH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "is", "was", "are", "were", "be", "been", "it", "its", "you", "your",
    "i", "me", "my", "we", "us", "he", "she", "they", "them", "his", "her",
    "that", "this", "these", "those", "for", "with", "as", "so", "not", "no",
    "do", "did", "does", "have", "has", "had", "what", "when", "where", "who",
    "how", "why", "all", "just", "get", "got", "go", "going", "know", "think",
    "will", "can", "cant", "dont", "im", "thats", "there", "here", "up", "out",
})

# Characters that dominate when an OCR pass mis-reads a bitmap subtitle.
OCR_NOISE_CHARS = "|~^@#"


# ---------------------------------------------------------------------------
# External binaries (MKVToolNix)
# ---------------------------------------------------------------------------
_MKVTOOLNIX_PATHS: dict[str, tuple[str, ...]] = {
    "mkvmerge": (
        r"C:\Program Files\MKVToolNix\mkvmerge.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe",
        "/usr/bin/mkvmerge",
        "/usr/local/bin/mkvmerge",
        "/opt/homebrew/bin/mkvmerge",
    ),
    "mkvextract": (
        r"C:\Program Files\MKVToolNix\mkvextract.exe",
        r"C:\Program Files (x86)\MKVToolNix\mkvextract.exe",
        "/usr/bin/mkvextract",
        "/usr/local/bin/mkvextract",
        "/opt/homebrew/bin/mkvextract",
    ),
}

MKVTOOLNIX_INSTALL_HINT = (
    "install MKVToolNix (https://mkvtoolnix.download/) so mkvmerge and "
    "mkvextract are on the PATH"
)


def find_mkvtoolnix_binary(name: str, explicit: str | None = None) -> str | None:
    """Locate ``mkvmerge``/``mkvextract`` on the PATH or in a known install dir."""
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return str(explicit_path)
        return shutil.which(explicit)
    found = shutil.which(name)
    if found:
        return found
    for install_path in _MKVTOOLNIX_PATHS.get(name, ()):
        if Path(install_path).is_file():
            return install_path
    return None


def _decode_stream(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def run_external_command(
    command: Sequence[str], *, timeout: float = 0.0
) -> tuple[int, str, str]:
    """Run one external binary, never raising on timeout or a missing program.

    Every binary this tool shells out to is optional. A missing program, a
    non-zero exit, or a timeout all come back as a plain ``(rc, out, err)`` so
    the caller can report the fix and fall through to the next strategy.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            timeout=timeout if timeout and timeout > 0 else None,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", f"could not run {command[0] if command else 'command'}: {exc}"
    return completed.returncode, _decode_stream(completed.stdout), _decode_stream(completed.stderr)


def _command_tail(text: str, max_lines: int = 3) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no output"
    return " | ".join(lines[-max_lines:])


# ---------------------------------------------------------------------------
# Track discovery and classification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EmbeddedSubtitleTrack:
    """One embedded subtitle stream worth trying to extract."""

    track_id: int
    codec_id: str
    language: str
    name: str
    kind: str  # "text" or "image"
    extension: str
    default: bool = False
    forced: bool = False
    sdh: bool = False
    rank: int = 0

    @property
    def label(self) -> str:
        parts = [f"track {self.track_id}", self.codec_id]
        if self.name:
            parts.append(self.name)
        if self.sdh:
            parts.append("SDH")
        return ", ".join(parts)


def _track_properties(track: dict[str, Any]) -> dict[str, Any]:
    props = track.get("properties")
    return props if isinstance(props, dict) else {}


def _track_flag(props: dict[str, Any], *names: str) -> bool:
    """Read a Matroska flag under either its modern or legacy JSON name."""
    for name in names:
        value = props.get(name)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes"}:
                return True
        elif value:
            return True
    return False


def subtitle_track_languages(track: dict[str, Any]) -> set[str]:
    props = _track_properties(track)
    codes = {
        str(props.get("language") or "").strip().lower(),
        str(props.get("language_ietf") or "").strip().lower(),
        str(props.get("tag_language") or "").strip().lower(),
    }
    return {re.split(r"[-_.]", code)[0] for code in codes if code}


def subtitle_track_is_english(track: dict[str, Any]) -> bool:
    """English either by tag or by an explicit English track name.

    A bare ``und`` stream is only English when its own name says so; the
    cleaner follows the identical rule, so the two tools cannot disagree about
    which stream is the movie's English subtitle.
    """
    languages = subtitle_track_languages(track)
    if languages & ENGLISH_LANGUAGE_TOKENS:
        return True
    if languages and not (languages <= {"und", ""}):
        return False
    return bool(re.search(r"\b(english|eng)\b", str(_track_properties(track).get("track_name") or "").lower()))


SUBTITLE_FORCED_NAME_RE = re.compile(r"\b(forced|foreign only|foreign parts only|signs?/?songs?)\b")
SUBTITLE_COMMENTARY_NAME_RE = re.compile(r"\b(commentary|riff|rifftrax)\b")


def subtitle_track_is_forced(track: dict[str, Any]) -> bool:
    """True for a signs/songs or foreign-parts-only track.

    Those tracks are deliberately incomplete: they carry only the lines a
    viewer cannot already understand. Publishing one as the movie's English
    subtitle would look like success while leaving the dialogue missing, so
    they never become a sidecar here.
    """
    props = _track_properties(track)
    if _track_flag(props, "flag_forced", "forced_track"):
        return True
    return bool(SUBTITLE_FORCED_NAME_RE.search(str(props.get("track_name") or "").lower()))


def subtitle_track_is_commentary(track: dict[str, Any]) -> bool:
    props = _track_properties(track)
    if _track_flag(props, "flag_commentary"):
        return True
    return bool(SUBTITLE_COMMENTARY_NAME_RE.search(str(props.get("track_name") or "").lower()))


def subtitle_track_is_sdh(track: dict[str, Any]) -> bool:
    props = _track_properties(track)
    if _track_flag(props, "flag_hearing_impaired"):
        return True
    return bool(re.search(r"\b(sdh|hearing[ -]impaired)\b", str(props.get("track_name") or "").lower()))


def classify_embedded_subtitle_tracks(
    tracks: Sequence[dict[str, Any]],
) -> list[EmbeddedSubtitleTrack]:
    """Pick the English subtitle streams worth extracting, best first.

    Non-English, forced, and commentary streams are dropped outright. Text
    streams outrank image streams (a conversion is free, OCR is minutes), and
    inside each class the container's default track wins before codec
    preference and track order.
    """
    candidates: list[EmbeddedSubtitleTrack] = []
    for track in tracks:
        if str(track.get("type") or "").strip().lower() != "subtitles":
            continue
        props = _track_properties(track)
        codec_id = str(props.get("codec_id") or "").strip().upper()
        if codec_id in EXTRACT_TEXT_CODECS:
            kind = "text"
            extension = EXTRACT_TEXT_CODECS[codec_id]
            rank = TEXT_CODEC_RANK.get(codec_id, 9)
        elif codec_id in EXTRACT_IMAGE_CODECS:
            kind = "image"
            extension = EXTRACT_IMAGE_CODECS[codec_id]
            rank = IMAGE_CODEC_RANK.get(codec_id, 9)
        else:
            continue
        if not subtitle_track_is_english(track):
            continue
        if subtitle_track_is_forced(track) or subtitle_track_is_commentary(track):
            continue
        try:
            track_id = int(track.get("id"))
        except (TypeError, ValueError):
            continue
        candidates.append(
            EmbeddedSubtitleTrack(
                track_id=track_id,
                codec_id=codec_id,
                language=str(props.get("language") or props.get("language_ietf") or "und"),
                name=str(props.get("track_name") or ""),
                kind=kind,
                extension=extension,
                default=_track_flag(props, "flag_default", "default_track"),
                sdh=subtitle_track_is_sdh(track),
                rank=rank,
            )
        )
    candidates.sort(key=lambda item: (0 if item.kind == "text" else 1, item.rank,
                                      0 if item.default else 1, item.track_id))
    return candidates


def probe_embedded_subtitle_tracks(
    video: Path, mkvmerge_bin: str, *, timeout: float = 300.0
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return the movie's subtitle tracks, or ``(None, reason)`` it could not."""
    rc, out, err = run_external_command([mkvmerge_bin, "-J", str(video)], timeout=timeout)
    if rc != 0:
        return None, f"mkvmerge could not read the movie (exit {rc}): {_command_tail(err or out)}"
    try:
        payload = json.loads(out)
    except (ValueError, TypeError):
        return None, "mkvmerge produced unreadable track information"
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return None, "mkvmerge reported no tracks"
    return [track for track in tracks if isinstance(track, dict)], ""


# ---------------------------------------------------------------------------
# SRT rendering, ASS/SSA and WebVTT conversion
# ---------------------------------------------------------------------------
SRT_TIMING_RE = re.compile(
    r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
)
ASS_TIMESTAMP_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})$")
ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def parse_srt_cues(text: str) -> list[tuple[str, str, str]]:
    """Parse ``text`` into ``[(start, end, body), ...]`` with SRT timestamps.

    Used to renumber and re-render anything this tool writes, so an extracted
    track can never carry a broken cue index, a stray BOM, or CRLF endings.
    """
    cues: list[tuple[str, str, str]] = []
    for block in re.split(r"\n\s*\n", normalize_srt_newlines(text).strip()):
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = SRT_TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        body = "\n".join(lines[timing_index + 1:]).strip()
        if not body:
            continue
        start = _pad_srt_timestamp(match.group(1))
        end = _pad_srt_timestamp(match.group(2))
        if start is None or end is None:
            continue
        cues.append((start, end, body))
    return cues


def _pad_srt_timestamp(value: str) -> str | None:
    value = value.strip().replace(".", ",")
    clock, _, millis = value.rpartition(",")
    if not clock:
        return None
    parts = clock.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    millis = (millis + "000")[:3]
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis}"


def render_srt_cues(cues: Sequence[tuple[str, str, str]]) -> str:
    """Render cues as a canonical, renumbered, UTF-8 SRT document."""
    chunks: list[str] = []
    for index, (start, end, body) in enumerate(cues, start=1):
        chunks.append(f"{index}\n{start} --> {end}\n{body}\n")
    return "\n".join(chunks)


def normalize_extracted_srt(text: str) -> str:
    """Re-render any SRT text into the canonical form this tool writes."""
    return render_srt_cues(parse_srt_cues(text))


def _ass_timestamp_to_srt(token: str) -> str | None:
    match = ASS_TIMESTAMP_RE.match(token.strip())
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{(fraction + '000')[:3]}"


def _ass_plain_text(raw: str) -> str:
    """Strip ASS/SSA styling so the cue survives the trip into an SRT file.

    Override blocks (``{\\i1}``) carry styling SRT cannot express; ``\\N`` and
    ``\\n`` are the format's line breaks and ``\\h`` its non-breaking space.
    Everything else is literal text a player is expected to show.
    """
    text = ASS_OVERRIDE_RE.sub("", raw)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    cleaned_lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in cleaned_lines if line).strip()


def ass_to_srt(text: str) -> str:
    """Convert an ASS/SSA subtitle document to canonical SRT text.

    Only ``Dialogue`` lines become cues; ``Comment`` lines are the format's
    non-displaying notes and are dropped. The event column order differs
    between SSA (v4) and ASS (v4+), so the ``Format:`` line is what selects
    the Start/End/Text columns rather than a fixed index.
    """
    body = normalize_srt_newlines(text)
    section = ""
    columns: list[str] = []
    parsed: list[tuple[str, str, str]] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "events":
            continue
        lowered = stripped.lower()
        if lowered.startswith("format:"):
            columns = [part.strip().lower() for part in stripped[len("format:"):].split(",")]
            continue
        if lowered.startswith("dialogue:"):
            payload = stripped[len("dialogue:"):]
        elif lowered.startswith("comment:"):
            continue
        else:
            continue
        if not columns:
            continue
        parts = payload.split(",", len(columns) - 1)
        if len(parts) != len(columns):
            continue
        row = dict(zip(columns, parts, strict=True))
        start = _ass_timestamp_to_srt(row.get("start", ""))
        end = _ass_timestamp_to_srt(row.get("end", ""))
        if not start or not end:
            continue
        cue = _ass_plain_text(row.get("text", ""))
        if not cue:
            continue
        parsed.append((start, end, cue))
    parsed.sort(key=lambda cue: cue[0])
    return render_srt_cues(parsed)


def vtt_to_srt(text: str) -> str:
    """Convert a WebVTT document to canonical SRT text (best effort)."""
    body = normalize_srt_newlines(text)
    if body.startswith("\ufeff"):
        body = body[1:]
    parsed: list[tuple[str, str, str]] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing_line = lines[timing_index]
        # WebVTT allows cue settings after the end timestamp; the regex takes
        # the two timestamps and ignores the rest.
        match = SRT_TIMING_RE.search(timing_line)
        if not match:
            continue
        start = _pad_srt_timestamp(match.group(1))
        end = _pad_srt_timestamp(match.group(2))
        if not start or not end:
            continue
        body_text = "\n".join(
            USF_TAG_RE.sub("", line) for line in lines[timing_index + 1:]
        ).strip()
        if not body_text:
            continue
        parsed.append((start, end, body_text))
    return render_srt_cues(parsed)


def usf_to_srt(text: str) -> str:
    """Convert the rare USF (XML) subtitle track to SRT text, best effort."""
    parsed: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r"<subtitle[^>]*start=\"(?P<start>[^\"]+)\"[^>]*end=\"(?P<end>[^\"]+)\"[^>]*>(?P<body>.*?)</subtitle>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        start = _pad_srt_timestamp(match.group("start").replace(".", ","))
        end = _pad_srt_timestamp(match.group("end").replace(".", ","))
        if not start or not end:
            continue
        fragments = USF_XML_TEXT_RE.findall(match.group("body"))
        cue = "\n".join(
            _html.unescape(USF_TAG_RE.sub("", fragment)).strip() for fragment in fragments
        ).strip()
        if not cue:
            continue
        parsed.append((start, end, cue))
    return render_srt_cues(parsed)


# ---------------------------------------------------------------------------
# Quality gate: is the extracted text a real English subtitle?
# ---------------------------------------------------------------------------
def non_latin_ratio(text: str) -> float:
    """Share of alphabetic characters outside the Latin blocks.

    A Cyrillic, Greek, CJK, or Arabic embedded track is not an English
    subtitle however good the extraction was.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    non_latin = sum(1 for char in letters if ord(char) > 0x024F)
    return non_latin / len(letters)


def extracted_subtitle_quality(
    text: str, *, min_cues: int = DEFAULT_EXTRACT_MIN_CUES, method: str = "text"
) -> tuple[bool, str]:
    """Decide whether extracted text may become the movie's English sidecar.

    A subtitle taken from the movie's own track is authoritative about timing
    but not about content: a mis-tagged foreign track or a failed OCR pass
    would both produce a file that looks like success. Every extracted track
    therefore passes the same conservative gate a download passes, plus two
    checks that only matter for extraction (cue count and OCR noise).
    """
    if not text.strip():
        return False, "the extracted track contained no subtitle text"
    if len(text.encode("utf-8", errors="replace")) > MAX_SUBTITLE_BYTES:
        return False, f"the extracted subtitle exceeds the {MAX_SUBTITLE_BYTES // (1024 * 1024)} MiB safety limit"
    if not looks_like_srt(text):
        return False, "the extracted track did not convert to valid SRT cues"
    cues = parse_srt_cues(text)
    if len(cues) < min_cues:
        return (
            False,
            f"only {len(cues)} cue(s) extracted; a complete movie track needs "
            f"at least {min_cues} (this track is probably signs/songs-only)",
        )
    sample = " ".join(cue[2] for cue in cues)
    if non_latin_ratio(sample) > 0.40:
        return False, "the extracted text is not Latin-script (this track is not English)"
    words = re.findall(r"[A-Za-z']+", sample)
    if len(words) >= 100:
        hits = sum(1 for word in words if word.lower() in ENGLISH_STOPWORDS)
        if hits / len(words) < 0.04:
            return False, "the extracted text does not read as English (OCR noise or a foreign track)"
    if method == "ocr":
        noise = sum(sample.count(char) for char in OCR_NOISE_CHARS)
        if noise and noise / max(1, len(sample)) > 0.02:
            return False, "the OCR output looks like noise rather than dialogue"
    return True, ""


# ---------------------------------------------------------------------------
# OCR backends for image-based subtitles (PGS/SUP, VobSub, DVB)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OcrBackend:
    """An external program that turns a bitmap subtitle stream into text."""

    key: str
    label: str
    program: tuple[str, ...]
    supports: frozenset[str]
    # "output": writes the path we pass. "sibling": writes next to the input.
    output_mode: str = "output"
    arg_template: tuple[str, ...] = ()

    def build_command(self, source: Path, output: Path, *, track_id: int, language: str) -> list[str]:
        if self.key == OCR_BACKEND_CUSTOM:
            mapping = {
                "{input}": str(source),
                "{output}": str(output),
                "{track}": str(track_id),
                "{lang}": OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
            }
            return [*self.program, *(mapping.get(token, token) for token in self.arg_template)]
        if self.key == OCR_BACKEND_PGSRIP:
            # pgsrip writes its .srt beside the input; there is no output flag.
            return [
                *self.program,
                "-l", OCR_PGSRIP_LANGUAGES.get(language.lower(), language),
                str(source),
            ]
        if self.key == OCR_BACKEND_SUP2SRT:
            return [
                *self.program,
                "-l", OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
                "-o", str(output),
                str(source),
            ]
        if self.key == OCR_BACKEND_SUBTITLEEDIT:
            # Subtitle Edit OCRs an image-based input and writes <input>.srt
            # beside it; there is no output-path flag in its CLI.
            return [*self.program, "/convert", str(source), "srt", "/encoding:utf-8"]
        if self.key == OCR_BACKEND_PGSTOSRT:
            return [
                *self.program,
                "--input", str(source),
                "--output", str(output),
                "--tesseractlanguage", OCR_TESSERACT_LANGUAGES.get(language.lower(), language),
            ]
        raise ValueError(f"unknown OCR backend: {self.key}")

    def result_path(self, source: Path, output: Path) -> Path:
        if self.output_mode == "sibling":
            return source.with_suffix(".srt")
        return output

    def supports_track(self, track: EmbeddedSubtitleTrack) -> bool:
        family = {
            "S_HDMV/PGS": "PGS",
            "S_VOBSUB": "VOBSUB",
            "S_DVBSUB": "DVBSUB",
        }.get(track.codec_id.upper(), "PGS")
        return family in self.supports


def _resolve_program(explicit: str, name: str, *search_paths: str) -> str | None:
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return str(explicit_path)
        found = shutil.which(explicit)
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    for search_path in search_paths:
        if Path(search_path).is_file():
            return search_path
    return None


def _subtitleedit_program(explicit: str = "") -> tuple[str, ...] | None:
    """Subtitle Edit is a Windows GUI app; ``mono`` runs it elsewhere."""
    known = (
        r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe",
        r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe",
        "/usr/lib/subtitleedit/SubtitleEdit.exe",
        "/opt/subtitleedit/SubtitleEdit.exe",
    )
    program = _resolve_program(explicit, "SubtitleEdit", *known)
    if program:
        return (program,)
    mono = shutil.which("mono")
    if mono:
        for candidate in known[2:]:
            if Path(candidate).is_file():
                return (mono, candidate)
        if explicit and explicit.lower().endswith(".exe") and Path(explicit).is_file():
            return (mono, explicit)
    return None


def _pgstosrt_program(explicit: str = "") -> tuple[str, ...] | None:
    """PgsToSrt ships as a .NET dll, so it needs dotnet plus a dll path."""
    dll = explicit or os.environ.get("PGSTOSRT_DLL", "").strip()
    if not dll or not Path(dll).is_file():
        return None
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return None
    return (dotnet, dll)


OCR_BACKEND_BUILDERS: dict[str, Callable[[str], OcrBackend | None]] = {}


def build_ocr_backend(key: str, explicit_bin: str = "") -> OcrBackend | None:
    if key == OCR_BACKEND_PGSRIP:
        program = _resolve_program(explicit_bin, "pgsrip")
        if not program:
            return None
        # pgsrip reads a .sup stream or an .mkv/.mks and OCRs the PGS tracks
        # of the languages named with -l; everything else is filtered out.
        return OcrBackend(key, "pgsrip + Tesseract", (program,), frozenset({"PGS"}),
                          output_mode="sibling")
    if key == OCR_BACKEND_SUP2SRT:
        program = _resolve_program(explicit_bin, "sup2srt")
        if not program:
            return None
        return OcrBackend(key, "sup2srt + Tesseract", (program,), frozenset({"PGS"}))
    if key == OCR_BACKEND_SUBTITLEEDIT:
        se_program = _subtitleedit_program(explicit_bin)
        if not se_program:
            return None
        # Subtitle Edit reads both PGS (.sup) and VobSub (.idx/.sub) inputs.
        return OcrBackend(key, "Subtitle Edit", se_program,
                          frozenset({"PGS", "VOBSUB", "DVBSUB"}), output_mode="sibling")
    if key == OCR_BACKEND_PGSTOSRT:
        pgstosrt_program = _pgstosrt_program(explicit_bin)
        if not pgstosrt_program:
            return None
        return OcrBackend(key, "PgsToSrt", pgstosrt_program, frozenset({"PGS"}))
    return None


OCR_INSTALL_HINT = (
    "install one image-subtitle OCR backend to extract PGS/VobSub tracks: "
    "pgsrip (pip install pgsrip, needs MKVToolNix + tesseract + tessdata), "
    "sup2srt + Tesseract (https://github.com/retrontology/sup2srt), Subtitle Edit "
    "(https://www.nikse.dk/subtitleedit), or PgsToSrt with PGSTOSRT_DLL set; "
    "text subtitle tracks are extracted without any of them"
)


def detect_ocr_backend(
    preferred: str = OCR_BACKEND_AUTO, *, explicit_bin: str = "", arg_template: str = ""
) -> tuple[OcrBackend | None, str]:
    """Return the OCR backend to use and a note saying what was (not) found.

    ``auto`` tries sup2srt, then Subtitle Edit, then PgsToSrt. Nothing here is
    fatal: an image-only movie simply falls through to the provider tiers, and
    the note is what the report and the log show as the reason.
    """
    if preferred == OCR_BACKEND_NONE:
        return None, "image-subtitle OCR is disabled (--ocr-backend none)"
    if preferred == OCR_BACKEND_CUSTOM or (arg_template.strip() and explicit_bin.strip()):
        program = _resolve_program(explicit_bin, "")
        if not program:
            return None, f"--ocr-backend custom needs --ocr-bin (not found: {explicit_bin or '(unset)'})"
        try:
            tokens = tuple(shlex.split(arg_template))
        except ValueError as exc:
            return None, f"--ocr-args could not be parsed ({exc})"
        if not any(token in {"{input}", "{output}"} or "{input}" in token or "{output}" in token
                   for token in tokens):
            return None, "--ocr-args must name both {input} and {output}"
        return OcrBackend(OCR_BACKEND_CUSTOM, "custom OCR command", (program,),
                          frozenset({"PGS", "VOBSUB", "DVBSUB"}), arg_template=tokens), ""
    if preferred in {OCR_BACKEND_AUTO, ""}:
        order = OCR_BACKEND_AUTO_ORDER
    elif preferred in OCR_BACKEND_CHOICES:
        order = (preferred,)
    else:
        return None, f"unknown --ocr-backend '{preferred}'"
    tried: list[str] = []
    for key in order:
        backend = build_ocr_backend(key, explicit_bin if preferred != OCR_BACKEND_AUTO else "")
        if backend is not None:
            return backend, ""
        tried.append(key)
    if preferred == OCR_BACKEND_AUTO:
        return None, f"no image-subtitle OCR backend found; {OCR_INSTALL_HINT}"
    return None, f"--ocr-backend {preferred} was not found; {OCR_INSTALL_HINT}"


def find_sibling_srt(source: Path, expected: Path) -> Path | None:
    """Locate the .srt a "writes beside its input" backend produced.

    Subtitle Edit and pgsrip both choose the output name themselves, and the
    rule differs by version (``movie.sup`` -> ``movie.srt`` vs ``movie.srt``
    vs a language-tagged name). Accept the documented name first, then fall
    back to the newest .srt that appeared next to the input, so a renamer
    change costs nothing here.
    """
    if expected.is_file() and expected.stat().st_size > 0:
        return expected
    try:
        siblings = sorted(
            (path for path in source.parent.glob("*.srt")
             if path.is_file() and path.stat().st_size > 0),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    return siblings[0] if siblings else None


def run_ocr(
    backend: OcrBackend,
    source: Path,
    output: Path,
    *,
    track_id: int = 0,
    language: str = "eng",
    timeout: float = DEFAULT_OCR_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """OCR one extracted bitmap subtitle stream into ``output``."""
    command = backend.build_command(source, output, track_id=track_id, language=language)
    rc, out, err = run_external_command(command, timeout=timeout)
    produced: Path | None = (
        find_sibling_srt(source, backend.result_path(source, output))
        if backend.output_mode == "sibling" else backend.result_path(source, output)
    )
    if rc == 0 and produced is not None and produced.is_file() and produced.stat().st_size > 0:
        if produced != output:
            try:
                shutil.move(str(produced), str(output))
            except OSError as exc:
                return False, f"could not collect the OCR output ({exc})"
        return True, ""
    detail = _command_tail(err or out)
    return False, f"{backend.label} could not OCR this track (exit {rc}): {detail}"


# ---------------------------------------------------------------------------
# Durable record of extracted sidecars (read by sync_subtitles.py)
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def extracted_ledger_path() -> Path:
    """Where the extraction record lives: outside the library, every time.

    It sits beside the other ReportsAndLogs artefacts next to this script, so
    every tool in the chain finds the same file regardless of which ``--log``
    path it was given. Override with ``SUBTITLE_EXTRACTED_LEDGER``.
    """
    override = os.environ.get(EXTRACTED_LEDGER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "ReportsAndLogs" / EXTRACTED_LEDGER_NAME


def load_extracted_ledger(path: Path | None = None) -> dict[str, Any]:
    """Read the extraction record; a missing or damaged file is an empty one."""
    target = path or extracted_ledger_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": EXTRACTED_LEDGER_VERSION, "sidecars": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("sidecars"), dict):
        return {"version": EXTRACTED_LEDGER_VERSION, "sidecars": {}}
    return payload


def record_extracted_sidecar(
    video: Path,
    sidecar: Path,
    *,
    track: EmbeddedSubtitleTrack,
    method: str,
    cue_count: int,
    sha256: str,
    ocr_backend: str = "",
    path: Path | None = None,
) -> bool:
    """Remember that ``sidecar`` came from the movie's own embedded track.

    Best effort by design: a read-only installation loses only the sync-tool
    shortcut, never the subtitle itself.
    """
    payload = load_extracted_ledger(path)
    payload["version"] = EXTRACTED_LEDGER_VERSION
    try:
        stat_result = video.stat(follow_symlinks=False)
        movie_size, movie_mtime = int(stat_result.st_size), int(stat_result.st_mtime_ns)
    except OSError:
        movie_size, movie_mtime = 0, 0
    payload["sidecars"][path_norm(sidecar)] = {
        "movie": str(video),
        "sidecar": str(sidecar),
        "movie_size": movie_size,
        "movie_mtime_ns": movie_mtime,
        "sha256": sha256,
        "track_id": track.track_id,
        "codec_id": track.codec_id,
        "track_name": track.name,
        "language": track.language,
        "method": method,
        "ocr_backend": ocr_backend,
        "cue_count": cue_count,
        "extracted_utc": utc_timestamp(),
    }
    try:
        atomic_write_json(path or extracted_ledger_path(), payload)
    except OSError:
        return False
    return True


def find_extracted_record(
    sidecar: Path, sha256: str | None = None, *, path: Path | None = None
) -> dict[str, Any] | None:
    """The extraction record for ``sidecar``, if the file is still the original.

    ``sha256`` is compared when given: a sidecar that was replaced by a
    download or edited by hand is no longer the extracted copy, so it is
    synced like any other subtitle.
    """
    payload = load_extracted_ledger(path)
    record = payload.get("sidecars", {}).get(path_norm(sidecar))
    if not isinstance(record, dict):
        return None
    if sha256 and str(record.get("sha256") or "") != sha256:
        return None
    return record


# ---------------------------------------------------------------------------
# One movie, end to end
# ---------------------------------------------------------------------------
@dataclass
class ExtractOptions:
    """Knobs for one extraction attempt (mirrors the fetcher's CLI flags)."""

    enabled: bool = True
    mkvmerge_bin: str | None = None
    mkvextract_bin: str | None = None
    ocr_backend: str = OCR_BACKEND_AUTO
    ocr_bin: str = ""
    ocr_args: str = ""
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SEC
    ocr_allowed: bool = True
    extract_timeout_seconds: float = DEFAULT_MKVEXTRACT_TIMEOUT_SEC
    min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    text_candidate_limit: int = DEFAULT_EXTRACT_TEXT_CANDIDATE_LIMIT
    image_candidate_limit: int = DEFAULT_EXTRACT_IMAGE_CANDIDATE_LIMIT
    dry_run: bool = False

    def resolved_backend(self) -> tuple[OcrBackend | None, str]:
        return detect_ocr_backend(self.ocr_backend, explicit_bin=self.ocr_bin,
                                  arg_template=self.ocr_args)


@dataclass
class ExtractionOutcome:
    """What one extraction attempt produced, and why if it produced nothing."""

    ok: bool = False
    detail: str = ""
    unavailable_reason: str = ""
    track: EmbeddedSubtitleTrack | None = None
    method: str = ""  # "text" or "ocr"
    ocr_backend: str = ""
    cue_count: int = 0
    dest: Path | None = None
    text: str = ""
    attempted: int = 0
    rejected: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """True when extraction was possible at all for this movie."""
        return not self.unavailable_reason


def extract_embedded_english_srt(
    video: Path,
    dest: Path,
    options: ExtractOptions | None = None,
    *,
    log_file: Path | None = None,
) -> ExtractionOutcome:
    """Create ``dest`` from the movie's own English subtitle track.

    Returns an :class:`ExtractionOutcome`. ``ok`` means ``dest`` holds a
    validated external English SRT (or, in a dry run, that it would) and no
    provider request is needed for this movie. ``unavailable_reason`` means
    extraction could not even be attempted here (no MKVToolNix, no usable
    English track, no OCR backend for an image-only movie) and names the fix.
    """
    opts = options or ExtractOptions()
    if not opts.enabled:
        return ExtractionOutcome(unavailable_reason="embedded extraction is disabled")
    if dest.exists():
        # Another actor (a manual copy, a concurrent run) already covered it.
        return ExtractionOutcome(unavailable_reason=f"{dest.name} already exists")

    mkvmerge_bin = find_mkvtoolnix_binary("mkvmerge", opts.mkvmerge_bin)
    mkvextract_bin = find_mkvtoolnix_binary("mkvextract", opts.mkvextract_bin)
    if not mkvmerge_bin or not mkvextract_bin:
        return ExtractionOutcome(
            unavailable_reason=f"MKVToolNix is not installed; {MKVTOOLNIX_INSTALL_HINT}"
        )

    tracks, probe_error = probe_embedded_subtitle_tracks(
        video, mkvmerge_bin, timeout=opts.extract_timeout_seconds
    )
    if tracks is None:
        return ExtractionOutcome(unavailable_reason=f"could not read the movie's tracks: {probe_error}")
    candidates = classify_embedded_subtitle_tracks(tracks)
    if not candidates:
        has_any_english = any(
            subtitle_track_is_english(track)
            for track in tracks
            if str(track.get("type") or "") == "subtitles"
        )
        reason = (
            "no complete English subtitle track (only forced/signs-only or commentary streams)"
            if has_any_english
            else "the movie has no English subtitle track"
        )
        return ExtractionOutcome(unavailable_reason=reason)

    backend, backend_note = opts.resolved_backend() if opts.ocr_allowed else (None, "")
    if not opts.ocr_allowed and any(item.kind == "image" for item in candidates):
        backend_note = f"the per-run OCR limit was reached; {OCR_INSTALL_HINT}"

    attempts: list[str] = []
    attempted = 0
    text_candidates = [item for item in candidates if item.kind == "text"][: max(0, opts.text_candidate_limit)]
    image_candidates = [item for item in candidates if item.kind == "image"][: max(0, opts.image_candidate_limit)]

    with tempfile.TemporaryDirectory(prefix="subtitle_extract_") as tmpdir:
        tmp = Path(tmpdir)
        for track in text_candidates:
            attempted += 1
            outcome = _extract_one_track(
                video, dest, track, tmp, opts, backend=None, log_file=log_file
            )
            if outcome.ok:
                return ExtractionOutcome(
                    ok=True,
                    detail=(f"would extract the embedded {track.label} -> {dest.name}"
                            if opts.dry_run else outcome.detail),
                    track=outcome.track,
                    method=outcome.method,
                    ocr_backend=outcome.ocr_backend,
                    cue_count=outcome.cue_count,
                    dest=dest,
                    text=outcome.text,
                    attempted=attempted,
                )
            attempts.append(f"{track.label}: {outcome.detail}")
        for track in image_candidates:
            if backend is None:
                attempts.append(f"{track.label}: {backend_note or 'no OCR backend available'}")
                continue
            if not backend.supports_track(track):
                attempts.append(f"{track.label}: {backend.label} cannot OCR {track.codec_id}")
                continue
            if opts.dry_run:
                attempted += 1
                # OCR takes minutes; a preview must not spend them.
                return ExtractionOutcome(
                    ok=True,
                    method="ocr",
                    track=track,
                    ocr_backend=backend.label,
                    dest=dest,
                    attempted=attempted,
                    detail=(f"would OCR the embedded {track.label} with {backend.label} "
                            f"-> {dest.name}"),
                )
            attempted += 1
            outcome = _extract_one_track(
                video, dest, track, tmp, opts, backend=backend, log_file=log_file
            )
            if outcome.ok:
                return ExtractionOutcome(
                    ok=True,
                    detail=outcome.detail,
                    track=outcome.track,
                    method=outcome.method,
                    ocr_backend=outcome.ocr_backend,
                    cue_count=outcome.cue_count,
                    dest=outcome.dest,
                    text=outcome.text,
                    attempted=attempted,
                )
            attempts.append(f"{track.label}: {outcome.detail}")

    detail = "; ".join(attempts) if attempts else "no embedded English track could be converted"
    return ExtractionOutcome(
        ok=False,
        detail=detail,
        attempted=attempted,
        rejected=tuple(attempts),
        unavailable_reason="" if attempted else (backend_note or detail),
    )


def _extract_one_track(
    video: Path,
    dest: Path,
    track: EmbeddedSubtitleTrack,
    tmp: Path,
    opts: ExtractOptions,
    *,
    backend: OcrBackend | None,
    log_file: Path | None = None,
) -> ExtractionOutcome:
    """Extract one track, convert it, validate it, and publish it as ``dest``."""
    mkvextract_bin = find_mkvtoolnix_binary("mkvextract", opts.mkvextract_bin)
    if not mkvextract_bin:
        return ExtractionOutcome(detail="mkvextract is not installed")
    staged = tmp / f"track{track.track_id}{track.extension}"
    rc, _out, err = run_external_command(
        [mkvextract_bin, "tracks", str(video), f"{track.track_id}:{staged}"],
        timeout=opts.extract_timeout_seconds,
    )
    if rc != 0:
        return ExtractionOutcome(detail=f"mkvextract failed (exit {rc}): {_command_tail(err)}")

    method = "text" if track.kind == "text" else "ocr"
    ocr_label = backend.label if backend is not None else ""
    produced_text = ""
    if track.kind == "text":
        try:
            raw = staged.read_bytes()
        except OSError as exc:
            return ExtractionOutcome(detail=f"could not read the extracted track ({exc})")
        try:
            decoded = decode_subtitle_bytes(raw)
        except (ValueError, OSError) as exc:
            return ExtractionOutcome(detail=f"the extracted track is not readable text ({exc})")
        decoded = normalize_srt_newlines(decoded)
        if decoded.startswith("\ufeff"):
            decoded = decoded[1:]
        if track.extension in {".ass", ".ssa"}:
            produced_text = ass_to_srt(decoded)
        elif track.extension == ".vtt":
            produced_text = vtt_to_srt(decoded)
        elif track.extension == ".usf":
            produced_text = usf_to_srt(decoded)
        else:
            produced_text = normalize_extracted_srt(decoded)
        if not produced_text.strip():
            return ExtractionOutcome(detail="the track converted to no subtitle cues")
    else:
        if backend is None:
            return ExtractionOutcome(detail="no OCR backend is available for this image track")
        ocr_output = tmp / f"track{track.track_id}.ocr.srt"
        ok, ocr_error = run_ocr(
            backend,
            staged,
            ocr_output,
            track_id=track.track_id,
            language=track.language or "eng",
            timeout=opts.ocr_timeout_seconds,
        )
        if not ok:
            return ExtractionOutcome(detail=ocr_error)
        try:
            produced_text = normalize_extracted_srt(
                normalize_srt_newlines(ocr_output.read_text(encoding="utf-8", errors="replace"))
            )
        except OSError as exc:
            return ExtractionOutcome(detail=f"could not read the OCR output ({exc})")

    good, reason = extracted_subtitle_quality(produced_text, min_cues=opts.min_cues, method=method)
    if not good:
        return ExtractionOutcome(detail=reason, track=track, method=method)

    cue_count = len(parse_srt_cues(produced_text))
    if opts.dry_run:
        return ExtractionOutcome(
            ok=True,
            detail=f"embedded {track.label} converts to {cue_count} cues",
            track=track,
            method=method,
            ocr_backend=ocr_label,
            cue_count=cue_count,
            dest=dest,
            text=produced_text,
        )

    try:
        # Create-only, exactly like a downloaded sidecar: a subtitle that
        # appears while this movie is being processed is preserved, never
        # silently overwritten.
        atomic_write_text(dest, produced_text, replace=False)
    except FileExistsError:
        return ExtractionOutcome(
            ok=True,
            detail=f"{dest.name} appeared during extraction; the existing sidecar was kept",
            track=track,
            method=method,
            cue_count=cue_count,
            dest=dest,
            text=produced_text,
        )
    except OSError as exc:
        return ExtractionOutcome(detail=f"could not write the extracted sidecar ({exc})", track=track)

    record_extracted_sidecar(
        video,
        dest,
        track=track,
        method=method,
        cue_count=cue_count,
        sha256=sha256_text(produced_text),
        ocr_backend=ocr_label,
    )
    log(
        f"Extracted {cue_count} cue(s) from the embedded {track.label} -> {dest.name}",
        log_file=log_file,
    )
    return ExtractionOutcome(
        ok=True,
        detail=(f"extracted {cue_count} cue(s) from the embedded {track.label}"
                + (f" via {ocr_label}" if method == "ocr" else "")),
        track=track,
        method=method,
        ocr_backend=ocr_label,
        cue_count=cue_count,
        dest=dest,
        text=produced_text,
    )


@dataclass
class QueueConfig:
    library: Path
    log_file: Path | None
    report_file: Path
    api_key: str = ""
    subdl_api_key: str = ""
    username: str = ""
    password: str = ""
    # ``daily_cap`` remains the OpenSubtitles cap for backwards-compatible
    # command-line/config names. SubDL publishes independently metered search
    # and download quotas, both tracked in the same durable ledger.
    daily_cap: int = DEVELOPMENT_ANONYMOUS_DAILY_CAP
    subdl_daily_cap: int = SUBDL_DEFAULT_DAILY_CAP
    min_movie_size_mb: float = MIN_MOVIE_SIZE_MB
    lock_timeout_seconds: float = 60.0
    retry_no_match: bool = False
    identity_fallback: bool = True
    dry_run: bool = False
    limit: int = 0
    auth_mode: str = DEFAULT_AUTH_MODE
    # Appended to preserve positional compatibility with pre-search-cap callers.
    subdl_search_daily_cap: int = SUBDL_DEFAULT_SEARCH_DAILY_CAP
    # Scraping fallback tier (vendored section below). 0 disables the tier;
    # the CLI resolves its default to SCRAPE_DEFAULT_SEARCH_DAILY_CAP per source.
    scrape_daily_cap: int = 0
    # Scraping sources to skip entirely (keys from SCRAPE_PROVIDER_ORDER).
    skip_sources: tuple[str, ...] = ()
    # Exit 0 even when movies finish the run without a validated SRT.
    allow_missing: bool = False
    # Embedded-subtitle extraction: the movie's own English track beats any
    # download (exact for this release, no quota, no timing correction).
    extract_embedded: bool = True
    extract_min_cues: int = DEFAULT_EXTRACT_MIN_CUES
    ocr_backend: str = OCR_BACKEND_AUTO
    ocr_bin: str = ""
    ocr_args: str = ""
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SEC
    # 0 = no per-run cap on OCR jobs (they are local work, not provider quota)
    ocr_limit: int = 0

    def extract_options(self, *, ocr_allowed: bool = True, dry_run: bool = False) -> ExtractOptions:
        return ExtractOptions(
            enabled=self.extract_embedded,
            ocr_backend=self.ocr_backend,
            ocr_bin=self.ocr_bin,
            ocr_args=self.ocr_args,
            ocr_timeout_seconds=self.ocr_timeout_seconds,
            ocr_allowed=ocr_allowed,
            min_cues=self.extract_min_cues,
            dry_run=dry_run,
        )

    @property
    def min_bytes(self) -> int:
        return int(self.min_movie_size_mb * 1024 * 1024)

    def fetcher_config(self) -> Config:
        return Config(
            library=self.library,
            log_file=self.log_file,
            report_file=self.report_file,
            api_key=self.api_key,
            subdl_api_key=self.subdl_api_key,
            username=self.username,
            password=self.password,
            dry_run=self.dry_run,
            min_movie_size_mb=self.min_movie_size_mb,
            lock_timeout_seconds=self.lock_timeout_seconds,
            identity_fallback=self.identity_fallback,
            auth_mode=self.auth_mode,
        )

def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()

def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.partial.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with stage.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, path)
    finally:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass

def new_state(library: Path) -> dict[str, Any]:
    """Create in-memory retry and quota state reconstructed from the run log."""
    return {"library": path_norm(library), "days": {}, "movies": {}, "_dirty_movies": set()}

def _ledger_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Return the changed movie records plus the small daily quota totals."""
    movies: dict[str, dict[str, Any]] = {}
    dirty = state.get("_dirty_movies") or set()
    for key in dirty:
        record = state["movies"].get(key)
        if isinstance(record, dict):
            movies[key] = {name: value for name, value in record.items() if name != "_dirty"}
    return {"library": state["library"], "days": state["days"], "movies": movies}

def load_state(log_path: Path | None, library: Path) -> dict[str, Any]:
    """Recover durable quota/retry state from append-only ledger events in the log.

    Ordinary log lines are ignored. A malformed or partial final event is ignored
    rather than blocking subtitle work; provider download reservations are never
    decremented, which keeps the quota guard conservative after interruption.
    """
    state = new_state(library)
    if log_path is None or not log_path.exists():
        return state
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                marker = line.find(LEDGER_EVENT + " ")
                if marker < 0:
                    continue
                try:
                    payload = json.loads(line[marker + len(LEDGER_EVENT) + 1:].strip())
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or payload.get("library") != state["library"]:
                    continue
                days, movies = payload.get("days"), payload.get("movies")
                if isinstance(days, dict) and isinstance(movies, dict):
                    state["days"].update(days)
                    state["movies"].update(movies)
    except OSError as exc:
        raise RuntimeError(f"could not read subtitle log ledger: {exc}") from exc
    return state

def persist_state(state: dict[str, Any], log_path: Path | None) -> None:
    """Append a compact, fsync-backed ledger checkpoint to the one allowed log."""
    if log_path is None:
        return
    payload = json.dumps(_ledger_payload(state), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] {LEDGER_EVENT} {payload}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        state["_dirty_movies"] = set()
    except OSError as exc:
        raise RuntimeError(f"could not persist subtitle log ledger: {exc}") from exc

def day_ledger(state: dict[str, Any], day: str) -> dict[str, int]:
    """Return a backward-compatible per-provider quota ledger for one UTC day.

    Older logs have only ``download_requests_reserved`` and
    ``successful_downloads``. They are historical OpenSubtitles values, so map
    them to the provider-specific fields on first read and continue writing the
    legacy reservation field for a smooth upgrade.
    """
    ledger = state["days"].setdefault(day, {})
    legacy_open_reserved = ledger.get("opensubtitles_download_requests_reserved",
                                     ledger.get("download_requests_reserved", 0))
    legacy_open_successful = ledger.get("opensubtitles_successful_downloads",
                                       ledger.get("successful_downloads", 0))
    defaults: dict[str, Any] = {
        "opensubtitles_download_requests_reserved": legacy_open_reserved,
        "subdl_search_requests_reserved": 0,
        "subdl_download_requests_reserved": 0,
        "opensubtitles_successful_downloads": legacy_open_successful,
        "subdl_successful_downloads": 0,
        "successful_downloads": ledger.get("successful_downloads", 0),
        "no_match": 0,
        "identity_review": 0,
        "errors": 0,
        "already_have": 0,
        "extracted": 0,
    }
    for field_name, default in defaults.items():
        try:
            ledger[field_name] = max(0, int(ledger.get(field_name, default) or 0))
        except (TypeError, ValueError):
            ledger[field_name] = 0
    # Legacy consumers and existing reports use this field for the
    # OpenSubtitles reservation count. Do not make SubDL downloads consume it.
    ledger["download_requests_reserved"] = ledger["opensubtitles_download_requests_reserved"]
    return ledger

def configured_providers(cfg: QueueConfig) -> tuple[str, ...]:
    providers: list[str] = []
    if cfg.api_key.strip():
        providers.append(PROVIDER_OPENSUBTITLES)
    if cfg.subdl_api_key.strip():
        providers.append(PROVIDER_SUBDL)
    return tuple(providers)

def active_scrape_sources(cfg: QueueConfig) -> tuple[str, ...]:
    """Scraping fallback sources enabled for this run, in failover order.

    A zero ``scrape_daily_cap`` disables the whole tier; ``skip_sources``
    removes individual sites (for example one that is down for everyone).
    """
    if cfg.scrape_daily_cap < 1:
        return ()
    skipped = set(cfg.skip_sources)
    return tuple(key for key in SCRAPE_PROVIDER_ORDER if key not in skipped)

def scrape_sources_enabled(cfg: QueueConfig) -> bool:
    return bool(active_scrape_sources(cfg))

def provider_daily_cap(cfg: QueueConfig, provider: str) -> int:
    if provider == PROVIDER_OPENSUBTITLES:
        return cfg.daily_cap
    if provider == PROVIDER_SUBDL:
        return cfg.subdl_daily_cap
    if is_scrape_provider(provider):
        return cfg.scrape_daily_cap
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_reservation_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_download_requests_reserved"
    if provider == PROVIDER_SUBDL:
        return "subdl_download_requests_reserved"
    if is_scrape_provider(provider):
        # Scraping sources meter one durable reservation per search; the
        # follow-up candidate downloads belong to the same search.
        return f"{provider}_search_requests_reserved"
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_success_field(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "opensubtitles_successful_downloads"
    if provider == PROVIDER_SUBDL:
        return "subdl_successful_downloads"
    if is_scrape_provider(provider):
        return f"{provider}_successful_downloads"
    raise ValueError(f"unknown subtitle provider: {provider}")

def provider_reserved(ledger: dict[str, int], provider: str) -> int:
    return int(ledger.get(provider_reservation_field(provider), 0) or 0)

def provider_has_quota(cfg: QueueConfig, ledger: dict[str, int], provider: str) -> bool:
    return provider_reserved(ledger, provider) < provider_daily_cap(cfg, provider)

def subdl_search_reserved(ledger: dict[str, int]) -> int:
    """Return durable SubDL search requests reserved for the current UTC day."""
    return max(0, int(ledger.get("subdl_search_requests_reserved", 0) or 0))

def subdl_search_has_quota(cfg: QueueConfig, ledger: dict[str, int]) -> bool:
    return subdl_search_reserved(ledger) < cfg.subdl_search_daily_cap

def reserve_subdl_search(ledger: dict[str, int]) -> int:
    """Reserve one SubDL API search before it can leave this process."""
    reserved = subdl_search_reserved(ledger) + 1
    ledger["subdl_search_requests_reserved"] = reserved
    return reserved

def reserve_provider_download(ledger: dict[str, int], provider: str) -> int:
    field_name = provider_reservation_field(provider)
    ledger[field_name] = provider_reserved(ledger, provider) + 1
    if provider == PROVIDER_OPENSUBTITLES:
        ledger["download_requests_reserved"] = ledger[field_name]
    return ledger[field_name]

def record_provider_success(ledger: dict[str, int], provider: str) -> None:
    field_name = provider_success_field(provider)
    ledger[field_name] = max(0, int(ledger.get(field_name, 0) or 0)) + 1
    ledger["successful_downloads"] = max(0, int(ledger.get("successful_downloads", 0) or 0)) + 1

def provider_label(provider: str) -> str:
    if provider == PROVIDER_OPENSUBTITLES:
        return "OpenSubtitles"
    if provider == PROVIDER_SUBDL:
        return "SubDL"
    if is_scrape_provider(provider):
        return scrape_provider_label(provider)
    return provider

def provider_quota_text(cfg: QueueConfig, ledger: dict[str, int]) -> str:
    """Format enabled providers' durable local quota reservations."""
    parts: list[str] = []
    for provider in configured_providers(cfg):
        downloads = f"downloads {provider_reserved(ledger, provider)}/{provider_daily_cap(cfg, provider)}"
        if provider == PROVIDER_SUBDL:
            parts.append(
                f"SubDL {downloads}; searches "
                f"{subdl_search_reserved(ledger)}/{cfg.subdl_search_daily_cap}"
            )
        else:
            parts.append(f"{provider_label(provider)} {downloads}")
    for provider in active_scrape_sources(cfg):
        searches = f"searches {provider_reserved(ledger, provider)}/{cfg.scrape_daily_cap}"
        parts.append(f"{provider_label(provider)} {searches}")
    return " · ".join(parts) or "no source configured"

def provider_configuration_text(cfg: QueueConfig) -> str:
    """Describe active providers without exposing any secret configuration."""
    parts: list[str] = []
    if cfg.api_key.strip():
        parts.append(f"OpenSubtitles {cfg.auth_mode}; cap {cfg.daily_cap}")
    if cfg.subdl_api_key.strip():
        subdl_role = "fallback" if cfg.api_key.strip() else "release-aware/title-year"
        parts.append(
            f"SubDL {subdl_role}; downloads {cfg.subdl_daily_cap}; "
            f"searches {cfg.subdl_search_daily_cap}"
        )
    scrape_keys = active_scrape_sources(cfg)
    if scrape_keys:
        parts.append(
            f"{len(scrape_keys)} scraping sources as fallback "
            f"({scrape_provider_label(scrape_keys[0])}, "
            f"{scrape_provider_label(scrape_keys[1])}, ...); {cfg.scrape_daily_cap} searches/day each"
        )
    return " · ".join(parts) or "no source configured"

def provider_policy_text(cfg: QueueConfig) -> str:
    """Explain the actual matching strength available in this run."""
    if not cfg.identity_fallback:
        if cfg.api_key.strip():
            return "OpenSubtitles exact moviehash matching only"
        return "title/year fallback disabled"
    scrape_suffix = ""
    if active_scrape_sources(cfg):
        scrape_suffix = " · 7-site scraping fallback for any remaining movie"
    if cfg.api_key.strip() and cfg.subdl_api_key.strip():
        return f"OpenSubtitles + SubDL as equal sources (release match scored ≥ 0.80) · most downloads wins{scrape_suffix}"
    if cfg.api_key.strip():
        return f"OpenSubtitles only · exact moviehash then conservative title/year{scrape_suffix}"
    if cfg.subdl_api_key.strip():
        return f"SubDL only (release match scored ≥ 0.80) · no exact moviehash provider{scrape_suffix}"
    if active_scrape_sources(cfg):
        return "no API provider configured · scraping sources only"
    return "no source configured"

def movie_key(video: Path, snapshot: VideoSnapshot) -> str:
    token = "|".join((path_norm(video), str(snapshot.device), str(snapshot.inode), str(snapshot.size), str(snapshot.mtime_ns)))
    return hashlib.sha256(token.encode("utf-8", errors="surrogatepass")).hexdigest()

def state_movie(state: dict[str, Any], key: str, video: Path) -> dict[str, Any]:
    record = state["movies"].setdefault(key, {"path": str(video), "status": "pending", "attempts": 0})
    record["path"] = str(video)
    state.setdefault("_dirty_movies", set()).add(key)
    return record

def set_movie_status(record: dict[str, Any], status: str, detail: str = "", **extras: Any) -> None:
    record["status"] = status
    record["detail"] = detail
    record["updated_utc"] = utc_timestamp()
    record.update(extras)

def inspect_existing_sidecars(video: Path) -> tuple[str, Path | None, str, str]:
    """Classify existing English sidecars without trusting filename alone.

    The cleaner's automatic external-subtitle policy requires the exact
    ``Movie.eng.srt`` name. A validated legacy ``Movie.en.srt`` is renamed in
    place to that canonical name. Any other noncanonical or invalid English
    sidecar is kept for manual review rather than triggering a duplicate
    download request.

    Returns ``(status, path, detail, reason)`` where ``reason`` is one of the
    ``REASON_*`` codes (empty for ``missing``, which means "go and fetch one").
    """
    exact = dest_for(video, Config())
    # dest_for uses only the video name and the fixed .eng.srt suffix, so no
    # configured library path leaks into the decision.
    promoted, promote_reason = promote_legacy_external_english_srt(video)
    if promoted is not None and promote_reason == "" and promoted == exact:
        # A successful rename (or an already-canonical sidecar) is re-validated
        # below through the normal candidate walk.
        pass
    elif promote_reason and "absent" not in promote_reason and "unusable" not in promote_reason:
        # Ambiguous dual-name or occupied-destination cases need a human.
        return (
            "review", exact if exact.exists() else None,
            f"legacy .en.srt could not be promoted to .eng.srt ({promote_reason})",
            REASON_SIDECAR_NAME,
        )
    candidates: list[Path] = []
    try:
        candidates = [
            path for path in sorted(video.parent.iterdir(), key=lambda item: item.name.casefold())
            if is_english_srt_sidecar(path, video.stem)
        ]
    except OSError:
        return "missing", None, "could not inspect sibling subtitles", ""
    if not candidates:
        return "missing", None, "no English SRT sidecar", ""
    covering = covering_english_srt_paths(video)
    for path in covering:
        if path not in candidates and not any(item.name.casefold() == path.name.casefold() for item in candidates):
            continue
        match = next((item for item in candidates if item.name.casefold() == path.name.casefold()), path)
        try:
            file_stat = match.stat(follow_symlinks=False)
            if match.is_symlink() or not match.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = normalize_srt_newlines(decode_subtitle_bytes(match.read_bytes()))
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if valid:
            return "covered", match, f"validated covering sidecar {match.name}", REASON_COVERED
    for path in candidates:
        try:
            file_stat = path.stat(follow_symlinks=False)
            if path.is_symlink() or not path.is_file() or file_stat.st_size <= 0 or file_stat.st_size > MAX_SUBTITLE_BYTES:
                continue
            text = normalize_srt_newlines(decode_subtitle_bytes(path.read_bytes()))
            valid = looks_like_srt(text)
        except (OSError, EOFError, ValueError):
            valid = False
        if valid:
            return (
                "review", path,
                f"'{path.name}' is a valid English SRT but not a covering .eng.srt or .eng.sdh.srt sidecar; "
                "rename or remove it to let this movie be fetched",
                REASON_SIDECAR_NAME,
            )
    broken = candidates[0]
    return (
        "review", broken,
        f"'{broken.name}' exists but is unusable (empty, truncated, or not an SRT); "
        "delete it and re-run to allow a replacement download",
        REASON_SIDECAR_UNUSABLE,
    )

def relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)

def make_scrape_transport() -> ScrapeTransport:
    """Factory for the scraping tier's HTTP transport (tests substitute a fake)."""
    return default_transport()

def build_scrape_chain(cfg: QueueConfig, ledger: dict[str, int],
                       state: dict[str, Any]) -> ScrapeChain | None:
    """Build this run's scraping failover chain, or None when the tier is off.

    The durable ledger's per-source search reservations seed the in-memory
    counters, and the callback persists a reservation before each search
    leaves this process, so an interrupted request still counts against the
    source's UTC cap on the next run.
    """
    keys = active_scrape_sources(cfg)
    if not keys:
        return None

    def reserve_search(key: str) -> None:
        reserved = provider_reserved(ledger, key)
        cap = provider_daily_cap(cfg, key)
        if reserved >= cap:
            raise SourceUnavailable(
                f"UTC daily search cap exhausted ({reserved}/{cap})")
        ledger[provider_reservation_field(key)] = reserved + 1
        persist_state(state, cfg.log_file)

    return ScrapeChain(
        keys=keys,
        transport=make_scrape_transport(),
        search_caps=dict.fromkeys(keys, cfg.scrape_daily_cap),
        reserved={key: provider_reserved(ledger, key) for key in keys},
        reserve_cb=reserve_search,
    )

# =============================================================================
# QUEUE PLANNING  (pure decisions, no I/O)
# =============================================================================
#
# Two questions decide whether a movie costs anything before a single request
# leaves this process: what its own ledger record already proved, and which
# sources still have local quota. Both used to be inline in ``queue_run``'s
# per-movie loop, where they could only be exercised by running the whole
# fetcher against live providers. They are ordinary functions of their inputs,
# so they are here, and tested as a table.


def has_new_provider(
    record: dict[str, Any],
    *,
    active_providers: Sequence[str],
    scrape_keys: Sequence[str],
) -> bool:
    """True if this run can offer a movie a source its record never saw."""
    prior = record.get("providers_checked")
    if not isinstance(prior, list):
        # A pre-SubDL ledger cannot say which sources it queried. Preserve
        # its intentional OpenSubtitles review hold unless the newly added
        # provider is actually enabled, then revisit once for that source.
        # The new scraping tier counts as a new source for such records.
        if scrape_keys and not record.get("scrape_checked"):
            return True
        return PROVIDER_SUBDL in active_providers
    previous = {str(provider) for provider in prior}
    if any(provider not in previous for provider in active_providers):
        return True
    # Legacy records predate the scraping tier: offer it to them once so
    # every previously-held movie is re-checked against all nine sources.
    return bool(scrape_keys and not record.get("scrape_checked"))


@dataclass(frozen=True)
class HistoryPlan:
    """What a movie's own ledger record says before any provider is asked.

    ``action`` is ``"fetch"`` when the movie is still worth spending requests
    on; otherwise it is the terminal verdict for this run, and ``detail`` and
    ``reason`` are the ones the report will show. The two scraping flags are
    facts the tier selection below needs, not decisions.
    """

    action: str = "fetch"
    detail: str = ""
    reason: str = ""
    scrape_tried_today: bool = False
    scrape_retry_today: bool = False

    @property
    def fetch(self) -> bool:
        return self.action == "fetch"


def plan_from_history(
    record: dict[str, Any],
    *,
    today: str,
    retry_no_match: bool,
    identity_fallback: bool,
    scrape_keys: Sequence[str],
    active_providers: Sequence[str],
) -> HistoryPlan:
    """Decide from the durable record alone whether to spend requests today.

    The economy this encodes: a movie the scraping tier exhausted *today* is
    not offered to it twice, one that exhausted it on an earlier day goes
    straight back to scraping (the API tiers are already known to miss for
    it), a deliberate manual-review hold is honoured until something changes,
    and a download reserved today is left for the next UTC day rather than
    reserved twice.
    """
    status = str(record.get("status") or "pending")
    scrape_failed = bool(record.get("scrape_failed"))
    scrape_failed_day = str(record.get("scrape_failed_utc_day") or "")
    tried_today = scrape_failed and scrape_failed_day == today
    retry_today = (
        scrape_failed
        and identity_fallback
        and bool(scrape_keys)
        and scrape_failed_day != today
    )
    flags = {"scrape_tried_today": tried_today, "scrape_retry_today": retry_today}

    if tried_today and status in ("manual_review", "no_match"):
        return HistoryPlan(
            "skip",
            "scraping sources were already exhausted for this movie today; "
            "retrying on the next UTC day",
            REASON_QUOTA, **flags,
        )
    if status == "no_match" and not (retry_no_match or identity_fallback):
        return HistoryPlan(
            "skip", "previous strict moviehash search had no match",
            REASON_NO_MATCH, **flags,
        )
    if (
        status == "manual_review"
        and not retry_no_match
        and not retry_today
        and not has_new_provider(record, active_providers=active_providers,
                                 scrape_keys=scrape_keys)
    ):
        return HistoryPlan(
            "review", "previous identity fallback was intentionally held for review",
            REASON_REVIEW, **flags,
        )
    if status == "reserved" and str(record.get("updated_utc") or "").startswith(today):
        return HistoryPlan(
            "skip",
            "a provider download was already reserved today; waiting for next UTC day",
            REASON_QUOTA, **flags,
        )
    return HistoryPlan(**flags)


@dataclass(frozen=True)
class SourcePlan:
    """Which sources may be asked about one movie, and which are merely funded.

    ``*_available`` means "configured and still inside its local daily cap";
    ``*_tier`` additionally means "worth asking for *this* movie". They differ
    on a scraping retry, where the API tiers are known to miss and are not
    re-queried, but are still counted as funded so the quota-exhausted break
    below does not mistake a retry for an empty wallet.
    """

    open_available: bool = False
    subdl_available: bool = False
    scrape_available: bool = False
    api_tiers_allowed: bool = True

    @property
    def open_tier(self) -> bool:
        return self.open_available and self.api_tiers_allowed

    @property
    def subdl_tier(self) -> bool:
        return self.subdl_available and self.api_tiers_allowed

    @property
    def exhausted(self) -> bool:
        """No configured source with an enabled mode has local capacity left."""
        return not (self.open_available or self.subdl_available or self.scrape_available)


def plan_sources(
    cfg: QueueConfig,
    ledger: dict[str, int],
    history: HistoryPlan,
    *,
    has_open: bool,
    has_subdl: bool,
    has_scrape_chain: bool,
    scrape_keys: Sequence[str],
) -> SourcePlan:
    """Decide which tiers this movie may be offered, spending nothing."""
    return SourcePlan(
        open_available=has_open and provider_has_quota(cfg, ledger, PROVIDER_OPENSUBTITLES),
        # SubDL has no byte-exact release hash, so --no-identity-fallback also
        # intentionally disables its release-aware/title-year lookup.
        subdl_available=(
            has_subdl
            and cfg.identity_fallback
            and provider_has_quota(cfg, ledger, PROVIDER_SUBDL)
            and subdl_search_has_quota(cfg, ledger)
        ),
        scrape_available=(
            has_scrape_chain
            and cfg.identity_fallback
            and any(provider_has_quota(cfg, ledger, key) for key in scrape_keys)
        ),
        # On a scraping retry the API tiers are already known to miss for this
        # movie, so they are not asked again; the scraping tier is.
        api_tiers_allowed=not (history.scrape_retry_today and not history.scrape_tried_today),
    )


def queue_run(cfg: QueueConfig) -> tuple[list[JobResult], dict[str, Any]]:
    """Process one daily batch with independent provider quotas.

    OpenSubtitles and SubDL are equal sources. Each movie is offered both
    providers' release-identifying routes - OpenSubtitles exact moviehash and
    SubDL score-gated filename match (score >= 0.80) - and the qualifying
    release with the most downloads wins, regardless of provider. When
    neither release route produces a pick, both providers' strict title/year
    routes are pooled the same way; SubDL's generic title route is used only
    when its release lookup returned no candidates at all, so a low-score
    release match never weakens to a generic one.
    Automatic selection only accepts a release that names the movie and its
    release year and carries a Blu-ray keyword, and ranks those by download
    count.
    """
    state = load_state(cfg.log_file, cfg.library)
    today = utc_day()
    ledger = day_ledger(state, today)
    fetcher_cfg = cfg.fetcher_config()

    def reserve_subdl_search_request() -> None:
        """Persist a search reservation before every SubDL API attempt."""
        if not subdl_search_has_quota(cfg, ledger):
            raise SubdlSearchQuotaExhausted(
                "SubDL daily search cap exhausted "
                f"({subdl_search_reserved(ledger)}/{cfg.subdl_search_daily_cap} requests reserved)"
            )
        reserve_subdl_search(ledger)
        # A network timeout can still count remotely, so never wait until the
        # response to make this local reservation durable.
        persist_state(state, cfg.log_file)

    results: list[JobResult] = []
    open_client = OpenSubtitlesClient(fetcher_cfg) if cfg.api_key.strip() else None
    subdl_client = (
        SubdlClient(cfg.subdl_api_key, before_search_request=reserve_subdl_search_request)
        if cfg.subdl_api_key.strip() else None
    )
    active_providers = configured_providers(cfg)
    scrape_keys = active_scrape_sources(cfg)
    # Dry runs spend no scraping requests: searches would count against the
    # real UTC caps, so the tier is skipped entirely (report says so).
    scrape_chain = build_scrape_chain(cfg, ledger, state) if not cfg.dry_run else None
    deferred_remaining = 0
    deferred_videos: list[Path] = []
    # Embedded extraction: OCR jobs are minutes of local CPU, so the run can
    # cap them (--ocr-limit) independently of every provider quota.
    ocr_jobs = 0
    extract_notes: set[str] = set()

    videos = discover_videos(cfg.library, cfg.min_bytes)
    if cfg.limit > 0:
        videos = videos[:cfg.limit]
    total = len(videos)
    log(
        f"Found {total} eligible movies. UTC local reservations: {provider_quota_text(cfg, ledger)}.",
        log_file=cfg.log_file,
    )

    def emit(index: int, status: str, video: Path, detail: str) -> None:
        log(
            f"[{index:03d}/{total:03d}] {status:<8} "
            f"{relative_text(video, cfg.library)} — {detail}",
            log_file=cfg.log_file,
        )

    for index, video in enumerate(videos, start=1):
        layout_issue = canonical_movie_layout_issue(video, cfg.library)
        if layout_issue:
            result = JobResult(video, "skip", layout_issue, reason=REASON_LAYOUT)
            results.append(result)
            emit(index, "SKIP", video, layout_issue)
            continue
        sidecar_status, existing, sidecar_detail, sidecar_reason = inspect_existing_sidecars(video)
        if sidecar_status == "covered" and existing is not None:
            ledger["already_have"] += 1
            result = JobResult(video, "have", sidecar_detail, existing, reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, sidecar_detail)
            continue
        if sidecar_status == "review":
            result = JobResult(video, "review", sidecar_detail, existing, reason=sidecar_reason)
            results.append(result)
            emit(index, "REVIEW", video, sidecar_detail)
            continue

        try:
            snapshot = video_snapshot(video)
            key = movie_key(video, snapshot)
        except OSError as exc:
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
            results.append(result)
            emit(index, "ERROR", video, str(exc))
            continue
        record = state_movie(state, key, video)

        # Embedded-subtitle extraction. A movie's own English track is exact
        # for this release, costs no provider request, and needs no timing
        # correction, so it beats any download - and it is attempted before
        # every ledger short-circuit below, because a movie a provider held
        # for review can still be covered by its own track for free.
        if cfg.extract_embedded:
            extract_dest = dest_for(video, fetcher_cfg)
            outcome = extract_embedded_english_srt(
                video,
                extract_dest,
                cfg.extract_options(
                    ocr_allowed=cfg.ocr_limit <= 0 or ocr_jobs < cfg.ocr_limit,
                    dry_run=cfg.dry_run,
                ),
                log_file=cfg.log_file,
            )
            if outcome.ok:
                if outcome.method == "ocr":
                    ocr_jobs += 1
                if cfg.dry_run:
                    result = JobResult(video, "dry-run", outcome.detail, extract_dest,
                                       reason=REASON_DRY_RUN)
                    results.append(result)
                    emit(index, "DRYRUN", video, outcome.detail)
                    continue
                set_movie_status(record, "extracted", outcome.detail, sidecar=str(extract_dest))
                persist_state(state, cfg.log_file)
                ledger["extracted"] += 1
                result = JobResult(video, "extracted", outcome.detail, extract_dest,
                                   reason=REASON_EXTRACTED)
                results.append(result)
                emit(index, "EXTRACT", video, outcome.detail)
                continue
            if outcome.unavailable_reason and outcome.unavailable_reason not in extract_notes:
                # A missing toolchain is worth one line per run; "this movie
                # has no English track" is the common case and stays silent.
                extract_notes.add(outcome.unavailable_reason)
                if "not installed" in outcome.unavailable_reason or "OCR" in outcome.unavailable_reason:
                    log(outcome.unavailable_reason, level="WARNING", log_file=cfg.log_file)

        # Scraping retry economy and the ledger holds: see plan_from_history.
        history = plan_from_history(
            record, today=today, retry_no_match=cfg.retry_no_match,
            identity_fallback=cfg.identity_fallback, scrape_keys=scrape_keys,
            active_providers=active_providers,
        )
        if not history.fetch:
            result = JobResult(video, history.action, history.detail, reason=history.reason)
            results.append(result)
            emit(index, "REVIEW" if history.action == "review" else "SKIP", video, history.detail)
            continue

        sources = plan_sources(
            cfg, ledger, history,
            has_open=open_client is not None,
            has_subdl=subdl_client is not None,
            has_scrape_chain=scrape_chain is not None,
            scrape_keys=scrape_keys,
        )
        open_available = sources.open_available
        subdl_available = sources.subdl_available
        api_tiers_allowed = sources.api_tiers_allowed
        open_tier_available = sources.open_tier
        subdl_tier_available = sources.subdl_tier
        if sources.exhausted:
            deferred_remaining = total - index + 1
            deferred_videos = list(videos[index - 1:])
            log(
                "QUOTA REACHED: no configured source with an enabled matching mode has "
                f"remaining local capacity ({provider_quota_text(cfg, ledger)}). "
                f"{deferred_remaining} movie(s) remain for the next UTC day.",
                level="WARNING", log_file=cfg.log_file,
            )
            break

        digest = ""
        pick: Candidate | None = None
        selected_provider = ""
        selection_method = ""
        selection_reason = "no usable Blu-ray English moviehash-matched human SRT naming the movie and its release year"
        providers_checked: list[str] = []
        subdl_downloads: dict[str, SubdlDownload] = {}
        # Tier 3 result: the validated bytes the chain already downloaded for
        # the winning scrape candidate (None until the chain produces one).
        scrape_download: bytes | None = None
        # Distinguish an exhausted SubDL cap before a lookup from a filename
        # lookup that actually returned a low-score or ambiguous candidate.
        # The former should be retried on the next quota day; the latter is a
        # deliberate manual-review decision.
        subdl_lookup_attempted = False
        # The selection policy matches the release name against the movie
        # title, so derive the canonical identity once and reuse it in both
        # the hash branch and the title/year fallback below.
        identity = movie_identity_from_video(video)

        open_lookup_error = ""
        pool_reasons: list[str] = []
        os_tier1: Candidate | None = None
        os_tier1_reason = (
            "no usable Blu-ray English moviehash-matched human SRT "
            "naming the movie and its release year"
        )
        subdl_tier1: Candidate | None = None
        subdl_tier1_reason = ""
        subdl_release_candidates: list[Candidate] = []

        # Tier 1 - release-identifying routes, queried as equal sources:
        # OpenSubtitles' exact-moviehash match and SubDL's score-gated
        # filename match. Whichever qualifying release has the most downloads
        # wins, regardless of provider.
        if open_tier_available and open_client is not None:
            providers_checked.append(PROVIDER_OPENSUBTITLES)
            emit(index, "SEARCH", video, "calculating moviehash and checking OpenSubtitles")
            try:
                digest = moviehash(video)
                if not video_snapshot_matches(video, snapshot):
                    raise RuntimeError("movie changed while calculating moviehash")
            except (RuntimeError, ValueError) as exc:
                # ValueError matters as much as RuntimeError here: moviehash()
                # raises it for a file below MIN_HASH_SIZE, and the size gate ran
                # at scan time, so a file truncated in between must not abort the
                # rest of the daily queue. A local movie problem cannot safely
                # fall through to title/year matching on another provider.
                set_movie_status(
                    record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["errors"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                results.append(result)
                emit(index, "ERROR", video, str(exc))
                continue
            try:
                candidates = open_client.search(movie_hash=digest, query=video.stem)
                os_tier1 = pick_candidate(candidates, fetcher_cfg, identity=identity)
            except (RuntimeError, ValueError) as exc:
                if not subdl_tier_available:
                    set_movie_status(
                        record, "error", str(exc), attempts=int(record.get("attempts", 0) or 0) + 1,
                        providers_checked=providers_checked,
                    )
                    ledger["errors"] += 1
                    persist_state(state, cfg.log_file)
                    result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                    results.append(result)
                    emit(index, "ERROR", video, str(exc))
                    continue
                open_lookup_error = f"OpenSubtitles moviehash lookup failed: {exc}"
                pool_reasons.append(open_lookup_error)
                emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
            if os_tier1 is not None:
                os_tier1_reason = (
                    "moviehash match; Blu-ray release naming the movie and its release year; "
                    "highest download count"
                )

        if os_tier1 is not None and (not cfg.identity_fallback or identity is None):
            # A strict hash match stands alone when nothing else may be
            # asked: the title/year fallback is disabled, or the filename
            # carries no canonical Title (Year) pair to search by.
            pick, selected_provider, selection_method, selection_reason = (
                os_tier1, PROVIDER_OPENSUBTITLES, "hash", os_tier1_reason,
            )

        if pick is None:
            if not cfg.identity_fallback:
                detail = (
                    "no usable Blu-ray English moviehash-matched human SRT naming the movie and its release year"
                    if open_available else
                    "no exact-moviehash provider is available and title/year fallback is disabled"
                )
                set_movie_status(
                    record, "no_match", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["no_match"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "skip", detail, reason=REASON_NO_MATCH)
                results.append(result)
                emit(index, "NO MATCH", video, detail)
                continue

            if identity is None:
                detail = (
                    "no strict hash match and filename is not canonical Title (Year)"
                    if open_available else
                    "SubDL title/year fallback requires a canonical Title (Year) filename"
                )
                set_movie_status(
                    record, "manual_review", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                )
                ledger["identity_review"] += 1
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail, reason=REASON_REVIEW)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue
            assert identity is not None

            if subdl_tier_available and subdl_client is not None:
                providers_checked.append(PROVIDER_SUBDL)
                emit(
                    index,
                    "SEARCH",
                    video,
                    f"checking SubDL release-aware filename match: {video.name}",
                )
                try:
                    subdl_lookup_attempted = True
                    subdl_release_candidates, subdl_downloads = subdl_client.search_filename(
                        video.name, identity,
                    )
                    subdl_tier1, subdl_tier1_reason = pick_subdl_identity_candidate(
                        subdl_release_candidates, identity, require_release_match_score=True,
                    )
                except SubdlSearchQuotaExhausted as exc:
                    # The callback fires before an outbound request. This movie
                    # was not fully evaluated, so defer it rather than turning a
                    # temporary provider limit into a manual-review decision.
                    detail = str(exc)
                    result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                    results.append(result)
                    emit(index, "SKIP", video, detail)
                    continue
                except (RuntimeError, ValueError) as exc:
                    detail = f"SubDL lookup failed: {exc}"
                    set_movie_status(
                        record, "error", detail, moviehash=digest,
                        attempts=int(record.get("attempts", 0) or 0) + 1,
                        providers_checked=providers_checked,
                    )
                    ledger["errors"] += 1
                    persist_state(state, cfg.log_file)
                    result = JobResult(video, "error", detail, reason=REASON_ERROR)
                    results.append(result)
                    emit(index, "ERROR", video, detail)
                    continue
            elif subdl_client is not None:
                if not api_tiers_allowed:
                    pool_reasons.append("SubDL: not re-queried on a scraping retry (known API miss)")
                elif not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    pool_reasons.append("SubDL: daily download cap exhausted")
                elif not subdl_search_has_quota(cfg, ledger):
                    pool_reasons.append("SubDL: daily search cap exhausted")
                else:
                    pool_reasons.append("SubDL: identity fallback disabled")

            tier1_entries: list[tuple[Candidate, str, str, str]] = []
            if os_tier1 is not None:
                tier1_entries.append((os_tier1, PROVIDER_OPENSUBTITLES, "hash", os_tier1_reason))
            if subdl_tier1 is not None:
                tier1_entries.append((subdl_tier1, PROVIDER_SUBDL, "subdl-release", subdl_tier1_reason))
            elif subdl_client is not None and subdl_lookup_attempted:
                if not subdl_release_candidates:
                    # The title route below will explain this provider's miss.
                    pass
                else:
                    # A low-score or ambiguous release match deliberately does
                    # not weaken to SubDL's generic title route, so this is the
                    # final SubDL verdict for the review detail.
                    pool_reasons.append(f"SubDL: {subdl_tier1_reason}")

            pick, selected_provider, selection_method, selection_reason = pick_pooled_candidates(
                tier1_entries, identity,
            )
            if pick is None and selection_reason:
                pool_reasons.append(selection_reason)

            if pick is None:
                # Tier 2 - strict title/year routes, also queried as equal
                # sources: OpenSubtitles title/year and SubDL's documented
                # title search.
                os_tier2: Candidate | None = None
                os_tier2_reason = ""
                subdl_tier2: Candidate | None = None
                subdl_tier2_reason = ""
                if open_tier_available and open_client is not None and not open_lookup_error:
                    emit(
                        index, "FALLBACK", video,
                        f"checking OpenSubtitles title/year: {identity.title} ({identity.year})",
                    )
                    try:
                        identity_candidates = open_client.search_identity(identity)
                        os_tier2, os_tier2_reason = pick_identity_candidate(identity_candidates, identity)
                    except (RuntimeError, ValueError) as exc:
                        if not subdl_tier_available:
                            set_movie_status(
                                record, "error", str(exc),
                                attempts=int(record.get("attempts", 0) or 0) + 1,
                                providers_checked=providers_checked,
                            )
                            ledger["errors"] += 1
                            persist_state(state, cfg.log_file)
                            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
                            results.append(result)
                            emit(index, "ERROR", video, str(exc))
                            continue
                        open_lookup_error = f"OpenSubtitles title/year lookup failed: {exc}"
                        pool_reasons.append(open_lookup_error)
                        emit(index, "FALLBACK", video, f"{open_lookup_error}; continuing to SubDL")
                elif open_client is not None and not open_lookup_error:
                    if not api_tiers_allowed:
                        pool_reasons.append("OpenSubtitles: not re-queried on a scraping retry (known API miss)")
                    else:
                        pool_reasons.append("OpenSubtitles: daily download cap exhausted")

                # The local canonical filename deliberately omits scene tags.
                # If SubDL's release lookup resolved nothing at all, use its
                # documented title route once, still requiring exact provider
                # title/year metadata. A low-score release match never weakens
                # to the generic route.
                subdl_title_allowed = subdl_lookup_attempted and not subdl_release_candidates
                if (
                    subdl_tier_available and subdl_client is not None
                    and subdl_title_allowed
                ):
                    emit(
                        index, "FALLBACK", video,
                        f"checking SubDL strict title/year: {identity.title} ({identity.year})",
                    )
                    try:
                        subdl_title_candidates, subdl_downloads = subdl_client.search_identity(identity)
                        subdl_tier2, subdl_tier2_reason = pick_subdl_identity_candidate(
                            subdl_title_candidates, identity,
                        )
                    except SubdlSearchQuotaExhausted as exc:
                        # The callback fires before an outbound request. This
                        # movie was not fully evaluated, so defer it rather
                        # than turning a temporary provider limit into a
                        # manual-review decision.
                        detail = str(exc)
                        result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                        results.append(result)
                        emit(index, "SKIP", video, detail)
                        continue
                    except (RuntimeError, ValueError) as exc:
                        detail = f"SubDL lookup failed: {exc}"
                        set_movie_status(
                            record, "error", detail, moviehash=digest,
                            attempts=int(record.get("attempts", 0) or 0) + 1,
                            providers_checked=providers_checked,
                        )
                        ledger["errors"] += 1
                        persist_state(state, cfg.log_file)
                        result = JobResult(video, "error", detail, reason=REASON_ERROR)
                        results.append(result)
                        emit(index, "ERROR", video, detail)
                        continue

                tier2_entries: list[tuple[Candidate, str, str, str]] = []
                if os_tier2 is not None:
                    tier2_entries.append((os_tier2, PROVIDER_OPENSUBTITLES, "identity", os_tier2_reason))
                elif open_client is not None and not open_lookup_error:
                    pool_reasons.append(
                        "OpenSubtitles: daily download cap exhausted"
                        if not open_available else
                        f"OpenSubtitles: {os_tier2_reason}"
                    )
                if subdl_tier2 is not None:
                    tier2_entries.append((subdl_tier2, PROVIDER_SUBDL, "subdl-identity", subdl_tier2_reason))
                elif subdl_client is not None and subdl_title_allowed:
                    pool_reasons.append(f"SubDL: {subdl_tier2_reason}")

                pick, selected_provider, selection_method, selection_reason = pick_pooled_candidates(
                    tier2_entries, identity,
                )
                if pick is None and selection_reason:
                    pool_reasons.append(selection_reason)

            if (pick is None and subdl_client is not None and not subdl_lookup_attempted
                    and not subdl_available and api_tiers_allowed):
                if not provider_has_quota(cfg, ledger, PROVIDER_SUBDL):
                    detail = "SubDL daily download cap exhausted before lookup; deferred to the next UTC day"
                else:
                    detail = "SubDL daily search cap exhausted before lookup; deferred to the next UTC day"
                result = JobResult(video, "skip", detail, reason=REASON_QUOTA)
                results.append(result)
                emit(index, "SKIP", video, detail)
                continue

            if pick is None and scrape_chain is not None:
                # Tier 3 - the scraping fallback sources (no API keys needed):
                # Subf2me, Podnapisi, Addic7ed, SubSource, Subsunacs, YIFY
                # Subtitles, Subs.Sab.BZ. Each source is searched once per
                # movie in failover order; a candidate wins only when it
                # names the movie, matches its release year, and decodes to
                # a valid SRT. The chain's breaker disables a source for the
                # rest of the run after repeated hard or parse failures.
                emit(
                    index, "SEARCH", video,
                    "checking scraping sources: "
                    + " · ".join(scrape_provider_label(key) for key in scrape_keys),
                )
                try:
                    scrape_cand, scrape_key, scrape_raw = run_scrape_chain(
                        SourceIdentity(
                            identity.title, identity.year, identity.normalized_title),
                        keys=tuple(scrape_keys),
                        chain=scrape_chain,
                        on_reason=lambda key, why, _reasons=pool_reasons: _reasons.append(
                            f"{scrape_provider_label(key)}: {why}"),
                    )
                except Exception as exc:  # a scraping-tier bug must not kill the run
                    pool_reasons.append(f"scraping sources failed: {type(exc).__name__}: {exc}")
                    scrape_cand, scrape_key, scrape_raw = None, "", None
                if scrape_cand is not None and scrape_raw is not None:
                    pick = Candidate(
                        file_id=f"scrape:{scrape_key}:{scrape_cand.file_id}",
                        release=scrape_cand.release or "",
                        moviehash_match=False,
                        downloads=int(scrape_cand.downloads or 0),
                        votes=0,
                        rating=float(scrape_cand.rating or 0.0),
                        trusted=False,
                        hearing_impaired=bool(scrape_cand.hearing_impaired),
                        machine_translated=False,
                        ai_translated=False,
                        foreign_parts_only=False,
                        language="en",
                        feature_title=scrape_cand.feature_title or identity.title,
                        feature_year=scrape_cand.feature_year or identity.year,
                    )
                    selected_provider = scrape_key
                    selection_method = "scrape"
                    selection_reason = (
                        f"scraping source {scrape_provider_label(scrape_key)} "
                        f"(candidate validated as an English SRT naming the movie)"
                    )
                    scrape_download = scrape_raw

            if pick is None:
                reason = "; ".join(pool_reasons) or selection_reason
                detail = f"identity fallback held for review: {reason}"
                extras: dict[str, Any] = {}
                if scrape_chain is not None:
                    # Every scraping source was offered to this movie and
                    # produced nothing usable today. It is retried on the
                    # next UTC day (see the retry gates at the top of the
                    # loop), and the scrape keys are deliberately not written
                    # into providers_checked so has_new_provider does not
                    # mistake a finished scraping attempt for a new provider.
                    extras = {
                        "scrape_checked": True,
                        "scrape_failed": True,
                        "scrape_failed_utc_day": today,
                    }
                set_movie_status(
                    record, "manual_review", detail, moviehash=digest,
                    attempts=int(record.get("attempts", 0) or 0) + 1,
                    providers_checked=providers_checked,
                    **extras,
                )
                ledger["identity_review"] += 1
                # The scraping chain's reservation callbacks already persisted
                # (and cleared) the dirty set, so re-mark this record: its
                # scrape_failed flags must survive for the next-UTC-day gate.
                state.setdefault("_dirty_movies", set()).add(key)
                persist_state(state, cfg.log_file)
                result = JobResult(video, "review", detail, reason=REASON_REVIEW)
                results.append(result)
                emit(index, "REVIEW", video, detail)
                continue

        dest = dest_for(video, fetcher_cfg)
        note = (
            f"provider={provider_label(selected_provider)}; method={selection_method}; id={pick.file_id}; "
            f"trusted={'yes' if pick.trusted else 'no'}; rating={pick.rating:g}/{pick.votes}; "
            f"{selection_reason}; {pick.release or 'unnamed release'}"
        )
        if cfg.dry_run:
            result = JobResult(video, "dry-run", note, dest, reason=REASON_DRY_RUN)
            results.append(result)
            emit(index, "WOULD GET", video, note)
            continue

        if is_scrape_provider(selected_provider):
            # The scraping chain already reserved and persisted the search
            # before fetching, and the bytes are on hand: no second
            # reservation, no "reserved" state (an interrupted write should
            # be retried immediately, not parked until the next UTC day).
            print(
                f"[{index:03d}/{total:03d}] SAVING {relative_text(video, cfg.library)} — "
                f"{provider_label(selected_provider)} (validated scraping candidate)",
                flush=True,
            )
        else:
            # Persist a provider-specific reservation before the download: an
            # interrupted request may still count against that provider's quota.
            reservation = reserve_provider_download(ledger, selected_provider)
            set_movie_status(
                record, "reserved", note, moviehash=digest, selection_method=selection_method,
                selected_provider=selected_provider, selected_file_id=str(pick.file_id),
                attempts=int(record.get("attempts", 0) or 0) + 1,
                providers_checked=providers_checked,
            )
            persist_state(state, cfg.log_file)
            print(
                f"[{index:03d}/{total:03d}] DOWNLOAD {relative_text(video, cfg.library)} — "
                f"{provider_label(selected_provider)} request "
                f"{reservation}/{provider_daily_cap(cfg, selected_provider)}",
                flush=True,
            )
        try:
            if selected_provider == PROVIDER_SUBDL:
                if subdl_client is None:
                    raise RuntimeError("SubDL client is unavailable")
                download = subdl_downloads.get(str(pick.file_id))
                if download is None:
                    raise RuntimeError("SubDL candidate download reference is missing")
                subdl_client.download_srt(download, dest, video=video, expected_video=snapshot)
            elif is_scrape_provider(selected_provider):
                # The chain already downloaded and validated these bytes
                # (valid_srt_bytes); the shared sidecar contract is applied
                # here exactly as for the API providers.
                if scrape_download is None:
                    raise RuntimeError("scraping candidate download reference is missing")
                if len(scrape_download) > MAX_SUBTITLE_BYTES:
                    raise RuntimeError(f"subtitle exceeds {MAX_SUBTITLE_BYTES} byte safety limit")
                text = decode_subtitle_bytes(scrape_download)
                text = normalize_srt_newlines(text)
                if not looks_like_srt(text):
                    raise RuntimeError("scraping payload is not a valid SRT subtitle")
                if not video_snapshot_matches(video, snapshot):
                    raise RuntimeError("movie changed during subtitle lookup; scraped SRT was not activated")
                try:
                    atomic_write_text(dest, text, replace=False)
                except FileExistsError as exc:
                    raise ConcurrentSidecarError(
                        "English SRT appeared during download; preserved the existing sidecar") from exc
            else:
                if open_client is None or not isinstance(pick.file_id, int):
                    raise RuntimeError("OpenSubtitles candidate has an invalid file identifier")
                open_client.download_srt(pick.file_id, dest, video=video, expected_video=snapshot)
        except ConcurrentSidecarError as exc:
            set_movie_status(record, "have", str(exc), sidecar=str(dest))
            ledger["already_have"] += 1
            result = JobResult(video, "have", str(exc), dest, reason=REASON_COVERED)
            results.append(result)
            emit(index, "HAVE", video, str(exc))
        except (RuntimeError, ValueError) as exc:
            # decode_subtitle_bytes() raises ValueError for a subtitle that
            # decompresses past MAX_SUBTITLE_BYTES, so a single hostile or
            # corrupt provider payload must not abort the rest of the library.
            set_movie_status(record, "error", str(exc))
            ledger["errors"] += 1
            result = JobResult(video, "error", str(exc), reason=REASON_ERROR)
            results.append(result)
            emit(index, "ERROR", video, str(exc))
        else:
            set_movie_status(record, "downloaded", note, sidecar=str(dest))
            record_provider_success(ledger, selected_provider)
            result = JobResult(video, "download", note, dest, reason=REASON_DOWNLOADED)
            results.append(result)
            emit(index, "SAVED", video, dest.name)
        persist_state(state, cfg.log_file)

    available_after_run = [
        provider for provider in active_providers
        if provider_has_quota(cfg, ledger, provider)
        and (provider != PROVIDER_SUBDL or cfg.identity_fallback)
        and (provider != PROVIDER_SUBDL or subdl_search_has_quota(cfg, ledger))
    ]
    available_after_run += [
        key for key in scrape_keys
        if provider_has_quota(cfg, ledger, key)
    ]
    covered_count = sum(
        1 for result in results
        if result.reason in (REASON_COVERED, REASON_DOWNLOADED, REASON_EXTRACTED)
        or (cfg.dry_run and result.reason == REASON_DRY_RUN)
    )
    summary = {
        "utc_day": today,
        # Legacy summary fields remain OpenSubtitles values for downstream
        # consumers that predate the second provider.
        "daily_cap": cfg.daily_cap,
        "download_requests_reserved": provider_reserved(ledger, PROVIDER_OPENSUBTITLES),
        "successful_downloads": ledger["successful_downloads"],
        "opensubtitles_daily_cap": cfg.daily_cap,
        "opensubtitles_download_requests_reserved": provider_reserved(ledger, PROVIDER_OPENSUBTITLES),
        "opensubtitles_successful_downloads": ledger["opensubtitles_successful_downloads"],
        "subdl_search_daily_cap": cfg.subdl_search_daily_cap,
        "subdl_search_requests_reserved": subdl_search_reserved(ledger),
        "subdl_daily_cap": cfg.subdl_daily_cap,
        "subdl_download_requests_reserved": provider_reserved(ledger, PROVIDER_SUBDL),
        "subdl_successful_downloads": ledger["subdl_successful_downloads"],
        "scrape_search_daily_cap": cfg.scrape_daily_cap,
        "scrape_sources_enabled": list(scrape_keys),
        "scrape_sources_status": scrape_chain.status() if scrape_chain is not None else {},
        "scrape_search_requests_reserved": {
            key: provider_reserved(ledger, key) for key in scrape_keys
        },
        "scrape_successful_downloads": {
            key: int(ledger.get(provider_success_field(key), 0) or 0) for key in scrape_keys
        },
        "quota_reached": not available_after_run,
        "deferred_remaining": deferred_remaining,
        "extracted_from_embedded": int(ledger.get("extracted", 0) or 0),
        "ledger_log": str(cfg.log_file),
        "movies_discovered": total,
        # Coverage is the product promise: every movie ends the run with a
        # validated English SRT (dry runs count their candidates as would-be
        # covered). Anything else - review holds, misses, errors, deferred -
        # is uncovered and names its movies in the report.
        "coverage_covered": covered_count,
        "coverage_total": total,
        # Which movies, not just how many: the report has to be able to name
        # what was never reached when all usable provider caps cut the batch short.
        "deferred_videos": deferred_videos,
    }
    return results, summary

@dataclass(frozen=True)
class NeedsBucket:
    """One reason a movie still has no usable external English SRT.

    ``order`` is implicit in the tuple order of :data:`NEEDS_SUBTITLE_BUCKETS`:
    the cheapest, most certain fix comes first, so the top of the report is
    always the thing to do next.
    """

    reason: str
    title: str
    quick: str
    fix: str

NEEDS_SUBTITLE_BUCKETS: tuple[NeedsBucket, ...] = (
    NeedsBucket(
        REASON_SIDECAR_UNUSABLE,
        "SIDECAR EXISTS BUT IS UNUSABLE",
        "delete the file, then re-run",
        "Delete the named file, then re-run this tool. Nothing replaces a sidecar it "
        "believes is already present, so a corrupt file blocks a good download forever.",
    ),
    NeedsBucket(
        REASON_SIDECAR_NAME,
        "SIDECAR NAME IS NOT CANONICAL",
        f"rename it to <movie>{EXTERNAL_SRT_SUFFIX}, or delete it",
        f"Rename the file to \"<movie>{EXTERNAL_SRT_SUFFIX}\" (or delete it) and re-run. "
        "Jellyfin and Plex only direct play that exact name, and this tool will not "
        "download a second copy over a subtitle that is already there.",
    ),
    NeedsBucket(
        REASON_LAYOUT,
        "LIBRARY LAYOUT MUST BE FIXED FIRST",
        "run movie_standardizer.py on that folder",
        "Each movie must be one MKV in a folder of the same name: "
        "\"Title (Year)/Title (Year).mkv\". Run movie_standardizer.py, or fix the "
        "folder by hand, and this movie will be picked up on the next run.",
    ),
    NeedsBucket(
        REASON_REVIEW,
        "HELD FOR MANUAL REVIEW",
        "inspect the candidate yourself, or wait for the next UTC day",
        "Every source (both API providers and the seven scraping fallbacks) was checked "
        "and nothing usable was found for this movie, so the download was deliberately "
        "not made. Catalogues grow and sources come back, so the scraping tier is offered "
        "to this movie again on every later UTC day automatically; you can also place the "
        "subtitle yourself or re-run with --retry-review.",
    ),
    NeedsBucket(
        REASON_NO_MATCH,
        "NO MATCHING SUBTITLE ON ANY SOURCE",
        "re-run on a later day, or add the SRT by hand",
        "No source - OpenSubtitles, SubDL, or any of the scraping fallbacks - returned a "
        "safe English SRT that names the movie and its year. Catalogues grow over time, so "
        "a later run can succeed; otherwise add the subtitle yourself.",
    ),
    NeedsBucket(
        REASON_QUOTA,
        "DEFERRED TO THE NEXT UTC DAY",
        "nothing to fix - re-run after the UTC day rolls over",
        "Every source's usable daily allowance was exhausted (API download caps and/or "
        "scraping search caps), so these movies were not searched. Re-run after the UTC day "
        "rolls over; no request is wasted.",
    ),
    NeedsBucket(
        REASON_ERROR,
        "ERRORS",
        "read the log entry for each one",
        "Something failed while reading the movie or talking to the provider. The log "
        "carries the exact error; fix the cause and re-run.",
    ),
)

DEFERRED_NOT_SCANNED = "never scanned: the UTC request cap was reached before this movie"

def movie_label(video: Path, library: Path) -> str:
    """The movie's folder, relative to the library.

    The layout contract is ``Title (Year)/Title (Year).mkv``, so the folder
    already names the movie; repeating the ``.mkv`` beside it only made every
    line longer without saying anything new.
    """
    if video.parent != library:
        return relative_text(video.parent, library)
    return relative_text(video, library)

def group_results(
    results: Sequence[JobResult], summary: dict[str, Any]
) -> tuple[dict[str, list[tuple[Path, str]]], list[JobResult], list[JobResult], list[JobResult],
           list[JobResult]]:
    """Split one run into (needs buckets, covered, downloaded, dry-run, extracted).

    Movies the quota cut off before they were scanned join the quota bucket so
    the report names them instead of only reporting a count.
    """
    buckets: dict[str, list[tuple[Path, str]]] = {bucket.reason: [] for bucket in NEEDS_SUBTITLE_BUCKETS}
    covered: list[JobResult] = []
    downloaded: list[JobResult] = []
    dry_run: list[JobResult] = []
    extracted: list[JobResult] = []
    for result in results:
        if result.reason == REASON_COVERED:
            covered.append(result)
        elif result.reason == REASON_DOWNLOADED:
            downloaded.append(result)
        elif result.reason == REASON_EXTRACTED:
            extracted.append(result)
        elif result.reason == REASON_DRY_RUN:
            dry_run.append(result)
        elif result.reason in buckets:
            buckets[result.reason].append((result.video, result.detail))
        else:  # a reason nobody knows about must still be visible, not dropped
            buckets.setdefault(REASON_ERROR, []).append((result.video, result.detail or result.status))
    for video in summary.get("deferred_videos") or ():
        buckets[REASON_QUOTA].append((Path(video), DEFERRED_NOT_SCANNED))
    for items in buckets.values():
        items.sort(key=lambda item: str(item[0]).casefold())
    covered.sort(key=lambda item: str(item.video).casefold())
    downloaded.sort(key=lambda item: str(item.video).casefold())
    dry_run.sort(key=lambda item: str(item.video).casefold())
    extracted.sort(key=lambda item: str(item.video).casefold())
    return buckets, covered, downloaded, dry_run, extracted

def report_provider_quota_text(cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Format provider reservations for the report, including old summaries."""
    parts: list[str] = []
    # Unit callers and old log-derived summaries have only the legacy
    # OpenSubtitles fields, so retain that display when no SubDL key is set.
    if cfg.api_key.strip() or not cfg.subdl_api_key.strip():
        reserved = int(summary.get("opensubtitles_download_requests_reserved",
                                   summary.get("download_requests_reserved", 0)) or 0)
        cap = int(summary.get("opensubtitles_daily_cap", summary.get("daily_cap", cfg.daily_cap)) or 0)
        parts.append(f"OpenSubtitles {reserved}/{cap} reserved · {max(0, cap - reserved)} left")
    if cfg.subdl_api_key.strip():
        downloads_reserved = int(summary.get("subdl_download_requests_reserved", 0) or 0)
        downloads_cap = int(summary.get("subdl_daily_cap", cfg.subdl_daily_cap) or 0)
        searches_reserved = int(summary.get("subdl_search_requests_reserved", 0) or 0)
        searches_cap = int(summary.get("subdl_search_daily_cap", cfg.subdl_search_daily_cap) or 0)
        parts.append(
            f"SubDL downloads {downloads_reserved}/{downloads_cap} reserved · "
            f"{max(0, downloads_cap - downloads_reserved)} left; searches "
            f"{searches_reserved}/{searches_cap} reserved · {max(0, searches_cap - searches_reserved)} left"
        )
    if summary.get("scrape_sources_enabled") or cfg.scrape_daily_cap > 0:
        reserved_by_source = summary.get("scrape_search_requests_reserved") or {}
        cap = int(summary.get("scrape_search_daily_cap", cfg.scrape_daily_cap) or 0)
        enabled = summary.get("scrape_sources_enabled")
        if enabled:
            total_reserved = sum(int(v) for v in reserved_by_source.values())
            parts.append(
                f"scraping ({len(enabled)} sources) {total_reserved} searches reserved today "
                f"({cap}/source cap)"
            )
        else:
            parts.append("scraping sources not configured for this run")
    return "  ·  ".join(parts) or "No source configured"

def report_download_text(cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Show a useful provider breakdown without breaking old report callers."""
    total = int(summary.get("successful_downloads", 0) or 0)
    parts: list[str] = []
    if cfg.api_key.strip():
        parts.append(f"OpenSubtitles {int(summary.get('opensubtitles_successful_downloads', 0) or 0)}")
    if cfg.subdl_api_key.strip():
        parts.append(f"SubDL {int(summary.get('subdl_successful_downloads', 0) or 0)}")
    scrape_success = summary.get("scrape_successful_downloads") or {}
    scrape_total = sum(int(v) for v in scrape_success.values())
    if scrape_total:
        detail = " · ".join(
            f"{scrape_provider_label(key)} {int(count)}"
            for key, count in scrape_success.items() if int(count or 0)
        )
        parts.append(f"scraping {scrape_total} ({detail})")
    return f"{total} successful this run" + (f" ({' · '.join(parts)})" if parts else "")

def extract_banner_text(cfg: QueueConfig) -> str:
    """One banner line saying what embedded extraction can do on this machine.

    Every binary behind this feature is optional, so the run has to say up
    front whether the movie's own tracks are usable here - otherwise an
    image-only library looks like a provider problem when it is really a
    missing OCR tool.
    """
    if not cfg.extract_embedded:
        return "disabled (--no-extract): every uncovered movie goes to the provider sources"
    if not find_mkvtoolnix_binary("mkvmerge") or not find_mkvtoolnix_binary("mkvextract"):
        return f"unavailable: {MKVTOOLNIX_INSTALL_HINT}"
    backend, note = detect_ocr_backend(cfg.ocr_backend, explicit_bin=cfg.ocr_bin,
                                       arg_template=cfg.ocr_args)
    text_part = f"text tracks (SRT/SSA/ASS) with mkvextract (>= {cfg.extract_min_cues} cues)"
    image_part = (f"image tracks (PGS/VobSub) with {backend.label}"
                  if backend is not None else f"image tracks (PGS/VobSub) skipped: {note}")
    return f"extracted before any download — {text_part}; {image_part}"


def build_report(results: Sequence[JobResult], cfg: QueueConfig, summary: dict[str, Any]) -> str:
    """Render the whole run as one report a human can act on in ten seconds.

    The two questions this report exists to answer come first and in full:
    which movies still need a subtitle, and which already have their external
    ``.eng.srt``.
    """
    buckets, covered, downloaded, dry_run, extracted = group_results(results, summary)
    needs = sum(len(items) for items in buckets.values())
    total = int(summary.get("movies_discovered") or len(results))
    covered_count = int(summary.get("coverage_covered", len(covered) + len(downloaded)
                              + (len(dry_run) if cfg.dry_run else 0)) or 0)
    coverage_pct = (100.0 * covered_count / total) if total else 100.0

    policy = provider_policy_text(cfg)
    sources_meta: list[str] = []
    if cfg.api_key.strip():
        sources_meta.append("OpenSubtitles")
    if cfg.subdl_api_key.strip():
        sources_meta.append("SubDL")
    scrape_enabled = summary.get("scrape_sources_enabled")
    if scrape_enabled:
        labels = [scrape_provider_label(key) for key in scrape_enabled]
        sources_meta.append("scraping fallback: " + " · ".join(labels))
    sources_meta_line = " + ".join(sources_meta) if sources_meta else "no source configured"
    scrape_status = summary.get("scrape_sources_status") or {}
    if scrape_status:
        sources_meta_line += "  ·  " + "; ".join(
            f"{scrape_provider_label(key)}: {text}" for key, text in scrape_status.items()
        )
    elif scrape_enabled and cfg.dry_run:
        sources_meta_line += "  ·  scraping sources skipped in dry-run (no requests are spent)"
    report = Report(
        "JELLYFIN DAILY SUBTITLE QUEUE REPORT",
        f"One validated external English {EXTERNAL_SRT_SUFFIX} beside every movie \u00b7 {policy}",
    )
    report.metas([
        ("Generated", f"{utc_timestamp()} (UTC)"),
        ("Library", cfg.library),
        ("Sources", sources_meta_line),
        ("Quota", f"{summary['utc_day']}  \u00b7  {report_provider_quota_text(cfg, summary)}"),
        ("Downloads", report_download_text(cfg, summary)),
        ("Policy", f"English human-authored UTF-8 SRT only  \u00b7  {policy}  \u00b7  {SELECTION_POLICY_TEXT}"),
        ("Ledger", cfg.log_file or "(none)"),
    ])

    rows: list[tuple[object, str, str]] = [
        (f"{covered_count}/{total} ({coverage_pct:.1f}%)",
         "COVERAGE: movies with a validated English SRT" + (" (would be covered)" if cfg.dry_run else ""),
         "the goal: 100% - every uncovered movie is named below"),
        (len(covered), "Already have .eng.srt", "validated sidecar beside the movie"),
        (len(extracted), "Extracted from the movie", "sidecar built from its own embedded track"),
        (len(downloaded), "Downloaded this run", f"written as <movie>{EXTERNAL_SRT_SUFFIX}"),
    ]
    if dry_run or cfg.dry_run:
        rows.append((len(dry_run), "Dry-run candidates", "no files were written"))
    rows.append((needs, "NEED A SUBTITLE", "action required \u00b7 every one is listed below"))
    rows.append((total, "Movies in the library", "every folder holding an eligible MKV"))
    report.blank()
    report.scorecard(rows)

    first_action = next(
        (bucket for bucket in NEEDS_SUBTITLE_BUCKETS if buckets.get(bucket.reason)), None
    )
    if first_action is not None:
        count = len(buckets[first_action.reason])
        report.paragraph(
            f"Start here: {count} movie(s) in \"{first_action.title}\" \u00b7 {first_action.quick}."
        )
    elif needs == 0:
        report.paragraph(
            f"Nothing to do: every one of the {total} movie(s) in the library has a "
            f"validated external English {EXTERNAL_SRT_SUFFIX}."
        )

    # ---- what still needs a subtitle -------------------------------------
    report.section(
        "MOVIES THAT NEED A SUBTITLE",
        count=needs,
        total=total,
        intro=(
            "Jellyfin and Plex direct play an external subtitle only when it is named exactly "
            f"\"<movie folder>{EXTERNAL_SRT_SUFFIX}\" and sits beside the MKV. Every movie below is "
            "missing one. Groups are ordered cheapest fix first."
        ),
    )
    if needs == 0:
        report.paragraph("None. Every movie already has a validated external English subtitle.")
    else:
        for bucket in NEEDS_SUBTITLE_BUCKETS:
            items = buckets.get(bucket.reason) or []
            if not items:
                continue
            report.subsection(bucket.title, count=len(items))
            report.paragraph(bucket.fix)
            report.blank()
            report.entries(
                [(movie_label(video, cfg.library), detail) for video, detail in items],
            )

    # ---- what this run changed -------------------------------------------
    if downloaded:
        report.section(
            "DOWNLOADED DURING THIS RUN",
            count=len(downloaded),
            total=total,
            intro="Each of these was matched, validated and written this run.",
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library),
              "detail": (result.dest.name if result.dest else "")}
             for result in downloaded],
            detail_column=48,
        )
    if extracted:
        report.section(
            "EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK",
            count=len(extracted),
            total=total,
            intro=(
                "These movies already carried an English subtitle track. It was extracted to the "
                f"canonical <movie>{EXTERNAL_SRT_SUFFIX} instead of downloading anything: it is exact "
                "for this release, it costs no provider request, and its cues come from the "
                "container's own timeline, so it needs no timing correction. "
                "mkv_track_cleaner.py still strips every embedded subtitle, leaving this sidecar "
                "as the sole subtitle option."
            ),
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in extracted],
        )

    if dry_run:
        report.section(
            "DRY-RUN CANDIDATES (NOTHING WAS WRITTEN)",
            count=len(dry_run),
            total=total,
            intro="Re-run without --dry-run to actually download these.",
        )
        report.entries(
            [{"text": movie_label(result.video, cfg.library), "detail": result.detail}
             for result in dry_run],
        )

    # ---- what is already covered -----------------------------------------
    report.section(
        f"MOVIES THAT ALREADY HAVE AN EXTERNAL {EXTERNAL_SRT_SUFFIX}",
        count=len(covered),
        total=total,
        intro=(
            "Every movie here has a validated sidecar with the exact canonical name, so "
            "Jellyfin and Plex will direct play it. No action needed."
        ),
    )
    if not covered:
        report.paragraph("None yet.")
    else:
        report.entries(
            [{"text": movie_label(result.video, cfg.library),
              "detail": (result.dest.name if result.dest else f"<movie>{EXTERNAL_SRT_SUFFIX}")}
             for result in covered],
            detail_column=48,
        )

    report.footer([
        f"Coverage this run: {covered_count} of {total} movie(s) "
        f"({coverage_pct:.1f}%) end with a validated external English SRT.",
        f"Durable quota and retry ledger  {cfg.log_file or '(none)'}",
        f"This report  {cfg.report_file}",
        "Re-running is always safe: covered movies are skipped without spending a request, "
        "uncovered movies are re-offered to the scraping sources on every UTC day, and "
        "the ledger keeps every run inside each source's UTC cap.",
    ])
    return report.render()

def write_report(results: Sequence[JobResult], cfg: QueueConfig, summary: dict[str, Any]) -> None:
    """Publish the report: written atomically, then echoed to the console."""
    text = build_report(results, cfg, summary)
    atomic_write_text(cfg.report_file, text, replace=True)
    print_text(text)
    log(f"Report written: {cfg.report_file}", log_file=cfg.log_file)

# =============================================================================
# COMPACT ROOT-LEVEL DRIVER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one validated external English SRT per Jellyfin MKV. "
            "OpenSubtitles and SubDL are equal sources: both providers' "
            "release-identifying routes are consulted (SubDL's score-gated "
            "release match requires score >= 0.80), and the qualifying release "
            "with the most downloads wins. A candidate is auto-selected only "
            "when its release name names the movie and its release year, "
            "carries a Blu-ray keyword, and has the most downloads."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=Path(LIBRARY_DIR),
                        help="Jellyfin movie-library root")
    parser.add_argument("--report", type=Path, default=Path(REPORT_FILE),
                        help="Single replaceable human-readable report outside the library")
    parser.add_argument("--log", type=Path, default=Path(LOG_FILE),
                        help="Single root log outside the media library")
    parser.add_argument(
        "--auth-mode", choices=(AUTH_MODE_DEVELOPMENT_ANONYMOUS, AUTH_MODE_USER),
        default=DEFAULT_AUTH_MODE,
        help=("OpenSubtitles download path. development-anonymous is the default and uses only an "
              "API key where the provider permits it; user is the authenticated fallback."),
    )
    parser.add_argument("--daily-cap", type=int, default=0, metavar="N",
                        help="Maximum OpenSubtitles download requests per UTC day (0 selects the free cap for --auth-mode)")
    parser.add_argument("--subdl-daily-cap", type=int, default=0, metavar="N",
                        help=("Maximum SubDL download requests per UTC day (0 uses the conservative "
                              f"free allowance of {SUBDL_DEFAULT_DAILY_CAP})"))
    parser.add_argument("--subdl-search-daily-cap", type=int, default=0, metavar="N",
                        help=("Maximum SubDL search requests per UTC day (0 uses the conservative "
                              f"free allowance of {SUBDL_DEFAULT_SEARCH_DAILY_CAP})"))
    parser.add_argument("--scrape-daily-cap", type=int, default=None, metavar="N",
                        help=(f"Maximum scraping-source search requests per UTC day per source "
                              f"(default uses the conservative allowance of "
                              f"{SCRAPE_DEFAULT_SEARCH_DAILY_CAP}; 0 disables the scraping "
                              "fallback sources entirely)"))
    parser.add_argument("--skip-source", action="append", default=[], metavar="SOURCE",
                        choices=list(SCRAPE_PROVIDER_ORDER),
                        help="Disable one scraping source for this run (repeatable)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="Exit 0 even when some movies finish without a validated English SRT "
                             "(default: exit 1 while any movie is uncovered, so the gap is loud)")
    parser.add_argument("--min-size", type=float, default=MIN_MOVIE_SIZE_MB, metavar="MB")
    parser.add_argument("--lock-timeout", type=float, default=60.0, metavar="SEC")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Process at most N movies (0 means all eligible movies)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview candidates; searches still run, but no download request or SRT write")
    parser.add_argument("--no-identity-fallback", dest="identity_fallback", action="store_false",
                        help="Disable all conservative non-hash fallback matching after hash misses")
    parser.set_defaults(identity_fallback=True)
    parser.add_argument("--retry-review", action="store_true",
                        help="Reconsider movies previously held for manual identity review")
    parser.add_argument("--no-extract", dest="extract_embedded", action="store_false",
                        help="Never build a sidecar from the movie's own embedded subtitle track "
                             "(default: extract before downloading anything)")
    parser.set_defaults(extract_embedded=True)
    parser.add_argument("--extract-min-cues", type=int, default=DEFAULT_EXTRACT_MIN_CUES, metavar="N",
                        help="Reject an embedded track with fewer than N cues as signs/songs-only")
    parser.add_argument("--ocr-backend", default=OCR_BACKEND_AUTO, choices=list(OCR_BACKEND_CHOICES),
                        help="OCR program for image-based tracks (PGS/VobSub): auto picks the first "
                             "one installed; none disables image tracks entirely")
    parser.add_argument("--ocr-bin", default="", metavar="PATH",
                        help="Path to the OCR program or .NET dll (PgsToSrt) instead of searching PATH")
    parser.add_argument("--ocr-args", default="", metavar="ARGS",
                        help="Argument template for --ocr-backend custom, e.g. \"{input}\" \"{output}\" "
                             "(placeholders: {input} {output} {track} {lang})")
    parser.add_argument("--ocr-timeout", type=float, default=DEFAULT_OCR_TIMEOUT_SEC, metavar="SEC",
                        help="Per-movie OCR time limit (0 disables the limit)")
    parser.add_argument("--ocr-limit", type=int, default=0, metavar="N",
                        help="OCR at most N movies per run (0 means no cap; OCR is minutes of local CPU)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser

def resolve_daily_cap(auth_mode: str, requested_cap: int) -> int:
    """Select and bound the free daily limit for the explicit authentication path."""
    permitted = {
        AUTH_MODE_DEVELOPMENT_ANONYMOUS: DEVELOPMENT_ANONYMOUS_DAILY_CAP,
        AUTH_MODE_USER: USER_DAILY_CAP,
    }
    if auth_mode not in permitted:
        raise ValueError(f"unsupported authentication mode: {auth_mode}")
    cap = permitted[auth_mode] if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--daily-cap must be zero (automatic) or at least 1")
    if cap > permitted[auth_mode]:
        raise ValueError(
            f"--daily-cap {cap} exceeds the documented free limit for {auth_mode}: {permitted[auth_mode]}"
        )
    return cap

def resolve_subdl_daily_cap(requested_cap: int) -> int:
    """Choose SubDL's conservative free download allowance or a user override."""
    cap = SUBDL_DEFAULT_DAILY_CAP if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--subdl-daily-cap must be zero (automatic) or at least 1")
    return cap

def resolve_subdl_search_daily_cap(requested_cap: int) -> int:
    """Choose SubDL's free search allowance or a user plan-specific override."""
    cap = SUBDL_DEFAULT_SEARCH_DAILY_CAP if requested_cap == 0 else int(requested_cap)
    if cap < 1:
        raise ValueError("--subdl-search-daily-cap must be zero (automatic) or at least 1")
    return cap

def resolve_scrape_daily_cap(requested_cap: int | None) -> int:
    """Choose the scraping sources' conservative search allowance.

    None (the CLI default) keeps the tier on with the built-in allowance;
    0 disables the scraping fallback entirely; any positive N overrides it.
    """
    if requested_cap is None:
        return SCRAPE_DEFAULT_SEARCH_DAILY_CAP
    cap = int(requested_cap)
    if cap < 0:
        raise ValueError("--scrape-daily-cap must be zero (disabled) or at least 1")
    return cap

def compact_config_from_args(args: argparse.Namespace) -> QueueConfig:
    return QueueConfig(
        library=args.source.resolve(),
        log_file=args.log.resolve() if args.log else None,
        report_file=args.report.resolve(),
        api_key=(os.environ.get("OPENSUBTITLES_API_KEY") or OPENSUBTITLES_API_KEY).strip(),
        subdl_api_key=(os.environ.get("SUBDL_API_KEY") or SUBDL_API_KEY).strip(),
        username=(os.environ.get("OPENSUBTITLES_USERNAME") or OPENSUBTITLES_USERNAME).strip(),
        password=(os.environ.get("OPENSUBTITLES_PASSWORD") or OPENSUBTITLES_PASSWORD).strip(),
        daily_cap=resolve_daily_cap(str(args.auth_mode), int(args.daily_cap)),
        subdl_daily_cap=resolve_subdl_daily_cap(int(args.subdl_daily_cap)),
        subdl_search_daily_cap=resolve_subdl_search_daily_cap(int(args.subdl_search_daily_cap)),
        scrape_daily_cap=resolve_scrape_daily_cap(args.scrape_daily_cap),
        skip_sources=tuple(args.skip_source),
        allow_missing=bool(args.allow_missing),
        extract_embedded=bool(args.extract_embedded),
        extract_min_cues=max(1, int(args.extract_min_cues)),
        ocr_backend=str(args.ocr_backend),
        ocr_bin=str(args.ocr_bin),
        ocr_args=str(args.ocr_args),
        ocr_timeout_seconds=max(0.0, float(args.ocr_timeout)),
        ocr_limit=max(0, int(args.ocr_limit)),
        min_movie_size_mb=float(args.min_size),
        lock_timeout_seconds=max(0.0, float(args.lock_timeout)),
        retry_no_match=bool(args.retry_review),
        identity_fallback=bool(args.identity_fallback),
        dry_run=bool(args.dry_run),
        limit=max(0, int(args.limit)),
        auth_mode=str(args.auth_mode),
    )

def validate_compact_config(cfg: QueueConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.library.is_dir() or cfg.library.is_symlink():
        errors.append("--source must be an existing non-symlink movie-library directory")
    if cfg.daily_cap < 1:
        errors.append("--daily-cap must be at least 1")
    if cfg.subdl_daily_cap < 1:
        errors.append("--subdl-daily-cap must be at least 1")
    if cfg.subdl_search_daily_cap < 1:
        errors.append("--subdl-search-daily-cap must be at least 1")
    if cfg.auth_mode not in {AUTH_MODE_DEVELOPMENT_ANONYMOUS, AUTH_MODE_USER}:
        errors.append("--auth-mode is unsupported")
    if not configured_providers(cfg) and not active_scrape_sources(cfg):
        errors.append(
            "configure OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY, or keep the scraping "
            "sources enabled (--scrape-daily-cap 0 disables them)"
        )
    if cfg.api_key and cfg.auth_mode == AUTH_MODE_USER and (not cfg.username or not cfg.password):
        errors.append("--auth-mode user requires an OpenSubtitles username and password")
    if cfg.subdl_api_key.strip() and not cfg.api_key.strip() and not cfg.identity_fallback:
        errors.append("SubDL-only mode requires fallback matching; omit --no-identity-fallback")
    if cfg.ocr_backend not in OCR_BACKEND_CHOICES:
        errors.append(f"--ocr-backend must be one of: {', '.join(OCR_BACKEND_CHOICES)}")
    if cfg.extract_min_cues < 1:
        errors.append("--extract-min-cues must be at least 1")
    if cfg.ocr_timeout_seconds < 0:
        errors.append("--ocr-timeout must be zero (no limit) or greater")
    if cfg.ocr_limit < 0:
        errors.append("--ocr-limit must be zero (no cap) or greater")
    if cfg.min_movie_size_mb < 0 or cfg.lock_timeout_seconds < 0 or cfg.limit < 0:
        errors.append("--min-size, --lock-timeout, and --limit must be non-negative")
    if cfg.report_file == cfg.library or cfg.report_file.is_relative_to(cfg.library):
        errors.append("--report must be outside the Jellyfin media library")
    if cfg.log_file and (cfg.log_file == cfg.library or cfg.log_file.is_relative_to(cfg.library)):
        errors.append("--log must be outside the Jellyfin media library")
    return errors

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    try:
        enable_utf8_stdio()
        cfg = compact_config_from_args(args)
        errors = validate_compact_config(cfg)
        if errors:
            for error in errors:
                print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        mode = "DRY-RUN (nothing will be written)" if cfg.dry_run else "LIVE"
        print_text(report_banner(
            "JELLYFIN EXTERNAL ENGLISH SRT FETCHER",
            f"One validated external English {EXTERNAL_SRT_SUFFIX} per movie",
            [
                ("Mode", mode),
                ("Library", cfg.library),
                ("Policy", "English human-authored UTF-8 SRT; " + provider_policy_text(cfg) + "; " + SELECTION_POLICY_TEXT),
                ("Sources", provider_configuration_text(cfg) + " (UTC caps)"),
                ("Ledger", cfg.log_file),
                ("Report", cfg.report_file),
                ("Embedded tracks", extract_banner_text(cfg)),
            ],
        ))
        with CoordinationLock(cfg.library, timeout_seconds=cfg.lock_timeout_seconds):
            results, summary = queue_run(cfg)
            write_report(results, cfg, summary)
        if any(result.status == "error" for result in results):
            return 1
        uncovered = int(summary.get("coverage_total", 0)) - int(summary.get("coverage_covered", 0))
        if uncovered > 0 and not cfg.allow_missing:
            print(
                f"Coverage incomplete: {uncovered} of {summary.get('coverage_total')} movie(s) "
                "still lack a validated English SRT. They are named in the report and are "
                "re-offered to the scraping sources on the next UTC day. "
                "Use --allow-missing to exit 0 anyway.",
                file=sys.stderr,
            )
            return 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Subtitle fetcher failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1


def run_self_tests() -> int:
    """Field smoke test: can this copy hash a movie and judge a subtitle?

    The provider clients, the scraping tiers, the quota ledger and the
    extraction path are covered exhaustively in ``tests/selftests/``. The two
    things worth re-checking on an unfamiliar machine are the moviehash (a
    wrong one silently degrades every lookup to title/year matching) and the
    sidecar contract.
    """
    def moviehash_is_stable() -> bool:
        data = bytes(range(256)) * 600  # > 2 * 64 KiB so both chunks are real
        first = moviehash_bytes(data)
        return first == moviehash_bytes(data) and len(first) == 16

    def a_real_srt_validates() -> bool:
        with tempfile.TemporaryDirectory(prefix="fetcher_smoke_") as td:
            srt = Path(td) / "Movie (2020).eng.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n", encoding="utf-8")
            return validate_srt_sidecar(srt)[0]

    def html_is_rejected() -> bool:
        with tempfile.TemporaryDirectory(prefix="fetcher_smoke_") as td:
            srt = Path(td) / "Movie (2020).eng.srt"
            srt.write_text("<!DOCTYPE html><html>not a subtitle</html>", encoding="utf-8")
            return not validate_srt_sidecar(srt)[0]

    def the_sidecar_path_is_canonical() -> bool:
        movie = Path("/library/Movie (2020)/Movie (2020).mkv")
        return exact_external_english_srt_path(movie).name == "Movie (2020).eng.srt"

    return run_field_smoke_test("subtitle_fetcher.py", [
        ("the moviehash is stable", moviehash_is_stable),
        ("a valid .eng.srt is accepted", a_real_srt_validates),
        ("an HTML error page is rejected", html_is_rejected),
        ("the sidecar path is canonical", the_sidecar_path_is_canonical),
    ])

if __name__ == "__main__":
    raise SystemExit(main())
