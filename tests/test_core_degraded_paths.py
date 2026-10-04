"""Degraded filesystem, lock, resolver, and report paths preserve safe outcomes."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from organizekit.core import config, fsio, locking, report, subtitles, toolchain

GOOD_SRT = "1\n00:00:01,000 --> 00:00:02,000\nDialogue.\n\n"


class AtomicPublishFailureTests(unittest.TestCase):
    def test_create_if_absent_never_overwrites_a_concurrent_sidecar(self) -> None:
        """A racing subtitle download wins over a legacy promotion or cache write."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "Movie.eng.srt"
            target.write_text("the hand-placed subtitle", encoding="utf-8")
            with mock.patch.object(fsio.os, "link", side_effect=FileExistsError(errno.EEXIST, "exists")), \
                    self.assertRaises(FileExistsError):
                fsio.atomic_write_text(target, GOOD_SRT, replace=False)
            self.assertEqual(target.read_text(encoding="utf-8"), "the hand-placed subtitle")
            self.assertEqual([path.name for path in Path(td).iterdir()], [target.name])

    def test_a_write_error_keeps_the_previous_report_and_removes_staging(self) -> None:
        """An ordinary fsync failure must not publish a partial replacement."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            target.write_text("last complete report", encoding="utf-8")
            with mock.patch.object(fsio.os, "fsync", side_effect=OSError("disk full")), \
                    self.assertRaisesRegex(OSError, "disk full"):
                fsio.atomic_write_text(target, "new report")
            self.assertEqual(target.read_text(encoding="utf-8"), "last complete report")
            self.assertEqual([path.name for path in Path(td).iterdir()], [target.name])

    def test_a_post_publish_stage_unlink_failure_does_not_undo_the_report(self) -> None:
        """Once the hard link is published, cleanup failure leaves a harmless twin."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            real_unlink = Path.unlink

            def fail_only_for_stage(path: Path, *args: object, **kwargs: object) -> None:
                if path.name.endswith(".tmp"):
                    raise OSError("read-only directory")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_only_for_stage):
                fsio.atomic_write_text(target, "complete report", replace=False)
            stages = [path for path in Path(td).iterdir() if path.name.endswith(".tmp")]
            self.assertEqual(target.read_text(encoding="utf-8"), "complete report")
            self.assertEqual(len(stages), 1)
            self.assertTrue(os.path.samefile(target, stages[0]), "the leftover name points at published bytes")
            real_unlink(stages[0])


@unittest.skipIf(os.name == "nt", "these lock error codes are exercised through POSIX flock")
class LockFailureTests(unittest.TestCase):
    def test_only_contention_is_busy_in_strict_mode(self) -> None:
        """A genuine lock I/O error is not silently reported as another run."""
        import fcntl

        with tempfile.TemporaryFile() as handle:
            busy = OSError(errno.EAGAIN, "held by another process")
            with mock.patch.object(fcntl, "flock", side_effect=busy):
                self.assertFalse(locking.try_file_lock(handle))
                self.assertFalse(locking.try_file_lock(handle, strict_non_contention=True))
            broken = OSError(errno.EBADF, "invalid lock handle")
            with mock.patch.object(fcntl, "flock", side_effect=broken):
                self.assertFalse(locking.try_file_lock(handle), "legacy run locks fail closed")
                with self.assertRaises(OSError) as caught:
                    locking.try_file_lock(handle, strict_non_contention=True)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_coordination_timeout_closes_its_handle_before_another_run_retries(self) -> None:
        """A timed-out waiter must not retain an OS lock after returning an error."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "library"
            waiter = locking.CoordinationLock(target, timeout_seconds=0)
            with mock.patch.object(locking, "try_file_lock", return_value=False), \
                    self.assertRaises(locking.LockTimeoutError):
                waiter.acquire()
            with locking.CoordinationLock(target, timeout_seconds=0.2):
                self.assertTrue(waiter.path.exists(), "the stable lock file may remain after release")

    def test_keyboard_interrupt_during_acquire_does_not_strand_the_library_lock(self) -> None:
        """Cancellation while waiting must close the descriptor and unblock recovery."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "library"
            interrupted = locking.CoordinationLock(target, timeout_seconds=30)
            with mock.patch.object(locking, "try_file_lock", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                interrupted.acquire()
            with locking.CoordinationLock(target, timeout_seconds=0.2):
                self.assertTrue(interrupted.path.exists())

    def test_a_failed_explicit_unlock_still_closes_the_run_lock_handle(self) -> None:
        """The handle close is the final release if the OS rejects explicit unlock."""
        import fcntl

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run.lock"
            held = locking.ExclusiveRunLock(path, 0.2)
            held.__enter__()
            with mock.patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "unlock failed")):
                held.__exit__(None, None, None)
            with locking.ExclusiveRunLock(path, 0.2):
                self.assertTrue(path.is_file(), "a later run can take the lock after descriptor close")


class SidecarFallbackTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        movie = root / "Movie (2020).mkv"
        movie.write_bytes(b"movie")
        return movie, subtitles.legacy_external_english_srt_path(movie), subtitles.exact_external_english_srt_path(movie)

    def test_invalid_sidecar_paths_are_never_accepted_as_covering_subtitles(self) -> None:
        """A missing, unreadable, oversized, or linked file cannot certify coverage."""
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.eng.srt"
            ok, reason = subtitles.validate_srt_sidecar(missing)
            self.assertFalse(ok)
            self.assertIn("could not stat", reason)

            target = Path(td) / "target.srt"
            target.write_text(GOOD_SRT, encoding="utf-8")
            link = Path(td) / "linked.eng.srt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this filesystem")
            ok, reason = subtitles.validate_srt_sidecar(link)
            self.assertFalse(ok)
            self.assertIn("not a regular file", reason)

            oversized = Path(td) / "oversized.eng.srt"
            with oversized.open("wb") as handle:
                handle.truncate(subtitles.EXTERNAL_SRT_MAX_BYTES + 1)
            ok, reason = subtitles.validate_srt_sidecar(oversized)
            self.assertFalse(ok)
            self.assertIn("safety limit", reason)

            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("share is offline")):
                ok, reason = subtitles.validate_srt_sidecar(target)
            self.assertFalse(ok)
            self.assertIn("could not read", reason)

    def test_a_legacy_fallback_will_not_rename_over_a_racing_download(self) -> None:
        """When hard links are unavailable, the last-moment existence check protects the winner."""
        with tempfile.TemporaryDirectory() as td:
            movie, legacy, canonical = self._paths(Path(td))
            legacy.write_text(GOOD_SRT, encoding="utf-8")
            winner = "1\n00:00:04,000 --> 00:00:05,000\nNew download.\n\n"

            def unsupported_but_publish(_source: str, destination: str) -> None:
                Path(destination).write_text(winner, encoding="utf-8")
                raise OSError(errno.ENOTSUP, "hard links are unavailable")

            with mock.patch.object(subtitles.os, "link", side_effect=unsupported_but_publish):
                promoted, reason = subtitles.promote_legacy_external_english_srt(movie)
            self.assertIsNone(promoted)
            self.assertIn("occupied", reason)
            self.assertEqual(canonical.read_text(encoding="utf-8"), winner)
            self.assertEqual(legacy.read_text(encoding="utf-8"), GOOD_SRT)

    def test_a_failed_fallback_rename_preserves_the_legacy_subtitle(self) -> None:
        """A no-hardlink filesystem error cannot consume the only good subtitle."""
        with tempfile.TemporaryDirectory() as td:
            movie, legacy, canonical = self._paths(Path(td))
            legacy.write_text(GOOD_SRT, encoding="utf-8")
            with mock.patch.object(subtitles.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")), \
                    mock.patch.object(subtitles.os, "replace", side_effect=OSError(errno.EIO, "disk failure")):
                promoted, reason = subtitles.promote_legacy_external_english_srt(movie)
            self.assertIsNone(promoted)
            self.assertIn("could not rename", reason)
            self.assertEqual(legacy.read_text(encoding="utf-8"), GOOD_SRT)
            self.assertFalse(canonical.exists())

    def test_unlink_failure_after_linking_leaves_two_names_for_the_same_bytes(self) -> None:
        """A harmless duplicate is safer than reporting failure after publication."""
        with tempfile.TemporaryDirectory() as td:
            movie, legacy, canonical = self._paths(Path(td))
            legacy.write_text(GOOD_SRT, encoding="utf-8")
            real_unlink = Path.unlink

            def refuse_legacy(path: Path, *args: object, **kwargs: object) -> None:
                if path == legacy:
                    raise OSError("legacy name cannot be removed")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=refuse_legacy):
                promoted, reason = subtitles.promote_legacy_external_english_srt(movie)
            self.assertEqual(promoted, canonical)
            self.assertEqual(reason, "")
            self.assertTrue(legacy.exists())
            self.assertTrue(os.path.samefile(legacy, canonical))
            self.assertEqual(canonical.read_text(encoding="utf-8"), GOOD_SRT)


class ResolverDegradationTests(unittest.TestCase):
    def test_a_broken_sibling_resolver_falls_back_to_path(self) -> None:
        """A broken optional resolver may not turn an installed binary into a false negative."""
        import audio_standardizer as aus
        import bitdepth
        import mkv_track_cleaner as tc
        import subtitle_extractor as sx

        def which(binary: str) -> str:
            return f"/fake-bin/{binary}"

        with mock.patch.object(toolchain.shutil, "which", side_effect=which), \
                mock.patch.object(sx, "find_mkvtoolnix_binary", side_effect=RuntimeError("broken import")), \
                mock.patch.object(tc, "resolve_mkvmerge_path", side_effect=RuntimeError("broken resolver")), \
                mock.patch.object(bitdepth, "find_ffprobe", side_effect=RuntimeError("broken resolver")), \
                mock.patch.object(aus, "find_ffprobe", side_effect=RuntimeError("broken resolver")):
            self.assertTrue(toolchain.mkvtoolnix_installed())
            self.assertTrue(toolchain.mkvmerge_installed())
            self.assertTrue(toolchain.mkvextract_installed())
            self.assertTrue(toolchain.ffprobe_installed())
            self.assertTrue(toolchain.ffmpeg_installed())

    def test_a_prerequisite_probe_exception_is_reported_as_a_skip(self) -> None:
        """An unexpected probe failure stops only that step, not the pipeline."""
        step = toolchain.Step("injected", "organize.py", "Injected step", "--dir")

        def broken_probe() -> bool:
            raise RuntimeError("probe crashed")

        with mock.patch.dict(toolchain.PREREQUISITES, {"injected": (broken_probe, "tool unavailable")}):
            self.assertEqual(toolchain.prerequisite_issue(step), "tool unavailable")


class ReportFallbackTests(unittest.TestCase):
    def test_title_and_right_status_fit_without_overflow(self) -> None:
        """Long report rows stay within the terminal width without losing normal status alignment."""
        normal = report.Report("Run", width=64).title_line("Cleaning", right="done").render().splitlines()
        crowded = report.Report("Run", width=64).title_line("A very long descriptive title" * 3, right="done").render()
        self.assertTrue(normal[-1].rstrip().endswith("done"))
        self.assertTrue(all(len(line) <= 64 for line in crowded.splitlines()))

    def test_partial_report_does_not_claim_more_items_than_the_scan_found(self) -> None:
        """An interrupted sweep must not render a nonsensical '5 of 3' count."""
        rendered = report.Report("Run", width=40).section("Failed", count=5, total=3).render()
        self.assertIn("5", rendered)
        self.assertNotIn("5 of 3", rendered)


class DotenvFallbackTests(unittest.TestCase):
    def test_exported_and_quoted_values_load_without_overriding_the_shell(self) -> None:
        """A copied .env accepts common syntax while an explicit export remains authoritative."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text(
                "# comment\n\nexport ORGANIZE_TEST_DOTENV='file value'\n"
                "ORGANIZE_TEST_DOTENV_SHELL=from-file\nmalformed\n=empty-key\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"ORGANIZE_TEST_DOTENV_SHELL": "shell value"}):
                values = config.load_dotenv(path)
                self.assertEqual(values["ORGANIZE_TEST_DOTENV"], "file value")
                self.assertEqual(os.environ["ORGANIZE_TEST_DOTENV"], "file value")
                self.assertEqual(os.environ["ORGANIZE_TEST_DOTENV_SHELL"], "shell value")
                self.assertNotIn("", values)

    def test_an_unreadable_candidate_is_skipped_for_the_next_config_file(self) -> None:
        """One damaged config file cannot hide a later usable candidate."""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.env"
            good = Path(td) / "good.env"
            bad.write_bytes(b"\xff")
            good.write_text("ORGANIZE_TEST_FALLBACK=usable\n", encoding="utf-8")
            with mock.patch.object(config, "_dotenv_candidates", return_value=[bad, good]), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ORGANIZE_TEST_FALLBACK", None)
                values = config.load_dotenv()
                self.assertEqual(os.environ["ORGANIZE_TEST_FALLBACK"], "usable")
            self.assertEqual(values, {"ORGANIZE_TEST_FALLBACK": "usable"})


if __name__ == "__main__":
    unittest.main()
