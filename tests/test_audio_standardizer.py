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
    def test_truehd_and_dtshd_gain_an_ac3_track_that_passes_verification(self) -> None:
        truehd = self.movie("TrueHD Film (2001)", TRUEHD_ONLY)
        dtshd = self.movie("DTS HD Film (2002)", DTSHD_ONLY)
        done = self.movie("Ready Film (2003)", EAC3_DONE)
        odd = self.movie("Odd Film (2005)", UNKNOWN_AUDIO)
        timestamp_before = done.read_bytes()

        code = self._run(env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)

        # The two lossless files were replaced by a verified superset.
        for path, before_streams in ((truehd, TRUEHD_ONLY), (dtshd, DTSHD_ONLY)):
            streams = self.payload_of(path)["streams"]
            with self.subTest(movie=path.name):
                self.assertEqual(len(streams), len(before_streams["streams"]) + 1)
                self.assertEqual(streams[-1]["codec_name"], "ac3")
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
            self.assertIn("ac3", args)
            self.assertIn("640k", args)
            self.assertIn("0:1", args)  # '-map 0:1' input-side
        # State remembers what was done.
        rows = self.plan_rows()
        self.assertEqual(rows["TrueHD Film (2001)"], aus.STATUS_TRANSCODED)
        self.assertEqual(rows["DTS HD Film (2002)"], aus.STATUS_TRANSCODED)

        # A second pass is a no-op: the appended AC-3 makes both files settled
        # (the transcoded bucket is empty; both now report chain-native).
        code = self._run()
        self.assertEqual(code, 0)
        report = self.report_text()
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
        # With passthrough refused, the same file is transcoded.
        before = film.read_bytes()
        code = self._run("--no-dts-passthrough",
                        env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        self.assertNotEqual(film.read_bytes(), before)
        self.assertEqual(self.payload_of(film)["streams"][-1]["codec_name"], "ac3")

    def test_the_default_arc_wiring_transcodes_multichannel_flac(self) -> None:
        # The soundbar hangs off the TV's ARC port: plain ARC/optical carries
        # stereo PCM only, so a 6-channel FLAC must become AC-3 by default.
        film = self.movie("Concert (2010)", FLAC_MULTI)
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertIn("Concert (2010).mkv", self.report_section("WOULD TRANSCODE"))
        # Explicitly rewired through the bar's HDMI IN, the same file is done.
        self.assertEqual(self._run("--wiring", pc.WIRING_SOUNDBAR_HDMI_IN, "--dry-run"), 0)
        self.assertIn("Concert (2010).mkv", self.report_section("DECODE-TO-PCM"))
        # And the default run actually performs the conversion it planned.
        code = self._run(env={"FAKE_FFMPEG_LOG": str(self.tmp / "ffmpeg_invocations.jsonl")})
        self.assertEqual(code, 0)
        self.assertEqual(self.payload_of(film)["streams"][-1]["codec_name"], "ac3")

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

    def test_empty_and_junk_only_libraries_are_clean_runs(self) -> None:
        stray = self.library / "sample-clip.mkv"
        stray.write_bytes(b"not a movie")
        self.assertEqual(self._run(), 0)
        self.assertIn("Movies inspected", self.report_text())


class PlannerUnitTests(unittest.TestCase):
    """Process-free checks of the planner (run on every platform)."""

    def setUp(self) -> None:
        self.cfg = aus.Config(dry_run=True)

    def test_every_real_world_codec_family_has_a_verdict(self) -> None:
        # self.cfg uses the shipped default wiring, tv-arc: plain ARC/optical
        # can only carry stereo PCM, so multichannel PCM-decodes are AC-3
        # transcode candidates by default.
        cases = {
            "eac3": aus.STATUS_NATIVE, "ac3": aus.STATUS_NATIVE,
            "dts": aus.STATUS_DTS, "aac": aus.STATUS_PLANNED, "flac": aus.STATUS_PLANNED,
            "truehd": aus.STATUS_PLANNED, "gsm_ms": aus.STATUS_REVIEW,
        }
        for codec, wanted in cases.items():
            with self.subTest(codec=codec):
                v = aus.plan_for_payload("m.mkv", _payload({"codec_name": codec}), self.cfg)
                self.assertEqual(v.status, wanted)

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

    def test_the_transcode_source_is_the_highest_tier_lossless_master(self) -> None:
        # Two lossless masters, no native track: AC-3 comes from the
        # highest-tier one (DTS-HD MA ranks above TrueHD in the chain's
        # master-preference table — both decode losslessly, so this is about
        # determinism, and the cleaner agrees with the same table).
        payload = _payload({"codec_name": "truehd", "channels": 8},
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
