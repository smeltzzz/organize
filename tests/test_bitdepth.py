"""Tests for the pure 10-bit / HDR classification in ``bitdepth.py``."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import bitdepth as tb

MediaProbeCache = tb.MediaProbeCache
atomic_write_text = tb.atomic_write_text
path_norm = tb.path_norm
format_bytes = tb.format_bytes
format_duration = tb.format_duration


class BitDepthTests(unittest.TestCase):
    def test_pix_fmt_bit_depth(self) -> None:
        self.assertEqual(tb.bit_depth_from_pix_fmt("yuv420p10le"), 10)
        self.assertEqual(tb.bit_depth_from_pix_fmt("yuv420p"), 8)
        self.assertIsNone(tb.bit_depth_from_pix_fmt("unknown"))

    def test_resolve_bit_depth(self) -> None:
        depth, evidence = tb.resolve_bit_depth({"pix_fmt": "yuv420p10le"})
        self.assertEqual(depth, 10)
        self.assertIn("yuv420p10le", evidence)


class CategorizeTests(unittest.TestCase):
    def test_categorize(self) -> None:
        self.assertEqual(tb.categorize(None, False), tb.STATUS_REVIEW_UNKNOWN_DEPTH)
        self.assertEqual(tb.categorize(8, False), tb.STATUS_QUEUE)
        self.assertEqual(tb.categorize(8, True), tb.STATUS_REVIEW_8BIT_HDR)
        self.assertEqual(tb.categorize(10, True), tb.STATUS_SKIP_HDR)
        self.assertEqual(tb.categorize(10, False), tb.STATUS_SKIP_SDR)


class HdrTests(unittest.TestCase):
    def test_hdr_transfer_detected(self) -> None:
        stream = {"color_transfer": "smpte2084", "pix_fmt": "yuv420p10le"}
        is_hdr, flavors, _ = tb.classify_hdr(stream, None)
        self.assertTrue(is_hdr)
        self.assertIn("HDR10", flavors)


class StreamChoiceTests(unittest.TestCase):
    """Which video inside a container gets inspected.

    Reading the wrong one reports the wrong movie's bit depth, and a wrong
    "8-bit" is a re-encode of a file that was already fine.
    """

    def _stream(self, *, index: int, width: int, height: int, **kw: Any) -> dict[str, Any]:
        stream = {
            "index": index, "codec_type": "video", "codec_name": "hevc",
            "width": width, "height": height, "pix_fmt": "yuv420p10le",
            "profile": "Main 10", "bits_per_raw_sample": "10",
        }
        stream.update(kw)
        return stream

    def _payload(self, streams: list[dict[str, Any]]) -> dict[str, Any]:
        return {"streams": streams, "format": {}}

    def test_the_default_stream_beats_a_bigger_bonus_feature(self) -> None:
        """A scope main feature is shorter than a full-frame featurette."""
        main = self._stream(index=0, width=1920, height=816,
                            disposition={"default": 1}, bit_rate="8000000")
        bonus = self._stream(index=1, width=1920, height=1080, pix_fmt="yuv420p",
                             bits_per_raw_sample="8", disposition={"default": 0},
                             bit_rate="3000000")
        for order in ([main, bonus], [bonus, main]):
            stream = tb.pick_video_stream(self._payload(order))
            self.assertEqual(stream["height"], 816, "the featurette must not win")

    def test_size_still_decides_when_the_container_marks_nothing(self) -> None:
        small = self._stream(index=0, width=1280, height=720, pix_fmt="yuv420p",
                             bits_per_raw_sample="8")
        big = self._stream(index=1, width=3840, height=2160)
        stream = tb.pick_video_stream(self._payload([small, big]))
        self.assertEqual(stream["height"], 2160)

    def test_cover_art_is_never_inspected(self) -> None:
        art = {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
               "disposition": {"attached_pic": 1}, "width": 100, "height": 100}
        movie = self._stream(index=1, width=1920, height=1080)
        stream = tb.pick_video_stream(self._payload([art, movie]))
        self.assertEqual(stream["index"], 1)


class BitDepthConflictTests(unittest.TestCase):
    """Two fields can claim different bit depths. Neither is trusted."""

    def _probe(self, **stream: Any) -> Any:
        payload = {"streams": [dict({
            "index": 0, "codec_type": "video", "codec_name": "hevc",
            "pix_fmt": "yuv420p10le", "profile": "Main 10",
        }, **stream)], "format": {}}
        return tb.result_from_probe("/m/Movie.mkv", payload)

    def test_agreeing_metadata_is_not_a_conflict(self) -> None:
        self.assertIsNone(tb.bit_depth_conflict({"bits_per_raw_sample": "10",
                                                 "pix_fmt": "yuv420p10le"}))

    def test_a_10bit_pixel_format_behind_an_8bit_raw_sample_is_reviewed(self) -> None:
        result = self._probe(bits_per_raw_sample="8")
        self.assertEqual(result.status, tb.STATUS_REVIEW_UNKNOWN_DEPTH)
        self.assertIn("not queued", result.bit_depth_evidence)
        self.assertIn("8", result.bit_depth_evidence)
        self.assertIn("10", result.bit_depth_evidence)
        self.assertIn("?", result.info)  # the depth is claimed, not known

    def test_the_reverse_disagreement_is_also_reviewed(self) -> None:
        result = self._probe(bits_per_raw_sample="10", pix_fmt="yuv420p")
        self.assertEqual(result.status, tb.STATUS_REVIEW_UNKNOWN_DEPTH)

    def test_a_conflict_is_never_queued_for_re_encoding(self) -> None:
        result = self._probe(bits_per_raw_sample="8")
        self.assertNotEqual(result.status, tb.STATUS_QUEUE)


class DolbyVisionTests(unittest.TestCase):
    """Dolby Vision is more than one thing: the profile says what plays it."""

    HDR = {"color_transfer": "smpte2084", "color_primaries": "bt2020"}

    def _probe(self, side_data: dict[str, Any] | None = None, **stream: Any) -> Any:
        base = {"index": 0, "codec_type": "video", "codec_name": "hevc",
                "pix_fmt": "yuv420p10le", "profile": "Main 10",
                "bits_per_raw_sample": "10"}
        base.update(self.HDR)
        base.update(stream)
        if side_data is not None:
            base["side_data_list"] = [side_data]
        return tb.result_from_probe("/m/Movie.mkv", {"streams": [base], "format": {}})

    @staticmethod
    def _dovi(profile: int, compat: int, el: int = 0) -> dict[str, Any]:
        return {"side_data_type": "DOVI configuration record", "dv_profile": profile,
                "dv_bl_signal_compatibility_id": compat, "el_present_flag": el,
                "bl_present_flag": 1}

    def test_profile_81_reports_its_hdr10_base(self) -> None:
        result = self._probe(self._dovi(8, 1))
        self.assertIn("Dolby Vision", result.hdr_flavors)
        self.assertEqual(result.status, tb.STATUS_SKIP_HDR)
        self.assertIn("profile 8.1", result.dv_profile)
        self.assertIn("HDR10 base", result.dv_profile)

    def test_profile_5_reports_that_it_has_no_fallback(self) -> None:
        result = self._probe(self._dovi(5, 0))
        self.assertIn("no SDR/HDR10 fallback", result.dv_profile)

    def test_a_dual_layer_profile_says_so(self) -> None:
        result = self._probe(self._dovi(7, 1, el=1))
        self.assertIn("dual layer", result.dv_profile)

    def test_an_hlg_base_layer_is_named(self) -> None:
        result = self._probe(self._dovi(8, 4))
        self.assertIn("profile 8.4", result.dv_profile)

    def test_a_record_without_a_profile_leaves_it_undescribed(self) -> None:
        result = self._probe({"side_data_type": "DOVI configuration record"})
        self.assertIn("Dolby Vision", result.hdr_flavors)
        self.assertEqual(result.dv_profile, "")

    def test_a_dolby_vision_codec_name_is_dolby_vision(self) -> None:
        # Rare, but it means DV by definition even with no record to read.
        result = self._probe(None, codec_name="dvh1", **{"color_transfer": "bt709",
                                                         "color_primaries": "bt709"})
        self.assertIn("Dolby Vision", result.hdr_flavors)
        self.assertTrue(result.hdr)

    def test_a_plain_hevc_file_is_not_dolby_vision(self) -> None:
        result = self._probe(None, **{"color_transfer": "bt709", "color_primaries": "bt709"})
        self.assertNotIn("Dolby Vision", result.hdr_flavors)
        self.assertEqual(result.dv_profile, "")


class ProbeCacheWiringTests(unittest.TestCase):
    """inspect_movie must actually consult the cache, not just accept one."""

    PAYLOAD = {
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264",
            "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
            "color_transfer": "bt709", "color_primaries": "bt709",
            "color_space": "bt709", "duration": "6000.0",
        }],
        "format": {"duration": "6000.0", "size": "1000000", "bit_rate": "1333333"},
    }

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="tbit_cache_")
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.movie = self.root / "Film (2020).mkv"
        self.movie.write_bytes(b"x" * 4096)
        self.calls: list[Path] = []

        def fake_probe(_binary, file_path, _cfg):
            self.calls.append(Path(file_path))
            return dict(self.PAYLOAD)

        self._real = tb.run_ffprobe
        tb.run_ffprobe = fake_probe
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        tb.run_ffprobe = self._real

    def _cache(self, enabled: bool = True):
        return MediaProbeCache(self.root / "cache.json", tool="10bit", enabled=enabled)

    def test_second_call_reuses_the_probe(self) -> None:
        cache = self._cache()
        first = tb.inspect_movie(self.movie, tb.Config(), cache)
        second = tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first.status, second.status)
        self.assertEqual(tb.STATUS_QUEUE, second.status)

    def test_verdict_is_recomputed_not_replayed(self) -> None:
        # The cached payload is reused but the classification runs again, so
        # the result still reflects the current rules.
        cache = self._cache()
        tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(len(self.calls), 1)
        again = tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(again.category, tb.CATEGORY_LABELS[tb.STATUS_QUEUE])

    def test_changed_file_is_reprobed(self) -> None:
        cache = self._cache()
        tb.inspect_movie(self.movie, tb.Config(), cache)
        self.movie.write_bytes(b"y" * 8192)  # size and mtime both change
        tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(len(self.calls), 2)

    def test_no_cache_flag_probes_every_time(self) -> None:
        cache = self._cache(enabled=False)
        tb.inspect_movie(self.movie, tb.Config(), cache)
        tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(len(self.calls), 2)

    def test_probe_failure_is_not_cached(self) -> None:
        cache = self._cache()

        def failing(*_a):
            self.calls.append(Path("boom"))
            raise RuntimeError("ffprobe failed")

        tb.run_ffprobe = failing
        result = tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(result.status, tb.STATUS_ERROR)
        # Restoring the working probe must succeed: the failure was not stored.
        tb.run_ffprobe = lambda _b, _f, _c: dict(self.PAYLOAD)
        recovered = tb.inspect_movie(self.movie, tb.Config(), cache)
        self.assertEqual(recovered.status, tb.STATUS_QUEUE)

    def test_cache_without_a_tool_argument_still_works(self) -> None:
        tb.inspect_movie(self.movie, tb.Config())
        tb.inspect_movie(self.movie, tb.Config())
        self.assertEqual(len(self.calls), 2)


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


class ByteAndDurationFormatterTests(unittest.TestCase):
    def test_byte_and_duration_formatters(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(2048), "2.00 KiB")
        self.assertEqual(format_bytes(5 * 1024 ** 3), "5.00 GiB")
        self.assertEqual(format_duration(None), "-")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
