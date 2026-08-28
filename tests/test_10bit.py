"""Tests for the pure 10-bit / HDR classification in ``10bit.py``.

``10bit.py`` cannot be imported by its real name (it starts with a digit), so
it is loaded on a per-package name and registered in ``sys.modules``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from common import MediaProbeCache

_SCRIPT = Path(__file__).resolve().parents[1] / "10bit.py"
_name = "_tbit"
_spec = importlib.util.spec_from_file_location(_name, _SCRIPT)
tb = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules[_name] = tb
_spec.loader.exec_module(tb)  # type: ignore[union-attr]


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


if __name__ == "__main__":
    unittest.main()
