"""End-to-end runs of ``audio_standardizer.py`` against fake FFmpeg binaries.

The tool's value is in what it *does*: one ffmpeg invocation per movie whose
best audio cannot leave the G454V, an appended AC-3 track, an atomic publish,
and an original that is never half-replaced. Fakes run as real child
processes (``tests/fakebin.py``) so quoting, exit codes and the second
verification probe are all real; only the heavy lifting is simulated.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fake_ffprobe as fakeff
import fakebin

import audio_standardizer as aus
from organizekit.core import playbackchain as pc

WINDOWS = os.name == "nt"


def _payload(*streams: dict, duration: str = "7200.000000") -> dict:
    """An ffprobe payload with a hevc video stream plus ``streams`` audio."""
    out = [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]
    for position, stream in enumerate(streams, start=1):
        entry = {"index": position, "codec_type": "audio", "channels": 6,
                 "sample_rate": 48000,
                 "tags": {"language": "eng"}, "disposition": {"default": position == 1}}
        entry.update(stream)
        out.append(entry)
    return {"streams": out, "format": {"duration": duration, "size": "8388608"}}


TRUEHD_ONLY = _payload({"codec_name": "truehd", "channels": 8})
DTSHD_ONLY = _payload({"codec_name": "dts", "profile": "DTS-HD MA", "channels": 8})
EAC3_DONE = _payload({"codec_name": "eac3", "channels": 6})
DTS_CORE = _payload({"codec_name": "dts", "channels": 6})
UNKNOWN_AUDIO = _payload({"codec_name": "gsm_ms", "channels": 2})
FLAC_MULTI = _payload({"codec_name": "flac", "channels": 6})
# A real E-AC-3 stream whose title narrates the source it came from — the
# shape that used to be misread as transcode-bound (ffprobe reports no
# profile for E-AC-3, and the title then sat in the profile's field).
EAC3_TITLED_TRUEHD = _payload(
    {"codec_name": "eac3", "profile": "unknown", "channels": 6,
     "tags": {"language": "eng", "title": "TrueHD 7.1"}})
EAC3_TITLED_DTSHD = _payload(
    {"codec_name": "eac3", "channels": 6,
     "tags": {"language": "eng", "title": "DTS-HD MA 7.1"}})
FOREIGN_JPN = _payload(
    {"codec_name": "truehd", "channels": 8, "tags": {"language": "jpn"}},
    {"codec_name": "dts", "channels": 6, "tags": {"language": "eng"}},
)


class ChainFixture(unittest.TestCase):
    """Common scaffolding: a Movies/ library and both fake binaries."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="affix_e2e_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.library = self.tmp / "Movies"
        self.library.mkdir()
        self.log = self.tmp / "out" / "audio_standardizer.log"
        self.report = self.tmp / "out" / "audio_standardizer_report.txt"
        self.state_db = self.tmp / "out" / "state.db"
        self.ffmpegs = fakebin.install_python_shim(self.tmp / "bin", "ffmpeg", "fake_ffmpeg")
        self.ffprobes = fakebin.install_python_shim(self.tmp / "bin", "ffprobe", "fake_ffprobe")
        self._saved_log_file = aus.log.file
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        aus.log.file = self._saved_log_file

    def movie(self, name: str, payload: dict, size: int = 2 * 1024 * 1024) -> Path:
        path = self.library / name / f"{name}.mkv"
        fakeff.write_movie(path, payload, size=size)
        return path

    def _run(self, *extra: str, env: dict[str, str] | None = None) -> int:
        argv = ["--source", str(self.library), "--log", str(self.log),
                "--report", str(self.report), "--state-db", str(self.state_db),
                "--ffprobe", str(self.ffprobes), "--ffmpeg", str(self.ffmpegs),
                "--workers", "2", *extra]
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, env or {}):
            return aus.main(argv)

    def report_text(self) -> str:
        return self.report.read_text(encoding="utf-8")

    def report_section(self, header_fragment: str) -> str:
        """The text of one report section (headers always print; order varies)."""
        text = self.report_text()
        start = text.index(header_fragment)
        rest = text[start + len(header_fragment):]
        nxt = rest.find("\n  ══ ")
        return rest[:nxt] if nxt != -1 else rest

    def plan_rows(self) -> dict[str, str]:
        """Movie name -> verdict, from the shared state cache when published."""
        from organizekit.core import KIND_AUDIOFIT, open_state

        store = open_state(self.state_db, tool="tests")
        self.addCleanup(store.close)
        return {Path(key).stem: row.verdict
                for (key, _), row in store.verdicts(KIND_AUDIOFIT).items()}

    def ffmpeg_invocations(self) -> list[list[str]]:
        log_path = self.tmp / "ffmpeg_invocations.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def payload_of(path: Path) -> dict:
        return fakeff.read_payload(path)


@unittest.skipIf(WINDOWS, "the fakes are launched through a POSIX shebang")
class DryRunPlansTheWholeLibraryTests(ChainFixture):
    """--dry-run states every verdict and touches nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.truehd = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        self.dtshd = self.movie("DTS HD Film (2002)", DTSHD_ONLY)
        self.eac3 = self.movie("Ready Film (2003)", EAC3_DONE)
        self.dts = self.movie("DTS Film (2004)", DTS_CORE)
        self.unknown = self.movie("Odd Film (2005)", UNKNOWN_AUDIO)
        self.before = {p: p.read_bytes() for p in (self.truehd, self.dtshd, self.eac3, self.dts, self.unknown)}

    def test_a_dry_run_changes_no_bytes(self) -> None:
        self.assertEqual(self._run("--dry-run"), 0)
        for path, before in self.before.items():
            with self.subTest(movie=path.name):
                self.assertEqual(path.read_bytes(), before)

    def test_every_verdict_is_named_in_the_report(self) -> None:
        self.assertEqual(self._run("--dry-run"), 0)
        report = self.report_text()
        # Dry-run verbs: PLANNED rows are named by their will-do status.
        for verdict in (aus.STATUS_DTS, aus.STATUS_REVIEW):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, report)
        self.assertIn("WOULD TRANSCODE", report)
        self.assertIn("CHAIN-NATIVE", report)
        # And applying the same library must cross those rows over.
        self.assertEqual(self._run(), 0)
        report = self.report_text()
        self.assertIn(aus.STATUS_TRANSCODED, report)

    def test_the_plan_is_pure_without_ffmpeg(self) -> None:
        # Doctor-level degradation: ffprobe alone must still print the plan.
        self.assertEqual(self._run("--dry-run", "--ffmpeg", str(self.tmp / "no-such-ffmpeg")), 0)


@unittest.skipIf(WINDOWS, "the fakes are launched through a POSIX shebang")
class RealTranscodeRunTests(ChainFixture):
    def test_truehd_and_dtshd_gain_a_dolby_digital_plus_track_that_passes_verification(self) -> None:
        truehd = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        dtshd = self.movie("DTS HD Film (2002)", DTSHD_ONLY)
        done = self.movie("Ready Film (2003)", EAC3_DONE)
        odd = self.movie("Odd Film (2005)", UNKNOWN_AUDIO)
        timestamp_before = done.read_bytes()

        code = self._run(env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)

        # The two lossless files were replaced by a verified superset. On the
        # default wiring the synthesized track is Dolby Digital Plus folded to
        # 5.1 — the widest layout ffmpeg's Dolby encoders can actually write
        # (an 8-channel master no longer buys an 8-channel promise that fails).
        for path, before_streams in ((truehd, TRUEHD_ONLY), (dtshd, DTSHD_ONLY)):
            streams = self.payload_of(path)["streams"]
            with self.subTest(movie=path.name):
                self.assertEqual(len(streams), len(before_streams["streams"]) + 1)
                self.assertEqual(streams[-1]["codec_name"], "eac3")
                self.assertEqual(streams[-1]["channels"], 6)
                self.assertEqual(streams[0]["codec_name"], "hevc")
        # The already-native file and the reviewable one were never opened.
        self.assertEqual(done.read_bytes(), timestamp_before)
        self.assertEqual(self.payload_of(odd)["streams"][1]["codec_name"], "gsm_ms")

        # Exactly two ffmpeg runs, shaped as the docs promise.
        calls = self.ffmpeg_invocations()
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertIn("-c:a:1", args)
            self.assertIn("eac3", args)
            self.assertIn("640k", args)
            self.assertIn("0:1", args)  # '-map 0:1' input-side
            # The command line itself must respect the encoder ceiling — this
            # is the exact argument (`-ac:a:1 8`) that made ffmpeg fail and
            # write nothing for every >=7.1 master before 8.4.0.
            ac_idx = args.index("-ac:a:1")
            self.assertEqual(args[ac_idx + 1], "6")
        # State remembers what was done.
        rows = self.plan_rows()
        self.assertEqual(rows["TrueHD Film (2001)"], aus.STATUS_TRANSCODED)
        self.assertEqual(rows["DTS HD Film (2002)"], aus.STATUS_TRANSCODED)

        # A second pass is a no-op: the appended AC-3 makes both files settled
        # (the transcoded bucket is empty; both now report chain-native).
        code = self._run()
        self.assertEqual(code, 0)
        rows = self.plan_rows()
        self.assertEqual(rows["TrueHD Film (2001)"], aus.STATUS_NATIVE)
        self.assertEqual(rows["DTS HD Film (2002)"], aus.STATUS_NATIVE)

    def test_the_source_is_the_native_language_track_not_the_dub(self) -> None:
        film = self.movie("Jidaigeki (1999)", FOREIGN_JPN)
        code = self._run(env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        (args,) = self.ffmpeg_invocations()
        self.assertIn("language=jpn", args)
        streams = self.payload_of(film)["streams"]
        self.assertEqual(streams[-1]["tags"]["language"], "jpn")

    def test_dts_core_is_accepted_by_default_and_transcoded_on_request(self) -> None:
        film = self.movie("DTS Film (2004)", DTS_CORE)
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.payload_of(film)["streams"][1]["codec_name"], "dts")
        # With passthrough refused, the same file is transcoded — to the
        # default wiring's Dolby Digital Plus target.
        before = film.read_bytes()
        code = self._run("--no-dts-passthrough",
                        env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        self.assertNotEqual(film.read_bytes(), before)
        self.assertEqual(self.payload_of(film)["streams"][-1]["codec_name"], "eac3")

    def test_the_default_hdmi_in_wiring_accepts_multichannel_flac(self) -> None:
        # The default wiring is the chain as cabled: the Chromecast feeds the
        # soundbar's HDMI IN, which carries multichannel PCM, so a 6-channel
        # FLAC is done — no ffmpeg run, no new track, not even in a plan.
        film = self.movie("Concert (2010)", FLAC_MULTI)
        before = film.read_bytes()
        code = self._run("--dry-run",
                         env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        self.assertIn("Concert (2010).mkv", self.report_section("DECODE-TO-PCM"))
        self.assertIn(pc.WIRING_SOUNDBAR_HDMI_IN, self.report_text())
        # Applying the plan leaves the file byte-identical and runs no ffmpeg.
        self.assertEqual(self._run(), 0)
        self.assertEqual(film.read_bytes(), before)
        self.assertEqual(self.ffmpeg_invocations(), [])
        self.assertEqual(self.plan_rows()["Concert (2010)"], aus.STATUS_PCM)

    def test_the_explicit_arc_wiring_transcodes_multichannel_flac(self) -> None:
        # `--wiring tv-arc` is the supported alternative: this TV offers PCM
        # only for HDMI sources, so a 6-channel FLAC becomes AC-3.
        film = self.movie("Concert (2010)", FLAC_MULTI)
        self.assertEqual(self._run("--wiring", pc.WIRING_TV_ARC, "--dry-run"), 0)
        self.assertIn("Concert (2010).mkv", self.report_section("WOULD TRANSCODE"))
        self.assertIn(pc.WIRING_TV_ARC, self.report_text())
        # And the run actually performs the conversion it planned.
        code = self._run("--wiring", pc.WIRING_TV_ARC,
                         env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        self.assertEqual(self.payload_of(film)["streams"][-1]["codec_name"], "ac3")

    def test_the_environment_variable_selects_the_arc_alternative(self) -> None:
        # No flag: ORGANIZE_PLAYBACK_WIRING is the documented override, and
        # setting it to tv-arc re-enables the multichannel-PCM compensation.
        self.movie("Concert (2010)", FLAC_MULTI)
        code = self._run("--dry-run",
                         env={pc.WIRING_ENV_VAR: pc.WIRING_TV_ARC})
        self.assertEqual(code, 0)
        self.assertIn("Concert (2010).mkv", self.report_section("WOULD TRANSCODE"))
        # Unset (the ambient default), the same file plans no work at all.
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertIn("Concert (2010).mkv", self.report_section("DECODE-TO-PCM"))

    def test_an_eac3_stream_titled_like_a_lossless_master_is_never_transcoded(self) -> None:
        """The reported false positive, end to end.

        ffprobe reports no (or `unknown`) profile for E-AC-3 streams, so the
        track title used to be read as the profile field: an actual `eac3`
        stream titled "TrueHD 7.1" was scheduled for an AC-3 transcode that
        never should have happened. It must be chain-native, untouched, and
        absent from the transcode bucket — in the report and in state.
        """
        titled = self.movie("Titled Film (2007)", EAC3_TITLED_TRUEHD)
        titled_dts = self.movie("Titled DTS Film (2008)", EAC3_TITLED_DTSHD)
        # A genuine lossless master in the same run must still be transcoded,
        # so this regression cannot pass by disabling the transcode entirely.
        master = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = {path: path.read_bytes() for path in (titled, titled_dts)}
        env = {"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")}

        code = self._run("--dry-run", env=env)
        self.assertEqual(code, 0)
        for path in (titled, titled_dts):
            with self.subTest(movie=path.name):
                self.assertIn(path.name, self.report_section("CHAIN-NATIVE"))
                self.assertNotIn(path.name, self.report_section("WOULD TRANSCODE"))
                self.assertEqual(self.plan_rows()[path.stem], aus.STATUS_NATIVE)
        self.assertIn("TrueHD Film (2001).mkv", self.report_section("WOULD TRANSCODE"))

        # Applying is the same: the titled E-AC-3 files are untouched and no
        # ffmpeg runs for them; the TrueHD master is converted exactly once.
        code = self._run(env=env)
        self.assertEqual(code, 0)
        for path in (titled, titled_dts):
            self.assertEqual(path.read_bytes(), before[path])
            self.assertEqual(self.payload_of(path)["streams"][1]["codec_name"], "eac3")
            self.assertEqual(self.plan_rows()[path.stem], aus.STATUS_NATIVE)
        runs = self.ffmpeg_invocations()
        self.assertEqual(len(runs), 1, "only the real TrueHD master may be transcoded")
        self.assertIn(str(master), " ".join(runs[0]))

    def test_a_seeding_release_is_deferred_not_broken(self) -> None:
        film = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        seeds = self.tmp / "torrents"
        seeds.mkdir()
        seed = seeds / film.name
        os.link(film, seed)
        before = film.read_bytes()
        self.assertEqual(self._run(), 0)
        self.assertEqual(film.read_bytes(), before, "the seeding hardlink must survive")
        report = self.report_text()
        self.assertIn(aus.STATUS_DEFERRED, report)
        self.assertIn("still hardlinked to a seeding source", report)

    def test_a_failed_encode_costs_nothing_original(self) -> None:
        film = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = film.read_bytes()
        code = self._run(env={"FAKE_FFMPEG_RC": "9"})
        self.assertEqual(code, 1)
        self.assertEqual(film.read_bytes(), before)
        self.assertFalse(list(self.library.rglob("*.audiofit-*.tmp.mkv")),
                         "the temp file is swept after a failure")
        report = self.report_text()
        self.assertIn(aus.STATUS_ERROR, report)
        self.assertIn("Conversion failed!", report)

    def test_an_output_ffmpeg_claims_but_cannot_have_made_is_refused(self) -> None:
        film = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = film.read_bytes()
        code = self._run(env={"FAKE_FFMPEG_WRONG_TRACK": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(film.read_bytes(), before)

    def test_an_appended_track_of_the_wrong_width_is_refused(self) -> None:
        """The *geometry* half of the publish proof, which nothing had tested.

        README's safety invariant 8 promises "the appended track must be the
        requested Dolby codec with the promised geometry", and `verify_output`
        checks both — but only the codec half had a knob and a test. Dropping
        the channel comparison would have left the suite green while a stereo
        track was published as a movie's new default audio, over the lossless
        master it was made from. A wrong width is not a hypothetical: it is
        what an encoder that cannot honour `-ac` produces, which is the shape
        of the failure 8.4.0 was released for.
        """
        film = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = film.read_bytes()
        code = self._run(env={"FAKE_FFMPEG_WRONG_CHANNELS": "2"})
        self.assertEqual(code, 1)
        self.assertEqual(film.read_bytes(), before,
                         "a refused publish must leave the original alone")
        self.assertFalse(list(self.library.rglob("*.audiofit-*.tmp.mkv")),
                         "and must sweep its own staging file")
        report = self.report_text()
        self.assertIn(aus.STATUS_ERROR, report)
        self.assertIn("verification refused the transcode", report)
        self.assertIn("expected 6ch", report)

    def test_an_encode_that_reported_success_having_written_nothing_is_refused(self) -> None:
        """ffmpeg exiting 0 with no output file is the 8.4.0 failure, verbatim.

        Its E-AC-3 encoder does not downmix a 7.1 request; it fails and leaves
        no file at all. The tool's guard for that is `not tmp.is_file()` beside
        the return code, and the fake's `FAKE_FFMPEG_NO_OUTPUT` knob had never
        been used — so the branch that catches "success with nothing to show
        for it" was the one branch of the publish path with no test.
        """
        film = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        before = film.read_bytes()
        code = self._run(env={"FAKE_FFMPEG_NO_OUTPUT": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(film.read_bytes(), before)
        self.assertFalse(list(self.library.rglob("*.audiofit-*.tmp.mkv")))
        self.assertIn(aus.STATUS_ERROR, self.report_text())

    def test_empty_and_junk_only_libraries_are_clean_runs(self) -> None:
        stray = self.library / "sample-clip.mkv"
        stray.write_bytes(b"not a movie")
        self.assertEqual(self._run(), 0)
        self.assertIn("Movies inspected", self.report_text())


class StaleTempSweepTests(unittest.TestCase):
    """The housekeeping sweep may remove abandoned debris, and nothing else.

    ``scan()`` starts by deleting every ``*.audiofit-*.tmp.mkv`` under the
    library, because a run killed mid-transcode leaves one behind and it must
    never masquerade as a movie. It asked two questions too few:

    * **Was this a dry run?** ``--dry-run`` is documented as "everything except
      the mutation", and this tool's own report line says "dry-run (no file
      modified)" - yet the sweep deleted a file inside the library anyway. The
      cleaner does not do this: its recovery pass is gated on the single-instance
      lock, which a dry run deliberately does not take.
    * **Is it actually abandoned?** The run lock is keyed by the library path, so
      a sweep over ``/movies`` and a live transcode under ``/movies/4K`` hold
      *different* locks and overlap. The overlapping run's in-flight temp carries
      exactly this pattern, so the sweep deleted the file ffmpeg was writing into
      and that run reported "ffmpeg failed" for a healthy movie. The cleaner's
      recovery has always aged its orphans first (``ORPHAN_MIN_AGE_SECONDS``);
      the age now lives in ``organizekit.core`` and both tools ask it.

    These run without the fake binaries: a library holding only debris is
    discovered as empty, so the sweep is the whole run.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="affix_sweep_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.library = self.tmp / "Movies"
        self.library.mkdir()
        self.log_file = self.tmp / "out" / "audiofit.log"
        self.report = self.tmp / "out" / "audiofit_report.txt"
        self._saved = (aus.log.file, aus.log.live)
        aus.log.file = self.log_file
        aus.log.live = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        aus.log.file, aus.log.live = self._saved

    def _stray(self, title: str = "Film (2001)", *, age: float | None = None,
               pid: int = 4242) -> Path:
        """A debris file named exactly the way ``transcode_movie`` names one."""
        folder = self.library / title
        folder.mkdir(parents=True, exist_ok=True)
        stray = folder / f".{title}.audiofit-{pid}.tmp.mkv"
        stray.write_bytes(b"debris" * 100)
        if age is not None:
            stamped = time.time() - age
            os.utime(stray, (stamped, stamped))
        return stray

    def _scan(self, *, dry_run: bool = False) -> int:
        cfg = aus.Config(
            source_dir=self.library, log_file=self.log_file, report_file=self.report,
            state_db=self.tmp / "out" / "state.db", dry_run=dry_run,
            use_state=False, use_cache=False, min_file_size_mb=1.0,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            return aus.scan(cfg)

    def _listing(self) -> list[str]:
        return sorted(str(p.relative_to(self.library)) for p in self.library.rglob("*"))

    def _log_text(self) -> str:
        return self.log_file.read_text(encoding="utf-8")

    def test_a_dry_run_leaves_the_library_file_for_file(self) -> None:
        """The one mode that promises an untouched library must keep it."""
        stray = self._stray(age=3600.0)
        before = self._listing()
        self.assertEqual(self._scan(dry_run=True), 0)
        self.assertEqual(self._listing(), before,
                         "a dry run changed what is in the library")
        self.assertTrue(stray.is_file(), "the debris is still there to sweep later")
        self.assertIn("Would remove stale temp file", self._log_text())
        self.assertIn("dry-run (no file modified)", self.report.read_text(encoding="utf-8"))

    def test_a_live_run_still_sweeps_abandoned_debris(self) -> None:
        stray = self._stray(age=3600.0)
        self.assertEqual(self._scan(), 0)
        self.assertFalse(stray.exists(), "aged debris is this tool's to clean up")
        self.assertIn("Removed stale temp file", self._log_text())

    def test_a_temp_a_sibling_run_may_still_be_writing_is_left_alone(self) -> None:
        """Fresh debris is indistinguishable from work in flight, so it waits."""
        stray = self._stray()  # mtime = now: a live transcode could be writing it
        self.assertEqual(self._scan(), 0)
        self.assertTrue(stray.is_file(),
                        "a young temp belongs to the run that is writing it")
        self.assertNotIn("Removed stale temp file", self._log_text())

    def test_the_age_it_waits_for_is_the_cleaners_age(self) -> None:
        """One rule for both sweeps, imported rather than retyped."""
        from organizekit import core

        self.assertIs(aus.orphan_is_abandoned, core.orphan_is_abandoned)
        stray = self._stray(age=core.ORPHAN_MIN_AGE_SECONDS + 1.0)
        self.assertEqual(self._scan(), 0)
        self.assertFalse(stray.exists())

    def test_another_tools_staging_is_never_this_sweep_business(self) -> None:
        """The cleaner's orphan temp and journal are the cleaner's to recover.

        audiofit cannot read a cleaner transaction journal, so it has no way to
        know whether that staging file was verified. Deleting it would destroy
        the one artifact ``cleanup_orphan_temps`` promotes a crashed-but-complete
        remux from.
        """
        folder = self.library / "Film (2001)"
        folder.mkdir()
        cleaner_temp = folder / "temp_clean_abc123__Film (2001).mkv"
        cleaner_temp.write_bytes(b"x" * 1024)
        journal = folder / ".track_cleaner.abc123.json"
        journal.write_text("{}", encoding="utf-8")
        aged = time.time() - 3600.0
        for path in (cleaner_temp, journal):
            os.utime(path, (aged, aged))
        before = self._listing()
        self.assertEqual(self._scan(), 0)
        self.assertEqual(self._listing(), before)


class VerificationGuardTests(unittest.TestCase):
    """Every clause of the publish proof, asserted on the guard itself.

    README's safety invariant 8: "the audio standardizer re-probes its own
    output before swapping: every original stream must still be there, the
    appended track must be the requested Dolby codec with the promised
    geometry, and duration drift over ~3 s refuses the publish." The
    end-to-end tests above drive that through a real child process, but only
    on POSIX (the fakes are launched through a shebang), and only for the two
    refusals a fake binary can simulate. These run everywhere and cover the
    clauses nothing else reaches: a stream that vanished, a wrong audio count,
    a probe that cannot be read, and a probe that cannot be run.

    The swap this guard protects is irreversible — it publishes over a movie's
    only copy of its lossless master — so a clause that is merely untested is
    a clause that can be dropped in a refactor without anything going red.
    """

    OLD = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "truehd", "channels": 8},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
        ],
        "format": {"duration": "7200.0"},
    }

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="affix_verify_")
        self.addCleanup(self._td.cleanup)
        self.produced = Path(self._td.name) / "out.mkv"
        self.produced.write_bytes(b"not read by the guard; the probe is stubbed")
        self.cfg = aus.Config(dry_run=False)

    def _verdict(self, channels: int = 8) -> aus.AudioVerdict:
        return aus.AudioVerdict(
            path=str(self.produced), status=aus.STATUS_TRANSCODED,
            category=aus.CATEGORY_LABELS[aus.STATUS_TRANSCODED], info="",
            source_stream=1, source_codec="truehd", source_channels=channels,
            target=pc.target_audio_for(channels),
        )

    def _check(self, new_payload: dict, verdict: aus.AudioVerdict | None = None):
        verdict = verdict or self._verdict()
        with mock.patch.object(aus, "run_ffprobe", return_value=new_payload):
            return aus.verify_output(self.produced, verdict, self.cfg, self.OLD)

    @staticmethod
    def _superset(**changes) -> dict:
        """The OLD payload plus the Dolby track ffmpeg was asked to append."""
        payload = json.loads(json.dumps(VerificationGuardTests.OLD))
        appended = {"index": len(payload["streams"]), "codec_type": "audio",
                    "codec_name": "eac3", "channels": 6}
        appended.update(changes.pop("appended", {}))
        payload["streams"].append(appended)
        for key, value in changes.items():
            payload.setdefault("format", {})[key] = value
        return payload

    def test_a_faithful_superset_is_accepted(self) -> None:
        self.assertEqual(self._check(self._superset()), (True, ""))

    def test_the_appended_track_must_be_the_codec_that_was_asked_for(self) -> None:
        ok, why = self._check(self._superset(appended={"codec_name": "dts"}))
        self.assertFalse(ok)
        self.assertIn("not eac3", why)

    def test_the_appended_track_must_have_the_promised_geometry(self) -> None:
        ok, why = self._check(self._superset(appended={"channels": 2}))
        self.assertFalse(ok)
        self.assertIn("expected 6ch", why)

    def test_a_vanished_video_stream_refuses_the_publish(self) -> None:
        payload = self._superset()
        payload["streams"] = [s for s in payload["streams"] if s["codec_type"] != "video"]
        ok, why = self._check(payload)
        self.assertFalse(ok)
        self.assertIn("video stream set changed", why)

    def test_a_vanished_subtitle_stream_refuses_the_publish(self) -> None:
        """Invariant 8 says *every* original stream, and subtitles are streams.

        audiofit only ever opens an MKV and writes an MKV with `-map 0 -c
        copy`, so a subtitle that is missing from the output is not a
        container difference - something dropped a track the movie had. The
        guard used to compare video and count audio, leaving the third class
        of stream unwatched.
        """
        payload = self._superset()
        payload["streams"] = [s for s in payload["streams"] if s["codec_type"] != "subtitle"]
        ok, why = self._check(payload)
        self.assertFalse(ok)
        self.assertIn("subtitle stream set changed", why)

    def test_an_audio_track_lost_on_the_way_out_refuses_the_publish(self) -> None:
        payload = self._superset()
        payload["streams"] = [s for s in payload["streams"]
                              if not (s["codec_type"] == "audio" and s["codec_name"] == "truehd")]
        ok, why = self._check(payload)
        self.assertFalse(ok)
        self.assertIn("expected +1", why)

    def test_duration_drift_beyond_the_tolerance_refuses_the_publish(self) -> None:
        ok, why = self._check(self._superset(duration="7204.5"))
        self.assertFalse(ok)
        self.assertIn("duration drifted", why)

    def test_drift_inside_the_tolerance_is_not_a_refusal(self) -> None:
        self.assertEqual(self._check(self._superset(duration="7202.0")), (True, ""))

    def test_a_duration_the_probe_cannot_read_is_a_refusal_not_a_crash(self) -> None:
        ok, why = self._check(self._superset(duration="not-a-number"))
        self.assertFalse(ok)
        self.assertIn("could not read probes", why)

    def test_a_verification_probe_that_will_not_run_refuses_the_publish(self) -> None:
        verdict = self._verdict()
        with mock.patch.object(aus, "run_ffprobe", side_effect=RuntimeError("ffprobe died")):
            ok, why = aus.verify_output(self.produced, verdict, self.cfg, self.OLD)
        self.assertFalse(ok)
        self.assertIn("verification ffprobe failed", why)

    def test_the_tv_arc_wiring_is_verified_against_its_own_target(self) -> None:
        """The proof follows the wiring: AC-3 5.1, not the default wiring's DD+."""
        verdict = aus.AudioVerdict(
            path=str(self.produced), status=aus.STATUS_TRANSCODED,
            category=aus.CATEGORY_LABELS[aus.STATUS_TRANSCODED], info="",
            source_stream=1, source_codec="truehd", source_channels=8,
            target=pc.target_audio_for(8, pc.WIRING_TV_ARC),
        )
        payload = json.loads(json.dumps(self.OLD))
        payload["streams"].append({"index": 3, "codec_type": "audio",
                                   "codec_name": "eac3", "channels": 6})
        ok, why = self._check(payload, verdict)
        self.assertFalse(ok)
        self.assertIn("not ac3", why)
        payload["streams"][-1]["codec_name"] = "ac3"
        self.assertEqual(self._check(payload, verdict), (True, ""))


class PlannerUnitTests(unittest.TestCase):
    """Process-free checks of the planner (run on every platform)."""

    def setUp(self) -> None:
        self.cfg = aus.Config(dry_run=True)

    def test_every_real_world_codec_family_has_a_verdict(self) -> None:
        # self.cfg uses the DEFAULT wiring, soundbar-hdmi-in: the soundbar's
        # HDMI IN carries multichannel PCM, so multichannel PCM-decodes are
        # accepted as-is; only the lossless-HD masters still need work.
        cases = {
            "eac3": aus.STATUS_NATIVE, "ac3": aus.STATUS_NATIVE,
            "dts": aus.STATUS_DTS, "aac": aus.STATUS_PCM, "flac": aus.STATUS_PCM,
            "truehd": aus.STATUS_PLANNED, "gsm_ms": aus.STATUS_REVIEW,
        }
        for codec, wanted in cases.items():
            with self.subTest(codec=codec):
                v = aus.plan_for_payload("m.mkv", _payload({"codec_name": codec}), self.cfg)
                self.assertEqual(v.status, wanted)

    def test_the_default_config_is_the_hdmi_in_wiring(self) -> None:
        # No flag, no environment override: the planner must assume the chain
        # as cabled, which accepts multichannel PCM sources untouched.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(pc.WIRING_ENV_VAR, None)
            self.assertEqual(aus.Config().wiring, pc.WIRING_SOUNDBAR_HDMI_IN)
            self.assertEqual(aus.resolve_wiring(None), pc.WIRING_SOUNDBAR_HDMI_IN)
        for codec in ("aac", "flac"):
            with self.subTest(codec=codec):
                v = aus.plan_for_payload("m.mkv", _payload({"codec_name": codec}), self.cfg)
                self.assertEqual(v.status, aus.STATUS_PCM)
                self.assertIsNone(v.target, "no AC-3 target is planned by default")

    def test_stereo_pcm_decodes_stay_fine_even_over_arc(self) -> None:
        for codec in ("aac", "flac"):
            with self.subTest(codec=codec):
                v = aus.plan_for_payload(
                    "m.mkv", _payload({"codec_name": codec, "channels": 2}), self.cfg)
                self.assertEqual(v.status, aus.STATUS_PCM)

    def test_the_hdmi_in_wiring_accepts_multichannel_pcm(self) -> None:
        cfg = aus.Config(dry_run=True, wiring=pc.WIRING_SOUNDBAR_HDMI_IN)
        for codec in ("aac", "flac"):
            with self.subTest(codec=codec):
                v = aus.plan_for_payload("m.mkv", _payload({"codec_name": codec}), cfg)
                self.assertEqual(v.status, aus.STATUS_PCM)

    def test_the_arc_alternative_transcodes_multichannel_pcm(self) -> None:
        cfg = aus.Config(dry_run=True, wiring=pc.WIRING_TV_ARC)
        for codec in ("aac", "flac"):
            with self.subTest(codec=codec):
                v = aus.plan_for_payload("m.mkv", _payload({"codec_name": codec}), cfg)
                self.assertEqual(v.status, aus.STATUS_PLANNED)
                assert v.target is not None
                self.assertEqual(v.target.codec, "ac3")

    def test_the_default_wiring_targets_dolby_digital_plus_folded_to_51(self) -> None:
        # A lossless 7.1 master on the chain as cabled: the synthesized track
        # is Dolby Digital Plus (official G454V passthrough, decoded by the
        # AX3125H's HDMI IN), folded to 5.1 - the ceiling of the encoder that
        # has to build it. The format could carry 7.1; no ffmpeg Dolby encoder
        # can write one, and asking it to fails the whole transcode.
        v = aus.plan_for_payload("m.mkv", TRUEHD_ONLY, self.cfg)
        self.assertEqual(v.status, aus.STATUS_PLANNED)
        assert v.target is not None
        self.assertEqual(v.target.codec, "eac3")
        self.assertEqual(v.target.channels, pc.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS)
        self.assertEqual(v.target.channel_name, "5.1")
        self.assertEqual(v.target.bitrate, "640k")
        # The tv-arc alternative keeps the every-hop AC-3 target, folded too.
        cfg = aus.Config(dry_run=True, wiring=pc.WIRING_TV_ARC)
        v = aus.plan_for_payload("m.mkv", TRUEHD_ONLY, cfg)
        assert v.target is not None
        self.assertEqual(v.target.codec, "ac3")
        self.assertEqual(v.target.channels, 6)

    def test_a_native_track_below_the_keeper_never_settles_the_file(self) -> None:
        """The 8.3.1 fix: the verdict is about the KEEPER, not about existence.

        A pool holding a 7.1 lossless master and a narrow AC-3 2.0: the
        master ranks first (it reaches a 5.1 bed, the stereo track can never
        become anything), so the cleanup keeps the master and deletes the
        AC-3. Answering `native-ok` because "some AC-3 exists" is what left
        those movies with only audio the chain cannot emit - transcoders on
        every play. The file is not settled; it is planned, and the appended
        DD+ track is what the cleaner keeps afterwards.
        """
        payload = _payload({"codec_name": "truehd", "channels": 8},
                           {"codec_name": "ac3", "channels": 2})
        v = aus.plan_for_payload("m.mkv", payload, self.cfg)
        self.assertNotIn(v.status, aus.SETTLED_AUDIOFIT)
        self.assertEqual(v.status, aus.STATUS_PLANNED)
        self.assertEqual(v.source_stream, 1)
        self.assertEqual(v.source_codec, "truehd")
        # And after the bake-in, the same file IS settled: the appended DD+
        # 5.1 now ranks above the master (same reach, no server work), so the
        # keeper is a track the chain emits - the invariant, stated the other
        # way round.
        after = dict(payload)
        target = v.target
        assert target is not None
        after["streams"] = list(payload["streams"]) + [
            {"index": 3, "codec_type": "audio", "codec_name": target.codec,
             "channels": target.channels, "sample_rate": target.sample_rate,
             "tags": {"language": "eng"}, "disposition": {"default": 0}}]
        v_after = aus.plan_for_payload("m.mkv", after, self.cfg)
        self.assertEqual(v_after.status, aus.STATUS_NATIVE)

    def test_a_titled_eac3_stream_is_native_from_the_ffprobe_fields(self) -> None:
        """The classifier reads codec_name/profile; the title never decides."""
        for payload in (EAC3_TITLED_TRUEHD, EAC3_TITLED_DTSHD):
            with self.subTest(title=payload["streams"][1]["tags"]["title"]):
                v = aus.plan_for_payload("m.mkv", payload, self.cfg)
                self.assertEqual(v.status, aus.STATUS_NATIVE)
                self.assertIsNone(v.target)
                self.assertEqual(v.audio_class, pc.AUDIO_NATIVE)
                # The blob is built with stable field positions, so the title
                # can never be read as the codec's profile.
                self.assertEqual(aus._stream_blob(payload["streams"][1]),
                                 "EAC3 UNKNOWN TRUEHD 7.1"
                                 if payload is EAC3_TITLED_TRUEHD else
                                 "EAC3 - DTS-HD MA 7.1")

    def test_the_transcode_source_is_the_highest_tier_master_not_the_widest(self) -> None:
        # Two lossless masters, no native track. Since 8.4.0 both reach the
        # SAME 5.1 bed - the encoder ceiling means a 7.1 master no longer
        # reaches further than a 5.1 one - so the widest is not special any
        # more and the chain's master-preference table picks the input:
        # DTS-HD MA outranks TrueHD, exactly as it already did at equal
        # layouts. Before the cap this fixture chose TrueHD *because* 7.1
        # stayed 7.1; that premise was false, and it is what made the whole
        # >=7.1 library's audiofit fail. Same table the cleaner ranks with,
        # so the two tools still cannot disagree about which master to burn.
        payload = _payload({"codec_name": "truehd", "channels": 8},
                           {"codec_name": "dts", "profile": "DTS-HD MA", "channels": 6})
        v = aus.plan_for_payload("m.mkv", payload, self.cfg)
        self.assertEqual(v.status, aus.STATUS_PLANNED)
        self.assertEqual(v.source_stream, 2)
        self.assertEqual(v.source_codec, "dts")

    def test_at_an_equal_layout_the_highest_tier_master_is_the_source(self) -> None:
        # Determinism, not preference: both reach 5.1, so the chain's
        # master-preference table breaks the tie (DTS-HD MA ranks above TrueHD
        # — both decode losslessly) and picks the same source every run.
        payload = _payload({"codec_name": "truehd", "channels": 6},
                           {"codec_name": "dts", "profile": "DTS-HD MA", "channels": 6})
        v = aus.plan_for_payload("m.mkv", payload, self.cfg)
        self.assertEqual(v.status, aus.STATUS_PLANNED)
        self.assertEqual(v.source_stream, 2)
        self.assertEqual(v.source_codec, "dts")

    def test_nothing_planned_means_no_target(self) -> None:
        v = aus.plan_for_payload("m.mkv", EAC3_DONE, self.cfg)
        self.assertIsNone(v.target)
        self.assertIn(v.status, aus.SETTLED_AUDIOFIT)

    def test_stereo_truehd_stays_stereo_not_fake_51(self) -> None:
        v = aus.plan_for_payload("m.mkv", _payload({"codec_name": "truehd", "channels": 2}),
                                 self.cfg)
        assert v.target is not None
        self.assertEqual(v.target.channels, 2)
        self.assertEqual(v.target.bitrate, "192k")


if __name__ == "__main__":
    unittest.main()
