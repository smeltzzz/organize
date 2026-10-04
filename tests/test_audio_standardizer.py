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
