"""Tests for the pure 10-bit / HDR classification in ``10bit.py``.

``10bit.py`` cannot be imported by its real name (it starts with a digit), so
it is loaded on a per-package name and registered in ``sys.modules``.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
