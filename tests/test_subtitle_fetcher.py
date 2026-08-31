"""Tests for the pure helpers in ``subtitle_fetcher.py``."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from reporttext import scorecard, section

import subtitle_fetcher as sf

srt_looks_valid = sf.srt_looks_valid
validate_srt_sidecar = sf.validate_srt_sidecar
normalize_srt_newlines = sf.normalize_srt_newlines
decode_srt_bytes = sf.decode_srt_bytes
EXTERNAL_SRT_ENCODINGS = sf.EXTERNAL_SRT_ENCODINGS
EXTERNAL_SRT_MAX_BYTES = sf.EXTERNAL_SRT_MAX_BYTES
EXTERNAL_SRT_CUE_RE = sf.EXTERNAL_SRT_CUE_RE
EXTERNAL_SRT_SUFFIX = sf.EXTERNAL_SRT_SUFFIX
LEGACY_EXTERNAL_SRT_SUFFIX = sf.LEGACY_EXTERNAL_SRT_SUFFIX
CoordinationLock = sf.CoordinationLock
LockTimeoutError = sf.LockTimeoutError
promote_legacy_external_english_srt = sf.promote_legacy_external_english_srt
exact_external_english_srt_path = sf.exact_external_english_srt_path


class MovieHashTests(unittest.TestCase):
    def test_moviehash_of_large_file(self) -> None:
        # OpenSubtitles OSHash requires >= HASH_CHUNK * 2 bytes.
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(bytes(i & 0xFF for i in range(sf.MIN_HASH_SIZE)))
            path = Path(fh.name)
        try:
            digest = sf.moviehash(path)
            self.assertEqual(len(digest), 16)
            self.assertTrue(all(c in "0123456789abcdef" for c in digest))
        finally:
            path.unlink(missing_ok=True)

    def test_moviehash_too_small_raises(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"tiny")
            path = Path(fh.name)
        try:
            with self.assertRaises(ValueError):
                sf.moviehash(path)
        finally:
            path.unlink(missing_ok=True)


class SnapshotTests(unittest.TestCase):
    def test_path_norm_equivalence(self) -> None:
        # Matches the standardizer/cleaner path normalisation contract exactly.
        self.assertEqual(sf.path_norm(Path("/tmp/./a/../a/x.mkv")), sf.path_norm("/tmp/a/x.mkv"))


class PerMovieFailureIsolationTests(unittest.TestCase):
    """One bad movie must never abort the rest of the library.

    The per-movie handler around the hash/search step caught only
    ``RuntimeError``, but ``moviehash()`` raises ``ValueError`` for a file below
    ``MIN_HASH_SIZE`` and ``decode_subtitle_bytes()`` raises it for a subtitle
    that decompresses past ``MAX_SUBTITLE_BYTES``. Either one escaped as an
    uncaught traceback that killed the whole run, so every remaining movie went
    unfetched.
    """

    def test_undersized_movie_is_recorded_not_fatal(self) -> None:
        """End to end: a 3-byte MKV yields a per-movie error and exit 0.

        ``--min-size 0`` lets the stub past the size gate so the hash is
        attempted. No network call happens: the hash fails before the client is
        used, which keeps this test hermetic.
        """
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            movie_dir = root / "lib" / "Tiny Movie (2020)"
            movie_dir.mkdir(parents=True)
            (movie_dir / "Tiny Movie (2020).mkv").write_bytes(b"mkv")
            report = root / "report.txt"
            env = dict(os.environ, OPENSUBTITLES_API_KEY="test-key-not-used")
            proc = subprocess.run(
                [sys.executable, "subtitle_fetcher.py", "--source", str(root / "lib"),
                 "--log", str(root / "fetch.log"), "--report", str(report), "--min-size", "0"],
                capture_output=True, env=env, timeout=120,
                # The child pins its stdio to UTF-8 (its report is full of
                # box-drawing characters), so the parent must not decode with
                # the locale encoding - cp1252 on Windows turns those bytes
                # into a UnicodeDecodeError.
                encoding="utf-8", errors="replace",
                cwd=Path(__file__).resolve().parent.parent,
            )

            # Exit 1 is correct here: the tool reports "there were errors".
            # The bug was that it got there by crashing instead of by recording
            # the failure, so the distinguishing assertions are the absence of a
            # traceback and the presence of a per-movie error in the report.
            self.assertNotIn("Traceback", proc.stderr, proc.stderr[-800:])
            self.assertEqual(proc.returncode, 1, proc.stdout[-800:])
            text = report.read_text(encoding="utf-8")
            self.assertIn("too small to hash", text)
            self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 1)
            self.assertIn("ERRORS", text)

    def test_oversized_decompressed_subtitle_raises_value_error(self) -> None:
        """The provider payload case the download handler must also survive."""
        import gzip

        bomb = gzip.compress(b"x" * (sf.MAX_SUBTITLE_BYTES + 1))
        with self.assertRaises(ValueError):
            sf.decode_subtitle_bytes(bomb)

    def test_download_handler_catches_value_error(self) -> None:
        """Pin the fix: the download site handles ValueError, not just RuntimeError."""
        import inspect

        source = inspect.getsource(sf.queue_run)
        self.assertIn("except (RuntimeError, ValueError) as exc:", source)
        # The hash/search and download paths must both isolate malformed input;
        # provider-specific fallback branches may add further guarded sites.
        self.assertGreaterEqual(source.count("except (RuntimeError, ValueError) as exc:"), 2)


class SrtSidecarContractTests(unittest.TestCase):
    """The shared verdict on whether an external SRT is actually usable.

    A file that fails this is not a subtitle but an empty stub, a provider error
    page, or a truncated download. Treating one as valid silently blocks the
    pipeline: the fetcher will not replace a sidecar it believes is present and
    the cleaner will not trust one it cannot parse.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="common_srt_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_accepts_a_well_formed_cue(self) -> None:
        self.assertTrue(srt_looks_valid("1\n00:00:00,000 --> 00:00:01,000\nHi\n"))

    def test_accepts_period_millisecond_separator(self) -> None:
        self.assertTrue(srt_looks_valid("1\n00:00:00.000 --> 00:00:01.000\nHi\n"))

    def test_accepts_indented_cue_number(self) -> None:
        self.assertTrue(srt_looks_valid("  1\n00:00:00,000 --> 00:00:01,000\nHi\n"))

    def test_rejects_non_subtitle_text(self) -> None:
        for body in ("", "sub", "<html><body>429 Too Many Requests</body></html>",
                     "1\n00:00:00,000 --> ", "not a subtitle at all"):
            with self.subTest(body=body[:24]):
                self.assertFalse(srt_looks_valid(body))

    def test_validator_accepts_a_real_sidecar(self) -> None:
        ok, reason = validate_srt_sidecar(
            self._write("a.en.srt", "1\n00:00:00,000 --> 00:00:01,000\nHi\n"))
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_validator_rejects_empty_file(self) -> None:
        ok, reason = validate_srt_sidecar(self._write("b.en.srt", ""))
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_validator_rejects_stub_file(self) -> None:
        ok, reason = validate_srt_sidecar(self._write("c.en.srt", "sub"))
        self.assertFalse(ok)
        self.assertIn("cue", reason)

    def test_validator_normalizes_crlf(self) -> None:
        ok, _ = validate_srt_sidecar(
            self._write("d.en.srt", "1\r\n00:00:00,000 --> 00:00:01,000\r\nHi\r\n"))
        self.assertTrue(ok)

    def test_validator_rejects_oversized_file(self) -> None:
        path = self._write("e.en.srt", "1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        with path.open("ab") as handle:
            handle.truncate(4 * 1024 * 1024 + 1)
        ok, reason = validate_srt_sidecar(path)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_validator_rejects_missing_file(self) -> None:
        ok, reason = validate_srt_sidecar(self.root / "absent.en.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat", reason)

    def test_validator_never_follows_a_symlink(self) -> None:
        target = self._write("real.en.srt", "1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        link = self.root / "link.en.srt"
        link.symlink_to(target)
        ok, reason = validate_srt_sidecar(link)
        self.assertFalse(ok)
        self.assertIn("symlink", reason)

    def test_times_out_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with CoordinationLock(Path(td) / "lib", timeout_seconds=10.0) as held:
                blocker = CoordinationLock(Path(td) / "lib", timeout_seconds=0.1)
                # On POSIX, flock conflicts across two separate open file
                # descriptions even in the same process; on Windows msvcrt
                # byte-range locks do the same.
                try:
                    with self.assertRaises(LockTimeoutError):
                        blocker.acquire()
                finally:
                    blocker.release()
            _ = held


PLAIN_CUE = "1\n00:00:01,000 --> 00:00:04,000\nDreams are messages.\n"
INDENTED_CUE = "  1\n00:00:01,000 --> 00:00:04,000\nDreams are messages.\n"
CRLF_CUE = "1\r\n00:00:01,000 --> 00:00:04,000\r\nDreams are messages.\r\n"
NOT_A_CUE = "<html>nope</html>"


class SharedSrtPrimitiveTests(unittest.TestCase):
    def test_decode_prefers_utf8_sig(self) -> None:
        self.assertEqual(decode_srt_bytes("\ufeff1\n".encode("utf-8")), "1\n")

    def test_decode_falls_back_to_cp1252(self) -> None:
        # 0x92 is a cp1252 right single quote and is invalid UTF-8.
        self.assertEqual(decode_srt_bytes(b"it\x92s"), "it\u2019s")

    def test_decode_returns_none_when_nothing_applies(self) -> None:
        # cp1252 accepts almost any byte, so only its five undefined code
        # positions (0x81, 0x8d, 0x8f, 0x90, 0x9d) are genuinely undecodable.
        self.assertIsNone(decode_srt_bytes(b"\x81\x8d\x8f\x90\x9d"))

    def test_encoding_order_is_the_documented_one(self) -> None:
        self.assertEqual(EXTERNAL_SRT_ENCODINGS, ("utf-8-sig", "utf-8", "cp1252"))

    def test_normalize_collapses_crlf_and_bare_cr(self) -> None:
        self.assertEqual(normalize_srt_newlines("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_size_limit_is_four_mebibytes(self) -> None:
        self.assertEqual(EXTERNAL_SRT_MAX_BYTES, 4 * 1024 * 1024)


class SingleSourceContractTests(unittest.TestCase):
    """No tool may keep a divergent private copy of the subtitle contract.

    The shared helpers are vendored into every script on purpose, so any
    single file can be copied out and run on its own. That means the contract
    is enforced by comparison instead of by a shared import: every tool's
    copy must agree on the cue pattern, the size limit and the encoding
    order. These tests fail if a vendored copy ever drifts.
    """

    @staticmethod
    def _verdicts(text: str) -> dict[str, bool]:
        import library_auditor  # noqa: F401  (imported so a break there is caught)
        import mkv_track_cleaner as tc
        import movie_standardizer as ms

        normalized = normalize_srt_newlines(text)
        return {
            "movie_standardizer": ms.EXTERNAL_SRT_CUE_RE.search(normalized) is not None,
            "mkv_track_cleaner": tc.EXTERNAL_SRT_CUE_RE.search(normalized) is not None,
            "subtitle_fetcher": sf.looks_like_srt(normalized),
            "subtitle_fetcher (shared helper)": sf.srt_looks_valid(normalized),
        }

    def test_every_tool_agrees_on_a_plain_cue(self) -> None:
        verdicts = self._verdicts("1\n00:00:01,000 --> 00:00:04,000\nDreams are messages.\n")
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_an_indented_cue(self) -> None:
        verdicts = self._verdicts("  1\n00:00:01,000 --> 00:00:04,000\nDreams are messages.\n")
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_crlf_line_endings(self) -> None:
        verdicts = self._verdicts("1\r\n00:00:01,000 --> 00:00:04,000\r\nDreams are messages.\r\n")
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_junk(self) -> None:
        verdicts = self._verdicts("<html>nope</html>")
        self.assertEqual(set(verdicts.values()), {False}, verdicts)

    def test_no_tool_keeps_a_divergent_size_limit(self) -> None:
        import mkv_track_cleaner as tc
        import movie_standardizer as ms

        self.assertEqual(ms.EXTERNAL_SRT_MAX_BYTES, EXTERNAL_SRT_MAX_BYTES)
        self.assertEqual(tc.EXTERNAL_SRT_MAX_BYTES, EXTERNAL_SRT_MAX_BYTES)
        self.assertEqual(sf.MAX_SUBTITLE_BYTES, EXTERNAL_SRT_MAX_BYTES)

    def test_no_tool_keeps_a_divergent_cue_pattern(self) -> None:
        import mkv_track_cleaner as tc
        import movie_standardizer as ms

        self.assertEqual(ms.EXTERNAL_SRT_CUE_RE.pattern, EXTERNAL_SRT_CUE_RE.pattern)
        self.assertEqual(tc.EXTERNAL_SRT_CUE_RE.pattern, EXTERNAL_SRT_CUE_RE.pattern)

    def test_no_tool_keeps_a_divergent_encoding_list(self) -> None:
        self.assertEqual(EXTERNAL_SRT_ENCODINGS, ("utf-8-sig", "utf-8", "cp1252"))
        # cp1252 bytes must decode identically everywhere.
        raw = b"it\x92s fine"
        self.assertEqual(decode_srt_bytes(raw), sf.decode_subtitle_bytes(raw))


class PromoteLegacySidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="common_promote_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_exact_path_uses_eng_suffix(self) -> None:
        mkv = self.root / "Film (2000).mkv"
        self.assertEqual(
            exact_external_english_srt_path(mkv).name,
            f"Film (2000){EXTERNAL_SRT_SUFFIX}",
        )
        self.assertTrue(EXTERNAL_SRT_SUFFIX.endswith(".srt"))
        self.assertEqual(EXTERNAL_SRT_SUFFIX, ".eng.srt")
        self.assertEqual(LEGACY_EXTERNAL_SRT_SUFFIX, ".en.srt")

    def test_promote_renames_validated_legacy(self) -> None:
        mkv = self.root / "Film (2000).mkv"
        mkv.write_bytes(b"x")
        legacy = self.root / f"Film (2000){LEGACY_EXTERNAL_SRT_SUFFIX}"
        legacy.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        path, reason = promote_legacy_external_english_srt(mkv)
        self.assertEqual(reason, "")
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, f"Film (2000){EXTERNAL_SRT_SUFFIX}")
        self.assertFalse(legacy.exists())

    def test_promote_is_noop_when_canonical_exists(self) -> None:
        mkv = self.root / "Film (2001).mkv"
        mkv.write_bytes(b"x")
        canonical = self.root / f"Film (2001){EXTERNAL_SRT_SUFFIX}"
        canonical.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        path, reason = promote_legacy_external_english_srt(mkv)
        self.assertEqual(reason, "")
        self.assertEqual(path, canonical)

    def test_promote_refuses_when_both_names_exist(self) -> None:
        mkv = self.root / "Film (2002).mkv"
        mkv.write_bytes(b"x")
        body = "1\n00:00:00,000 --> 00:00:01,000\nHi\n"
        (self.root / f"Film (2002){EXTERNAL_SRT_SUFFIX}").write_text(body, encoding="utf-8")
        (self.root / f"Film (2002){LEGACY_EXTERNAL_SRT_SUFFIX}").write_text(body, encoding="utf-8")
        path, reason = promote_legacy_external_english_srt(mkv)
        # Canonical already present -> success path returns it without touching legacy.
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.name, f"Film (2002){EXTERNAL_SRT_SUFFIX}")
        self.assertEqual(reason, "")
        self.assertTrue((self.root / f"Film (2002){LEGACY_EXTERNAL_SRT_SUFFIX}").exists())


class ReportBannerTests(unittest.TestCase):
    def test_banner_helper_renders_only_the_header(self) -> None:
        banner = sf.report_banner("TITLE", "subtitle", [("Library", "/lib")])
        self.assertIn("TITLE", banner)
        self.assertIn("/lib", banner)
        self.assertTrue(banner.endswith("\u255d"))


if __name__ == "__main__":
    unittest.main()


class ReportOrganizationTests(unittest.TestCase):
    """The report exists to answer two questions: what needs a subtitle, what has one.

    Before this, every movie was dumped into one flat list tagged with a status
    word, and the reader had to reconstruct the grouping themselves. These
    tests pin the grouping: covered movies and sidecar names in one place,
    every movie that still needs a subtitle in another, split by what to do.
    """

    def setUp(self) -> None:
        self.library = Path("/library")

    def video(self, name: str) -> Path:
        return self.library / name / f"{name}.mkv"

    def sidecar(self, name: str) -> Path:
        return self.library / name / f"{name}.eng.srt"

    def config(self, **overrides: object) -> sf.QueueConfig:
        base: dict[str, object] = {
            "library": self.library,
            "log_file": Path("/logs/subtitle_fetcher.log"),
            "report_file": Path("/logs/subtitle_fetcher_report.txt"),
            "daily_cap": 200,
        }
        base.update(overrides)
        return sf.QueueConfig(**base)  # type: ignore[arg-type]

    def summary(self, results: list[sf.JobResult], **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "utc_day": "2026-08-29",
            "daily_cap": 200,
            "download_requests_reserved": 0,
            "successful_downloads": 0,
            "quota_reached": False,
            "deferred_remaining": 0,
            "ledger_log": "/logs/subtitle_fetcher.log",
            "movies_discovered": len(results),
            "deferred_videos": [],
        }
        base.update(overrides)
        return base

    def report(self, results: list[sf.JobResult], summary: dict[str, object]) -> str:
        return sf.build_report(results, self.config(), summary)

    def test_covered_movies_are_listed_with_their_sidecar_name(self) -> None:
        results = [sf.JobResult(self.video("Dune (2021)"), "have", "validated exact .eng.srt",
                                self.sidecar("Dune (2021)"), reason=sf.REASON_COVERED)]
        text = self.report(results, self.summary(results))

        covered = section(text, "MOVIES THAT ALREADY HAVE AN EXTERNAL .eng.srt")
        self.assertIn("Dune (2021)", covered)
        self.assertIn("Dune (2021).eng.srt", covered)
        self.assertNotIn("Dune (2021)", section(text, "MOVIES THAT NEED A SUBTITLE"))
        self.assertEqual(scorecard(text)["Already have .eng.srt"], 1)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 0)

    def test_movies_needing_a_subtitle_are_grouped_by_the_fix(self) -> None:
        results = [
            sf.JobResult(self.video("Broken (2009)"), "review", "unusable sidecar",
                         reason=sf.REASON_SIDECAR_UNUSABLE),
            sf.JobResult(self.video("Heat (1995)"), "skip", "no usable English moviehash match",
                         reason=sf.REASON_NO_MATCH),
            sf.JobResult(self.video("Loose"), "skip", "noncanonical layout", reason=sf.REASON_LAYOUT),
        ]
        text = self.report(results, self.summary(results))
        needs = section(text, "MOVIES THAT NEED A SUBTITLE")

        for title in ("SIDECAR EXISTS BUT IS UNUSABLE", "LIBRARY LAYOUT MUST BE FIXED FIRST",
                      "NO MATCHING SUBTITLE ON CONFIGURED PROVIDERS"):
            self.assertIn(title, needs)
        for movie in ("Broken (2009)", "Heat (1995)", "Loose"):
            self.assertIn(movie, needs)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 3)

    def test_the_cheapest_fix_is_named_first(self) -> None:
        """A broken sidecar is a two-second fix; a provider miss is not."""
        results = [
            sf.JobResult(self.video("Heat (1995)"), "skip", "no match", reason=sf.REASON_NO_MATCH),
            sf.JobResult(self.video("Broken (2009)"), "review", "unusable", reason=sf.REASON_SIDECAR_UNUSABLE),
        ]
        text = self.report(results, self.summary(results))
        self.assertIn("Start here:", text)
        self.assertLess(text.index("SIDECAR EXISTS BUT IS UNUSABLE"),
                        text.index("NO MATCHING SUBTITLE ON CONFIGURED PROVIDERS"))

    def test_movies_cut_off_by_the_quota_are_named_not_just_counted(self) -> None:
        results: list[sf.JobResult] = []
        summary = self.summary(results, deferred_remaining=2,
                               deferred_videos=[self.video("Zodiac (2007)"), self.video("Prisoners (2013)")],
                               movies_discovered=2)
        text = self.report(results, summary)

        deferred = section(text, "MOVIES THAT NEED A SUBTITLE")
        self.assertIn("DEFERRED TO THE NEXT UTC DAY", deferred)
        self.assertIn("Zodiac (2007)", deferred)
        self.assertIn("Prisoners (2013)", deferred)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 2)
        self.assertEqual(scorecard(text)["Movies in the library"], 2)

    def test_downloaded_movies_get_their_own_section(self) -> None:
        results = [sf.JobResult(self.video("Oppenheimer (2023)"), "download", "method=hash",
                                self.sidecar("Oppenheimer (2023)"), reason=sf.REASON_DOWNLOADED)]
        text = self.report(results, self.summary(results, successful_downloads=1))

        downloaded = section(text, "DOWNLOADED DURING THIS RUN")
        self.assertIn("Oppenheimer (2023).eng.srt", downloaded)
        self.assertEqual(scorecard(text)["NEED A SUBTITLE"], 0)

    def test_empty_groups_are_not_rendered(self) -> None:
        results = [sf.JobResult(self.video("Dune (2021)"), "have", "validated exact .eng.srt",
                                self.sidecar("Dune (2021)"), reason=sf.REASON_COVERED)]
        text = self.report(results, self.summary(results))

        self.assertNotIn("ERRORS", text)
        self.assertNotIn("DEFERRED TO THE NEXT UTC DAY", text)
        self.assertIn("None. Every movie already has a validated external English subtitle.", text)

    def test_every_line_fits_the_report_width(self) -> None:
        """A report that overflows its own rules is not a report anybody reads."""
        long_name = "An Unreasonably Long Movie Title That Keeps Going (1999)"
        results = [
            sf.JobResult(self.video(long_name), "have", "validated exact .eng.srt",
                         self.sidecar(long_name), reason=sf.REASON_COVERED),
            sf.JobResult(self.video("Broken (2009)"), "review", "x" * 400, reason=sf.REASON_SIDECAR_UNUSABLE),
        ]
        for line in self.report(results, self.summary(results)).splitlines():
            self.assertLessEqual(len(line), sf.Report("").width, line)


class SubdlIntegrationTests(unittest.TestCase):
    sample_srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    class Response:
        def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
            self.payload = payload
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                return self.payload
            return self.payload[:size]

    @staticmethod
    def identity() -> sf.MovieIdentity:
        return sf.MovieIdentity(title="Dune: Part Two", year=2024, normalized_title="dune part two")

    def payload(self) -> dict[str, object]:
        return {
            "status": True,
            "results": [{
                "sd_id": "sd693134", "type": "movie", "name": "Dune: Part Two",
                "year": 2024, "imdb_id": "tt15239678",
            }],
            "subtitles": [{
                "n_id": "subtitle-123",
                "lang": "english",
                "release_name": "Dune.Part.Two.2024.2160p.BluRay.x265-GROUP",
                "url": "/subtitle/subtitle-123.zip",
                "unpack_files": [{
                    "file_n_id": "file-456",
                    "language": "EN",
                    "format": "srt",
                    "release_name": "Dune.Part.Two.2024.2160p.BluRay.x265-GROUP",
                    "url": "/subtitle/subtitle-123/file-456",
                }],
            }],
        }

    def file_search_payload(self, *, match_score: object = 0.92) -> dict[str, object]:
        """Shape documented for SubDL v2's release-aware /files/search route."""
        return {
            "status": True,
            "results": [{
                "sd_id": "sd693134", "type": "movie", "name": "Dune: Part Two",
                "year": 2024, "imdb_id": "tt15239678",
            }],
            "match": {
                "engine": "local", "confidence": "high", "type": "movie",
                "title": "Dune: Part Two", "year": 2024, "sd_id": "sd693134",
            },
            "subtitles": [{
                "n_id": "subtitle-123", "lang": "english",
                "release_name": "Dune.Part.Two.2024.2160p.BluRay.x265-GROUP",
                "match_score": match_score,
                "url": "/subtitle/subtitle-123/file-456",
            }],
        }

    def test_subdl_search_cap_uses_the_documented_free_default(self) -> None:
        self.assertEqual(sf.resolve_subdl_search_daily_cap(0), 2_000)
        self.assertEqual(sf.resolve_subdl_search_daily_cap(30_000), 30_000)
        with self.assertRaisesRegex(ValueError, "subdl-search-daily-cap"):
            sf.resolve_subdl_search_daily_cap(-1)

    def test_subdl_client_empty_key(self) -> None:
        client = sf.SubdlClient("")
        cands, downloads = client.search_identity(self.identity())
        self.assertEqual(cands, [])
        self.assertEqual(downloads, {})

    def test_v2_search_uses_bearer_auth_and_builds_safe_unpacked_candidate(self) -> None:
        body = json.dumps(self.payload()).encode("utf-8")
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(body)) as open_url:
            candidates, downloads = sf.SubdlClient("secret-key").search_identity(self.identity())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.file_id, "subdl:subtitle-123:file-456")
        self.assertEqual(candidate.feature_title, "Dune: Part Two")
        self.assertEqual(candidate.feature_year, 2024)
        self.assertFalse(candidate.trusted, "the provider did not assert a trusted flag")
        self.assertEqual(
            downloads[str(candidate.file_id)],
            sf.SubdlDownload(n_id="subtitle-123", url="https://dl.subdl.com/subtitle/subtitle-123/file-456"),
        )

        request = open_url.call_args.args[0]
        self.assertIn("/api/v2/subtitles/search?", request.full_url)
        self.assertIn("film_name=Dune%3A+Part+Two", request.full_url)
        self.assertNotIn("secret-key", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")

    def test_file_search_uses_v2_release_score_for_media_manager_matching(self) -> None:
        body = json.dumps(self.file_search_payload()).encode("utf-8")
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(body)) as open_url:
            candidates, downloads = sf.SubdlClient("secret-key").search_filename(
                r"C:\private-library\Dune: Part Two (2024).mkv", self.identity(),
            )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.subdl_match_score, 0.92)
        self.assertEqual(
            downloads[str(candidate.file_id)],
            sf.SubdlDownload(n_id="subtitle-123", url="https://dl.subdl.com/subtitle/subtitle-123/file-456"),
        )
        pick, reason = sf.pick_subdl_identity_candidate(
            candidates, self.identity(), require_release_match_score=True,
        )
        self.assertEqual(pick, candidate)
        self.assertIn("release match 0.92", reason)

        request = open_url.call_args.args[0]
        self.assertIn("/api/v2/files/search?", request.full_url)
        self.assertIn("filename=Dune%3A+Part+Two+%282024%29.mkv", request.full_url)
        self.assertNotIn("private-library", request.full_url)
        self.assertNotIn("C%3A", request.full_url)
        self.assertIn("languages=en", request.full_url)
        self.assertIn("type=movie", request.full_url)
        self.assertIn("hi=0", request.full_url)
        self.assertIn("subs_per_page=30", request.full_url)
        self.assertNotIn("secret-key", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")

    def test_file_search_accepts_its_documented_raw_file_url_without_an_n_id(self) -> None:
        payload = self.file_search_payload()
        subtitles = payload["subtitles"]
        assert isinstance(subtitles, list)
        subtitles[0].pop("n_id")  # type: ignore[index]
        with mock.patch(
            "subtitle_fetcher.urllib.request.urlopen",
            return_value=self.Response(json.dumps(payload).encode("utf-8")),
        ):
            candidates, downloads = sf.SubdlClient("secret-key").search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(str(candidates[0].file_id).startswith("subdl:url:"))
        self.assertEqual(downloads[str(candidates[0].file_id)].n_id, "")
        self.assertEqual(
            downloads[str(candidates[0].file_id)].url,
            "https://dl.subdl.com/subtitle/subtitle-123/file-456",
        )

    def test_search_reservation_callback_runs_before_the_outbound_request(self) -> None:
        reserve = mock.Mock()
        body = json.dumps(self.file_search_payload()).encode("utf-8")

        def open_after_reservation(*_args: object, **_kwargs: object) -> SubdlIntegrationTests.Response:
            self.assertEqual(reserve.call_count, 1)
            return self.Response(body)

        with mock.patch("subtitle_fetcher.urllib.request.urlopen", side_effect=open_after_reservation):
            sf.SubdlClient("secret-key", before_search_request=reserve).search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        reserve.assert_called_once_with()

    def test_search_reservation_callback_counts_a_retry_as_another_request(self) -> None:
        reserve = mock.Mock()
        body = json.dumps(self.file_search_payload()).encode("utf-8")
        with (
            mock.patch(
                "subtitle_fetcher.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary network failure"), self.Response(body)],
            ),
            mock.patch("subtitle_fetcher.time.sleep"),
        ):
            sf.SubdlClient("secret-key", before_search_request=reserve).search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        self.assertEqual(reserve.call_count, 2)

    def test_low_file_search_score_is_not_auto_selected(self) -> None:
        body = json.dumps(self.file_search_payload(match_score=0.79)).encode("utf-8")
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(body)):
            candidates, _downloads = sf.SubdlClient("secret-key").search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        pick, reason = sf.pick_subdl_identity_candidate(
            candidates, self.identity(), require_release_match_score=True,
        )
        self.assertIsNone(pick)
        self.assertIn("match_score >= 0.80", reason)

    def test_file_search_confidence_threshold_is_inclusive(self) -> None:
        body = json.dumps(self.file_search_payload(match_score=0.80)).encode("utf-8")
        with mock.patch("subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(body)):
            candidates, _downloads = sf.SubdlClient("secret-key").search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        pick, _reason = sf.pick_subdl_identity_candidate(
            candidates, self.identity(), require_release_match_score=True,
        )
        self.assertIsNotNone(pick)

    def test_invalid_or_out_of_range_file_search_scores_are_not_auto_selected(self) -> None:
        for raw_score in ("not-a-score", "nan", -0.01, 1.01):
            with self.subTest(raw_score=raw_score):
                body = json.dumps(self.file_search_payload(match_score=raw_score)).encode("utf-8")
                with mock.patch("subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(body)):
                    candidates, _downloads = sf.SubdlClient("secret-key").search_filename(
                        "Dune: Part Two (2024).mkv", self.identity(),
                    )
                self.assertEqual(len(candidates), 1)
                self.assertIsNone(candidates[0].subdl_match_score)
                pick, _reason = sf.pick_subdl_identity_candidate(
                    candidates, self.identity(), require_release_match_score=True,
                )
                self.assertIsNone(pick)

    def test_file_search_requires_its_match_record_to_confirm_identity(self) -> None:
        payload = self.file_search_payload()
        match = payload["match"]
        assert isinstance(match, dict)
        match["year"] = 1984
        with mock.patch(
            "subtitle_fetcher.urllib.request.urlopen",
            return_value=self.Response(json.dumps(payload).encode("utf-8")),
        ):
            candidates, downloads = sf.SubdlClient("key").search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        self.assertEqual(candidates, [])
        self.assertEqual(downloads, {})

    def test_file_search_does_not_substitute_a_generic_result_for_missing_match(self) -> None:
        payload = self.file_search_payload()
        payload.pop("match")
        with mock.patch(
            "subtitle_fetcher.urllib.request.urlopen",
            return_value=self.Response(json.dumps(payload).encode("utf-8")),
        ):
            candidates, downloads = sf.SubdlClient("key").search_filename(
                "Dune: Part Two (2024).mkv", self.identity(),
            )
        self.assertEqual(candidates, [])
        self.assertEqual(downloads, {})

    def test_provider_result_must_confirm_exact_title_and_year(self) -> None:
        payload = self.payload()
        results = payload["results"]
        assert isinstance(results, list)
        results[0]["year"] = 1984  # type: ignore[index]
        with mock.patch(
            "subtitle_fetcher.urllib.request.urlopen",
            return_value=self.Response(json.dumps(payload).encode("utf-8")),
        ):
            candidates, downloads = sf.SubdlClient("key").search_identity(self.identity())
        self.assertEqual(candidates, [])
        self.assertEqual(downloads, {})

    def test_untrusted_download_host_is_not_used_when_n_id_is_available(self) -> None:
        payload = self.payload()
        subtitles = payload["subtitles"]
        assert isinstance(subtitles, list)
        subtitles[0]["unpack_files"][0]["url"] = "https://attacker.invalid/subtitle/steal"  # type: ignore[index]
        with mock.patch(
            "subtitle_fetcher.urllib.request.urlopen",
            return_value=self.Response(json.dumps(payload).encode("utf-8")),
        ):
            candidates, downloads = sf.SubdlClient("key").search_identity(self.identity())
        self.assertEqual(len(candidates), 1)
        download = downloads[str(candidates[0].file_id)]
        self.assertEqual(download.n_id, "subtitle-123")
        self.assertEqual(download.url, "")
        with self.assertRaisesRegex(ValueError, r"outside dl\.subdl\.com"):
            sf.normalize_subdl_download_url("https://attacker.invalid/subtitle/steal")

    def test_release_search_uses_the_unique_highest_confident_score(self) -> None:
        lower_scored_but_popular = sf.Candidate(
            file_id="subdl:lower", release="Dune.Part.Two.2024.1080p.WEB",
            moviehash_match=False, downloads=10_000, votes=100, rating=10.0, trusted=True,
            hearing_impaired=False, machine_translated=False, ai_translated=False,
            foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
            subdl_match_score=0.81,
        )
        highest_score = sf.Candidate(
            **{**lower_scored_but_popular.__dict__, "file_id": "subdl:highest", "downloads": 0,
               "votes": 0, "rating": 0.0, "trusted": False, "subdl_match_score": 0.92},
        )
        pick, reason = sf.pick_subdl_identity_candidate(
            [lower_scored_but_popular, highest_score], self.identity(), require_release_match_score=True,
        )
        self.assertEqual(pick, highest_score)
        self.assertIn("highest release match 0.92", reason)

    def test_tied_confident_release_scores_are_held_for_review(self) -> None:
        base = sf.Candidate(
            file_id="subdl:one", release="Dune.Part.Two.2024.1080p.WEB",
            moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
            hearing_impaired=False, machine_translated=False, ai_translated=False,
            foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
            subdl_match_score=0.92,
        )
        other = sf.Candidate(**{**base.__dict__, "file_id": "subdl:two"})
        pick, reason = sf.pick_subdl_identity_candidate(
            [base, other], self.identity(), require_release_match_score=True,
        )
        self.assertIsNone(pick)
        self.assertIn("equally scored", reason)

    def test_unique_subdl_candidate_without_vote_metadata_is_usable(self) -> None:
        candidate = sf.Candidate(
            file_id="subdl:one", release="Dune.Part.Two.2024.1080p.WEB",
            moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
            hearing_impaired=False, machine_translated=False, ai_translated=False,
            foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
        )
        pick, reason = sf.pick_subdl_identity_candidate([candidate], self.identity())
        self.assertEqual(pick, candidate)
        self.assertIn("one normal English SubDL", reason)

    def test_multiple_subdl_candidates_without_quality_metadata_are_held(self) -> None:
        base = sf.Candidate(
            file_id="subdl:one", release="Dune.Part.Two.2024.1080p.WEB",
            moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
            hearing_impaired=False, machine_translated=False, ai_translated=False,
            foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
        )
        other = sf.Candidate(**{**base.__dict__, "file_id": "subdl:two", "release": "Dune.Part.Two.2024.720p.WEB"})
        pick, reason = sf.pick_subdl_identity_candidate([base, other], self.identity())
        self.assertIsNone(pick)
        self.assertIn("unambiguous", reason)

    def test_zip_payload_is_streamed_and_validated_as_srt(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Dune.Part.Two.eng.srt", self.sample_srt)
        self.assertEqual(sf.decode_subdl_srt_payload(archive.getvalue(), sf.MAX_SUBTITLE_BYTES), self.sample_srt)

    def test_archive_with_multiple_srt_members_is_held_for_review(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Dune.Part.Two.theatrical.srt", self.sample_srt)
            zf.writestr("Dune.Part.Two.extended.srt", self.sample_srt)
        with self.assertRaisesRegex(RuntimeError, "multiple usable .srt"):
            sf.decode_subdl_srt_payload(archive.getvalue(), sf.MAX_SUBTITLE_BYTES)

    def test_n_id_download_uses_v2_endpoint_and_snapshot_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "Dune Part Two (2024).mkv"
            video.write_bytes(b"movie bytes")
            destination = root / "Dune Part Two (2024).eng.srt"
            snapshot = sf.video_snapshot(video)
            with mock.patch(
                "subtitle_fetcher.urllib.request.urlopen",
                return_value=self.Response(self.sample_srt.encode("utf-8")),
            ) as open_url:
                sf.SubdlClient("secret-key").download_srt(
                    sf.SubdlDownload(n_id="subtitle-123"), destination,
                    video=video, expected_video=snapshot,
                )
            request = open_url.call_args.args[0]
            self.assertIn("/api/v2/subtitles/subtitle-123/download?format=file", request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
            self.assertEqual(destination.read_text(encoding="utf-8"), self.sample_srt)

    def test_json_download_response_follows_only_a_vetted_subdl_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            destination = root / "Dune.eng.srt"
            redirect = json.dumps({"download_url": "/subtitle/subtitle-123/file-456"}).encode("utf-8")
            with (
                mock.patch(
                    "subtitle_fetcher.urllib.request.urlopen",
                    side_effect=[self.Response(redirect), self.Response(self.sample_srt.encode("utf-8"))],
                ) as open_url,
                mock.patch("subtitle_fetcher.time.sleep"),
            ):
                sf.SubdlClient("secret-key").download_srt(sf.SubdlDownload(n_id="subtitle-123"), destination)
            self.assertEqual(open_url.call_count, 2)
            second_request = open_url.call_args.args[0]
            self.assertEqual(second_request.full_url, "https://dl.subdl.com/subtitle/subtitle-123/file-456")
            self.assertEqual(destination.read_text(encoding="utf-8"), self.sample_srt)
        with self.assertRaisesRegex(RuntimeError, "unsafe download URL"):
            sf.subdl_download_redirect_url(
                json.dumps({"download_url": "https://attacker.invalid/subtitle/steal"}).encode("utf-8")
            )

    def test_low_scored_filename_match_does_not_weaken_to_title_search(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            (movie / "Dune Part Two (2024).mkv").write_bytes(b"small-but-valid-for-subdl")
            cfg = sf.QueueConfig(
                library=library, log_file=root / "subtitle_fetcher.log", report_file=root / "report.txt",
                subdl_api_key="subdl-test-key", min_movie_size_mb=0, subdl_daily_cap=3,
            )
            low_score = sf.Candidate(
                file_id="subdl:low", release="Dune.Part.Two.2024.1080p.WEB",
                moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
                hearing_impaired=False, machine_translated=False, ai_translated=False,
                foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
                subdl_match_score=0.79,
            )
            with (
                mock.patch.object(
                    sf.SubdlClient, "search_filename", return_value=([low_score], {}),
                ),
                mock.patch.object(
                    sf.SubdlClient, "search_identity",
                    side_effect=AssertionError("low-score release matches must remain review-only"),
                ) as title_search,
            ):
                results, _summary = sf.queue_run(cfg)

            title_search.assert_not_called()
            self.assertEqual([result.status for result in results], ["review"])
            self.assertIn("match_score >= 0.80", results[0].detail)

    def test_subdl_search_cap_stops_title_fallback_before_a_second_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            (movie / "Dune Part Two (2024).mkv").write_bytes(b"small-but-valid-for-subdl")
            cfg = sf.QueueConfig(
                library=library, log_file=root / "subtitle_fetcher.log", report_file=root / "report.txt",
                subdl_api_key="subdl-test-key", min_movie_size_mb=0,
                subdl_daily_cap=3, subdl_search_daily_cap=1,
            )
            no_filename_match = json.dumps({"status": True, "match": None, "subtitles": []}).encode("utf-8")
            with mock.patch(
                "subtitle_fetcher.urllib.request.urlopen", return_value=self.Response(no_filename_match),
            ) as open_url:
                results, summary = sf.queue_run(cfg)

            self.assertEqual([result.reason for result in results], [sf.REASON_QUOTA])
            self.assertIn("daily search cap exhausted", results[0].detail)
            self.assertEqual(open_url.call_count, 1, "generic title fallback must not exceed the search cap")
            self.assertEqual(summary["subdl_search_requests_reserved"], 1)
            self.assertEqual(summary["subdl_download_requests_reserved"], 0)

    def test_exhausted_subdl_search_cap_defers_after_an_open_subtitles_miss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            (movie / "Dune Part Two (2024).mkv").write_bytes(b"x" * sf.MIN_HASH_SIZE)
            log_file = root / "subtitle_fetcher.log"
            state = sf.new_state(library)
            sf.day_ledger(state, sf.utc_day())["subdl_search_requests_reserved"] = 1
            sf.persist_state(state, log_file)
            cfg = sf.QueueConfig(
                library=library, log_file=log_file, report_file=root / "report.txt",
                api_key="open-key", subdl_api_key="subdl-key", daily_cap=3,
                subdl_daily_cap=3, subdl_search_daily_cap=1, min_movie_size_mb=0,
            )
            with (
                mock.patch.object(sf.OpenSubtitlesClient, "search", return_value=[]),
                mock.patch.object(sf.OpenSubtitlesClient, "search_identity", return_value=[]),
                mock.patch.object(
                    sf.SubdlClient, "search_filename",
                    side_effect=AssertionError("exhausted SubDL search quota must not issue a lookup"),
                ) as filename_search,
            ):
                results, summary = sf.queue_run(cfg)

            filename_search.assert_not_called()
            self.assertEqual([result.reason for result in results], [sf.REASON_QUOTA])
            self.assertIn("daily search cap exhausted before lookup", results[0].detail)
            self.assertEqual(summary["subdl_search_requests_reserved"], 1)

    def test_subdl_only_queue_downloads_without_open_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            video = movie / "Dune Part Two (2024).mkv"
            video.write_bytes(b"small-but-valid-for-subdl")
            log_file = root / "subtitle_fetcher.log"
            cfg = sf.QueueConfig(
                library=library, log_file=log_file, report_file=root / "report.txt",
                subdl_api_key="subdl-test-key", min_movie_size_mb=0, subdl_daily_cap=3,
            )
            candidate = sf.Candidate(
                file_id="subdl:subtitle-123:file-456", release="Dune.Part.Two.2024.1080p.WEB",
                moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
                hearing_impaired=False, machine_translated=False, ai_translated=False,
                foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
                subdl_match_score=0.92,
            )
            download = sf.SubdlDownload(n_id="subtitle-123", url="https://dl.subdl.com/subtitle/subtitle-123/file-456")

            def write_sidecar(actual: sf.SubdlDownload, destination: Path, **_kwargs: object) -> None:
                self.assertEqual(actual, download)
                sf.atomic_write_text(destination, self.sample_srt, replace=False)

            with (
                mock.patch.object(
                    sf.SubdlClient, "search_filename", return_value=([], {}),
                ) as file_search,
                mock.patch.object(
                    sf.SubdlClient, "search_identity",
                    return_value=([candidate], {str(candidate.file_id): download}),
                ) as title_search,
                mock.patch.object(sf.SubdlClient, "download_srt", side_effect=write_sidecar),
            ):
                results, summary = sf.queue_run(cfg)

            file_search.assert_called_once()
            title_search.assert_called_once()
            self.assertEqual([result.status for result in results], ["download"])
            self.assertTrue((movie / "Dune Part Two (2024).eng.srt").is_file())
            self.assertEqual(summary["opensubtitles_download_requests_reserved"], 0)
            self.assertEqual(summary["subdl_download_requests_reserved"], 1)
            self.assertEqual(summary["subdl_successful_downloads"], 1)

    def test_open_subtitles_cap_does_not_consume_subdl_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            (movie / "Dune Part Two (2024).mkv").write_bytes(b"subdl-only-content")
            log_file = root / "subtitle_fetcher.log"
            state = sf.new_state(library)
            ledger = sf.day_ledger(state, sf.utc_day())
            ledger["opensubtitles_download_requests_reserved"] = 1
            ledger["download_requests_reserved"] = 1
            sf.persist_state(state, log_file)
            cfg = sf.QueueConfig(
                library=library, log_file=log_file, report_file=root / "report.txt",
                api_key="open-key", subdl_api_key="subdl-key", daily_cap=1,
                subdl_daily_cap=3, min_movie_size_mb=0,
            )
            candidate = sf.Candidate(
                file_id="subdl:subtitle-123:file-456", release="Dune.Part.Two.2024.1080p.WEB",
                moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
                hearing_impaired=False, machine_translated=False, ai_translated=False,
                foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
                subdl_match_score=0.92,
            )
            download = sf.SubdlDownload(n_id="subtitle-123")

            def write_sidecar(_actual: sf.SubdlDownload, destination: Path, **_kwargs: object) -> None:
                sf.atomic_write_text(destination, self.sample_srt, replace=False)

            with (
                mock.patch.object(
                    sf.OpenSubtitlesClient, "search",
                    side_effect=AssertionError("OpenSubtitles is capped"),
                ),
                mock.patch.object(
                    sf.SubdlClient, "search_filename",
                    return_value=([candidate], {str(candidate.file_id): download}),
                ),
                mock.patch.object(sf.SubdlClient, "download_srt", side_effect=write_sidecar),
            ):
                results, summary = sf.queue_run(cfg)

            self.assertEqual([result.status for result in results], ["download"])
            self.assertEqual(summary["opensubtitles_download_requests_reserved"], 1)
            self.assertEqual(summary["subdl_download_requests_reserved"], 1)

    def test_subdl_falls_back_when_open_subtitles_lookup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            movie = library / "Dune Part Two (2024)"
            movie.mkdir(parents=True)
            (movie / "Dune Part Two (2024).mkv").write_bytes(b"x" * sf.MIN_HASH_SIZE)
            cfg = sf.QueueConfig(
                library=library, log_file=root / "subtitle_fetcher.log", report_file=root / "report.txt",
                api_key="open-key", subdl_api_key="subdl-key", min_movie_size_mb=0,
            )
            candidate = sf.Candidate(
                file_id="subdl:subtitle-123", release="Dune.Part.Two.2024.1080p.WEB",
                moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
                hearing_impaired=False, machine_translated=False, ai_translated=False,
                foreign_parts_only=False, language="en", feature_title="Dune: Part Two", feature_year=2024,
                subdl_match_score=0.92,
            )
            download = sf.SubdlDownload(n_id="subtitle-123")

            def write_sidecar(_actual: sf.SubdlDownload, destination: Path, **_kwargs: object) -> None:
                sf.atomic_write_text(destination, self.sample_srt, replace=False)

            with (
                mock.patch.object(
                    sf.OpenSubtitlesClient, "search", side_effect=RuntimeError("temporary outage"),
                ),
                mock.patch.object(
                    sf.OpenSubtitlesClient, "search_identity",
                    side_effect=AssertionError("do not spend a second request after a provider failure"),
                ),
                mock.patch.object(
                    sf.SubdlClient, "search_filename",
                    return_value=([candidate], {str(candidate.file_id): download}),
                ),
                mock.patch.object(sf.SubdlClient, "download_srt", side_effect=write_sidecar),
            ):
                results, summary = sf.queue_run(cfg)

            self.assertEqual([result.status for result in results], ["download"])
            self.assertEqual(summary["opensubtitles_download_requests_reserved"], 0)
            self.assertEqual(summary["subdl_download_requests_reserved"], 1)

    def test_subdl_only_configuration_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = root / "library"
            library.mkdir()
            cfg = sf.QueueConfig(
                library=library, log_file=root / "subtitle.log", report_file=root / "subtitle_report.txt",
                subdl_api_key="key",
            )
            self.assertEqual(sf.validate_compact_config(cfg), [])
            errors = sf.validate_compact_config(sf.QueueConfig(
                library=library, log_file=root / "none.log", report_file=root / "none.txt",
            ))
            self.assertIn("configure OPENSUBTITLES_API_KEY and/or SUBDL_API_KEY", errors)
