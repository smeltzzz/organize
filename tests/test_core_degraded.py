"""Degraded operation in the shared core: what happens when the machine says no.

The core modules are the ones every tool imports, so a failure here is not one
tool's problem - it is six. Each of these tests injects the failure the code
already claims to survive and asserts the *degradation it promised*, not merely
that no exception escaped:

* a ``.env`` that cannot be read is "no configuration", never a dead tool;
* a publish that fails halfway leaves the previous bytes and no staging debris;
* a sidecar that is a symlink, oversized, or undecodable is refused with a
  reason a report can print - and a promotion never overwrites a sidecar that
  appeared while it was working;
* a sibling tool module that will not import degrades to a plain PATH lookup,
  because ``doctor`` has to answer on the machine it is run on;
* a smoke-test check that crashes is a *failed check*, which is the answer the
  operator asked for;
* a console that will not take UTF-8 or VT mode loses colour, not output;
* a probe cache that is corrupt, foreign, or unwritable is a cold cache - a
  cache has no business failing a run that would otherwise have worked.

Where a branch only exists on Windows, :mod:`tests.platforms` holds ``os.name``
at ``"nt"`` so the coverage runner measures the code a Windows operator runs.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import platforms

from organizekit.core import (
    atomic_write_text,
    config,
    console,
    fsio,
    probecache,
    smoke,
    subtitles,
    text,
    toolchain,
)
from organizekit.core import playbackchain as pc

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class DotenvTests(unittest.TestCase):
    """``.env`` is documented, so every documented form has to work."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dotenv_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.touched: list[str] = []

    def _load(self, body: str) -> dict[str, str]:
        env = self.root / ".env"
        env.write_text(body, encoding="utf-8")
        loaded = config.load_dotenv(env)
        for key in loaded:
            self.touched.append(key)
            self.addCleanup(os.environ.pop, key, None)
        return loaded

    def test_comments_blanks_and_lines_without_an_assignment_are_skipped(self) -> None:
        """A typo in a config file must not stop a run that would otherwise work.

        ``load_dotenv`` is called on the startup path of every tool; raising on
        a stray line would turn one bad character in a user's ``.env`` into a
        toolkit that will not start.
        """
        loaded = self._load(
            "# a comment\n"
            "\n"
            "   \n"
            "this line has no equals sign\n"
            "ORGANIZE_TEST_FIRST=1\n"
        )
        self.assertEqual(loaded, {"ORGANIZE_TEST_FIRST": "1"})
        self.assertEqual(os.environ.get("ORGANIZE_TEST_FIRST"), "1")

    def test_a_leading_export_and_surrounding_quotes_are_stripped(self) -> None:
        """``.env`` files are shared with shell users, who write ``export``."""
        loaded = self._load(
            'export ORGANIZE_TEST_SECOND="two words"\n'
            "ORGANIZE_TEST_THIRD='single'\n"
            "ORGANIZE_TEST_FOURTH=  spaced  \n"
            'ORGANIZE_TEST_FIFTH="unbalanced\n'
        )
        self.assertEqual(loaded["ORGANIZE_TEST_SECOND"], "two words")
        self.assertEqual(loaded["ORGANIZE_TEST_THIRD"], "single")
        self.assertEqual(loaded["ORGANIZE_TEST_FOURTH"], "spaced")
        self.assertEqual(loaded["ORGANIZE_TEST_FIFTH"], '"unbalanced',
                         "a quote that is not a pair is part of the value")

    def test_a_line_with_an_empty_key_is_skipped(self) -> None:
        self.assertEqual(self._load("=novalue\nORGANIZE_TEST_SIXTH=6\n"),
                         {"ORGANIZE_TEST_SIXTH": "6"})

    def test_a_real_environment_variable_beats_the_file(self) -> None:
        os.environ["ORGANIZE_TEST_SEVENTH"] = "from the environment"
        self.addCleanup(os.environ.pop, "ORGANIZE_TEST_SEVENTH", None)
        self._load("ORGANIZE_TEST_SEVENTH=from the file\n")
        self.assertEqual(os.environ["ORGANIZE_TEST_SEVENTH"], "from the environment")

    def test_no_readable_env_file_anywhere_is_not_an_error(self) -> None:
        """The lookup itself has to survive a filesystem that will not answer.

        ``_dotenv_candidates`` resolves the entry-point script, the working
        directory and the installation root. Any of those can fail on a NAS -
        a working directory that was deleted under the process is the common
        one, and it makes ``Path.cwd()`` raise. What must not happen is a tool
        dying at startup because it could not look for an optional file.
        """
        def unresolvable(self: Path, **kwargs: object) -> Path:
            raise OSError("cannot resolve anything on this share")

        with mock.patch.object(sys, "argv", ["organize.py"]), \
                mock.patch.object(Path, "resolve", unresolvable), \
                mock.patch.object(Path, "cwd", side_effect=OSError("cwd was deleted")):
            self.assertEqual(config._dotenv_candidates(), [],
                             "every candidate lookup failed, and none of them raised")
            self.assertEqual(config.load_dotenv(), {})
            self.assertEqual(config.resolve_library(None), config.default_library_root(),
                             "with no .env the resolver still answers")

    def test_a_dotenv_beside_the_entry_point_is_still_found_when_the_cwd_is_gone(self) -> None:
        """One failure in the candidate list must not cost the other candidates."""
        env = self.root / ".env"
        env.write_text("ORGANIZE_TEST_EIGHTH=8\n", encoding="utf-8")
        self.addCleanup(os.environ.pop, "ORGANIZE_TEST_EIGHTH", None)
        with mock.patch.object(sys, "argv", [str(self.root / "organize.py")]), \
                mock.patch.object(Path, "cwd", side_effect=OSError("cwd was deleted")):
            self.assertEqual(config.load_dotenv(), {"ORGANIZE_TEST_EIGHTH": "8"})


class PlatformDefaultTests(unittest.TestCase):
    """The defaults a machine with nothing configured actually gets."""

    def test_the_library_default_is_the_documented_path_on_each_platform(self) -> None:
        with platforms.windows():
            self.assertEqual(config.default_library_root(), Path(r"E:\torrents\final_organized"))
        with platforms.posix():
            self.assertEqual(config.default_library_root(), Path.home() / "Media" / "Movies")

    def test_the_origin_of_the_resolved_root_is_named_for_the_message_that_uses_it(self) -> None:
        """An error has to say which knob set the root, or nobody can fix it."""
        saved = {name: os.environ.pop(name, None)
                 for name in (config.LIBRARY_ENV_VAR, config.LEGACY_LIBRARY_ENV_VAR)}
        self.addCleanup(self._restore, saved)
        self.assertEqual(config.describe_library_origin(Path("/explicit")), "--source")
        os.environ[config.LIBRARY_ENV_VAR] = "/from/env"
        self.assertEqual(config.describe_library_origin(None), config.LIBRARY_ENV_VAR)
        os.environ.pop(config.LIBRARY_ENV_VAR)
        os.environ[config.LEGACY_LIBRARY_ENV_VAR] = "/from/legacy"
        self.assertEqual(config.describe_library_origin(None), config.LEGACY_LIBRARY_ENV_VAR)
        os.environ.pop(config.LEGACY_LIBRARY_ENV_VAR)
        self.assertIn("the default library root", config.describe_library_origin(None))
        # A whitespace-only flag is not a flag: the resolver falls through, and
        # so must the description of it.
        self.assertEqual(config.describe_library_origin(Path("   ")),
                         f"the default library root ({config.default_library_root()})")

    @staticmethod
    def _restore(saved: dict[str, str | None]) -> None:
        for name, value in saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_windows_reports_go_to_the_documented_volume_when_it_is_there(self) -> None:
        """The scar: a Windows box with no E: got a default nothing could write.

        Every tool then exited 2 while saving its own report. So the documented
        directory is used only when that volume actually exists, and the
        per-user state directory otherwise - both halves asserted here, because
        the coverage runner has no E: drive and would otherwise only ever see
        one of them.
        """
        real_exists = Path.exists
        with platforms.windows(), mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}, clear=False), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertEqual(config.default_reports_root(),
                             Path(r"E:\torrents\tools\ReportsAndLogs"))
        with platforms.windows(), mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData"},
                                                  clear=False), \
                mock.patch.object(Path, "exists", real_exists):
            self.assertEqual(config.default_reports_root(),
                             Path(r"C:\Users\x\AppData") / "organize")
        with platforms.windows(), mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(Path, "exists", real_exists):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(config.default_reports_root(),
                             Path.home() / "AppData" / "Local" / "organize")

    def test_posix_reports_follow_the_state_convention(self) -> None:
        with platforms.posix(), mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/var/state"}):
            self.assertEqual(config.default_reports_root(), Path("/var/state/organize"))
        with platforms.posix():
            os.environ.pop("XDG_STATE_HOME", None)
            self.assertEqual(config.default_reports_root(),
                             Path.home() / ".local" / "state" / "organize")


class AtomicWriteTests(unittest.TestCase):
    """``atomic_write_text``: all or nothing, and no debris either way."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="atomic_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.target = self.root / "report.txt"

    def _names(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir())

    def test_a_failed_publish_keeps_the_previous_bytes_and_removes_the_stage(self) -> None:
        """The report a reader has is better than half of the next one.

        ``os.replace`` failing is not hypothetical: a Windows antivirus holding
        the destination, or a share that went read-only mid-run. The previous
        content must survive *and* the staging file must not be left where the
        next run's housekeeping would have to guess about it.
        """
        atomic_write_text(self.target, "the good report")
        with mock.patch.object(fsio.os, "replace", side_effect=OSError("read-only share")), \
                self.assertRaises(OSError):
            atomic_write_text(self.target, "the report that never landed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "the good report")
        self.assertEqual(self._names(), ["report.txt"], "no staging debris is left behind")

    def test_a_publish_that_refuses_to_clobber_leaves_the_winner_and_no_stage(self) -> None:
        """``replace=False`` is the create-if-absent contract the fetcher needs.

        A hand-placed or concurrently extracted sidecar must win over a
        download, so the failure is propagated *and* the staged copy is
        removed - otherwise every refused download leaves a hidden file beside
        the movie forever.
        """
        self.target.write_text("the sidecar that was there first", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            atomic_write_text(self.target, "the download", replace=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"),
                         "the sidecar that was there first")
        self.assertEqual(self._names(), ["report.txt"])

    def test_a_stage_that_cannot_be_unlinked_does_not_turn_a_publish_into_a_failure(self) -> None:
        """After ``os.link`` the content is published; the stage is a second name.

        Failing to remove it must not report an error for work that succeeded -
        it leaves one harmless duplicate, which is strictly better than telling
        an operator their subtitle was not written when it was.
        """
        real_unlink = Path.unlink
        with mock.patch.object(Path, "unlink", side_effect=OSError("directory is read-only")):
            atomic_write_text(self.target, "published", replace=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "published")
        leftovers = [name for name in self._names() if name != "report.txt"]
        self.assertEqual(len(leftovers), 1, "the duplicate stays; the publish still succeeded")
        real_unlink(self.root / leftovers[0], missing_ok=True)

    def test_a_stage_that_cannot_be_removed_after_a_failed_publish_is_not_a_second_error(self) -> None:
        """The OSError that matters is the publish's, not the cleanup's.

        If removing the staged file also failed - the share went away
        altogether - raising that instead would report a cleanup problem and
        hide the reason nothing was published.
        """
        atomic_write_text(self.target, "the good report")
        with mock.patch.object(fsio.os, "replace", side_effect=OSError("read-only share")), \
                mock.patch.object(Path, "unlink", side_effect=OSError("share went away")), \
                self.assertRaises(OSError) as caught:
            atomic_write_text(self.target, "the report that never landed")
        self.assertIn("read-only share", str(caught.exception))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "the good report")

    def test_a_refused_clobber_whose_stage_cannot_be_removed_still_reports_the_refusal(self) -> None:
        self.target.write_text("the winner", encoding="utf-8")
        with mock.patch.object(Path, "unlink", side_effect=OSError("share went away")), \
                self.assertRaises(FileExistsError):
            atomic_write_text(self.target, "the loser", replace=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "the winner")

    def test_a_link_publish_still_writes_durably(self) -> None:
        atomic_write_text(self.target, "linked\n", replace=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "linked\n")


class SnapshotTests(unittest.TestCase):
    def test_a_path_that_cannot_be_read_never_matches_its_snapshot(self) -> None:
        """"I cannot prove it is unchanged" has to mean "refuse to replace it".

        Both tools that overwrite a movie call this between reading the source
        and publishing over it. Answering True for a path that has disappeared
        would let a remux publish on top of whatever arrived in the meantime.
        """
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "Film (2020).mkv"
            movie.write_bytes(b"bytes")
            snapshot = fsio.source_snapshot(movie)
            self.assertTrue(fsio.source_snapshot_matches(movie, snapshot))
            movie.unlink()
            self.assertFalse(fsio.source_snapshot_matches(movie, snapshot))

    def test_a_snapshot_missing_a_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "Film (2020).mkv"
            movie.write_bytes(b"bytes")
            truncated = dict(fsio.source_snapshot(movie))
            truncated.pop("inode")
            self.assertFalse(fsio.source_snapshot_matches(movie, truncated))
            self.assertFalse(fsio.source_snapshot_matches(movie, {}))


class Sha256Tests(unittest.TestCase):
    def test_a_file_larger_than_one_chunk_is_digested_whole(self) -> None:
        """The read loop is 1 MiB; a movie is never one chunk.

        A digest that stopped at the first chunk would still look like a
        digest, and every "did this file change?" answer built on it would be
        wrong for files above the chunk size - i.e. for every movie.
        """
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.bin"
            payload = bytes(range(256)) * (5 * 1024)  # 1.25 MiB, not a round chunk
            big.write_bytes(payload)
            self.assertEqual(fsio.sha256_file(big), hashlib.sha256(payload).hexdigest())

    def test_an_empty_file_has_the_empty_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.bin"
            empty.write_bytes(b"")
            self.assertEqual(fsio.sha256_file(empty), hashlib.sha256(b"").hexdigest())


class SidecarContractTests(unittest.TestCase):
    """``validate_srt_sidecar`` and the legacy-name promotion built on it."""

    VALID = "1\n00:00:01,000 --> 00:00:02,000\nA line of dialogue\n\n"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sidecar_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.movie = self.root / "Film (2020).mkv"
        self.movie.write_bytes(b"not really a movie")

    def _sidecar(self, name: str, body: str | bytes | None = None) -> Path:
        path = self.root / name
        content = self.VALID if body is None else body
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_a_sidecar_that_cannot_be_stat_ed_is_refused_with_a_reason(self) -> None:
        ok, reason = subtitles.validate_srt_sidecar(self.root / "absent.eng.srt")
        self.assertFalse(ok)
        self.assertIn("could not stat subtitle", reason)

    def test_a_symlink_is_refused_even_when_it_points_at_a_valid_subtitle(self) -> None:
        """The contract says non-symlink, and the reason is a real one.

        A sidecar is a file the toolkit may rename, replace or delete. Through
        a symlink those operations land somewhere the operator did not mean -
        outside the library, or on the only copy of a hand-written subtitle.
        """
        real = self._sidecar("real.srt")
        link = self.root / "Film (2020).eng.srt"
        link.symlink_to(real)
        ok, reason = subtitles.validate_srt_sidecar(link)
        self.assertFalse(ok)
        self.assertIn("not a regular file", reason)
        self.assertTrue(real.is_file(), "the target was not touched")

    def test_an_oversized_sidecar_is_refused(self) -> None:
        """A 4 MiB ceiling on a text file is a guard against an HTML error page.

        Reading one into memory to discover it is not a subtitle is how a
        mis-pointed download becomes a stuck tool.
        """
        huge = self.root / "Film (2020).eng.srt"
        with huge.open("wb") as handle:
            handle.write(b"1\n00:00:01,000 --> 00:00:02,000\n")
            handle.write(b"x" * (subtitles.EXTERNAL_SRT_MAX_BYTES + 1))
        ok, reason = subtitles.validate_srt_sidecar(huge)
        self.assertFalse(ok)
        self.assertIn("safety limit", reason)

    def test_an_empty_sidecar_is_refused(self) -> None:
        ok, reason = subtitles.validate_srt_sidecar(self._sidecar("Film (2020).eng.srt", ""))
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_a_sidecar_that_cannot_be_read_is_refused(self) -> None:
        path = self._sidecar("Film (2020).eng.srt")
        real_read = Path.read_bytes
        with mock.patch.object(Path, "read_bytes",
                               side_effect=OSError(errno.EACCES, "permission denied")):
            ok, reason = subtitles.validate_srt_sidecar(path)
        self.assertFalse(ok)
        self.assertIn("could not read subtitle", reason)
        self.assertTrue(real_read(path), "the file itself is untouched")

    def test_a_sidecar_that_is_not_text_is_refused(self) -> None:
        # 0x81 is not a character in any of the three encodings the contract
        # allows, so this is the "not text at all" case rather than the
        # "text with no cue in it" case.
        ok, reason = subtitles.validate_srt_sidecar(
            self._sidecar("Film (2020).eng.srt", b"\x81\x81\x81\x81"))
        self.assertFalse(ok)
        self.assertIn("unsupported text encoding", reason)

    def test_a_valid_sidecar_is_accepted(self) -> None:
        self.assertEqual(subtitles.validate_srt_sidecar(self._sidecar("Film (2020).eng.srt")),
                         (True, ""))

    def test_promotion_renames_a_validated_legacy_sidecar(self) -> None:
        self._sidecar("Film (2020).en.srt")
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(reason, "")
        self.assertEqual(promoted, self.root / "Film (2020).eng.srt")
        self.assertTrue(promoted.is_file())
        self.assertFalse((self.root / "Film (2020).en.srt").exists(),
                         "the legacy name goes away; one file, one name")

    def test_an_absent_legacy_sidecar_is_reported_as_absent(self) -> None:
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertEqual(reason, "legacy .en.srt is absent")

    def test_promotion_never_overwrites_an_existing_canonical_sidecar(self) -> None:
        """The two names are not protected by the same lock.

        The extractor holds a library-wide coordination lock; the auditor holds
        only its own per-directory run lock. So a freshly extracted
        ``.eng.srt`` can land between the promotion's existence check and its
        publish, and ``os.replace`` destroyed it once. Publishing with
        ``os.link`` is what makes that impossible.
        """
        self._sidecar("Film (2020).en.srt")
        canonical = self._sidecar("Film (2020).eng.srt", "the one that was extracted first")
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, canonical, "the canonical name already answers the question")
        self.assertEqual(reason, "")
        self.assertEqual(canonical.read_text(encoding="utf-8"), "the one that was extracted first")
        self.assertTrue((self.root / "Film (2020).en.srt").is_file(),
                        "and the legacy file was not renamed over it")

    def test_a_canonical_name_held_by_a_symlink_is_reported_as_occupied(self) -> None:
        """A symlink at the destination is not a sidecar and must not be replaced.

        Renaming through it would write the promoted subtitle wherever the link
        points - possibly outside the library - so the promotion refuses and
        says why instead.
        """
        self._sidecar("Film (2020).en.srt")
        elsewhere = self._sidecar("elsewhere.srt")
        (self.root / "Film (2020).eng.srt").symlink_to(elsewhere)
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("occupied", reason)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), self.VALID,
                         "the link target was written through, not over")

    def test_a_destination_that_appears_between_the_check_and_the_link_is_not_clobbered(self) -> None:
        self._sidecar("Film (2020).en.srt")
        canonical = self.root / "Film (2020).eng.srt"
        real_link = os.link

        def racing_link(src: str, dst: str, **kwargs: object) -> None:
            Path(dst).write_text("extracted while the promotion was running", encoding="utf-8")
            real_link(src, dst, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(subtitles.os, "link", racing_link):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("appeared concurrently", reason)
        self.assertEqual(canonical.read_text(encoding="utf-8"),
                         "extracted while the promotion was running")

    def test_a_canonical_name_occupied_by_something_that_is_not_a_file_is_refused(self) -> None:
        self._sidecar("Film (2020).en.srt")
        (self.root / "Film (2020).eng.srt").mkdir()
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("occupied", reason)

    def test_an_uninspectable_destination_is_reported_rather_than_guessed_at(self) -> None:
        self._sidecar("Film (2020).en.srt")
        with mock.patch.object(Path, "exists", side_effect=OSError("share went away")):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("could not inspect canonical sidecar", reason)

    def test_an_uninspectable_legacy_name_is_reported(self) -> None:
        real_exists = Path.exists

        def flaky(self: Path) -> bool:
            if self.name.endswith(".en.srt"):
                raise OSError("share went away")
            return real_exists(self)

        with mock.patch.object(Path, "exists", flaky):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("could not inspect legacy sidecar", reason)

    def test_an_unusable_legacy_sidecar_is_left_where_it_is(self) -> None:
        """Promoting a broken subtitle would hide the breakage under a good name."""
        legacy = self._sidecar("Film (2020).en.srt", "")
        promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("unusable", reason)
        self.assertTrue(legacy.is_file())
        self.assertFalse((self.root / "Film (2020).eng.srt").exists())

    def test_a_filesystem_without_hardlinks_falls_back_to_a_rename_that_still_checks(self) -> None:
        """FAT32, exFAT and some SMB shares cannot link at all.

        The fallback is a plain rename, which *can* overwrite - so the
        destination is re-checked first. That re-check is the whole reason the
        fallback is safe, and it is the line that would be dropped by anyone
        "simplifying" the branch.
        """
        legacy = self._sidecar("Film (2020).en.srt")

        def no_hardlinks(src: str, dst: str, **kwargs: object) -> None:
            raise OSError(errno.EPERM, "operation not permitted")

        with mock.patch.object(subtitles.os, "link", no_hardlinks):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.root / "Film (2020).eng.srt")
        self.assertEqual(reason, "")
        self.assertFalse(legacy.exists(), "a rename moves it")
        self.assertTrue(promoted.is_file())

    def test_the_rename_fallback_refuses_a_destination_that_appeared_meanwhile(self) -> None:
        self._sidecar("Film (2020).en.srt")
        canonical = self.root / "Film (2020).eng.srt"
        real_exists = Path.exists
        checks = {"canonical": 0}

        def no_hardlinks(src: str, dst: str, **kwargs: object) -> None:
            raise OSError(errno.EXDEV, "cross-device link")

        def appears_on_the_recheck(self: Path) -> bool:
            # The first two looks happen before the link is attempted; the
            # third is the fallback's own re-check, and that is the one that
            # has to see the file that arrived in between.
            if self == canonical:
                checks["canonical"] += 1
                return checks["canonical"] > 2
            return real_exists(self)

        with mock.patch.object(subtitles.os, "link", no_hardlinks), \
                mock.patch.object(Path, "exists", appears_on_the_recheck):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("occupied", reason)
        self.assertFalse(canonical.exists(), "nothing was written over the name")

    def test_a_rename_that_fails_is_reported_not_swallowed(self) -> None:
        self._sidecar("Film (2020).en.srt")

        def no_hardlinks(src: str, dst: str, **kwargs: object) -> None:
            raise OSError(errno.EPERM, "operation not permitted")

        with mock.patch.object(subtitles.os, "link", no_hardlinks), \
                mock.patch.object(subtitles.os, "replace",
                                  side_effect=OSError("read-only filesystem")):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("could not rename legacy .en.srt", reason)
        self.assertTrue((self.root / "Film (2020).en.srt").is_file(), "the legacy file survived")

    def test_a_link_error_that_is_not_about_hardlinks_is_reported_as_itself(self) -> None:
        self._sidecar("Film (2020).en.srt")
        with mock.patch.object(subtitles.os, "link",
                               side_effect=OSError(errno.EIO, "input/output error")):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertIsNone(promoted)
        self.assertIn("could not promote legacy .en.srt", reason)

    def test_a_legacy_name_that_cannot_be_removed_is_harmless(self) -> None:
        """Both names now point at one validated file.

        Reporting the leftover ``.en.srt`` as a promotion failure would be
        worse than leaving it: the canonical sidecar exists and is valid, and
        the next call short-circuits on it.
        """
        self._sidecar("Film (2020).en.srt")
        with mock.patch.object(Path, "unlink", side_effect=OSError("directory is read-only")):
            promoted, reason = subtitles.promote_legacy_external_english_srt(self.movie)
        self.assertEqual(promoted, self.root / "Film (2020).eng.srt")
        self.assertEqual(reason, "")
        self.assertTrue(promoted.is_file())


class ToolchainFallbackTests(unittest.TestCase):
    """A sibling tool that will not import must not take the caller down."""

    #: module -> (helper, the binaries the PATH fallback looks for)
    CASES = (
        ("subtitle_extractor", "mkvtoolnix_installed", ("mkvmerge", "mkvextract")),
        ("subtitle_extractor", "mkvextract_installed", ("mkvextract",)),
        ("bitdepth", "ffprobe_installed", ("ffprobe",)),
        ("audio_standardizer", "ffmpeg_installed", ("ffprobe", "ffmpeg")),
    )

    def test_a_broken_sibling_degrades_to_the_plain_path_lookup(self) -> None:
        """``doctor`` has to answer on the machine it is run on.

        These helpers ask the tool that will actually run the binary, so no two
        callers can disagree. When that tool cannot even be imported - a
        partial copy to a NAS, a syntax error on one Python version - the
        answer has to fall back to PATH rather than raise, because a
        prerequisite check that crashes takes the whole pipeline with it.
        """
        for module, helper, binaries in self.CASES:
            for present in (True, False):
                with self.subTest(module=module, helper=helper, present=present):
                    found = {name: "/usr/bin/" + name for name in binaries} if present else {}
                    with mock.patch.dict(sys.modules, {module: None}), \
                            mock.patch.object(shutil, "which", found.get):
                        self.assertIs(getattr(toolchain, helper)(), present)

    def test_the_fallback_needs_every_binary_the_step_uses(self) -> None:
        """One of two is none: the extractor drives mkvmerge *and* mkvextract."""
        with mock.patch.dict(sys.modules, {"subtitle_extractor": None}), \
                mock.patch.object(shutil, "which",
                                  lambda name: "/usr/bin/mkvmerge" if name == "mkvmerge" else None):
            self.assertFalse(toolchain.mkvtoolnix_installed())
        with mock.patch.dict(sys.modules, {"subtitle_extractor": None}), \
                mock.patch.object(shutil, "which", lambda name: None):
            self.assertFalse(toolchain.mkvtoolnix_installed())
            self.assertFalse(toolchain.ffmpeg_installed())

    def test_the_answer_comes_from_the_tool_that_runs_the_binary(self) -> None:
        """No PATH-only answer when the tool's own resolver knows better.

        MKVToolNix's Windows installer does not put itself on PATH, so the
        tools search their standard install locations as well. A check that
        only looked at PATH would report "not installed" on a machine that can
        run the step fine.
        """
        import subtitle_extractor as sx

        with mock.patch.object(sx, "find_mkvtoolnix_binary",
                               lambda name, explicit=None: f"C:/mkvtoolnix/{name}.exe"):
            self.assertTrue(toolchain.mkvtoolnix_installed())
            self.assertTrue(toolchain.mkvextract_installed())
        with mock.patch.object(sx, "find_mkvtoolnix_binary",
                               lambda name, explicit=None: None if name == "mkvextract" else "x"):
            self.assertFalse(toolchain.mkvtoolnix_installed())
            self.assertFalse(toolchain.mkvextract_installed())

    def test_ffprobe_is_asked_through_the_inspector(self) -> None:
        import bitdepth

        with mock.patch.object(bitdepth, "find_ffprobe", lambda explicit=None: "/opt/ffprobe"):
            self.assertTrue(toolchain.ffprobe_installed())
        with mock.patch.object(bitdepth, "find_ffprobe", lambda explicit=None: None):
            self.assertFalse(toolchain.ffprobe_installed())

    def test_a_prerequisite_probe_that_raises_is_a_skip_with_a_reason(self) -> None:
        """``check()`` is an arbitrary caller-supplied probe.

        Whatever it raises, the answer is "this step cannot run" plus the
        sentence the report prints - never an exception out of the pipeline,
        which would abort a sweep that had already done real work.
        """
        step = toolchain.STEPS["cleaner"]
        check, reason = toolchain.PREREQUISITES["cleaner"]

        def exploding() -> bool:
            raise RuntimeError("the resolver blew up")

        with mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (exploding, reason)}):
            self.assertEqual(toolchain.prerequisite_issue(step), reason)

        with mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (exploding, "")}):
            self.assertEqual(toolchain.prerequisite_issue(step), "prerequisite check failed")

        with mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (lambda: True, reason)}):
            self.assertIsNone(toolchain.prerequisite_issue(step),
                              "a probe that answers yes leaves the step runnable")
        with mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (lambda: False, reason)}):
            self.assertEqual(toolchain.prerequisite_issue(step), reason)
        self.assertTrue(callable(check))

    def test_a_step_whose_script_is_missing_is_skipped_before_anything_is_probed(self) -> None:
        step = toolchain.STEPS["cleaner"]
        probed = []

        def tripwire() -> bool:
            probed.append(True)
            return True

        with mock.patch.object(toolchain, "tool_is_available", lambda script: False), \
                mock.patch.dict(toolchain.PREREQUISITES, {"cleaner": (tripwire, "")}):
            issue = toolchain.prerequisite_issue(step)
        self.assertEqual(issue, "mkv_track_cleaner.py is missing from this directory")
        self.assertEqual(probed, [], "a missing script needs no binary probe")


class TextLayoutTests(unittest.TestCase):
    """Report text is read in a terminal of some width, or not read at all."""

    def test_a_non_positive_width_clips_to_nothing(self) -> None:
        self.assertEqual(text.clip_text("anything", 0), "")
        self.assertEqual(text.clip_text("anything", -5), "")

    def test_text_that_already_fits_is_returned_unchanged(self) -> None:
        self.assertEqual(text.clip_text("short", 80), "short")

    def test_a_width_too_small_for_the_ellipsis_hard_truncates(self) -> None:
        """"ab..." is longer than the column it was asked to fit in.

        The ellipsis is a marker, not a licence to overrun: every report line
        is padded to the width, and one long line breaks the alignment of the
        whole box.
        """
        self.assertEqual(text.clip_text("abcdef", 3), "abc")
        self.assertEqual(text.clip_text("abcdef", 2), "ab")
        clipped = text.clip_text("/media/movies/a very long movie name.mkv", 20)
        self.assertEqual(len(clipped), 20, "the ellipsis is inside the budget, not added to it")
        self.assertTrue(clipped.endswith("..."))

    def test_an_empty_paragraph_stays_an_empty_line_when_wrapping(self) -> None:
        """Blank lines in a report are paragraph breaks, not something to drop."""
        self.assertEqual(text.wrap_text("one\n\ntwo", 40), ["one", "", "two"])
        self.assertEqual(text.wrap_text("   \nx", 40), ["", "x"])

    def test_wrapping_never_returns_nothing(self) -> None:
        self.assertEqual(text.wrap_text("", 40), [""])
        self.assertEqual(text.wrap_path_text("", 40), [""])

    def test_path_wrapping_keeps_a_short_paragraph_whole_and_strips_its_tail(self) -> None:
        """Once anything has to be broken, the short paragraphs are kept as written."""
        long_tail = "/".join(["a-directory-name-that-is-far-too-long"] * 3) + "/movie.mkv"
        lines = text.wrap_path_text(f"short/path.mkv\n\n{long_tail}", 40)
        self.assertEqual(lines[0], "short/path.mkv")
        self.assertEqual(lines[1], "", "a blank paragraph stays a blank line")
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)

    def test_path_wrapping_breaks_after_separators_not_inside_names(self) -> None:
        """The tail of a path is what a reader scans for.

        ``wrap_text`` would split a movie folder name in half; breaking after
        ``/`` and ``\\`` keeps the name on one line, which is the whole reason
        the report has its own path wrapper.
        """
        long_path = "/media/movies/Some Very Long Movie Name (2020)/Some Very Long Movie Name (2020).mkv"
        lines = text.wrap_path_text(long_path, 40)
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)
        self.assertEqual("".join(line.replace(" ", "") for line in lines).count("/"),
                         long_path.count("/"))
        self.assertTrue(any(line.rstrip().endswith(".mkv") for line in lines),
                        "the file name is never split")


class SmokeTestTests(unittest.TestCase):
    """``--self-test`` on each tool: the field diagnostic an operator runs first."""

    def test_a_check_that_crashes_is_a_failed_check_not_a_crashed_self_test(self) -> None:
        """A smoke test that crashes has still told you the answer you asked for.

        ``--self-test`` is what someone runs on a NAS with no terminal history
        and no way to read a traceback. Exit 1 plus the exception type is the
        useful answer; an uncaught exception is a wall of text and a status
        code a wrapper cannot interpret.
        """
        def broken() -> bool:
            raise RuntimeError("the report renderer is gone")

        out = io.StringIO()
        with redirect_stdout(out):
            code = smoke.run_field_smoke_test("testtool", [("a broken check", broken)])
        self.assertEqual(code, 1)
        printed = out.getvalue()
        self.assertIn("FAIL  a broken check", printed)
        self.assertIn("RuntimeError: the report renderer is gone", printed)
        self.assertIn("SELF-TEST FAILED: 1 of 4 checks", printed)
        self.assertIn("a broken check", printed)

    def test_a_check_that_returns_false_is_also_a_failure(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = smoke.run_field_smoke_test("testtool", [("a false check", lambda: False)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  a false check", out.getvalue())

    def test_the_shared_checks_pass_on_a_clean_checkout(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = smoke.run_field_smoke_test("testtool")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("SELF-TEST PASSED: 3 checks", out.getvalue())
        self.assertIn("python -m unittest discover -s tests", out.getvalue())


class ConsoleTests(unittest.TestCase):
    """Output must survive a console that cannot do what was asked of it."""

    def test_a_stream_that_refuses_to_be_reconfigured_does_not_stop_the_tool(self) -> None:
        """Every tool pins its stdio to UTF-8 at startup, before any work.

        A closed or detached stream - a Windows service, ``nohup`` with stdout
        closed - raises ``ValueError`` from ``reconfigure``. Since this runs
        first, an unhandled raise here means the tool dies before it reads a
        single argument.
        """
        class Closed:
            def reconfigure(self, **kwargs: object) -> None:
                raise ValueError("I/O operation on closed file")

        with mock.patch.object(sys, "stdout", Closed()), mock.patch.object(sys, "stderr", Closed()):
            console.enable_utf8_stdio()  # must not raise

    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        """``redirect_stdout`` replaces the stream with one that has no such method."""
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            console.enable_utf8_stdio()
        self.assertEqual(stream.getvalue(), "")

    def test_colour_is_assumed_available_off_windows(self) -> None:
        with platforms.posix():
            self.assertTrue(console.enable_windows_vt())

    def test_a_windows_console_with_no_kernel32_gets_no_colour(self) -> None:
        """ctypes has no ``windll`` on a POSIX host, which is the same shape as
        a Windows host where the call cannot be made: colour is optional, so
        the answer is False and the report is printed without escapes.
        """
        with platforms.windows(), mock.patch.object(ctypes, "windll", None, create=True):
            self.assertFalse(console.enable_windows_vt())

    def test_an_invalid_standard_handle_gets_no_colour(self) -> None:
        """``GetStdHandle`` answers 0 or -1 for a detached console.

        Passing that on to ``GetConsoleMode`` is how a tool ends up with a
        ctypes ``ArgumentError`` instead of a plain-text report.
        """
        for handle in (0, -1):
            with self.subTest(handle=handle):
                kernel32 = SimpleNamespace(GetStdHandle=lambda std, value=handle: value)
                with platforms.windows(), \
                        mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                          create=True):
                    self.assertFalse(console.enable_windows_vt())

    def test_a_console_mode_read_that_fails_gets_no_colour(self) -> None:
        kernel32 = SimpleNamespace(GetStdHandle=lambda std: 1234,
                                   GetConsoleMode=lambda handle, mode: 0)
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True):
            self.assertFalse(console.enable_windows_vt())

    def test_an_existing_vt_mode_is_left_alone(self) -> None:
        """The mode is OR-ed, never assigned: a literal write drops every other flag."""
        set_calls: list[int] = []

        def get_console_mode(handle: int, mode: ctypes.c_uint32) -> int:
            mode.value = 0x0004 | 0x0080  # VT already on, plus a flag that is not ours
            return 1

        kernel32 = SimpleNamespace(
            GetStdHandle=lambda std: 1234,
            GetConsoleMode=get_console_mode,
            SetConsoleMode=lambda handle, mode: set_calls.append(mode) or 1,
        )
        # ``ctypes.byref`` hands the callee a CArgObject, which a Python double
        # cannot write through; passing the object itself is the same call
        # convention as far as the code under test is concerned.
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True), \
                mock.patch.object(ctypes, "byref", lambda obj: obj):
            self.assertTrue(console.enable_windows_vt())
        self.assertEqual(set_calls, [], "a console already in VT mode is not rewritten")

    def test_a_console_without_vt_gets_it_switched_on_without_losing_other_flags(self) -> None:
        set_calls: list[int] = []

        def get_console_mode(handle: int, mode: ctypes.c_uint32) -> int:
            mode.value = 0x0008  # some other flag the console already had
            return 1

        kernel32 = SimpleNamespace(
            GetStdHandle=lambda std: 1234,
            GetConsoleMode=get_console_mode,
            SetConsoleMode=lambda handle, mode: set_calls.append(mode) or 1,
        )
        with platforms.windows(), \
                mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel32),
                                  create=True), \
                mock.patch.object(ctypes, "byref", lambda obj: obj):
            self.assertTrue(console.enable_windows_vt())
        self.assertEqual(set_calls, [0x0008 | 0x0004],
                         "the existing mode is OR-ed with VT, not replaced by it")


class PlaybackChainTests(unittest.TestCase):
    def test_blank_codec_text_is_not_classified_as_anything(self) -> None:
        """``_classify_audio_segment`` answers None for text it has no opinion on.

        The classifier only ever sees authoritative codec text, and the
        chain's ranking is built from its answers; classifying an empty or
        whitespace-only codec as some family would put a track the probe
        could not name into the ranking as if it were known.
        """
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(blank=repr(blank)):
                self.assertIsNone(pc._classify_audio_segment(blank))
        self.assertIsNotNone(pc._classify_audio_segment("TRUEHD"))


class ProbeCacheDegradedTests(unittest.TestCase):
    """A cache is rebuildable by definition, so it may never fail a run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="probecache_")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.json_cache = self.root / "probe_cache.json"
        self.db = self.root / "state.db"

    def _json_cache_with(self, document: object) -> Path:
        self.json_cache.write_text(json.dumps(document), encoding="utf-8")
        return self.json_cache

    def test_a_json_cache_that_is_not_an_object_is_a_cold_cache(self) -> None:
        """A list, a string or a number where the document should be.

        Whatever wrote it, the answer has to be "nothing cached" - re-probing
        every movie costs minutes, guessing at a layout costs a wrong answer.
        """
        for document in ([], "text", 42, None):
            with self.subTest(document=document):
                path = self._json_cache_with(document)
                backend = probecache._JsonBackend(path, "10bit", 1)
                self.assertEqual(backend.load(), {})

    def test_a_json_cache_whose_entries_are_not_an_object_is_a_cold_cache(self) -> None:
        path = self._json_cache_with({"schema": 1, "tool": "10bit", "entries": ["not", "a", "map"]})
        self.assertEqual(probecache._JsonBackend(path, "10bit", 1).load(), {})

    def test_an_entry_that_is_not_an_object_is_dropped_not_crashed_on(self) -> None:
        path = self._json_cache_with(
            {"schema": 1, "tool": "10bit", "entries": {"/m/a.mkv": {"size": 1}, "/m/b.mkv": "junk"}})
        loaded = probecache._JsonBackend(path, "10bit", 1).load()
        self.assertEqual(list(loaded), ["/m/a.mkv"])

    def test_a_cache_that_cannot_be_written_does_not_fail_the_run(self) -> None:
        """The scar shape: a report directory on a share that went read-only.

        Every tool saves its probe cache at the end of a run. Raising there
        would turn a finished, successful sweep into a non-zero exit code that
        a cron wrapper pages about - for a file whose only purpose is to make
        the *next* run faster.
        """
        backend = probecache._JsonBackend(self.json_cache, "10bit", 1)
        with mock.patch.object(probecache, "atomic_write_text",
                               side_effect=OSError("read-only filesystem")):
            backend.save({"/m/a.mkv": {"size": 1, "mtime_ns": 2, "payload": {"x": 1}}})
        self.assertFalse(self.json_cache.exists(), "nothing half-written is left behind")

    def test_a_working_json_cache_round_trips(self) -> None:
        backend = probecache._JsonBackend(self.json_cache, "10bit", 1)
        entries = {"/m/a.mkv": {"size": 1, "mtime_ns": 2, "payload": {"container": {}}}}
        backend.save(entries)
        self.assertEqual(backend.load(), entries)

    def test_a_database_whose_probe_table_cannot_be_read_is_a_cold_cache(self) -> None:
        """A table another tool is mid-migration on, or a file that is not a DB.

        The probe cache lives in the shared state database, which more than one
        process writes; a failed SELECT has to cost a re-probe, not a run.
        """
        backend = probecache._SqliteBackend(self.db, "10bit")
        real_connect = probecache._SqliteBackend._connect
        faulty = FaultyConnection(real_connect(backend), ("SELECT",))
        with mock.patch.object(backend, "_connect", lambda: faulty):
            self.assertEqual(backend.load(), {})
        self.assertTrue(faulty.executed, "the read was attempted before it failed")

    def test_an_entry_without_a_payload_is_not_written_to_the_database(self) -> None:
        backend = probecache._SqliteBackend(self.db, "10bit")
        backend.save({"/m/a.mkv": {"size": 1, "mtime_ns": 2},
                      "/m/b.mkv": {"size": 3, "mtime_ns": 4, "payload": {"tracks": []}}})
        self.assertEqual(list(backend.load()), ["/m/b.mkv"])

    def test_a_save_that_fails_mid_transaction_is_rolled_back_and_swallowed(self) -> None:
        """A half-written cache is worse than an empty one, and neither is fatal.

        The rollback matters beyond tidiness: leaving the transaction open
        would make the next ``BEGIN IMMEDIATE`` fail, and from there every
        write in the run silently happens outside a transaction.
        """
        backend = probecache._SqliteBackend(self.db, "10bit")
        backend.save({"/m/a.mkv": {"size": 1, "mtime_ns": 2, "payload": {"first": True}}})
        real_connect = probecache._SqliteBackend._connect
        faulty = FaultyConnection(real_connect(backend), ("INSERT",))
        with mock.patch.object(backend, "_connect", lambda: faulty):
            backend.save({"/m/b.mkv": {"size": 9, "mtime_ns": 9, "payload": {"second": True}}})
        self.assertIn("ROLLBACK", faulty.executed, "the open transaction is ended, not abandoned")
        self.assertEqual(list(backend.load()), ["/m/a.mkv"],
                         "the interrupted save changed nothing")

    def test_a_database_that_cannot_be_connected_to_is_a_cold_cache(self) -> None:
        backend = probecache._SqliteBackend(self.root / "sub" / "state.db", "10bit")
        with mock.patch.object(probecache.sqlite3, "connect",
                               side_effect=sqlite3.OperationalError("unable to open database")):
            self.assertEqual(backend.load(), {})
            backend.save({"/m/a.mkv": {"size": 1, "mtime_ns": 2, "payload": {}}})


class FaultyConnection:
    """A sqlite connection that fails on chosen statements, and records them all.

    ``sqlite3.Connection`` attributes are read-only, so the failure cannot be
    patched onto the real object; the proxy stands in for it and forwards
    everything else. ``_real_connect`` is the untouched ``_connect`` of the
    backend under test, captured before the proxy is installed.
    """

    def __init__(self, real: sqlite3.Connection, fail_on: tuple[str, ...]) -> None:
        self._real = real
        self.fail_on = tuple(token.upper() for token in fail_on)
        self.executed: list[str] = []

    def _guard(self, sql: str) -> None:
        self.executed.append(sql.split()[0].upper())
        if any(token in sql.upper() for token in self.fail_on):
            raise sqlite3.OperationalError("database is locked")

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        self._guard(sql)
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args: object, **kwargs: object) -> object:
        self._guard(sql)
        return self._real.executemany(sql, *args, **kwargs)

    def close(self) -> None:
        self._real.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


if __name__ == "__main__":
    unittest.main()
