"""End-to-end runs of ``subtitle_extractor.main()`` against a fake MKVToolNix.

The extractor's other suites drive ``subprocess`` through a stand-in callable,
which tests every decision but none of the plumbing: the argument list it really
sends ``mkvextract``, the temp file that comes back, the conversion of that
file, the validation, the atomic publish beside the movie, and the ledger entry
that records where the sidecar came from. This module runs the tool unmodified
against ``tests/fake_mkvmerge.py``, launched as a real child process, with a
real library on disk and no mocks in the extraction path.

The provider tier is the one exception: an OpenSubtitles lookup is served by a
fake transport rather than a network, because the suite must stay offline. The
local extraction chain - which is the part this feature is built around - is
entirely real.

Like the cleaner's and the inspector's end-to-end suites, the fake is launched
through a POSIX shebang, so these tests are POSIX-only; the in-process suites
cover the same decisions on every platform.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fake_mkvmerge as fake
import fakebin
import fakeprovider

import subtitle_extractor as sx

WINDOWS = os.name == "nt"
GOOD_KEY = "test-api-key"


def write_movie(path: Path, tracks: list[dict], *, size: int = 300_000) -> None:
    fake.write_movie(path, fake.make_spec(tracks), size=size)


class ExtractorRunFixture(unittest.TestCase):
    """A canonical library, a PATH full of fakes, and one command line."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sx_e2e_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.bin = self.tmp / "bin"
        fakebin.install_python_shim(self.bin, "mkvmerge", "fake_mkvmerge")
        fakebin.install_python_shim(self.bin, "mkvextract", "fake_mkvmerge")
        self.library = self.tmp / "Movies"
        self.reports = self.tmp / "reports"
        self._saved_env = {name: os.environ.get(name) for name in
                           (sx.EXTRACTED_LEDGER_ENV, "OPENSUBTITLES_API_KEY",
                            "OPENSUBTITLES_USERNAME", "OPENSUBTITLES_PASSWORD")}
        for name in self._saved_env:
            os.environ.pop(name, None)
        os.environ[sx.EXTRACTED_LEDGER_ENV] = str(self.tmp / "extracted.json")
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def movie(self, folder: str, tracks: list[dict], *, name: str | None = None) -> Path:
        directory = self.library / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name or f"{folder}.mkv")
        # Above the 300 MB default? No: tests pass --min-size 0, which is the
        # same knob an operator with a library of short films would use.
        write_movie(path, tracks)
        return path

    def run_tool(self, *extra: str) -> tuple[int, str, str]:
        """Call the tool the way the command line does, with the fakes on PATH."""
        # main() takes the arguments after the program name, like any argv[1:].
        argv = ["--source", str(self.library),
                "--report", str(self.reports / "report.txt"),
                "--log", str(self.reports / "run.log"),
                "--min-size", "0", "--extract-min-cues", "2", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(os, "environ", dict(os.environ, PATH=str(self.bin) + os.pathsep
                                                   + os.environ.get("PATH", ""))), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sx.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def sidecar(self, movie: Path) -> Path:
        return movie.with_suffix(".eng.srt")

    def ledger(self) -> dict:
        path = Path(os.environ[sx.EXTRACTED_LEDGER_ENV])
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@unittest.skipIf(WINDOWS, "the fake MKVToolNix is launched through a POSIX shebang")
class RealExtractionTests(ExtractorRunFixture):
    """The sidecar contract, proven through a real subprocess."""

    def test_an_ass_track_becomes_a_validated_srt_beside_the_movie(self) -> None:
        movie = self.movie("The Matrix (1999)", [
            fake.video_track(),
            fake.audio_track(default=True),
            fake.subtitle_track(codec="ASS", codec_id="S_TEXT/ASS"),
        ])
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, f"exit {code}\n{out}\n{err}")
        sidecar = self.sidecar(movie)
        self.assertTrue(sidecar.is_file(), "the sidecar is written beside the movie")
        self.assertEqual(sidecar.parent, movie.parent)
        text = sidecar.read_text(encoding="utf-8")
        self.assertIn("There is no spoon.", text, "the ASS dialogue was converted")
        self.assertNotIn("[Script Info]", text, "what lands on disk is SRT, not ASS")
        self.assertTrue(text.startswith("1\n"), "cues are renumbered from 1")
        self.assertNotIn("\r", text, "newlines are canonical")
        self.assertEqual(sidecar.read_bytes()[:3], b"1\n0", "no BOM is written")

    def test_an_srt_track_is_extracted_verbatim_enough(self) -> None:
        movie = self.movie("Heat (1995)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, f"exit {code}\n{out}\n{err}")
        text = self.sidecar(movie).read_text(encoding="utf-8")
        self.assertIn("I know kung fu.", text)
        self.assertIn("00:00:01,000 --> 00:00:03,000", text)

    def test_the_ledger_says_which_track_the_sidecar_came_from(self) -> None:
        movie = self.movie("Dune (2021)", [
            fake.video_track(), fake.audio_track(default=True),
            fake.subtitle_track(codec="ASS", codec_id="S_TEXT/ASS"),
        ])
        self.assertEqual(self.run_tool()[0], 0)
        records = json.dumps(self.ledger())
        self.assertIn(movie.name, records)
        self.assertIn("embedded", records, "provenance names the method")
        self.assertIn("S_TEXT/ASS", records, "and the exact codec it came from")

    def test_an_existing_sidecar_is_never_rewritten(self) -> None:
        movie = self.movie("Oldboy (2003)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        placed = self.sidecar(movie)
        placed.write_text("1\n00:00:01,000 --> 00:00:02,000\nHand placed.\n\n", encoding="utf-8")
        before = placed.read_bytes()
        code, _out, _err = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(placed.read_bytes(), before, "the sidecar is authoritative")

    def test_an_image_only_movie_gets_no_sidecar_and_is_named_in_the_report(self) -> None:
        """OCR is gone: a bitmap track is reported, never transcribed."""
        movie = self.movie("Akira (1988)", [
            fake.video_track(), fake.audio_track(default=True),
            fake.subtitle_track(codec="HDMV PGS", codec_id="S_HDMV/PGS"),
        ])
        code, out, _err = self.run_tool()
        self.assertEqual(code, 0)
        self.assertFalse(self.sidecar(movie).exists(), "no sidecar for bitmaps")
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("Akira (1988)", report)
        self.assertIn("API key", report, "the report names the fix for image-only movies")

    def test_a_failed_extraction_leaves_nothing_behind(self) -> None:
        """mkvextract's failure must not publish a half-sidecar."""
        movie = self.movie("Broken (2001)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        os.environ["FAKE_MKVEXTRACT_RC"] = "7"
        self.addCleanup(os.environ.pop, "FAKE_MKVEXTRACT_RC", None)
        code, _out, _err = self.run_tool()
        self.assertEqual(code, 0, "a single failed movie is reported, not fatal")
        self.assertFalse(self.sidecar(movie).exists())
        self.assertEqual(list(movie.parent.glob("*.partial*")), [])
        self.assertEqual(list(movie.parent.glob(".*")), [], "no staging files in the library")

    def test_the_run_is_repeatable_and_idempotent(self) -> None:
        movie = self.movie("Alien (1979)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        self.assertEqual(self.run_tool()[0], 0)
        first = self.sidecar(movie).read_bytes()
        self.assertEqual(self.run_tool()[0], 0)
        self.assertEqual(self.sidecar(movie).read_bytes(), first, "second run changes nothing")

    def test_an_unwritable_folder_is_refused_without_asking_the_provider(self) -> None:
        movie = self.movie("Readonly (2020)", [
            fake.video_track(), fake.audio_track(default=True),
            fake.subtitle_track(codec="HDMV PGS", codec_id="S_HDMV/PGS"),
        ])
        os.chmod(movie.parent, 0o500)
        self.addCleanup(os.chmod, movie.parent, 0o700)
        os.environ["OPENSUBTITLES_API_KEY"] = GOOD_KEY
        # A transport that fails the test if anything ever reaches it: the
        # point is that the folder is checked *before* the provider is asked,
        # so an unwritable library costs no downloads.
        transport = fakeprovider.hash_search_transport()
        try:
            with mock.patch.object(sx, "urlopen", transport):
                code, out, _err = self.run_tool()
        finally:
            os.environ.pop("OPENSUBTITLES_API_KEY", None)
        combined = out + (self.reports / "run.log").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("not writable", combined)
        self.assertEqual(transport.requests, [], "the provider was never asked")
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("LOOKUP FAILED", report)
        self.assertIn("Readonly (2020)", report)

    def test_an_mp4_is_read_through_the_bridge(self) -> None:
        """A non-Matroska container goes through a temporary subtitle-only MKV."""
        folder = self.library / "Bridge (2015)"
        folder.mkdir(parents=True)
        movie = folder / "Bridge (2015).mp4"
        write_movie(movie, [fake.video_track(), fake.audio_track(default=True),
                            fake.subtitle_track()])
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, f"exit {code}\n{out}\n{err}")
        self.assertTrue(self.sidecar(movie).is_file(), "the sidecar sits beside the .mp4")
        self.assertIn("I know kung fu.", self.sidecar(movie).read_text(encoding="utf-8"))
        self.assertEqual([p for p in folder.iterdir() if p.name != movie.name],
                         [self.sidecar(movie)], "the bridge is not left in the library")


@unittest.skipIf(WINDOWS, "the fake MKVToolNix is launched through a POSIX shebang")
class RealReportTests(ExtractorRunFixture):
    """What the operator is told, from a real run."""

    def test_the_scorecard_counts_the_extraction(self) -> None:
        self.movie("Counted (2010)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        self.assertEqual(self.run_tool()[0], 0)
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("1/1 (100.0%)", report)
        self.assertIn("EXTRACTED FROM THE MOVIE'S OWN EMBEDDED TRACK", report)
        self.assertIn("Coverage this run: 1 of 1 movie(s)", report)

    def test_a_filtered_library_is_not_reported_as_coverage(self) -> None:
        """The regression this suite was written alongside: 0 movies, green report."""
        self.movie("Tiny (2020)", [
            fake.video_track(), fake.audio_track(default=True), fake.subtitle_track(),
        ])
        argv = ["--source", str(self.library),
                "--report", str(self.reports / "report.txt"),
                "--log", str(self.reports / "run.log"),
                "--min-size", "5000", "--extract-min-cues", "2"]
        with mock.patch.object(os, "environ", dict(os.environ, PATH=str(self.bin) + os.pathsep
                                                   + os.environ.get("PATH", ""))), \
                contextlib.redirect_stdout(io.StringIO()):
            code = sx.main(argv)
        self.assertEqual(code, 0)
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("Nothing was inspected", report)
        self.assertNotIn("Nothing to do", report)
        self.assertNotIn("100.0%", report)

    def test_the_provider_tier_is_mocked_but_reachable_from_a_real_run(self) -> None:
        """The one networked branch: a fake transport, everything else real.

        An image-only movie with a key configured takes the whole route -
        moviehash, search, download, validation, atomic publish - with the
        local toolchain standing in as a real subprocess. Only the three HTTP
        answers are canned, because the suite is offline by contract.
        """
        movie = self.movie("Online (2019)", [
            fake.video_track(), fake.audio_track(default=True),
            fake.subtitle_track(codec="HDMV PGS", codec_id="S_HDMV/PGS"),
        ])
        transport = fakeprovider.hash_search_transport(payload=fakeprovider.FULL_SRT_PAYLOAD)
        os.environ["OPENSUBTITLES_API_KEY"] = GOOD_KEY
        try:
            with mock.patch.object(sx, "urlopen", transport):
                code, out, _err = self.run_tool()
        finally:
            os.environ.pop("OPENSUBTITLES_API_KEY", None)
        self.assertEqual(code, 0, out)
        sidecar = self.sidecar(movie)
        self.assertTrue(sidecar.is_file(), "the downloaded subtitle is installed")
        self.assertIn("Line number 1 of dialogue", sidecar.read_text(encoding="utf-8"))
        urls = transport.urls()
        self.assertEqual(len(urls), 3, f"search, download request, CDN fetch: {urls}")
        self.assertIn("moviehash_match=only", urls[0])
        self.assertIn("moviehash=", urls[0])
        self.assertEqual(urls[2], "https://dl.opensubtitles.com/download/abc/Fake.2021.srt")
        # The subtitle came from the provider, and the ledger says so.
        self.assertIn("download", json.dumps(self.ledger()))

    def test_a_movie_the_provider_does_not_match_gets_no_sidecar(self) -> None:
        movie = self.movie("Unmatched (2018)", [
            fake.video_track(), fake.audio_track(default=True),
            fake.subtitle_track(codec="HDMV PGS", codec_id="S_HDMV/PGS"),
        ])
        transport = fakeprovider.FakeTransport(fakeprovider.search_answer())
        os.environ["OPENSUBTITLES_API_KEY"] = GOOD_KEY
        try:
            with mock.patch.object(sx, "urlopen", transport):
                code, _out, _err = self.run_tool()
        finally:
            os.environ.pop("OPENSUBTITLES_API_KEY", None)
        self.assertEqual(code, 0)
        self.assertFalse(self.sidecar(movie).exists(), "an empty search installs nothing")
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("Unmatched (2018)", report)
        self.assertIn("no exact-hash", report)


if __name__ == "__main__":
    unittest.main()
