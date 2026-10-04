"""Degraded paths in the inspector and the auditor.

Both tools are read-only, and that is the invariant every failure here has to
keep: an unreadable file is a report row, a filesystem that answers with an
error is a finding, and a cache that cannot be written is a warning - never a
crash, never a wrong verdict, and never a write into the library.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import bitdepth
import library_auditor as la

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _probe_payload(tracks: list[dict], *, fmt_tags: dict | None = None) -> dict:
    streams = [{"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
                "pix_fmt": "yuv420p", "profile": "Main 10", "tags": {}}]
    streams.extend(tracks)
    payload: dict = {"streams": streams, "format": {"tags": fmt_tags or {}}}
    return payload


class FfprobeDegradationTests(unittest.TestCase):
    """ffprobe is an external process: missing, hanging, or answering nonsense
    are all normal answers to plan for, and each maps to a report row."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_edge_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_an_explicit_binary_that_exists_is_used_before_path(self) -> None:
        explicit = self.root / "ffprobe"
        explicit.write_bytes(b"#!/bin/sh\n")
        with mock.patch.object(bitdepth.shutil, "which", return_value="/usr/bin/ffprobe"):
            self.assertEqual(str(explicit), bitdepth.find_ffprobe(str(explicit)))

    def test_a_path_lookup_that_succeeds_becomes_a_candidate(self) -> None:
        with mock.patch.object(bitdepth.shutil, "which", return_value="/usr/bin/ffprobe"):
            self.assertEqual("/usr/bin/ffprobe", bitdepth.find_ffprobe())

    def test_a_binary_that_cannot_be_started_is_not_working(self) -> None:
        with mock.patch.object(bitdepth.subprocess, "run", side_effect=OSError("no exec")):
            self.assertFalse(bitdepth.ffprobe_works("/usr/bin/nope"))

    def test_a_binary_that_hangs_is_not_working(self) -> None:
        with mock.patch.object(bitdepth.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ffprobe", 10)):
            self.assertFalse(bitdepth.ffprobe_works("/usr/bin/slow"))

    def test_a_launch_failure_is_reported_as_the_probe_failing(self) -> None:
        with mock.patch.object(bitdepth.subprocess, "run", side_effect=OSError("no exec")), \
                self.assertRaises(RuntimeError) as caught:
            bitdepth.run_ffprobe("/usr/bin/nope", self.root / "movie.mkv", bitdepth.Config())
        self.assertIn("failed to launch ffprobe", str(caught.exception))

    def test_json_that_is_not_an_object_is_refused(self) -> None:
        proc = types.SimpleNamespace(returncode=0, stdout="[1, 2]", stderr="")
        with mock.patch.object(bitdepth.subprocess, "run", return_value=proc), \
                self.assertRaises(RuntimeError) as caught:
            bitdepth.run_ffprobe("/usr/bin/ffprobe", self.root / "movie.mkv", bitdepth.Config())
        self.assertIn("not an object", str(caught.exception))

    def test_a_movie_that_cannot_be_statted_is_an_error_row_not_a_crash(self) -> None:
        result = bitdepth.inspect_movie(self.root / "gone.mkv", bitdepth.Config())
        self.assertEqual(bitdepth.STATUS_ERROR, result.status)
        self.assertTrue(result.error)


class ClassificationEdgeTests(unittest.TestCase):
    """The pure classifiers must answer "unknown" rather than guess."""

    def test_numbers_that_are_not_numbers_are_ignored(self) -> None:
        self.assertIsNone(bitdepth._as_int("not a number"))
        self.assertIsNone(bitdepth._as_float("not a number"))

    def test_modern_pixel_formats_carry_their_depth(self) -> None:
        self.assertEqual(10, bitdepth.bit_depth_from_pix_fmt("yuv420p10le"))
        self.assertEqual(12, bitdepth.bit_depth_from_pix_fmt("p012le"))
        self.assertEqual(16, bitdepth.bit_depth_from_pix_fmt("p016le"))

    def test_a_12_bit_profile_is_read_as_twelve_bit(self) -> None:
        self.assertEqual(12, bitdepth.bit_depth_from_profile("Main 12"))

    def test_hdr_format_tags_are_evidence_when_the_transfer_characteristic_is_missing(self) -> None:
        """A muxer that writes only ``HDR_Format`` still tags Dolby Vision and
        HDR10+; the verdict must notice them rather than call the file SDR."""
        tags = {"HDR_Format": "Dolby Vision / SMPTE ST 2086", "HDR_Format_Compatibility": "HDR10+"}
        stream = _probe_payload([], fmt_tags=tags)["streams"][0]
        is_hdr, flavors, evidence = bitdepth.classify_hdr(stream, {"tags": tags})
        self.assertTrue(is_hdr)
        self.assertIn("Dolby Vision", flavors)
        self.assertIn("HDR10+", flavors)
        self.assertTrue(any("HDR format tag" in item for item in evidence))


class DiscoveryEdgeTests(unittest.TestCase):
    """A file that disappears between the walk and the stat is skipped, not
    counted, and never raises: discovery runs on live media libraries."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_discover_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.cfg = bitdepth.Config(source_dir=self.root, min_file_size_mb=0)

    def test_a_junk_name_is_not_a_movie(self) -> None:
        (self.root / "Movie (2000).mkv").write_bytes(b"x" * 32)
        (self.root / ".hidden.mkv").write_bytes(b"x" * 32)
        (self.root / "Movie (2000).sample.mkv").write_bytes(b"x" * 32)
        (self.root / "Movie (2000).mkv.parts").write_bytes(b"x" * 32)
        self.assertEqual(["Movie (2000).mkv"],
                         [p.name for p in bitdepth.discover_videos(self.root, self.cfg)])

    def test_a_file_that_vanishes_before_the_stat_is_skipped(self) -> None:
        movie = self.root / "Gone (2001).mkv"
        movie.write_bytes(b"x" * 32)
        real_stat = Path.stat

        def stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self.name == movie.name:
                raise OSError(errno.ENOENT, "gone")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            self.assertEqual([], bitdepth.discover_videos(self.root, self.cfg))

    def test_a_missing_root_is_an_empty_scan(self) -> None:
        self.assertEqual([], bitdepth.discover_videos(self.root / "nope", self.cfg))


class ReportAndStateDegradationTests(unittest.TestCase):
    """The report and the shared cache are outputs; neither may fail a scan
    that has already read the library, and neither may touch the movies."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_report_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def _result(self, **overrides: object) -> bitdepth.ProbeResult:
        base = {"path": str(self.root / "Movie (2000).mkv"), "status": bitdepth.STATUS_SKIP_SDR,
                "category": "10-bit SDR", "info": "depth 10 from pix_fmt", "size_bytes": 1024,
                "dv_profile": "Profile 8.1", "hdr_flavors": ["HDR10"],
                "chain_video": "HDR10 (tone-mapped)"}
        base.update(overrides)
        return bitdepth.ProbeResult(**base)  # type: ignore[arg-type]

    def test_a_dolby_vision_profile_is_rendered_in_the_entry(self) -> None:
        text = bitdepth.build_report([self._result()], bitdepth.Config(source_dir=self.root), 1.0)
        self.assertIn("Dolby Vision", text)

    def test_an_unwritable_report_is_reported_without_raising(self) -> None:
        cfg = bitdepth.Config(source_dir=self.root, report_file=self.root / "no" / "way" / "r.txt")
        cfg.report_file.parent.mkdir(parents=True)
        cfg.report_file.parent.chmod(0o500)
        self.addCleanup(cfg.report_file.parent.chmod, 0o700)
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("root and Windows ignore the directory mode")
        self.assertFalse(bitdepth.write_report([], cfg, 0.0))

    def test_a_cache_write_that_fails_is_a_warning_not_a_failed_scan(self) -> None:
        cfg = bitdepth.Config(source_dir=self.root, state_db=self.root / "state.db")
        with mock.patch.object(bitdepth, "open_state") as opened:
            opened.return_value.enabled = True
            opened.return_value.record.side_effect = RuntimeError("cache on fire")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                published = bitdepth.publish_state([self._result()], cfg)
        self.assertEqual(0, published)


class BitdepthCliEdgeTests(unittest.TestCase):
    """The driver's own refusals: a config error is an exit code, and a source
    that does not exist is named rather than discovered as "no movies"."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="bd_cli_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_a_worker_count_of_zero_is_a_config_error(self) -> None:
        cfg = bitdepth.Config(source_dir=self.root, workers=0)
        self.assertIn("--workers must be greater than zero", bitdepth.validate_config(cfg))

    def test_a_source_that_does_not_exist_exits_2(self) -> None:
        cfg = bitdepth.Config(source_dir=self.root / "gone", log_file=self.root / "l.log",
                              report_file=self.root / "r.txt")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = bitdepth.scan(cfg)
        self.assertEqual(2, code)

    def test_a_dry_run_names_the_files_and_probes_nothing(self) -> None:
        (self.root / "Movie (2000).mkv").write_bytes(b"x" * 64)
        cfg = bitdepth.Config(source_dir=self.root, log_file=self.root / "l.log",
                              report_file=self.root / "r.txt", dry_run=True, workers=1,
                              min_file_size_mb=0, use_cache=False, use_state=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = bitdepth.scan(cfg)
        self.assertEqual(0, code)
        self.assertIn("dry-run", out.getvalue())


class AuditorFolderEdgeTests(unittest.TestCase):
    """Classification is a series of looks at the filesystem, and every one of
    them can fail. Each failure becomes a finding, never an exception."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="la_edge_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.folder = self.root / "Movie (2000)"
        self.folder.mkdir()
        (self.folder / "Movie (2000).mkv").write_bytes(b"x" * 32)

    def test_a_folder_that_cannot_be_listed_is_inaccessible(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=OSError(errno.EACCES, "denied")):
            files, reason = la.direct_movie_files(self.folder)
        self.assertEqual([], files)
        self.assertIn("denied", reason)
        with mock.patch.object(Path, "iterdir", side_effect=OSError(errno.EACCES, "denied")):
            self.assertEqual("INACCESSIBLE", la.classify_folder(self.folder).state)

    def test_a_movie_that_cannot_be_statted_makes_the_folder_inaccessible(self) -> None:
        """The file passes the is_file() look and then vanishes before the size
        is read - the second look is the one that has to be guarded."""
        real_stat = Path.stat
        calls = {"n": 0}

        def stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self.suffix == ".mkv":
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError(errno.EIO, "I/O error")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            files, reason = la.direct_movie_files(self.folder)
        self.assertEqual([], files)
        self.assertIn("cannot stat", reason)

    def test_a_sidecar_listing_that_fails_makes_the_folder_inaccessible(self) -> None:
        real_iterdir = Path.iterdir
        calls = {"n": 0}

        def iterdir(self: Path) -> object:
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(errno.EACCES, "denied")
            return real_iterdir(self)

        with mock.patch.object(Path, "iterdir", iterdir):
            audit = la.classify_folder(self.folder)
        self.assertEqual("INACCESSIBLE", audit.state)

    def test_a_canonical_path_occupied_by_a_directory_is_a_noncanonical_finding(self) -> None:
        """A directory squatting on ``Movie (2000).eng.srt`` blocks promotion;
        the audit has to say so rather than call the folder canonical."""
        (self.folder / "Movie (2000).eng.srt").mkdir()
        (self.folder / "Movie (2000).en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        audit = la.classify_folder(self.folder)
        self.assertEqual("NONCANONICAL_SIDECAR", audit.state)
        self.assertIn("could not be promoted", audit.detail)


class AuditorLibraryEdgeTests(unittest.TestCase):
    """The library-level walk: an unenumerable root, a folder whose
    classification raises, and a cache write that fails."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="la_lib_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.library = self.root / "lib"
        self.library.mkdir()
        self.cfg = la.Config(source_dir=self.library, log_file=self.root / "l.log",
                             report_file=self.root / "r.txt")

    def test_a_library_that_cannot_be_enumerated_audits_as_empty(self) -> None:
        with mock.patch.object(Path, "iterdir", side_effect=OSError(errno.EACCES, "denied")):
            audit = la.audit_library(self.cfg)
        self.assertEqual([], audit.folders)

    def test_a_defect_in_classification_is_raised_not_hidden(self) -> None:
        """"classify_folder swallows the errors it expects", so anything that
        escapes it is a bug and must not be audited as a clean folder."""
        self._movie("Movie (2000)")
        with mock.patch.object(la, "classify_folder", side_effect=RuntimeError("defect")), \
                self.assertRaises(RuntimeError):
            la.audit_library(self.cfg)

    def _movie(self, title: str) -> Path:
        folder = self.library / title
        folder.mkdir()
        movie = folder / f"{title}.mkv"
        movie.write_bytes(b"x" * 64)
        (folder / f"{title}.eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        return movie

    def test_a_folder_with_no_single_movie_is_skipped_by_the_cache_publish(self) -> None:
        folder = self.library / "Multi (2001)"
        folder.mkdir()
        (folder / "Multi (2001) - 1080p.mkv").write_bytes(b"x" * 32)
        (folder / "Multi (2001) - 2160p.mkv").write_bytes(b"x" * 32)
        audit = la.audit_library(self.cfg)
        db = self.root / "state.db"
        with la.open_state(db, tool="audit") as store:
            published = la.publish_state(audit, self.cfg)
            self.assertEqual(0, published)
            self.assertEqual({}, store.movies())

    def test_a_cache_write_that_fails_is_a_warning_not_a_failed_audit(self) -> None:
        self._movie("Movie (2000)")
        audit = la.audit_library(self.cfg)
        with mock.patch.object(la, "open_state") as opened:
            opened.return_value.enabled = True
            opened.return_value.see_movies.side_effect = RuntimeError("cache on fire")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                published = la.publish_state(audit, self.cfg)
        self.assertEqual(1, published, "the movies still exist; only the cache failed")

    def test_an_unknown_state_gets_a_readable_label(self) -> None:
        self.assertEqual("SOME NEW THING", la._state_label("SOME_NEW_THING"))


class AuditorConfigEdgeTests(unittest.TestCase):
    def test_log_and_report_inside_the_library_are_config_errors(self) -> None:
        cfg = la.Config(source_dir=Path("/tmp/lib"), log_file=Path("/tmp/lib/a.log"),
                        report_file=Path("/tmp/lib/a.log"), lock_timeout_seconds=-1,
                        workers=-1)
        errors = la.validate_config(cfg)
        self.assertTrue(any("--lock-timeout" in e for e in errors))
        self.assertTrue(any("--log must be outside" in e for e in errors))
        self.assertTrue(any("--log and --report must be different" in e for e in errors))
        self.assertTrue(any("--workers" in e for e in errors))

    def test_an_interrupt_is_130_and_a_defect_is_1(self) -> None:
        with mock.patch.object(la, "run", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(130, la.main(["--source", "/tmp"]))
        with mock.patch.object(la, "run", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, la.main(["--source", "/tmp"]))

    def test_the_self_test_flag_runs_the_field_smoke_test(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = la.main(["--self-test"])
        self.assertEqual(0, code)
        self.assertIn("SELF-TEST PASSED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
