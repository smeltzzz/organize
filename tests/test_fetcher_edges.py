"""The fetcher's edges: what it refuses, what it finds, what it remembers.

Everything around the provider work. A run starts by reading its own
configuration, walking a library, reading yesterday's ledger out of the log,
and looking at what is already on disk next to each movie; it ends by shelling
out to whatever of MKVToolNix is installed. None of that is glamorous and all
of it is where a run either does nothing at all or does something to the wrong
file.

These are the last uncovered branches in `subtitle_fetcher.py` worth having:
the refusals in the config validator (each one is a message an operator has to
act on), the skips in the library walk, the layout contract, the ledger reader
faced with a log somebody has been editing, sidecar inspection when the folder
will not cooperate, and the two toolchain helpers whose entire job is to fail
politely.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import subtitle_fetcher as sf

SRT = "1\n00:00:01,000 --> 00:00:04,000\nGood evening.\n"


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()

    def movie(self, title: str = "Rear Window (1954)", size: int = 400 * 1024 * 1024) -> Path:
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        video = folder / f"{title}.mkv"
        with video.open("wb") as handle:
            handle.truncate(size)
        return video


class ConfigurationIsCheckedBeforeAnyMovieIsTouchedTests(TempCase):
    """Each message here is the one line an operator gets to act on."""

    def config(self, **overrides: Any) -> sf.QueueConfig:
        base: dict[str, Any] = {
            "library": self.library,
            "log_file": self.root / "fetcher.log",
            "report_file": self.root / "report.txt",
            "api_key": "opensubtitles-key",
            "identity_fallback": True,
        }
        base.update(overrides)
        return sf.QueueConfig(**base)

    def errors(self, **overrides: Any) -> list[str]:
        return sf.validate_compact_config(self.config(**overrides))

    def test_a_workable_configuration_has_nothing_to_say(self) -> None:
        self.assertEqual(self.errors(), [])

    def test_the_library_must_exist_and_be_a_real_directory(self) -> None:
        self.assertIn("--source must be an existing non-symlink movie-library directory",
                      self.errors(library=self.root / "nowhere"))

    def test_a_symlinked_library_is_refused(self) -> None:
        link = self.root / "linked"
        link.symlink_to(self.library, target_is_directory=True)
        self.assertIn("--source must be an existing non-symlink movie-library directory",
                      self.errors(library=link))

    def test_a_daily_cap_below_one_would_do_no_work(self) -> None:
        self.assertIn("--daily-cap must be at least 1", self.errors(daily_cap=0))
        self.assertIn("--subdl-daily-cap must be at least 1", self.errors(subdl_daily_cap=0))
        self.assertIn("--subdl-search-daily-cap must be at least 1",
                      self.errors(subdl_search_daily_cap=0))

    def test_an_unknown_auth_mode_is_refused(self) -> None:
        self.assertIn("--auth-mode is unsupported", self.errors(auth_mode="oauth"))

    def test_user_auth_needs_a_username_and_a_password(self) -> None:
        errors = self.errors(auth_mode=sf.AUTH_MODE_USER, username="someone")
        self.assertIn("--auth-mode user requires an OpenSubtitles username and password", errors)

    def test_no_provider_and_no_scraping_is_a_run_that_cannot_do_anything(self) -> None:
        errors = self.errors(api_key="", subdl_api_key="", scrape_daily_cap=0)
        self.assertTrue(any("keep the scraping" in error for error in errors), errors)

    def test_subdl_alone_needs_the_fallback_it_depends_on(self) -> None:
        """SubDL has no hash route, so switching off fallback leaves nothing."""
        errors = self.errors(api_key="", subdl_api_key="subdl-key", identity_fallback=False)
        self.assertIn("SubDL-only mode requires fallback matching; omit --no-identity-fallback",
                      errors)

    def test_an_unknown_ocr_backend_lists_the_ones_that_exist(self) -> None:
        errors = self.errors(ocr_backend="magic")
        self.assertTrue(any(error.startswith("--ocr-backend must be one of:") for error in errors))

    def test_the_ocr_and_extraction_numbers_have_floors(self) -> None:
        self.assertIn("--extract-min-cues must be at least 1", self.errors(extract_min_cues=0))
        self.assertIn("--ocr-timeout must be zero (no limit) or greater",
                      self.errors(ocr_timeout_seconds=-1))
        self.assertIn("--ocr-limit must be zero (no cap) or greater", self.errors(ocr_limit=-1))
        self.assertIn("--workers must be non-negative (0 = decide from the CPU count)",
                      self.errors(workers=-1))

    def test_negative_sizes_and_timeouts_are_one_message(self) -> None:
        errors = self.errors(min_movie_size_mb=-1, lock_timeout_seconds=-2, limit=-3)
        self.assertEqual(
            [error for error in errors if error.startswith("--min-size")],
            ["--min-size, --lock-timeout, and --limit must be non-negative"],
        )

    def test_the_report_may_not_be_written_into_the_media_library(self) -> None:
        """Jellyfin scans that folder; a report in it becomes a fake movie."""
        self.assertIn("--report must be outside the Jellyfin media library",
                      self.errors(report_file=self.library / "report.txt"))

    def test_the_log_may_not_be_written_into_the_media_library_either(self) -> None:
        self.assertIn("--log must be outside the Jellyfin media library",
                      self.errors(log_file=self.library / "logs" / "fetcher.log"))

    def test_every_problem_is_reported_at_once(self) -> None:
        """One run, one list: an operator should not fix these one per attempt."""
        errors = self.errors(daily_cap=0, ocr_limit=-1, auth_mode="oauth")
        self.assertEqual(len(errors), 3, errors)


class FindingTheMoviesTests(TempCase):
    """`discover_videos` decides what the run is even about."""

    def found(self, min_mb: float = 300) -> list[str]:
        videos = sf.discover_videos(self.library, int(min_mb * 1024 * 1024))
        return [str(path.relative_to(self.library)) for path in videos]

    def test_a_canonical_movie_is_found(self) -> None:
        self.movie()
        self.assertEqual(self.found(), ["Rear Window (1954)/Rear Window (1954).mkv"])

    def test_results_come_back_in_a_stable_order(self) -> None:
        for title in ("Zodiac (2007)", "arrival (2016)", "Manhunter (1986)"):
            self.movie(title)
        self.assertEqual([name.split("/")[0] for name in self.found()],
                         ["arrival (2016)", "Manhunter (1986)", "Zodiac (2007)"])

    def test_a_file_that_is_not_an_mkv_is_not_a_movie_here(self) -> None:
        folder = self.library / "Heat (1995)"
        folder.mkdir()
        (folder / "Heat (1995).mp4").write_bytes(b"x" * (400 * 1024 * 1024))
        self.assertEqual(self.found(), [])

    def test_a_sample_is_not_a_movie(self) -> None:
        video = self.movie()
        video.rename(video.with_name("Rear Window (1954)-sample.mkv"))
        self.assertEqual(self.found(), [])

    def test_a_movie_below_the_size_floor_is_left_alone(self) -> None:
        self.movie(size=10 * 1024 * 1024)
        self.assertEqual(self.found(), [])

    def test_a_symlinked_movie_is_never_followed(self) -> None:
        real = self.movie("Real (2000)")
        folder = self.library / "Linked (2000)"
        folder.mkdir()
        (folder / "Linked (2000).mkv").symlink_to(real)
        self.assertEqual(self.found(), ["Real (2000)/Real (2000).mkv"])

    def test_a_symlinked_folder_is_not_descended_into(self) -> None:
        outside = self.root / "elsewhere"
        (outside / "Ghost (1990)").mkdir(parents=True)
        with (outside / "Ghost (1990)" / "Ghost (1990).mkv").open("wb") as handle:
            handle.truncate(400 * 1024 * 1024)
        (self.library / "shortcut").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.found(), [])

    def test_extras_and_disc_folders_are_skipped(self) -> None:
        for special in ("Extras", "BDMV"):
            folder = self.library / "Alien (1979)" / special
            folder.mkdir(parents=True)
            with (folder / f"{special}.mkv").open("wb") as handle:
                handle.truncate(400 * 1024 * 1024)
        self.assertEqual(self.found(), [])

    def test_a_file_that_vanishes_mid_walk_is_skipped_not_fatal(self) -> None:
        video = self.movie()
        real_stat = Path.stat

        def vanishing(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            if self.name == video.name and kwargs.get("follow_symlinks", True):
                raise OSError("no such file")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(sf.Path, "stat", vanishing):
            self.assertEqual(self.found(), [])


class TheOneMoviePerFolderContractTests(TempCase):
    """A sidecar is named after its folder; the layout has to earn that."""

    def test_a_canonical_movie_has_no_issue(self) -> None:
        self.assertIsNone(sf.canonical_movie_layout_issue(self.movie(), self.library))

    def test_a_movie_loose_in_the_library_root(self) -> None:
        video = self.library / "Rear Window (1954).mkv"
        video.write_bytes(b"x")
        issue = sf.canonical_movie_layout_issue(video, self.library)
        self.assertIn("directly under the library root", str(issue))

    def test_a_symlinked_movie(self) -> None:
        real = self.movie("Real (2000)")
        folder = self.library / "Linked (2000)"
        folder.mkdir()
        link = folder / "Linked (2000).mkv"
        link.symlink_to(real)
        issue = sf.canonical_movie_layout_issue(link, self.library)
        self.assertIn("not a regular non-symlink file", str(issue))

    def test_a_name_that_does_not_match_its_folder(self) -> None:
        video = self.movie()
        renamed = video.with_name("something else.mkv")
        video.rename(renamed)
        issue = sf.canonical_movie_layout_issue(renamed, self.library)
        self.assertIn("stem does not match", str(issue))

    def test_two_movies_in_one_folder(self) -> None:
        video = self.movie()
        (video.parent / "Rear Window (1954) - extended.mkv").write_bytes(b"x")
        issue = sf.canonical_movie_layout_issue(video, self.library)
        self.assertIn("found 2", str(issue))

    def test_a_folder_that_cannot_be_read(self) -> None:
        video = self.movie()
        with mock.patch.object(sf.Path, "iterdir", side_effect=OSError("permission denied")):
            issue = sf.canonical_movie_layout_issue(video, self.library)
        self.assertIn("could not inspect movie folder", str(issue))


class ReadingYesterdaysLedgerTests(TempCase):
    """The log *is* the durable quota state, and people edit logs."""

    def write_log(self, *lines: str) -> Path:
        log = self.root / "fetcher.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log

    def event(self, payload: dict[str, Any]) -> str:
        return f"2026-09-06 12:00:00 [INFO] {sf.LEDGER_EVENT} {json.dumps(payload)}"

    def state_for(self, library: Path | None = None) -> dict[str, Any]:
        return sf.new_state(library or self.library)

    def test_no_log_means_a_fresh_ledger(self) -> None:
        state = sf.load_state(self.root / "absent.log", self.library)
        self.assertEqual(state["days"], {})
        self.assertEqual(state["movies"], {})

    def test_no_log_path_at_all_means_a_fresh_ledger(self) -> None:
        self.assertEqual(sf.load_state(None, self.library)["movies"], {})

    def test_a_checkpoint_is_read_back(self) -> None:
        key = self.state_for()["library"]
        log = self.write_log(
            "2026-09-06 11:59:59 [INFO] Scanning library",
            self.event({"library": key, "days": {"2026-09-06": {"opensubtitles": 3}},
                        "movies": {"abc": {"status": "covered"}}}),
        )
        state = sf.load_state(log, self.library)
        self.assertEqual(state["days"]["2026-09-06"]["opensubtitles"], 3)
        self.assertEqual(state["movies"]["abc"]["status"], "covered")

    def test_later_checkpoints_win(self) -> None:
        key = self.state_for()["library"]
        log = self.write_log(
            self.event({"library": key, "days": {"2026-09-06": {"opensubtitles": 1}}, "movies": {}}),
            self.event({"library": key, "days": {"2026-09-06": {"opensubtitles": 7}}, "movies": {}}),
        )
        self.assertEqual(sf.load_state(log, self.library)["days"]["2026-09-06"]["opensubtitles"], 7)

    def test_a_half_written_event_is_skipped_and_the_read_goes_on(self) -> None:
        """An interrupted run leaves half a line; later checkpoints still count."""
        key = self.state_for()["library"]
        log = self.write_log(
            self.event({"library": key, "days": {"2026-09-06": {"opensubtitles": 2}}, "movies": {}}),
            f"2026-09-06 12:00:01 [INFO] {sf.LEDGER_EVENT} " + '{"library": "' + key + '", "days"',
            self.event({"library": key, "days": {"2026-09-06": {"opensubtitles": 5}}, "movies": {}}),
        )
        self.assertEqual(sf.load_state(log, self.library)["days"]["2026-09-06"]["opensubtitles"], 5)

    def test_a_checkpoint_for_another_library_is_not_this_run_s_quota(self) -> None:
        log = self.write_log(self.event({"library": "/some/other/library",
                                         "days": {"2026-09-06": {"opensubtitles": 99}},
                                         "movies": {}}))
        self.assertEqual(sf.load_state(log, self.library)["days"], {})

    def test_a_payload_that_is_not_an_object_is_ignored(self) -> None:
        log = self.write_log(f"[INFO] {sf.LEDGER_EVENT} [1, 2, 3]")
        self.assertEqual(sf.load_state(log, self.library)["days"], {})

    def test_a_payload_with_the_wrong_field_types_is_ignored(self) -> None:
        key = self.state_for()["library"]
        log = self.write_log(self.event({"library": key, "days": "all of them", "movies": None}))
        self.assertEqual(sf.load_state(log, self.library)["days"], {})

    def test_a_log_that_cannot_be_read_stops_the_run(self) -> None:
        """Guessing an empty ledger here would silently re-spend the allowance."""
        directory = self.root / "fetcher.log"
        directory.mkdir()
        with self.assertRaisesRegex(RuntimeError, "could not read subtitle log ledger"):
            sf.load_state(directory, self.library)


class LookingAtWhatIsAlreadyThereTests(TempCase):
    """`inspect_existing_sidecars` decides whether a movie costs a request."""

    def setUp(self) -> None:
        super().setUp()
        self.video = self.movie()
        self.folder = self.video.parent

    def sidecar(self, suffix: str, text: str = SRT) -> Path:
        path = self.folder / f"{self.video.stem}{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def inspect(self) -> tuple[str, Path | None, str, str]:
        return sf.inspect_existing_sidecars(self.video)

    def test_a_movie_with_nothing_beside_it(self) -> None:
        status, path, detail, reason = self.inspect()
        self.assertEqual((status, path, reason), ("missing", None, ""))
        self.assertEqual(detail, "no English SRT sidecar")

    def test_a_valid_covering_sidecar_costs_no_request(self) -> None:
        self.sidecar(".eng.srt")
        status, path, _detail, reason = self.inspect()
        self.assertEqual((status, reason), ("covered", sf.REASON_COVERED))
        self.assertEqual(path, self.folder / f"{self.video.stem}.eng.srt")

    def test_an_sdh_sidecar_covers_the_movie_too(self) -> None:
        self.sidecar(".eng.sdh.srt")
        self.assertEqual(self.inspect()[0], "covered")

    def test_a_valid_english_srt_under_another_name_is_held_for_review(self) -> None:
        """Renaming somebody's file is not this tool's decision."""
        self.sidecar(".english.forced.srt")
        status, _path, detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_NAME))
        self.assertIn("rename or remove it", detail)

    def test_an_unusable_sidecar_is_named_so_it_can_be_deleted(self) -> None:
        self.sidecar(".eng.srt", text="<html>not a subtitle</html>")
        status, path, detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_UNUSABLE))
        self.assertIn("unusable", detail)
        self.assertEqual(path, self.folder / f"{self.video.stem}.eng.srt")

    def test_an_empty_sidecar_is_unusable_rather_than_covering(self) -> None:
        self.sidecar(".eng.srt", text="")
        self.assertEqual(self.inspect()[3], sf.REASON_SIDECAR_UNUSABLE)

    def test_a_covering_sidecar_over_the_safety_limit_is_not_accepted(self) -> None:
        """Whatever that file is, it is too big to be a subtitle for this movie."""
        self.sidecar(".eng.srt")
        with mock.patch.object(sf, "MAX_SUBTITLE_BYTES", 8):
            status, _path, _detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_UNUSABLE))

    def test_a_symlinked_covering_sidecar_is_held_for_review_not_followed(self) -> None:
        """A link where the subtitle should be is a question, not an answer."""
        real = self.root / "somewhere.srt"
        real.write_text(SRT, encoding="utf-8")
        link = self.folder / f"{self.video.stem}.eng.srt"
        link.symlink_to(real)
        status, _path, detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_NAME))
        self.assertIn("occupied", detail)
        self.assertTrue(link.is_symlink(), "the link was left exactly as it was")

    def test_a_sidecar_that_cannot_be_read_is_not_treated_as_valid(self) -> None:
        path = self.sidecar(".eng.srt")
        with mock.patch.object(sf.Path, "read_bytes", side_effect=OSError("I/O error")):
            status, _path, _detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_UNUSABLE))
        self.assertTrue(path.exists(), "nothing was deleted to reach that verdict")

    def test_a_folder_that_cannot_be_listed_is_reported_as_missing(self) -> None:
        with mock.patch.object(sf.Path, "iterdir", side_effect=OSError("permission denied")):
            status, path, detail, reason = self.inspect()
        self.assertEqual((status, path, detail, reason),
                         ("missing", None, "could not inspect sibling subtitles", ""))

    def test_a_lone_legacy_sidecar_is_promoted_and_covers_the_movie(self) -> None:
        legacy = self.sidecar(".en.srt")
        status, path, _detail, reason = self.inspect()
        self.assertEqual((status, reason), ("covered", sf.REASON_COVERED))
        self.assertEqual(path, self.folder / f"{self.video.stem}.eng.srt")
        self.assertFalse(legacy.exists(), "it was renamed, not copied")

    def test_a_legacy_sidecar_is_never_promoted_onto_something_that_is_not_a_file(self) -> None:
        """Two names for one language: a human decides which one survives."""
        legacy = self.sidecar(".en.srt")
        (self.folder / f"{self.video.stem}.eng.srt").mkdir()
        status, _path, detail, reason = self.inspect()
        self.assertEqual((status, reason), ("review", sf.REASON_SIDECAR_NAME))
        self.assertIn("legacy .en.srt could not be promoted", detail)
        self.assertTrue(legacy.exists(), "the only real subtitle here was left alone")


class TheToolsThisRunShellsOutToTests(TempCase):
    """Every external binary is optional, so every lookup may answer "no"."""

    def test_an_explicit_path_that_exists_is_used_as_given(self) -> None:
        binary = self.root / "mkvmerge"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertEqual(sf.find_mkvtoolnix_binary("mkvmerge", str(binary)), str(binary))

    def test_an_explicit_name_is_looked_up_on_the_path(self) -> None:
        with mock.patch.object(sf.shutil, "which", return_value="/usr/bin/mkvmerge") as which:
            self.assertEqual(sf.find_mkvtoolnix_binary("mkvmerge", "mkvmerge"), "/usr/bin/mkvmerge")
        which.assert_called_once_with("mkvmerge")

    def test_an_explicit_path_that_is_wrong_is_not_quietly_replaced(self) -> None:
        with mock.patch.object(sf.shutil, "which", return_value=None):
            self.assertIsNone(sf.find_mkvtoolnix_binary("mkvmerge", str(self.root / "nope")))

    def test_the_path_is_tried_before_the_install_locations(self) -> None:
        with mock.patch.object(sf.shutil, "which", return_value="/usr/bin/mkvextract"):
            self.assertEqual(sf.find_mkvtoolnix_binary("mkvextract"), "/usr/bin/mkvextract")

    def test_a_known_install_location_is_the_fallback(self) -> None:
        installed = self.root / "MKVToolNix" / "mkvmerge.exe"
        installed.parent.mkdir()
        installed.write_text("", encoding="utf-8")
        with (mock.patch.object(sf.shutil, "which", return_value=None),
              mock.patch.dict(sf._MKVTOOLNIX_PATHS,
                              {"mkvmerge": (str(self.root / "absent.exe"), str(installed))})):
            self.assertEqual(sf.find_mkvtoolnix_binary("mkvmerge"), str(installed))

    def test_a_missing_toolchain_answers_none(self) -> None:
        with (mock.patch.object(sf.shutil, "which", return_value=None),
              mock.patch.dict(sf._MKVTOOLNIX_PATHS, {"mkvmerge": ()})):
            self.assertIsNone(sf.find_mkvtoolnix_binary("mkvmerge"))

    def test_a_command_that_runs_returns_its_streams(self) -> None:
        rc, out, err = sf.run_external_command(["python3", "-c", "print('hi')"])
        self.assertEqual((rc, out.strip(), err), (0, "hi", ""))

    def test_a_command_that_hangs_is_a_timeout_not_a_stuck_run(self) -> None:
        with mock.patch.object(sf.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("sleep", 5.0)):
            rc, out, err = sf.run_external_command(["sleep", "600"], timeout=5)
        self.assertEqual((rc, out), (124, ""))
        self.assertEqual(err, "timed out after 5s")

    def test_a_program_that_is_not_installed_says_which_one(self) -> None:
        rc, out, err = sf.run_external_command([str(self.root / "not-installed")])
        self.assertEqual((rc, out), (127, ""))
        self.assertIn("could not run", err)

    def test_an_empty_command_is_answered_rather_than_raised(self) -> None:
        """This helper's whole contract is that it returns instead of raising."""
        self.assertEqual(sf.run_external_command([]),
                         (127, "", "could not run command: no program was given"))


class OpeningASubtitleArchiveTests(unittest.TestCase):
    """Scraped sources ship zips; `pick_zip_subtitle` opens exactly one file."""

    def zipped(self, *members: tuple[str, str]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in members:
                archive.writestr(name, text)
        return buffer.getvalue()

    def test_the_utf8_copy_is_preferred(self) -> None:
        raw = self.zipped(("movie.srt", "1\n00:00:01,000 --> 00:00:02,000\nlatin\n"),
                          ("movie.utf8.srt", SRT))
        self.assertIn("Good evening", sf.pick_zip_subtitle(raw).decode("utf-8"))

    def test_otherwise_the_first_srt_wins(self) -> None:
        raw = self.zipped(("readme.txt", "hello"), ("movie.srt", SRT))
        self.assertIn("Good evening", sf.pick_zip_subtitle(raw).decode("utf-8"))

    def test_with_no_srt_the_first_entry_is_tried(self) -> None:
        raw = self.zipped(("movie.sub", SRT))
        self.assertIn("Good evening", sf.pick_zip_subtitle(raw).decode("utf-8"))

    def test_something_that_is_not_an_archive(self) -> None:
        with self.assertRaisesRegex(sf.ScrapeSourceError, "non-zip payload"):
            sf.pick_zip_subtitle(b"<html>login required</html>")

    def test_an_empty_archive(self) -> None:
        with self.assertRaisesRegex(sf.ScrapeSourceError, "archive is empty"):
            sf.pick_zip_subtitle(self.zipped())

    def test_an_archive_that_is_damaged(self) -> None:
        raw = bytearray(self.zipped(("movie.srt", SRT * 50)))
        raw[len(raw) - 30:] = b"\x00" * 30
        with self.assertRaisesRegex(sf.ScrapeSourceError, "unreadable subtitle archive"):
            sf.pick_zip_subtitle(bytes(raw))


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
