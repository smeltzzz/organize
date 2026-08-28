"""Unit tests for the shared ``common`` infrastructure module.

Runs with only the standard library:

    python3 -m unittest discover -s tests
    # or, with pytest installed:
    pytest tests/test_common.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import common
from common import (
    EXTERNAL_SRT_ENCODINGS,
    EXTERNAL_SRT_MAX_BYTES,
    CoordinationLock,
    LockTimeoutError,
    MediaProbeCache,
    STANDARDIZER_LOCK_NAME,
    atomic_write_text,
    decode_srt_bytes,
    normalize_srt_newlines,
    path_is_within,
    path_norm,
    paths_equal,
    srt_looks_valid,
    validate_srt_sidecar,
)


class AtomicWriteTextTests(unittest.TestCase):
    def test_writes_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            atomic_write_text(target, "world")
            self.assertEqual(target.read_text(encoding="utf-8"), "world")

    def test_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b" / "report.txt"
            atomic_write_text(target, "x")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), "x")

    def test_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            atomic_write_text(target, "data")
            self.assertEqual([p.name for p in Path(td).iterdir()], ["report.txt"])


class PathHelpersTests(unittest.TestCase):
    def test_path_is_within(self) -> None:
        root = Path("/data/library")
        self.assertTrue(path_is_within(Path("/data/library/movie"), root))
        self.assertTrue(path_is_within(root, root))
        self.assertFalse(path_is_within(Path("/data/other/movie"), root))
        self.assertFalse(path_is_within(Path("/other"), root))

    def test_path_norm_and_equal(self) -> None:
        left = Path("/tmp/./media/../media/film.mkv")
        right = Path("/tmp/media/film.mkv")
        # normpath collapses the .. and duplicate segments.
        self.assertEqual(path_norm(left), path_norm(right))

    def test_paths_equal_samefile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.mkv"
            f.write_bytes(b"x")
            link = Path(td) / "b.mkv"
            try:
                link.hardlink_to(f)
            except OSError:
                self.skipTest("hardlink not supported on this filesystem")
            self.assertTrue(paths_equal(f, link))


class CoordinationLockTests(unittest.TestCase):
    def test_acquire_release_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with CoordinationLock(Path(td) / "lib", timeout_seconds=10.0):
                pass  # no exception means the lock was taken and released cleanly

    def test_path_is_deterministic(self) -> None:
        # The lock path must be identical for identical normalized targets so the
        # standardizer, cleaner and subtitle fetcher contend on the same file.
        # normpath collapses "." and ".."; on Windows normcase also lower-cases,
        # which is exactly what makes the tools agree on a shared key.
        lock_a = CoordinationLock(Path("/Data/./Library"))
        lock_b = CoordinationLock("/Data/Library")
        self.assertEqual(lock_a.path, lock_b.path)
        self.assertIn(STANDARDIZER_LOCK_NAME, lock_a.path.name)


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


class MediaProbeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="probe_cache_")
        self.path = Path(self._td.name) / "cache.json"
        self.addCleanup(self._td.cleanup)

    def test_cold_cache_is_a_miss(self) -> None:
        self.assertIsNone(MediaProbeCache(self.path, tool="t").get("a.mkv", 10, 1))

    def test_warm_cache_is_a_hit(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"streams": []})
        self.assertEqual(cache.get("a.mkv", 10, 1), {"streams": []})
        self.assertEqual((cache.hits, cache.misses), (1, 0))

    def test_size_change_invalidates(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"streams": []})
        self.assertIsNone(cache.get("a.mkv", 11, 1))

    def test_mtime_change_invalidates(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"streams": []})
        self.assertIsNone(cache.get("a.mkv", 10, 2))

    def test_survives_a_reload(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"streams": [{"id": 0}]})
        cache.save()
        self.assertEqual(MediaProbeCache(self.path, tool="t").get("a.mkv", 10, 1),
                         {"streams": [{"id": 0}]})

    def test_save_is_a_noop_when_nothing_changed(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.save()
        self.assertFalse(self.path.exists())

    def test_a_different_tool_cache_is_not_reused(self) -> None:
        cache = MediaProbeCache(self.path, tool="10bit")
        cache.put("a.mkv", 10, 1, {"streams": []})
        cache.save()
        self.assertIsNone(MediaProbeCache(self.path, tool="mkv_track_cleaner").get("a.mkv", 10, 1))

    def test_corrupt_cache_degrades_to_a_miss(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        cache = MediaProbeCache(self.path, tool="t")
        self.assertIsNone(cache.get("a.mkv", 10, 1))
        cache.put("a.mkv", 10, 1, {"streams": []})
        self.assertEqual(cache.get("a.mkv", 10, 1), {"streams": []})

    def test_truncated_json_degrades_to_a_miss(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"streams": []})
        cache.save()
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw[: len(raw) // 2], encoding="utf-8")
        self.assertIsNone(MediaProbeCache(self.path, tool="t").get("a.mkv", 10, 1))

    def test_foreign_schema_is_ignored(self) -> None:
        import json as _json

        self.path.write_text(_json.dumps(
            {"schema": MediaProbeCache.SCHEMA + 1, "tool": "t",
             "entries": {"a.mkv": {"size": 10, "mtime_ns": 1, "payload": {}}}}
        ), encoding="utf-8")
        self.assertIsNone(MediaProbeCache(self.path, tool="t").get("a.mkv", 10, 1))

    def test_disabled_cache_never_reads_writes_or_persists(self) -> None:
        cache = MediaProbeCache(self.path, tool="t", enabled=False)
        cache.put("a.mkv", 10, 1, {"streams": []})
        cache.save()
        self.assertIsNone(cache.get("a.mkv", 10, 1))
        self.assertFalse(self.path.exists())

    def test_evicts_oldest_past_the_cap(self) -> None:
        cache = MediaProbeCache(self.path, tool="t", max_entries=3)
        for index in range(5):
            cache.put(f"m{index}.mkv", 10, 1, {"i": index})
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get("m0.mkv", 10, 1))
        self.assertEqual(cache.get("m4.mkv", 10, 1), {"i": 4})

    def test_reinsert_refreshes_recency(self) -> None:
        cache = MediaProbeCache(self.path, tool="t", max_entries=2)
        cache.put("a.mkv", 10, 1, {"i": "a"})
        cache.put("b.mkv", 10, 1, {"i": "b"})
        cache.put("a.mkv", 10, 1, {"i": "a2"})  # refresh a
        cache.put("c.mkv", 10, 1, {"i": "c"})   # should evict b, not a
        self.assertEqual(cache.get("a.mkv", 10, 1), {"i": "a2"})
        self.assertIsNone(cache.get("b.mkv", 10, 1))
        self.assertEqual(cache.get("c.mkv", 10, 1), {"i": "c"})

    def test_keys_are_path_normalized(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("/lib/Film (2020)/Film (2020).mkv", 10, 1, {"ok": True})
        self.assertEqual(cache.get("/lib/Film (2020)//Film (2020).mkv", 10, 1), {"ok": True})

    def test_nondict_payload_is_ignored(self) -> None:
        cache = MediaProbeCache(self.path, tool="t")
        cache.put("a.mkv", 10, 1, {"ok": True})
        cache.save()
        import json as _json

        doc = _json.loads(self.path.read_text(encoding="utf-8"))
        doc["entries"][path_norm("a.mkv")]["payload"] = "not a dict"
        self.path.write_text(_json.dumps(doc), encoding="utf-8")
        self.assertIsNone(MediaProbeCache(self.path, tool="t").get("a.mkv", 10, 1))


class SingleSourceContractTests(unittest.TestCase):
    """No tool may keep a private copy of the subtitle contract.

    ``subtitle_fetcher.looks_like_srt`` used to be a fifth, drifted copy: it
    anchored the cue number at column 0, so it rejected indented cues that the
    other four tools accepted, and a perfectly good download was refused at the
    door. These tests fail if that ever happens again.
    """

    @staticmethod
    def _verdicts(text: str) -> dict[str, bool]:
        import library_auditor  # noqa: F401  (imported so a break there is caught)
        import mkv_track_cleaner as tc
        import movie_standardizer as ms
        import subtitle_fetcher as sf

        normalized = normalize_srt_newlines(text)
        return {
            "common": srt_looks_valid(normalized),
            "movie_standardizer": ms.EXTERNAL_SRT_CUE_RE.search(normalized) is not None,
            "mkv_track_cleaner": tc.EXTERNAL_SRT_CUE_RE.search(normalized) is not None,
            "subtitle_fetcher": sf.looks_like_srt(normalized),
        }

    def test_every_tool_agrees_on_a_plain_cue(self) -> None:
        verdicts = self._verdicts(PLAIN_CUE)
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_an_indented_cue(self) -> None:
        verdicts = self._verdicts(INDENTED_CUE)
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_crlf_line_endings(self) -> None:
        verdicts = self._verdicts(CRLF_CUE)
        self.assertEqual(set(verdicts.values()), {True}, verdicts)

    def test_every_tool_agrees_on_junk(self) -> None:
        verdicts = self._verdicts(NOT_A_CUE)
        self.assertEqual(set(verdicts.values()), {False}, verdicts)

    def test_no_tool_keeps_its_own_size_limit(self) -> None:
        import mkv_track_cleaner as tc
        import movie_standardizer as ms
        import subtitle_fetcher as sf

        self.assertEqual(ms.EXTERNAL_SRT_MAX_BYTES, EXTERNAL_SRT_MAX_BYTES)
        self.assertEqual(tc.EXTERNAL_SRT_MAX_BYTES, EXTERNAL_SRT_MAX_BYTES)
        self.assertEqual(sf.MAX_SUBTITLE_BYTES, EXTERNAL_SRT_MAX_BYTES)

    def test_no_tool_keeps_its_own_cue_pattern(self) -> None:
        import mkv_track_cleaner as tc
        import movie_standardizer as ms

        self.assertIs(ms.EXTERNAL_SRT_CUE_RE, common.EXTERNAL_SRT_CUE_RE)
        self.assertIs(tc.EXTERNAL_SRT_CUE_RE, common.EXTERNAL_SRT_CUE_RE)

    def test_no_tool_keeps_its_own_encoding_list(self) -> None:
        import subtitle_fetcher as sf

        # cp1252 bytes must decode identically everywhere.
        raw = b"it\x92s fine"
        self.assertEqual(decode_srt_bytes(raw), sf.decode_subtitle_bytes(raw))


if __name__ == "__main__":
    unittest.main()
