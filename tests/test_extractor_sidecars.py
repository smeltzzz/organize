"""What the extractor decides when a movie already has *something* beside it.

``inspect_existing_sidecars`` is the gate the whole tool stands behind: an
existing sidecar is authoritative, so a wrong answer here either overwrites a
subtitle somebody placed by hand or refuses to fetch one a movie needs. The
verdicts are ``covered`` (do nothing), ``review`` (a human decides) and
``missing`` (go and extract or download). These tests pin the edges:

* a covering sidecar that is empty, oversized, a symlink or unreadable is **not**
  covering - it is reported for a human with the fix named in the detail;
* a valid English SRT with a name the cleaner cannot use is ``review``, never
  ``missing``: the tool will not publish a second subtitle beside it;
* a legacy ``.en.srt`` whose canonical destination is occupied is ``review``,
  because promotion must not overwrite whatever is there;
* a folder that cannot be listed at all answers ``missing`` with a detail that
  says so - and the publish that follows is create-only, so nothing is lost;
* the writable-folder probe is a real write, because its answer decides whether
  a scarce provider download is spent.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subtitle_extractor as sx
from organizekit.core import JobOutcome

SRT = "1\n00:00:01,000 --> 00:00:02,000\nA line of English dialogue\n"


class FaultyPath(type(Path())):
    """A ``Path`` whose reads fail on demand, and whose children inherit it.

    ``iterdir()`` and ``with_name()`` hand back objects of the caller's own
    class, which makes a subclass the only way to fail one specific read without
    patching ``Path`` for every tool in the same process.
    """

    fail_read: frozenset[str] = frozenset()
    fail_iterdir = False
    fail_unlink = False

    def read_bytes(self) -> bytes:
        if self.name in type(self).fail_read:
            raise OSError(5, "Input/output error", self.name)
        return super().read_bytes()

    def iterdir(self):  # type: ignore[override]
        if type(self).fail_iterdir:
            raise OSError(5, "Input/output error", str(self))
        return super().iterdir()

    def unlink(self, *args: object, **kwargs: object) -> None:
        if type(self).fail_unlink:
            raise OSError(16, "Device or resource busy", self.name)
        super().unlink(*args, **kwargs)  # type: ignore[arg-type]


class SidecarFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sx_sidecar_")
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.folder = self.tmp / "Fake (2021)"
        self.folder.mkdir()
        self.movie = self.folder / "Fake (2021).mkv"
        self.movie.write_bytes(b"\0" * 4096)
        FaultyPath.fail_read = frozenset()
        FaultyPath.fail_iterdir = False
        FaultyPath.fail_unlink = False

    def sidecar(self, name: str, text: str = SRT) -> Path:
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def inspect(self, video: Path | None = None) -> tuple[str, Path | None, str, str]:
        return sx.inspect_existing_sidecars(video or self.movie)


class CoveringSidecarTests(SidecarFixture):
    def test_a_covering_sdh_sidecar_counts_as_covered(self) -> None:
        """``.eng.sdh.srt`` is a covering name, so the exact one is skipped past.

        An SDH sidecar is a real English subtitle; treating it as missing would
        have the tool extract or download a second one beside it.
        """
        placed = self.sidecar("Fake (2021).eng.sdh.srt")
        status, path, detail, reason = self.inspect()
        self.assertEqual(status, "covered")
        self.assertEqual(path, placed)
        self.assertIn("validated covering sidecar", detail)
        self.assertEqual(reason, sx.REASON_COVERED)

    def test_an_empty_covering_sidecar_is_not_covering(self) -> None:
        """Zero bytes is a failed download, not a subtitle - and it blocks the slot.

        The verdict is ``review`` with the fix in the detail: the operator deletes
        it, and only then may a replacement be fetched. Publishing over it would
        hide the fact that something else wrote a broken file into the library.
        """
        broken = self.sidecar("Fake (2021).eng.srt", "")
        status, path, detail, reason = self.inspect()
        self.assertEqual(status, "review")
        self.assertEqual(path, broken)
        self.assertEqual(reason, sx.REASON_SIDECAR_UNUSABLE)
        self.assertIn("unusable", detail)
        self.assertIn("delete it and re-run", detail)

    def test_an_oversized_covering_sidecar_is_not_covering(self) -> None:
        """A 4 MiB "subtitle" is an HTML error page or a wrong file, never a cue sheet."""
        self.sidecar("Fake (2021).eng.srt", "x" * (sx.MAX_SUBTITLE_BYTES + 1))
        status, _path, detail, reason = self.inspect()
        self.assertEqual(status, "review")
        self.assertEqual(reason, sx.REASON_SIDECAR_UNUSABLE)
        self.assertIn("unusable", detail)

    def test_a_covering_sidecar_that_cannot_be_read_is_not_covering(self) -> None:
        """A share that fails the read must not be reported as "already covered".

        ``covered`` means the movie needs nothing, so a read error there would
        strand the movie forever: every later run would agree it was fine.
        """
        video = FaultyPath(self.movie)
        self.sidecar("Fake (2021).eng.srt")
        FaultyPath.fail_read = frozenset({"Fake (2021).eng.srt"})
        status, _path, detail, reason = self.inspect(video)
        self.assertEqual(status, "review")
        self.assertEqual(reason, sx.REASON_SIDECAR_UNUSABLE)
        self.assertIn("unusable", detail)

    def test_a_symlinked_sidecar_is_not_a_candidate_at_all(self) -> None:
        """The link may point at another movie's subtitle, or outside the library.

        So a symlink never counts as this movie's English sidecar: the verdict is
        ``missing``, and the tool goes on to extract one of its own rather than
        trusting a file whose target nobody checked.
        """
        outside = self.tmp / "elsewhere.srt"
        outside.write_text(SRT, encoding="utf-8")
        (self.folder / "Fake (2021).english.srt").symlink_to(outside)
        status, path, detail, _reason = self.inspect()
        self.assertEqual(status, "missing")
        self.assertIsNone(path)
        self.assertEqual(detail, "no English SRT sidecar")
        self.assertTrue(outside.is_file(), "and the link target was not touched")


class NamedSidecarTests(SidecarFixture):
    def test_a_valid_sidecar_with_an_unusable_name_is_a_review_not_a_gap(self) -> None:
        """The cleaner needs the exact ``.eng.srt`` name; a near miss is a human's call.

        Reporting ``missing`` here would put a second subtitle beside the first,
        and the folder would then hold two English sidecars with different names -
        exactly the ambiguity the extractor exists to remove.
        """
        placed = self.sidecar("Fake (2021).english.srt")
        status, path, detail, reason = self.inspect()
        self.assertEqual(status, "review")
        self.assertEqual(path, placed)
        self.assertEqual(reason, sx.REASON_SIDECAR_NAME)
        self.assertIn("not a covering .eng.srt", detail)
        self.assertIn("rename or remove it", detail)

    def test_an_occupied_canonical_name_blocks_the_legacy_promotion(self) -> None:
        """A symlink on ``.eng.srt`` is refused, never overwritten.

        The extractor holds a coordination lock on the library, but the auditor
        and a hand edit do not, so whatever sits on the canonical name may have
        arrived a moment ago. Promotion publishes with a create-only link for
        exactly this reason; this is the branch that reports the refusal.
        """
        outside = self.tmp / "elsewhere.srt"
        outside.write_text(SRT, encoding="utf-8")
        (self.folder / "Fake (2021).eng.srt").symlink_to(outside)
        legacy = self.sidecar("Fake (2021).en.srt")

        status, _path, detail, reason = self.inspect()

        self.assertEqual(status, "review")
        self.assertEqual(reason, sx.REASON_SIDECAR_NAME)
        self.assertIn("could not be promoted", detail)
        self.assertIn("occupied", detail)
        self.assertTrue(legacy.is_file(), "the legacy sidecar is still there")
        self.assertEqual(outside.read_text(encoding="utf-8"), SRT,
                         "and the symlink's target was not written through")

    def test_a_valid_legacy_sidecar_is_promoted_and_then_counts_as_covered(self) -> None:
        """The contrast the two tests above depend on: a free slot is used."""
        self.sidecar("Fake (2021).en.srt")
        status, path, _detail, reason = self.inspect()
        self.assertEqual(status, "covered")
        self.assertEqual(reason, sx.REASON_COVERED)
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "Fake (2021).eng.srt")


class UnreadableFolderTests(SidecarFixture):
    def test_a_folder_that_cannot_be_listed_is_reported_as_unreadable(self) -> None:
        """``missing`` with a detail that says the folder could not be read.

        The publish that follows is create-only, so answering ``missing`` cannot
        overwrite a sidecar that is really there - but the detail is what tells an
        operator the verdict came from a folder nobody could read.
        """
        video = FaultyPath(self.movie)
        self.sidecar("Fake (2021).eng.srt")
        FaultyPath.fail_iterdir = True
        status, path, detail, reason = self.inspect(video)
        self.assertEqual(status, "missing")
        self.assertIsNone(path)
        self.assertEqual(detail, "could not inspect sibling subtitles")
        self.assertEqual(reason, "")


class WritableFolderTests(SidecarFixture):
    """``_folder_accepts_new_files`` decides whether a download may be spent."""

    def test_a_cheap_answer_of_no_is_checked_with_a_real_write(self) -> None:
        """``os.access`` lies about directories on some platforms, so probe for real."""
        with mock.patch.object(sx.os, "access", return_value=False):
            self.assertTrue(sx._folder_accepts_new_files(self.folder))
        leftovers = [p.name for p in self.folder.iterdir() if "write-probe" in p.name]
        self.assertEqual(leftovers, [], "the probe cleans up after itself")

    def test_an_access_check_that_raises_falls_back_to_the_probe(self) -> None:
        with mock.patch.object(sx.os, "access", side_effect=OSError(38, "not supported")):
            self.assertTrue(sx._folder_accepts_new_files(self.folder))

    def test_a_path_that_cannot_take_a_new_file_answers_false(self) -> None:
        """A read-only mount must be found out *before* the download is spent."""
        not_a_folder = self.tmp / "a-file"
        not_a_folder.write_text("x", encoding="utf-8")
        with mock.patch.object(sx.os, "access", return_value=False):
            self.assertFalse(sx._folder_accepts_new_files(not_a_folder))

    def test_a_probe_that_cannot_be_removed_still_answers_true(self) -> None:
        """The folder did accept the file; failing to delete it is not a refusal.

        Answering False here would strand every movie on a share that refuses
        unlink - no download, and a report claiming the folder is not writable.
        """
        folder = FaultyPath(self.folder)
        FaultyPath.fail_unlink = True
        with mock.patch.object(sx.os, "access", return_value=False):
            self.assertTrue(sx._folder_accepts_new_files(folder))
        for probe in self.folder.glob(".organize-write-probe.*"):
            probe.unlink()


class LedgerTests(SidecarFixture):
    """The provenance ledger: where a sidecar came from, and what happens if it breaks."""

    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.tmp / "extracted.json"
        self._saved_env = os.environ.get(sx.EXTRACTED_LEDGER_ENV)
        os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        self.addCleanup(self._restore_env)
        patch = mock.patch.object(sx, "extracted_ledger_path", return_value=self.ledger)
        patch.start()
        self.addCleanup(patch.stop)

    def _restore_env(self) -> None:
        if self._saved_env is None:
            os.environ.pop(sx.EXTRACTED_LEDGER_ENV, None)
        else:
            os.environ[sx.EXTRACTED_LEDGER_ENV] = self._saved_env

    def test_a_fetcher_era_ledger_is_still_honoured(self) -> None:
        """A library cut over from the fetching era keeps the provenance it had.

        Those records are still true, and dropping them would make every previously
        fetched sidecar look hand-written - which is what the "did this tool write
        it?" question is for.
        """
        legacy = self.tmp / sx.LEGACY_EXTRACTED_LEDGER_NAME
        legacy.write_text('{"version": 1, "sidecars": {"/m/Fake.eng.srt": {"method": "legacy"}}}',
                          encoding="utf-8")
        payload = sx.load_extracted_ledger()
        self.assertEqual(payload["sidecars"], {"/m/Fake.eng.srt": {"method": "legacy"}})

    def test_a_corrupt_legacy_ledger_is_treated_as_no_ledger(self) -> None:
        (self.tmp / sx.LEGACY_EXTRACTED_LEDGER_NAME).write_text("{not json", encoding="utf-8")
        payload = sx.load_extracted_ledger()
        self.assertEqual(payload, {"version": sx.EXTRACTED_LEDGER_VERSION, "sidecars": {}})

    def test_a_ledger_that_cannot_be_written_reports_failure_without_failing_the_run(
            self) -> None:
        """Provenance is a record, not a precondition: the sidecar still stands."""
        sidecar = self.sidecar("Fake (2021).eng.srt")
        track = sx.EmbeddedSubtitleTrack(track_id=1, codec_id="S_TEXT/UTF8", language="eng",
                                         name="English", kind="text", extension=".srt",
                                         default=True)
        with mock.patch.object(sx, "atomic_write_json", side_effect=OSError(30, "Read-only")):
            recorded = sx.record_extracted_sidecar(
                self.movie, sidecar, track=track, method="mkvextract", cue_count=2,
                sha256="0" * 64, path=self.ledger)
        self.assertFalse(recorded)
        self.assertTrue(sidecar.is_file(), "the sidecar the run published is untouched")


class LabelTests(SidecarFixture):
    def test_a_movie_is_labelled_by_its_folder_relative_to_the_library(self) -> None:
        library = self.tmp
        self.assertEqual(sx.movie_label(self.movie, library), "Fake (2021)")

    def test_a_movie_directly_under_the_root_is_labelled_by_its_own_name(self) -> None:
        loose = self.tmp / "Loose (2020).mkv"
        loose.write_bytes(b"x")
        self.assertEqual(sx.movie_label(loose, self.tmp), "Loose (2020).mkv")

    def test_a_path_outside_the_library_is_printed_in_full(self) -> None:
        """A relative path that does not exist would print as ``../..`` nonsense."""
        elsewhere = Path("/srv/other/Fake (2021).mkv")
        self.assertEqual(sx.relative_text(elsewhere, self.tmp), str(elsewhere))


class TriageTests(SidecarFixture):
    def test_a_noncanonical_layout_is_decided_before_any_sidecar_is_read(self) -> None:
        """A movie directly under the root is a layout finding, not a subtitle one."""
        loose = self.tmp / "Loose (2020).mkv"
        loose.write_bytes(b"x" * 1024)
        triage = sx.triage_movie(loose, self.tmp)
        self.assertIn("directly under the library root", triage.layout_issue)
        self.assertIsNone(triage.snapshot)
        self.assertFalse(triage.fetchable)

    def test_a_movie_that_cannot_be_identified_is_that_movie_s_error(self) -> None:
        """The identity read is what a download is keyed on, so it may not be guessed."""
        with mock.patch.object(sx, "video_snapshot",
                               side_effect=OSError(2, "No such file or directory")):
            triage = sx.triage_movie(self.movie, self.tmp)
        self.assertIn("No such file or directory", triage.error)
        self.assertFalse(triage.fetchable)
        self.assertEqual(triage.sidecar_status, "missing")

    def test_a_worker_failure_becomes_the_movie_s_verdict_not_the_end_of_the_run(self) -> None:
        """Triage runs in a pool; one unreadable folder must not take the run with it."""
        outcome: JobOutcome[Path, sx.Triage] = JobOutcome(
            index=0, item=self.movie, value=None, error=RuntimeError("pool defect"))
        verdict = sx.TriageQueue._verdict(outcome)
        self.assertEqual(verdict.error, "pool defect")
        self.assertEqual(verdict.video, self.movie)

    def test_an_error_with_no_message_still_names_its_type(self) -> None:
        outcome: JobOutcome[Path, sx.Triage] = JobOutcome(
            index=1, item=self.movie, value=None, error=RuntimeError())
        self.assertEqual(sx.TriageQueue._verdict(outcome).error, "RuntimeError")


class CandidateLabelTests(unittest.TestCase):
    def test_an_sdh_candidate_says_so_in_the_line_the_report_prints(self) -> None:
        """The operator reads this label to decide whether to keep the download."""
        candidate = sx.OpenSubtitlesCandidate(
            file_id=7, file_name="Fake.2021.srt", release="Fake.2021.1080p",
            hearing_impaired=True)
        self.assertIn("SDH", candidate.label)
        plain = sx.OpenSubtitlesCandidate(file_id=8, file_name="Fake.2021.srt",
                                          release="Fake.2021.1080p")
        self.assertNotIn("SDH", plain.label)


class TimestampPaddingTests(unittest.TestCase):
    def test_a_timestamp_with_no_hours_field_is_not_a_timestamp(self) -> None:
        """``01:02,500`` is a hand edit, not an SRT clock: refuse it, do not guess."""
        self.assertIsNone(sx._pad_srt_timestamp("01:02,500"))
        self.assertIsNone(sx._pad_srt_timestamp("00:00:01,000,900"))
        self.assertEqual(sx._pad_srt_timestamp("0:0:1,5"), "00:00:01,500")


class ConversionDropTests(unittest.TestCase):
    def test_a_stray_line_inside_the_ass_events_section_is_not_a_cue(self) -> None:
        ass = ("\ufeff[Script Info]\nTitle: x\n\n[Events]\nFormat: Layer, Start, End, Style, "
               "Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Style: Default,Arial,20\n"
               "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello there\n")
        srt = sx.ass_to_srt(ass)
        self.assertIn("Hello there", srt)
        self.assertNotIn("Style:", srt)

    def test_a_vtt_cue_with_timing_and_no_text_is_dropped(self) -> None:
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n00:00:03.000 --> 00:00:04.000\nSpoken\n"
        srt = sx.vtt_to_srt(vtt)
        self.assertIn("Spoken", srt)
        self.assertEqual(srt.count("-->"), 1, "the empty cue is not rendered as a blank one")


if __name__ == "__main__":
    unittest.main()
