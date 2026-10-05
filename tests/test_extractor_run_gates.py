"""``subtitle_extractor.py`` at its gates: argv, one pass over a messy library, downloads.

The extraction decisions have their own suites; this one covers what the tool
does *around* them, which is where a nightly run either tells the truth or does
damage:

* **A refused configuration exits 2 and probes nothing.** A report inside the
  library, a symlinked library root, a negative cap: each is refused before a
  single folder is read, because the extractor writes beside every movie it
  accepts.
* **One pass over a messy library reports one verdict per movie.** A
  non-canonical layout is a SKIP, an unusable sidecar is a REVIEW, a movie whose
  identity cannot be read is an ERROR - and an ERROR is what makes the run exit 1
  so a scheduler notices, while SKIP and REVIEW do not.
* **A dry run writes nothing and says what it would have written.**
* **Nothing eligible is a finding, not a pass.** A library whose every file was
  filtered by size or by a sample name reports why, with the counts, instead of
  "0 movies, all covered".
* **A download that cannot be published costs no sidecar and reports its quota.**
  The provider allowance is a handful per day, so every refusal carries the
  remaining count and the reset time, and a sidecar that appeared during the
  download wins over the one being written.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import fake_mkvmerge as fake
import fakeprovider
import hermetic
from fakeprovider import FakeTransport, download_answer, http_error, provider_entry, search_answer
from test_subtitle_extract import DownloadTierTests
from test_subtitle_extract_e2e import ExtractorRunFixture, write_movie

import subtitle_extractor as sx

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def pristine_tool() -> types.ModuleType:
    """A second copy of ``subtitle_extractor``, loaded from source.

    ``tests/selftests`` rebinds ``run_self_tests`` on the imported module, so the
    shipped field smoke test - the one a real ``--self-test`` on a NAS runs - is
    unreachable through it. Loading the file again under another name gives the
    shipped function back without disturbing the module the rest of the suite
    holds a reference to.
    """
    spec = importlib.util.spec_from_file_location(
        "subtitle_extractor_pristine", REPO / "subtitle_extractor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered while it executes: ``@dataclass`` resolves its own annotations
    # through ``sys.modules[cls.__module__]``, and this module builds dataclasses
    # at import time.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class PristineSelfTestTests(unittest.TestCase):
    """The shipped ``--self-test``, and proof it notices a broken tool."""

    def setUp(self) -> None:
        self.tool = pristine_tool()
        self.addCleanup(sys.modules.pop, "subtitle_extractor_pristine", None)

    def run_self_test(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.tool.main(["--self-test"])
        return code, out.getvalue()

    def test_the_shipped_smoke_test_passes_on_a_bare_machine(self) -> None:
        """No library, no MKVToolNix, no network: it builds its own fixtures."""
        code, printed = self.run_self_test()
        self.assertEqual(code, 0, printed)
        self.assertIn("self-test passed", printed.lower())

    def test_a_broken_hash_floor_is_caught_by_the_smoke_test(self) -> None:
        """The check that a too-small file is refused has to be able to fail.

        OpenSubtitles keys a lookup on the moviehash of a file of at least 128
        KiB; a hash computed over a smaller file matches nothing and silently
        wastes a download. Breaking the floor on purpose must turn the smoke test
        red, or the check is decoration.
        """
        permissive = mock.Mock(return_value=("0000000000000400", 1024))
        with mock.patch.object(self.tool, "moviehash_of_file", permissive):
            code, printed = self.run_self_test()
        self.assertNotEqual(code, 0, "a broken hash floor must fail the smoke test")
        self.assertIn("a file below the hash floor is refused", printed)

    def test_a_broken_download_link_check_is_caught_by_the_smoke_test(self) -> None:
        """The link gate is what keeps a fetched subtitle on the provider's HTTPS host."""
        with mock.patch.object(self.tool, "_require_provider_link", lambda link: None):
            code, printed = self.run_self_test()
        self.assertNotEqual(code, 0, "an accepted foreign link must fail the smoke test")
        self.assertIn("a foreign download link is refused", printed)


class ValidateConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sx_cfg_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.library = self.tmp / "Movies"
        self.library.mkdir()

    def config(self, **settings: object) -> sx.ExtractorConfig:
        base: dict[str, object] = {
            "library": self.library,
            "log_file": self.tmp / "run.log",
            "report_file": self.tmp / "report.txt",
        }
        base.update(settings)
        return sx.ExtractorConfig(**base)  # type: ignore[arg-type]

    def test_a_symlinked_library_root_is_refused(self) -> None:
        """The lock and every path decision are keyed on the library's real identity."""
        link = self.tmp / "link"
        link.symlink_to(self.library)
        errors = sx.validate_config(self.config(library=link))
        self.assertEqual(errors,
                         ["--source must be an existing non-symlink movie-library directory"])

    def test_a_missing_library_is_refused(self) -> None:
        errors = sx.validate_config(self.config(library=self.tmp / "nope"))
        self.assertIn("--source must be an existing non-symlink movie-library directory", errors)

    def test_a_negative_worker_count_is_refused(self) -> None:
        """0 means "decide from the CPU count"; a negative number means nothing."""
        self.assertEqual(sx.validate_config(self.config(workers=-1)),
                         ["--workers must be non-negative (0 = decide from the CPU count)"])

    def test_negative_caps_are_refused_together(self) -> None:
        errors = sx.validate_config(self.config(min_movie_size_mb=-1.0, limit=-5))
        self.assertEqual(errors, ["--min-size, --lock-timeout, and --limit must be non-negative"])

    def test_a_report_inside_the_library_is_refused(self) -> None:
        """The walk would find its own report, and the next run would read it."""
        self.assertEqual(
            sx.validate_config(self.config(report_file=self.library / "report.txt")),
            ["--report must be outside the Jellyfin media library"])

    def test_a_log_inside_the_library_is_refused(self) -> None:
        self.assertEqual(
            sx.validate_config(self.config(log_file=self.library / "run.log")),
            ["--log must be outside the Jellyfin media library"])


class LoggedRun(ExtractorRunFixture):
    """The e2e fixture plus the run log, which is where the tool's own claims live."""

    def log_text(self) -> str:
        return (self.reports / "run.log").read_text(encoding="utf-8")

    def report_text(self) -> str:
        return (self.reports / "report.txt").read_text(encoding="utf-8")


class MainGateTests(LoggedRun):
    """Exit codes and stderr: what a scheduler and an operator actually see."""

    def test_a_refused_configuration_exits_2_and_reads_no_folders(self) -> None:
        code, _stdout, stderr = self.run_tool("--report", str(self.library / "inside.txt"))
        self.assertEqual(code, 2)
        self.assertIn("Configuration error: --report must be outside the Jellyfin media library",
                      stderr)
        self.assertFalse((self.library / "inside.txt").exists())
        self.assertFalse((self.reports / "report.txt").exists(), "no report for a refused run")

    def test_a_movie_that_cannot_be_identified_makes_the_run_exit_1(self) -> None:
        """ERROR is the only status a scheduler is told about.

        A SKIP (layout) and a REVIEW (a human's sidecar) are findings the report
        carries, and exiting non-zero for them would train everybody to ignore the
        code. An identity failure is different: the movie was never inspected.
        """
        self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        with mock.patch.object(sx, "video_snapshot",
                               side_effect=OSError(5, "Input/output error")):
            code, _stdout, stderr = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("Input/output error", self.log_text())
        self.assertIn("ERROR", self.log_text())
        self.assertFalse(self.sidecar(self.library / "Fake (2021)" / "Fake (2021).mkv").exists())

    def test_a_layout_finding_and_a_review_do_not_fail_the_run(self) -> None:
        self.library.mkdir(parents=True, exist_ok=True)
        loose = self.library / "Loose (2020).mkv"
        write_movie(loose, [fake.video_track(), fake.subtitle_track()])
        reviewed = self.movie("Reviewed (2019)", [fake.video_track(), fake.subtitle_track()])
        (reviewed.parent / "Reviewed (2019).eng.srt").write_text("not a subtitle at all",
                                                                 encoding="utf-8")
        code, _stdout, _stderr = self.run_tool()
        self.assertEqual(code, 0, "findings are reported, not raised")
        logged = self.log_text()
        self.assertIn("SKIP", logged)
        self.assertIn("REVIEW", logged)
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("directly under the library root", report)
        self.assertIn("unusable", report)

    def test_a_control_c_exits_130(self) -> None:
        self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        with mock.patch.object(sx, "extraction_run", side_effect=KeyboardInterrupt):
            code, _stdout, stderr = self.run_tool()
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", stderr)

    def test_an_unexpected_failure_leaves_through_one_exit_code(self) -> None:
        """A traceback on stderr and 1, never an unhandled exception in a cron log."""
        self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        with mock.patch.object(sx, "extraction_run", side_effect=RuntimeError("pool defect")):
            code, _stdout, stderr = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("Subtitle extractor failure: pool defect", stderr)
        self.assertIn("Traceback", stderr)

class RunShapeTests(LoggedRun):
    """What one pass over a library does, and what it says it did."""

    def test_a_limit_stops_the_walk_after_n_movies(self) -> None:
        """``--limit`` is how an operator trials the tool on part of a library."""
        first = self.movie("Alpha (2001)", [fake.video_track(), fake.subtitle_track()])
        second = self.movie("Beta (2002)", [fake.video_track(), fake.subtitle_track()])
        code, _stdout, _stderr = self.run_tool("--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("Found 1 eligible movies.", self.log_text())
        self.assertTrue(self.sidecar(first).exists())
        self.assertFalse(self.sidecar(second).exists(), "the second movie was not touched")

    def test_a_dry_run_writes_nothing_and_reports_what_it_would_have_written(self) -> None:
        movie = self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        code, _stdout, _stderr = self.run_tool("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.sidecar(movie).exists(), "a preview publishes no sidecar")
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("DRY-RUN EXTRACTIONS (NOTHING WAS WRITTEN)", report)
        self.assertIn("Dry-run extractions", report)
        self.assertIn("Fake (2021)", report)
        self.assertEqual(self.ledger(), {}, "and it records no provenance")

    def test_a_library_of_only_sample_named_files_says_why_nothing_ran(self) -> None:
        """Zero eligible movies is a finding with the counts, not "all covered"."""
        folder = self.library / "Fake (2021)"
        folder.mkdir(parents=True)
        write_movie(folder / "Fake (2021)-sample.mkv",
                    [fake.video_track(), fake.subtitle_track()])
        code, _stdout, _stderr = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("Nothing eligible", self.log_text())
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("1 named like a sample", report)
        self.assertIn("none eligible", report)

    def test_a_library_below_the_size_floor_says_why_nothing_ran(self) -> None:
        self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        code, _stdout, _stderr = self.run_tool("--min-size", "100000")
        self.assertEqual(code, 0)
        logged = self.log_text()
        self.assertIn("Nothing eligible", logged)
        self.assertIn("smaller than --min-size", logged)
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("1 smaller than --min-size", report)

    def test_the_no_download_banner_says_image_only_movies_are_reported(self) -> None:
        """The banner is the operator's only warning that a tier is switched off."""
        self.movie("Fake (2021)", [fake.video_track(), fake.subtitle_track()])
        code, stdout, _stderr = self.run_tool("--no-download")
        self.assertEqual(code, 0)
        self.assertIn("disabled (--no-download): image-only movies are reported for a human",
                      stdout)

    def test_a_sidecar_that_appears_during_a_download_is_kept_and_counted_as_covered(
            self) -> None:
        """The download tier's create-only publish, seen from the run's own ledger.

        Somebody else's sidecar winning is a success, not a failure: the movie
        ends the run with a validated English subtitle, which is the product
        promise, and no download is reported as spent on it.
        """
        movie = self.movie("Fake (2021)",
                           [fake.video_track(),
                            fake.subtitle_track(codec="PGS", codec_id="S_HDMV/PGS")])
        dest = self.sidecar(movie)
        outcome = sx.DownloadOutcome(
            ok=True, covered_by_other=True, dest=dest,
            detail=f"{dest.name} appeared during the download; the existing sidecar was kept")
        with mock.patch.object(sx, "download_hash_matched_srt", return_value=outcome):
            code, _stdout, _stderr = self.run_tool()
        self.assertEqual(code, 0)
        logged = self.log_text()
        self.assertIn("HAVE", logged)
        self.assertIn("appeared during the download", logged)
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("Fake (2021)", report)

    def test_no_mkvtoolnix_is_reported_once_as_a_warning_not_per_movie(self) -> None:
        """A missing binary is one install instruction, not three thousand lines."""
        self.movie("Alpha (2001)", [fake.video_track(), fake.subtitle_track()])
        self.movie("Beta (2002)", [fake.video_track(), fake.subtitle_track()])
        argv = ["--source", str(self.library), "--report", str(self.reports / "report.txt"),
                "--log", str(self.reports / "run.log"), "--min-size", "0",
                "--extract-min-cues", "2"]
        with hermetic.no_media_tools(), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            code = sx.main(argv)
        self.assertEqual(code, 0, "no tool to extract with is a finding, not a crash")
        logged = self.log_text()
        warnings = [line for line in logged.splitlines()
                    if "[WARNING]" in line and "not installed" in line]
        self.assertEqual(len(warnings), 1, f"the warning is logged once: {logged}")
        report = (self.reports / "report.txt").read_text(encoding="utf-8")
        self.assertIn("NEED ATTENTION", report)


class GroupingTests(unittest.TestCase):
    def test_a_reason_nobody_knows_about_is_still_listed(self) -> None:
        """An unmapped reason must land in the error bucket, never be dropped.

        ``group_results`` is the last thing standing between a run's results and
        the report: a reason added later that no bucket claims would silently
        disappear from the only document an operator reads.
        """
        video = Path("/library/Fake (2021)/Fake (2021).mkv")
        results = [sx.JobResult(video, "weird", "something new happened", reason="invented")]
        buckets, covered, extracted, downloaded, dry_run = sx.group_results(results)
        self.assertIn((video, "something new happened"), buckets[sx.REASON_ERROR])
        self.assertEqual([covered, extracted, downloaded, dry_run], [[], [], [], []])

    def test_a_dry_run_result_is_forecast_not_history(self) -> None:
        video = Path("/library/Fake (2021)/Fake (2021).mkv")
        result = sx.JobResult(video, "dry-run", "would extract", reason=sx.REASON_DRY_RUN)
        self.assertEqual(sx.coverage_count([result], dry_run=True), 1)
        self.assertEqual(sx.coverage_count([result], dry_run=False), 0)


class DownloadOutcomeTests(DownloadTierTests):
    """The published-or-not decisions of one exact-hash lookup."""

    def snapshot(self, *, size: int | None = None) -> sx.VideoSnapshot:
        real = sx.video_snapshot(self.movie)
        if size is None:
            return real
        return sx.VideoSnapshot(device=real.device, inode=real.inode, size=size,
                                mtime_ns=real.mtime_ns)

    def test_a_movie_that_changed_size_since_triage_is_not_hashed_again(self) -> None:
        """The hash is the movie's identity; a different file needs a different lookup."""
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(),
                                  fakeprovider.SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(
                self.movie, self.dest, self.options(), snapshot=self.snapshot(size=999))
        self.assertFalse(outcome.ok)
        self.assertIn("changed size after it was inspected", outcome.unavailable_reason)
        self.assertEqual(transport.requests, [], "nothing was requested from the provider")
        self.assertFalse(self.dest.exists())

    def test_a_payload_that_is_not_readable_text_is_refused_with_its_quota(self) -> None:
        """A gzip bomb or a truncated archive is a failed download, not a sidecar."""
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(),
                                  b"\x1f\x8b" + b"not really gzip")
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_DOWNLOAD_FAILED)
        self.assertIn("not readable text", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_movie_replaced_while_its_subtitle_downloaded_writes_nothing(self) -> None:
        """The download was for the file that was hashed, not for whatever is there now.

        A remux can replace the movie mid-flight; publishing a subtitle fetched
        for the old file beside the new one is how a library ends up with a
        sidecar whose cues do not match the picture.
        """
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(),
                                  fakeprovider.SRT_PAYLOAD)
        real = sx.video_snapshot(self.movie)
        stale = sx.VideoSnapshot(device=real.device, inode=real.inode, size=real.size,
                                 mtime_ns=real.mtime_ns + 1)
        with mock.patch.object(sx, "urlopen", transport):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options(),
                                                   snapshot=stale)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_DOWNLOAD_FAILED)
        self.assertIn("the movie changed while its subtitle was being downloaded", outcome.detail)
        self.assertIn("nothing was written", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_movie_that_cannot_be_rechecked_writes_nothing_either(self) -> None:
        """Unverifiable is treated as changed: the safe answer is not to publish."""
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(),
                                  fakeprovider.SRT_PAYLOAD)
        real = sx.video_snapshot(self.movie)
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx, "video_snapshot",
                                  side_effect=OSError(5, "Input/output error")):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options(),
                                                   snapshot=real)
        self.assertFalse(outcome.ok)
        self.assertIn("could not be re-checked", outcome.detail)
        self.assertFalse(self.dest.exists())

    def test_a_sidecar_published_during_the_download_wins(self) -> None:
        """Create-only publish: the file that got there first is the one kept.

        Reported as a success, because the movie ends the run with a validated
        English sidecar - which is the promise - and as ``covered_by_other`` so the
        run does not count a download it did not install.
        """
        transport = FakeTransport(search_answer(provider_entry(11)), download_answer(),
                                  fakeprovider.SRT_PAYLOAD)

        def publish(path: Path, text: str, *, replace: bool = True) -> None:
            self.assertFalse(replace, "the download tier must publish create-only")
            path.write_text("1\n00:00:01,000 --> 00:00:02,000\nSomebody else got here first\n",
                            encoding="utf-8")
            raise FileExistsError(path.name)

        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx, "atomic_write_text", side_effect=publish):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertTrue(outcome.covered_by_other)
        self.assertIn("appeared during the download", outcome.detail)
        self.assertIn("Somebody else got here first",
                      self.dest.read_text(encoding="utf-8"), "the other file survived")

    def test_a_folder_that_will_not_take_the_sidecar_reports_its_spent_quota(self) -> None:
        """The download happened; the operator has to know it cost one of the day's few."""
        transport = FakeTransport(search_answer(provider_entry(11)),
                                  download_answer(remaining=3), fakeprovider.SRT_PAYLOAD)
        with mock.patch.object(sx, "urlopen", transport), \
                mock.patch.object(sx, "atomic_write_text",
                                  side_effect=OSError(30, "Read-only file system")):
            outcome = sx.download_hash_matched_srt(self.movie, self.dest, self.options())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, sx.REASON_DOWNLOAD_FAILED)
        self.assertIn("could not write the downloaded sidecar", outcome.detail)
        self.assertEqual(outcome.remaining, 3, "the run knows how much allowance is left")
        self.assertTrue(outcome.reset_time, "and when it comes back")


class DoctorProbeTests(unittest.TestCase):
    """``organize.py doctor`` asks this whether the key works; it must never raise."""

    def test_a_working_key_is_reported_with_the_provider_s_own_note(self) -> None:
        transport = FakeTransport(b'{"status": 200, "user": {"allowed_downloads": 20}}')
        with mock.patch.object(sx, "urlopen", transport):
            ok, note = sx.opensubtitles_check("test-key")
        self.assertTrue(ok)
        self.assertTrue(note)

    def test_a_refused_key_is_a_finding_not_a_crash(self) -> None:
        transport = FakeTransport(http_error(401, b'{"error": "Unauthorized", "message": "bad key"}'))
        with mock.patch.object(sx, "urlopen", transport):
            ok, note = sx.opensubtitles_check("wrong-key")
        self.assertFalse(ok)
        self.assertIn("bad key", note)

    def test_the_probe_spends_no_login_even_with_an_account_configured(self) -> None:
        """The doctor asks one question; it must not consume a session to do it."""
        transport = FakeTransport(b'{"status": 200, "user": {"allowed_downloads": 5}}')
        with mock.patch.object(sx, "urlopen", transport):
            ok, _note = sx.opensubtitles_check("k", username="u", password="p")
        self.assertTrue(ok)
        self.assertEqual(len(transport.urls()), 1)
        self.assertIn("/infos/formats", transport.urls()[0])


if __name__ == "__main__":
    unittest.main()
