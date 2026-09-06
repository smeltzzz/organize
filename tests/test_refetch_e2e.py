"""The replacement fetch: the one path that writes over a subtitle a user has.

`refetch_english_srt` is the seam between the two tools. `sync_subtitles.py`
calls it when ffsubsync cannot trust the sidecar it was given, and it is the
only code in this repo that is *allowed* to replace an existing subtitle file.
Everywhere else a sidecar is created or left alone.

That makes it the highest-consequence function in the fetcher and it had no
end-to-end coverage at all: the planners it calls were tested as pure
functions and the clients were tested against canned payloads, but nothing
drove the whole thing — search, fall back, download, validate, swap — over a
real file on disk. A regression here does not lose a download, it loses the
subtitle the caller already had.

So these tests run the real function against `tests/fake_provider.py`
installed at `urllib.request.urlopen`, and every one of them ends by asking
the same question: what is in the movie folder now? The contract being pinned:

* the live sidecar survives every possible failure — no key, no candidate,
  every candidate already tried, a provider outage, an HTML error page served
  as a subtitle, the movie changing mid-fetch;
* it is replaced only by bytes that arrived complete and validated, in one
  atomic swap, with nothing left behind either way;
* a symlink is never followed;
* the refusals stay refusals: machine-translated, AI-translated,
  foreign-parts-only and non-English uploads are not replacements, and a
  SubDL release match below the documented threshold is not one either.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fake_provider as fake

import subtitle_fetcher as sf

GOOD_RELEASE = "The.Dark.Knight.2008.1080p.BluRay.x264-GROUP"
OTHER_RELEASE = "The.Dark.Knight.2008.1080p.BluRay.x265-qXR"
BIG = 2 * 1024 * 1024

# What sync_subtitles hands over: a sidecar that is real, valid, and wrong.
ORIGINAL = (
    "1\n"
    "00:00:02,000 --> 00:00:05,000\n"
    "The subtitle that would not sync.\n"
)

ENV_KEYS = ("OPENSUBTITLES_API_KEY", "SUBDL_API_KEY", "OPENSUBTITLES_USERNAME",
            "OPENSUBTITLES_PASSWORD")


class RefetchFixture(unittest.TestCase):
    """One standardized movie, one sidecar the caller wants replaced."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sf_refetch_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.folder = self.root / "The Dark Knight (2008)"
        self.folder.mkdir()
        self.video = self.folder / "The Dark Knight (2008).mkv"
        with self.video.open("wb") as handle:
            handle.truncate(BIG)
        self.dest = self.folder / "The Dark Knight (2008).eng.srt"
        self.dest.write_text(ORIGINAL, encoding="utf-8")
        self.log = self.root / "refetch.log"

        self.set_keys(opensubtitles="test-api-key")

        # Pacing and retry backoff are arithmetic tested elsewhere; here they
        # would only add wall-clock time.
        gap = mock.patch.object(sf, "REQUEST_GAP_SEC", 0.0)
        gap.start()
        self.addCleanup(gap.stop)
        sleep = mock.patch.object(sf.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

        self.provider = fake.FakeOpenSubtitles(
            hash_results=[fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                                        title="The Dark Knight", year=2008)],
        )
        self.subdl = fake.FakeSubdl(
            release_results=[fake.subdl_subtitle("sub123", GOOD_RELEASE)],
        )
        self.both = fake.FakeProviders(self.provider, self.subdl)

    # -- arranging ----------------------------------------------------------

    def set_keys(self, *, opensubtitles: str = "", subdl: str = "") -> None:
        env = dict.fromkeys(ENV_KEYS, "")
        env["OPENSUBTITLES_API_KEY"] = opensubtitles
        env["SUBDL_API_KEY"] = subdl
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- running ------------------------------------------------------------

    def refetch(self, *, exclude: list[int | str] | None = None,
                provider: object | None = None) -> tuple[bool, str, str]:
        target = self.provider if provider is None else provider
        with mock.patch.object(sf.urllib.request, "urlopen", target):
            return sf.refetch_english_srt(
                self.video, self.dest,
                exclude_ids=exclude or [], log_file=self.log,
            )

    # -- looking at the result ---------------------------------------------

    def sidecar_text(self) -> str:
        return self.dest.read_text(encoding="utf-8")

    def folder_files(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir())

    def assert_original_survived(self) -> None:
        """Nothing about the caller's file changed, and nothing was left behind."""
        self.assertEqual(self.sidecar_text(), ORIGINAL)
        self.assertEqual(self.folder_files(),
                         ["The Dark Knight (2008).eng.srt", "The Dark Knight (2008).mkv"])


class ReplacingTheSidecarTests(RefetchFixture):
    """The path that ends with different bytes in the same file."""

    def test_a_validated_replacement_is_swapped_in(self) -> None:
        ok, file_id, detail = self.refetch()
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "9001")
        self.assertEqual(detail, GOOD_RELEASE)
        self.assertEqual(self.sidecar_text(), fake.SRT_TEXT)

    def test_the_swap_leaves_nothing_behind(self) -> None:
        self.assertTrue(self.refetch()[0])
        self.assertEqual(self.folder_files(),
                         ["The Dark Knight (2008).eng.srt", "The Dark Knight (2008).mkv"])

    def test_the_original_is_intact_until_the_download_has_finished(self) -> None:
        """The staging file is a separate file, not the sidecar being rewritten.

        A caller whose process dies mid-download must still find its own
        subtitle, so the replacement may only become visible in one step at
        the very end.
        """
        seen: list[str] = []
        self.provider.on_download = lambda: seen.append(self.sidecar_text())
        self.assertTrue(self.refetch()[0])
        self.assertEqual(seen, [ORIGINAL])

    def test_a_missing_sidecar_is_simply_written(self) -> None:
        """sync may have quarantined the bad file before asking for a new one."""
        self.dest.unlink()
        ok, _file_id, detail = self.refetch()
        self.assertTrue(ok, detail)
        self.assertEqual(self.sidecar_text(), fake.SRT_TEXT)


class NotDownloadingTheSameThingTwiceTests(RefetchFixture):
    """`exclude_ids` is how the caller says "that one did not sync either"."""

    def setUp(self) -> None:
        super().setUp()
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, downloads=900,
                          title="The Dark Knight", year=2008),
            fake.subtitle(9002, OTHER_RELEASE, downloads=100,
                          title="The Dark Knight", year=2008),
        ]

    def test_an_excluded_upload_is_passed_over_for_the_next_one(self) -> None:
        ok, file_id, detail = self.refetch(exclude=[9001])
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "9002")

    def test_the_exclusion_is_by_value_not_by_type(self) -> None:
        """sync_subtitles carries the ids it has already tried as strings."""
        ok, file_id, detail = self.refetch(exclude=["9001"])
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "9002")

    def test_when_every_upload_has_been_tried_the_original_stays(self) -> None:
        ok, file_id, detail = self.refetch(exclude=[9001, 9002])
        self.assertFalse(ok)
        self.assertEqual(file_id, "")
        self.assertIn("no unused qualifying English SRT", detail)
        self.assert_original_survived()


class RefusingToReplaceTests(RefetchFixture):
    """Every way this can fail ends with the caller's file untouched."""

    def test_without_a_key_nothing_is_attempted(self) -> None:
        self.set_keys()
        ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "")
        self.assertIn("no subtitle API key", detail)
        self.assertEqual(self.provider.calls, [])
        self.assert_original_survived()

    def test_a_symlink_sidecar_is_never_followed(self) -> None:
        real = self.root / "elsewhere.srt"
        real.write_text(ORIGINAL, encoding="utf-8")
        self.dest.unlink()
        self.dest.symlink_to(real)
        ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertEqual(detail, "refusing to replace a symlink sidecar")
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), ORIGINAL)
        self.assertEqual(self.provider.count("/download"), 0)

    def test_an_html_error_page_served_as_a_subtitle_is_rejected(self) -> None:
        self.provider.srt_text = "<!DOCTYPE html><html>rate limited</html>"
        ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertTrue(detail)
        self.assert_original_survived()

    def test_a_provider_outage_during_the_download_changes_nothing(self) -> None:
        self.provider.fail("/download", 429, 429, 429, 429, 429)
        ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertTrue(detail)
        self.assert_original_survived()

    def test_a_search_that_never_answers_changes_nothing(self) -> None:
        self.provider.fail("/subtitles", *([500] * 12))
        ok, _file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertIn("no unused qualifying English SRT", detail)
        self.assert_original_survived()
        self.assertIn("replacement hash search failed",
                      self.log.read_text(encoding="utf-8"))

    def test_the_second_look_at_the_staged_bytes_is_final(self) -> None:
        """What was written is validated again from disk before it is published.

        The client already checked the payload in memory, so the two verdicts
        agree by construction and nothing a provider can send reaches this
        branch — which is exactly why it is worth pinning: it is the last
        thing standing between "the bytes changed shape on the way to the
        filesystem" and a movie whose subtitle is now an error page. The only
        way to exercise it is to make the check fail.
        """
        with mock.patch.object(sf, "validate_srt_sidecar",
                               return_value=(False, "subtitle contains no valid SRT cue")):
            ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertIn("downloaded replacement is unusable", detail)
        self.assert_original_survived()

    def test_a_failure_at_the_publish_step_still_cleans_up(self) -> None:
        """The swap is the one step that can fail with bytes already staged.

        Everything earlier fails before a file exists; if the rename itself
        goes (a full disk, an I/O error), the staged download must not be left
        sitting in the movie folder for the auditor to find.
        """
        with mock.patch.object(sf.os, "replace",
                               side_effect=OSError("no space left on device")):
            ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertIn("no space left on device", detail)
        self.assert_original_survived()

    def test_the_movie_changing_under_the_fetch_stops_the_swap(self) -> None:
        """The subtitle was chosen for *this* file; if it is not this file, stop."""
        def rewrite_the_movie() -> None:
            # Appending rather than overwriting in place: the snapshot is
            # (device, inode, size, mtime), and a same-size edit inside one
            # filesystem timestamp tick is not detectable by design.
            with self.video.open("ab") as handle:
                handle.write(b"a different movie entirely")

        self.provider.on_download = rewrite_the_movie
        ok, file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertEqual(file_id, "9001")
        self.assertTrue(detail)
        self.assertEqual(self.sidecar_text(), ORIGINAL)

    def test_a_machine_translated_upload_is_not_a_replacement(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, machine_translated=True,
                          title="The Dark Knight", year=2008)]
        ok, _file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertIn("no unused qualifying English SRT", detail)
        self.assert_original_survived()

    def test_a_non_english_upload_is_not_a_replacement(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(9001, GOOD_RELEASE, language="es",
                          title="The Dark Knight", year=2008)]
        self.assertFalse(self.refetch()[0])
        self.assert_original_survived()

    def test_an_upload_for_another_movie_is_not_a_replacement(self) -> None:
        self.provider.hash_results = [
            fake.subtitle(9001, "Heat.1995.1080p.BluRay.x264-GROUP",
                          title="Heat", year=1995)]
        self.assertFalse(self.refetch()[0])
        self.assert_original_survived()

    def test_a_movie_that_is_not_named_canonically_gets_no_title_fallback(self) -> None:
        """Without `Title (Year)` there is no identity, so only the hash route runs."""
        odd = self.folder / "dark knight 1080p.mkv"
        self.video.rename(odd)
        self.video = odd
        self.provider.hash_results = []
        self.provider.identity_results = [
            fake.subtitle(9003, GOOD_RELEASE, moviehash_match=False,
                          title="The Dark Knight", year=2008)]
        ok, _file_id, detail = self.refetch()
        self.assertFalse(ok)
        self.assertIn("no unused qualifying English SRT", detail)
        self.assertEqual(self.provider.count("/download"), 0)


class FallingBackToTheTitleSearchTests(RefetchFixture):
    """No hash match is normal: the file may be a different cut of the movie."""

    def setUp(self) -> None:
        super().setUp()
        self.provider.hash_results = []
        self.provider.identity_results = [
            fake.subtitle(9003, GOOD_RELEASE, moviehash_match=False, downloads=700,
                          title="The Dark Knight", year=2008)]

    def test_the_title_search_answers_when_the_hash_search_does_not(self) -> None:
        ok, file_id, detail = self.refetch()
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "9003")
        self.assertEqual(self.sidecar_text(), fake.SRT_TEXT)

    def test_a_hash_search_outage_still_lets_the_title_search_run(self) -> None:
        self.provider.fail("moviehash", *([500] * 6))
        ok, file_id, detail = self.refetch()
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "9003")
        self.assertIn("replacement hash search failed",
                      self.log.read_text(encoding="utf-8"))


class TheSecondProviderTests(RefetchFixture):
    """SubDL is asked only when OpenSubtitles has nothing to offer."""

    def setUp(self) -> None:
        super().setUp()
        self.set_keys(opensubtitles="test-api-key", subdl="test-subdl-key")

    def test_subdl_answers_when_opensubtitles_has_nothing(self) -> None:
        self.provider.hash_results = []
        ok, file_id, detail = self.refetch(provider=self.both)
        self.assertTrue(ok, detail)
        # The id comes back namespaced by provider, which is what makes it
        # safe for the caller to feed straight back in as an exclusion.
        self.assertEqual(file_id, "subdl:sub123")
        self.assertEqual(self.sidecar_text(), fake.SRT_TEXT)
        self.assertEqual(self.subdl.count("download"), 1)

    def test_subdl_is_not_asked_when_opensubtitles_answered(self) -> None:
        ok, file_id, _detail = self.refetch(provider=self.both)
        self.assertTrue(ok)
        self.assertEqual(file_id, "9001")
        self.assertEqual(self.subdl.calls, [])

    def test_a_weak_subdl_release_match_falls_through_to_the_title_route(self) -> None:
        self.provider.hash_results = []
        self.subdl.release_results = [
            fake.subdl_subtitle("sub123", GOOD_RELEASE, match_score=0.4)]
        self.subdl.movies["thedarkknight"]["titled"] = [
            fake.subdl_subtitle("sub456", GOOD_RELEASE, match_score=None)]
        ok, file_id, detail = self.refetch(provider=self.both)
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "subdl:sub456")

    def test_subdl_alone_is_enough_to_try(self) -> None:
        self.set_keys(subdl="test-subdl-key")
        ok, file_id, detail = self.refetch(provider=self.both)
        self.assertTrue(ok, detail)
        self.assertEqual(file_id, "subdl:sub123")
        self.assertEqual(self.provider.calls, [])

    def test_a_subdl_outage_leaves_the_original_in_place(self) -> None:
        self.provider.hash_results = []
        self.subdl.fail("/files/search", *([500] * 6))
        self.subdl.fail("/subtitles/search", *([500] * 6))
        ok, _file_id, detail = self.refetch(provider=self.both)
        self.assertFalse(ok)
        self.assertIn("no unused qualifying English SRT", detail)
        self.assert_original_survived()
        self.assertIn("replacement SubDL search failed",
                      self.log.read_text(encoding="utf-8"))

    def test_an_excluded_subdl_upload_is_not_downloaded_again(self) -> None:
        """The id the caller was given last time is the id that excludes it."""
        self.provider.hash_results = []
        ok, _file_id, detail = self.refetch(exclude=["subdl:sub123"], provider=self.both)
        self.assertFalse(ok, detail)
        self.assertEqual(self.subdl.count("download"), 0)
        self.assert_original_survived()


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
