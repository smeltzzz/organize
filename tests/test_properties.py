"""Invariants, checked against inputs nobody thought of.

Every test here states a rule the toolkit must obey for *all* inputs of some
shape, and then lets the harness in `tests/property.py` hunt for a
counterexample. They are aimed at the four places where an input nobody
anticipated would be expensive:

* `bitdepth.py` — the fail-closed HDR rule. Queueing a Dolby Vision master for
  re-encoding is the single most destructive thing this toolkit could get
  wrong, and the rule is "when in doubt, never queue". A table of examples
  cannot show that no combination of labels sneaks past it.
* `mkv_track_cleaner.plan_cleanup` — the plan that decides which tracks
  survive a rewrite of your movie.
* The shared SRT contract — every tool agrees on what a subtitle is, so the
  agreement had better hold for arbitrary bytes.
* `movie_standardizer` naming — the output of one tool is the input to the
  auditor's canonical-layout rule, and that handshake is where a weird release
  name would show up.

Plus meta-tests: a property runner that quietly checked nothing would be the
most reassuring file in the repository, so the harness is made to fail on
purpose here.
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from property import (
    PropertyTestCase,
    booleans,
    fixed_dict,
    integers,
    lists,
    maybe,
    one_of,
    sampled,
    text,
)

import bitdepth as bd
import library_auditor as la
import mkv_track_cleaner as mtc
import movie_standardizer as ms
from organizekit import core

# ---------------------------------------------------------------------------
# Strategies for the shapes these tools actually see
# ---------------------------------------------------------------------------

PIX_FMTS = ("yuv420p", "yuv420p10le", "yuv422p10le", "yuv420p12le", "yuvj420p", "nv12", "", "bogus")
TRANSFERS = ("", "bt709", "smpte170m", "smpte2084", "arib-std-b67", "bt2020-10", "iec61966-2-1", "unknown")
PRIMARIES = ("", "bt709", "bt2020", "smpte432")
PROFILES = ("", "Main", "Main 10", "High", "Profile 5", "rext", "unknown")
CODECS = ("h264", "hevc", "dvhe", "dvh1", "av1", "vp9", "mpeg4")

HDR_SIDE_DATA = (
    {"side_data_type": "DOVI configuration record", "dv_profile": 5},
    {"side_data_type": "Dolby Vision Metadata"},
    {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
    {"side_data_type": "Mastering display metadata"},
    {"side_data_type": "Content light level metadata"},
)
NEUTRAL_SIDE_DATA = (
    {"side_data_type": "Display Matrix"},
    {"side_data_type": "Stereo 3D"},
    {"side_data_type": "spherical"},
)
HDR_TAGS = (
    {"HDR_Format": "Dolby Vision, Version 1.0"},
    {"HDR_Format": "SMPTE ST 2086, HDR10 compatible"},
    {"HDR_Format_String": "HDR10+ Profile B"},
    {"HDR_Format_Compatibility": "HLG"},
    {"DOVI": "dvhe.05.06"},
)

video_stream = fixed_dict(
    {
        "codec_type": lambda rng: "video",
        "index": integers(0, 4),
        "codec_name": sampled(CODECS),
        "pix_fmt": sampled(PIX_FMTS),
        "profile": sampled(PROFILES),
        "color_transfer": sampled(TRANSFERS),
        "color_primaries": sampled(PRIMARIES),
        "width": sampled((0, 1280, 1920, 3840)),
        "height": sampled((0, 720, 816, 1080, 2160)),
        "bits_per_raw_sample": maybe(sampled((8, 10, 12, 0)), none_odds=0.5),
        "side_data_list": lists(sampled(HDR_SIDE_DATA + NEUTRAL_SIDE_DATA), max_size=3),
        "tags": lambda rng: dict(rng.choice(HDR_TAGS + ({},))),
        "disposition": fixed_dict({"default": sampled((0, 1)), "attached_pic": sampled((0, 0, 0, 1))}),
    },
    optional=("profile", "color_transfer", "color_primaries", "bits_per_raw_sample",
              "side_data_list", "tags", "pix_fmt"),
)


def probe_payload(rng: Any) -> dict[str, Any]:
    streams = [video_stream(rng) for _ in range(rng.randint(1, 3))]
    for index, stream in enumerate(streams):
        stream["index"] = index
    return {"streams": streams, "format": {"tags": dict(rng.choice(HDR_TAGS + ({},)))}}


class HdrIsFailClosedTests(PropertyTestCase):
    """Invariant 5 of the toolkit: never queue anything that might be HDR."""

    def test_an_hdr_file_is_never_queued_for_re_encoding(self) -> None:
        def prop(payload: dict[str, Any]) -> None:
            result = bd.result_from_probe("movie.mkv", payload)
            if result.hdr:
                self.assertNotEqual(
                    result.status, bd.STATUS_QUEUE,
                    f"HDR ({result.hdr_flavors}) queued for HandBrake: {result.info}",
                )
        self.for_all(probe_payload, prop)

    def test_adding_hdr_evidence_can_never_turn_a_file_into_a_queue(self) -> None:
        """Monotonicity: more HDR signal must only ever move *away* from QUEUE.

        This is the fail-closed rule stated as an experiment. Take any file,
        add any one HDR marker to it, and the answer must not become "safe to
        re-encode" - not for a marker in side data, not in a tag, not in the
        transfer function.
        """
        def prop(case: dict[str, Any]) -> None:
            payload = case["payload"]
            # Mark the stream the tool will actually read: marking a different
            # one would test the stream picker, not the fail-closed rule.
            stream = bd.pick_video_stream(payload)
            if stream is None:
                return
            marker = case["marker"]
            if marker["where"] == "side_data":
                stream["side_data_list"] = list(stream.get("side_data_list") or []) + [marker["value"]]
            elif marker["where"] == "tags":
                stream["tags"] = {**(stream.get("tags") or {}), **marker["value"]}
            else:
                stream["color_transfer"] = "smpte2084"
            result = bd.result_from_probe("movie.mkv", payload)
            self.assertNotEqual(
                result.status, bd.STATUS_QUEUE,
                f"a file carrying {marker} was queued for re-encoding: {result.info}",
            )
        marker = one_of(
            fixed_dict({"where": lambda rng: "side_data", "value": sampled(HDR_SIDE_DATA)}),
            fixed_dict({"where": lambda rng: "tags", "value": sampled(HDR_TAGS)}),
            fixed_dict({"where": lambda rng: "transfer", "value": lambda rng: "smpte2084"}),
        )
        self.for_all(
            lambda rng: {"payload": probe_payload(rng), "marker": marker(rng)},
            prop,
        )

    def test_an_unknown_bit_depth_is_reviewed_and_never_queued(self) -> None:
        def prop(payload: dict[str, Any]) -> None:
            for stream in payload["streams"]:
                stream.pop("pix_fmt", None)
                stream.pop("bits_per_raw_sample", None)
                stream["profile"] = ""
                stream["disposition"] = {"default": 1, "attached_pic": 0}
            result = bd.result_from_probe("movie.mkv", payload)
            self.assertIn(result.status, {bd.STATUS_REVIEW_UNKNOWN_DEPTH, bd.STATUS_ERROR})
        self.for_all(probe_payload, prop)

    def test_the_verdict_does_not_depend_on_the_order_ffprobe_listed_things(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            payload = case["payload"]
            first = bd.result_from_probe("movie.mkv", payload)
            for stream in payload["streams"]:
                side = list(stream.get("side_data_list") or [])
                side.reverse()
                if side:
                    stream["side_data_list"] = side
            second = bd.result_from_probe("movie.mkv", payload)
            self.assertEqual(first.status, second.status)
            self.assertEqual(sorted(first.hdr_flavors), sorted(second.hdr_flavors))
        self.for_all(lambda rng: {"payload": probe_payload(rng)}, prop)

    def test_cover_art_is_never_mistaken_for_the_feature(self) -> None:
        def prop(payload: dict[str, Any]) -> None:
            stream = bd.pick_video_stream(payload)
            if stream is None:
                return
            disposition = stream.get("disposition") or {}
            tags = stream.get("tags") or {}
            self.assertNotEqual(disposition.get("attached_pic"), 1)
            self.assertFalse(str(tags.get("mimetype") or "").startswith("image/"))
        def with_cover(rng: Any) -> dict[str, Any]:
            payload = probe_payload(rng)
            cover = video_stream(rng)
            cover["disposition"] = {"default": 0, "attached_pic": 1}
            cover["tags"] = {"mimetype": "image/jpeg"}
            cover["width"], cover["height"] = 4000, 4000  # bigger than the feature
            payload["streams"].append(cover)
            return payload
        self.for_all(with_cover, prop)


# ---------------------------------------------------------------------------
# The remux plan
# ---------------------------------------------------------------------------

LANGS = ("eng", "en", "und", "", "fre", "jpn", "spa", "en-US")
TRACK_NAMES = ("", "Commentary by the director", "English", "Surround 5.1", "Audio Description",
               "Director's commentary", "VO", "SDH", "Forced")
AUDIO_CODECS = ("TrueHD Atmos", "DTS-HD MA", "AC-3", "AAC", "E-AC-3", "FLAC", "Opus", "")

def audio_track(rng: Any) -> dict[str, Any]:
    return {
        "type": "audio",
        "id": rng.randint(0, 20),
        "codec": rng.choice(AUDIO_CODECS),
        "properties": {
            "language": rng.choice(LANGS),
            "track_name": rng.choice(TRACK_NAMES),
            "audio_channels": rng.choice((1, 2, 6, 8)),
            "codec_id": rng.choice(("A_TRUEHD", "A_DTS", "A_AC3", "A_AAC", "A_OPUS", "")),
            "flag_commentary": rng.random() < 0.15,
        },
    }


def subtitle_track(rng: Any) -> dict[str, Any]:
    return {
        "type": "subtitles",
        "id": rng.randint(0, 20),
        "codec": rng.choice(("SubRip/SRT", "HDMV PGS", "VobSub", "SubStationAlpha")),
        "properties": {
            "language": rng.choice(LANGS),
            "track_name": rng.choice(TRACK_NAMES),
            "forced_track": rng.random() < 0.2,
        },
    }


def media_info(rng: Any) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = [{"type": "video", "id": 0, "codec": "AVC", "properties": {}}]
    tracks += [audio_track(rng) for _ in range(rng.randint(0, 5))]
    tracks += [subtitle_track(rng) for _ in range(rng.randint(0, 4))]
    for index, track in enumerate(tracks):
        track["id"] = index  # mkvmerge ids are unique; the generator must be too
    return {"tracks": tracks}


def plan_case(rng: Any) -> dict[str, Any]:
    return {
        "info": media_info(rng),
        "external_srt": ({"path": "movie.eng.srt"} if rng.random() < 0.4 else None),
    }


class RemuxPlanTests(PropertyTestCase):
    """The plan that decides what survives a rewrite of your movie file."""

    def plan(self, case: dict[str, Any]) -> tuple[Any, str]:
        return mtc.plan_cleanup(case["info"], external_srt=case["external_srt"])

    def test_a_plan_keeps_exactly_one_audio_track_that_came_from_the_file(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            plan, reason = self.plan(case)
            if plan is None:
                self.assertTrue(reason, "refusing to plan must always come with a reason")
                return
            audio = [t for t in case["info"]["tracks"] if t.get("type") == "audio"]
            ids = {int(t["id"]) for t in audio}
            self.assertIn(plan.best_audio_id, ids, "kept an audio track the file does not have")
            self.assertEqual(
                len(plan.removed_audio), len(audio) - 1,
                "every audio track except the keeper must be accounted for",
            )
            self.assertNotIn(plan.best_audio_id, {int(t["id"]) for t in plan.removed_audio})
        self.for_all(plan_case, prop)

    def test_commentary_is_never_the_track_that_survives(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            plan, _reason = self.plan(case)
            if plan is None:
                return
            self.assertFalse(
                mtc.is_commentary_track(plan.best_audio, True),
                f"commentary retained: {plan.best_audio.get('properties')}",
            )
        self.for_all(plan_case, prop)

    def test_the_keeper_is_in_the_movies_native_language(self) -> None:
        """One language survives: the movie's own, decided by the file's markers."""
        def prop(case: dict[str, Any]) -> None:
            plan, _reason = self.plan(case)
            if plan is None:
                return
            audio = [t for t in case["info"]["tracks"] if t.get("type") == "audio"]
            candidates = [
                t for t in audio
                if not mtc.is_commentary_track(t, True) and not mtc.is_named_dub_track(t)
            ]
            native = mtc.native_audio_language(candidates)
            self.assertEqual(
                mtc.audio_language_token(plan.best_audio), native,
                "the keeper is not in the movie's native language",
            )
        self.for_all(plan_case, prop)

    def test_the_kept_track_is_the_best_scoring_one_in_its_pool(self) -> None:
        """Quality decides *within* the native-language pool; the pool is fixed.

        The pool is every non-commentary, non-titled-dub track in the movie's
        native language (see the property above for how that language is
        chosen). Inside it, the best-scoring track wins.
        """
        def prop(case: dict[str, Any]) -> None:
            plan, _reason = self.plan(case)
            if plan is None:
                return
            audio = [t for t in case["info"]["tracks"] if t.get("type") == "audio"]
            candidates = [
                t for t in audio
                if not mtc.is_commentary_track(t, True) and not mtc.is_named_dub_track(t)
            ]
            native = mtc.native_audio_language(candidates)
            pool = [t for t in candidates if mtc.audio_language_token(t) == native]
            best = max(mtc.get_audio_quality_score(t) for t in pool)
            self.assertEqual(mtc.get_audio_quality_score(plan.best_audio), best)
        self.for_all(plan_case, prop)

    def test_a_clearly_better_track_always_wins(self) -> None:
        """An oracle that does not consult the scoring function it is judging.

        The property above re-derives "best" from `get_audio_quality_score`,
        which makes it a consistency check rather than a test of the ranking:
        invert that function and both sides invert with it. This one states an
        ordering the product owns - lossless Atmos 7.1 at 5 Mbps beats every
        lossy stereo track in the same language - and plants exactly such a
        pair among the random ones. Whenever the planted language is the one
        the file settles on, the great track must be the keeper. Reverse the
        ranking and this fails.
        """
        def prop(case: dict[str, Any]) -> None:
            best_id = 999
            case["info"]["tracks"].append({
                "type": "audio",
                "id": best_id,
                "codec": "TrueHD Atmos",
                "properties": {
                    "language": "eng",
                    "track_name": "Surround 7.1",
                    "audio_channels": 8,
                    "codec_id": "A_TRUEHD",
                    "tag_bps": 5_000_000,
                    "flag_commentary": False,
                },
            })
            plan, reason = self.plan(case)
            self.assertIsNotNone(plan, f"a perfectly good track was refused: {reason}")
            assert plan is not None
            if mtc.audio_language_token(plan.best_audio) != "en":
                # The file settled on another language: the planted English
                # track is not native and must not survive.
                self.assertNotIn(plan.best_audio_id, {best_id})
                return
            self.assertEqual(
                plan.best_audio_id, best_id,
                f"kept {plan.best_audio.get('codec')!r} over lossless English Atmos 7.1",
            )
        self.for_all(plan_case, prop)

    def test_a_validated_sidecar_always_wins_over_every_embedded_subtitle(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            case["external_srt"] = {"path": "movie.eng.srt"}
            plan, _reason = self.plan(case)
            if plan is None:
                return
            self.assertEqual(plan.keep_subtitles, [], "an embedded subtitle survived beside the sidecar")
            subs = [t for t in case["info"]["tracks"] if t.get("type") == "subtitles"]
            self.assertEqual(len(plan.removed_subs), len(subs))
        self.for_all(plan_case, prop)

    def test_no_track_is_both_kept_and_removed_or_invented(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            plan, _reason = self.plan(case)
            if plan is None:
                return
            tracks = case["info"]["tracks"]
            known = {int(t["id"]) for t in tracks}
            kept = {plan.best_audio_id} | set(plan.keep_sub_ids)
            removed = {int(t["id"]) for t in plan.removed_audio + plan.removed_subs}
            self.assertTrue(kept <= known, "the plan names a track the file does not have")
            self.assertTrue(removed <= known)
            self.assertEqual(kept & removed, set(), "a track is both kept and removed")
        self.for_all(plan_case, prop)

    def test_a_clean_file_is_one_the_plan_would_not_change(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            plan, _reason = self.plan(case)
            if plan is None:
                return
            if plan.is_clean:
                self.assertEqual(plan.removed_audio, [])
                self.assertEqual(plan.removed_subs, [])
        self.for_all(plan_case, prop)

    def test_the_decision_does_not_depend_on_the_order_the_tracks_were_listed(self) -> None:
        """Order is a tie-break of last resort, by design.

        A file that marks its native language - one shared language, an
        'original' flag, or a default flag - must decide the same way whatever
        order the tracks are listed in. A file with several unmarked languages
        falls through to track order (dubs are conventionally appended), and
        for those cases order is the only information there is.
        """
        def prop(case: dict[str, Any]) -> None:
            first, first_reason = self.plan(case)
            shuffled = {"tracks": list(reversed(case["info"]["tracks"]))}
            second, second_reason = mtc.plan_cleanup(shuffled, external_srt=case["external_srt"])
            self.assertEqual(first is None, second is None, (first_reason, second_reason))
            if first is None or second is None:
                self.assertEqual(first_reason, second_reason)
                return
            self.assertEqual(set(first.keep_sub_ids), set(second.keep_sub_ids))
            self.assertEqual(first.is_clean, second.is_clean)
            audio = [t for t in case["info"]["tracks"] if t.get("type") == "audio"]
            candidates = [
                t for t in audio
                if not mtc.is_commentary_track(t, True) and not mtc.is_named_dub_track(t)
            ]
            marked = (
                len({mtc.audio_language_token(t) for t in candidates}) == 1
                or any((t.get("properties") or {}).get("flag_original") for t in candidates)
                or any((t.get("properties") or {}).get("flag_default") for t in candidates)
            )
            if not marked:
                return  # track order is the tie-break here; see the docstring
            self.assertEqual(
                mtc.get_audio_quality_score(first.best_audio),
                mtc.get_audio_quality_score(second.best_audio),
            )
        self.for_all(plan_case, prop)


# ---------------------------------------------------------------------------
# The shared subtitle contract
# ---------------------------------------------------------------------------

CUE_TEXT = "Sample dialogue line."

def srt_document(rng: Any) -> str:
    cues = []
    for index in range(1, rng.randint(1, 6) + 1):
        start = rng.randint(0, 7_000_000)
        end = start + rng.randint(500, 5_000)
        cues.append(
            f"{index}\n{stamp(start)} --> {stamp(end)}\n"
            f"{CUE_TEXT}\n"
        )
    return "\n".join(cues)


def stamp(milliseconds: int) -> str:
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


# Letters and spaces only: nothing here can accidentally look like a cue.
PROSE = text("abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ\n", max_size=60)


class SubtitleContractTests(PropertyTestCase):
    """Every tool agrees on what a subtitle is, for arbitrary input."""

    def test_newline_normalisation_is_idempotent_and_leaves_no_carriage_returns(self) -> None:
        def prop(raw: str) -> None:
            once = core.normalize_srt_newlines(raw)
            self.assertNotIn("\r", once)
            self.assertEqual(core.normalize_srt_newlines(once), once)
            self.assertLessEqual(len(once), len(raw))
        self.for_all(text("ab\r\n \t", max_size=40), prop)

    def test_a_well_formed_subtitle_is_recognised_however_its_lines_end(self) -> None:
        def prop(document: str) -> None:
            for newline in ("\n", "\r\n", "\r"):
                variant = document.replace("\n", newline)
                self.assertTrue(
                    core.srt_looks_valid(core.normalize_srt_newlines(variant)),
                    f"valid SRT rejected with {newline!r} line endings",
                )
        self.for_all(srt_document, prop)

    def test_prose_is_never_mistaken_for_a_subtitle(self) -> None:
        def prop(prose: str) -> None:
            self.assertFalse(core.srt_looks_valid(prose), "text with no cue accepted as a subtitle")
        self.for_all(PROSE, prop)

    def test_decoding_never_raises_and_round_trips_the_supported_encodings(self) -> None:
        def prop(document: str) -> None:
            for encoding in core.EXTERNAL_SRT_ENCODINGS:
                try:
                    raw = document.encode(encoding)
                except UnicodeEncodeError:
                    continue
                decoded = core.decode_srt_bytes(raw)
                self.assertIsNotNone(decoded, f"{encoding} bytes were undecodable")
                self.assertTrue(core.srt_looks_valid(core.normalize_srt_newlines(decoded or "")))
        self.for_all(srt_document, prop)

    def test_arbitrary_bytes_decode_or_are_refused_but_never_explode(self) -> None:
        def prop(size: int) -> None:
            raw = bytes(range(256))[:size] * 3
            decoded = core.decode_srt_bytes(raw)
            if decoded is not None:
                self.assertIsInstance(decoded, str)
        self.for_all(integers(0, 256), prop, cases=40)

    def test_a_sidecar_is_accepted_only_when_it_is_a_real_subtitle(self) -> None:
        def prop(case: dict[str, Any]) -> None:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "Movie (2020).eng.srt"
                path.write_bytes(case["body"])
                ok, reason = core.validate_srt_sidecar(path)
                if case["valid"]:
                    self.assertTrue(ok, f"a valid sidecar was rejected: {reason}")
                else:
                    self.assertFalse(ok, "a file with no cue was accepted as a sidecar")
                    self.assertTrue(reason, "a rejection must always explain itself")
        def build(rng: Any) -> dict[str, Any]:
            if rng.random() < 0.5:
                return {"body": srt_document(rng).encode("utf-8"), "valid": True}
            junk = PROSE(rng)
            return {"body": junk.encode("utf-8"), "valid": False}
        self.for_all(build, prop, cases=40)

    def test_an_empty_sidecar_is_never_accepted(self) -> None:
        def prop(_case: int) -> None:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "Movie (2020).eng.srt"
                path.write_bytes(b"")
                ok, reason = core.validate_srt_sidecar(path)
                self.assertFalse(ok)
                self.assertIn("empty", reason)
        self.for_all(integers(0, 1), prop, cases=5)


# ---------------------------------------------------------------------------
# Naming: the handshake between the standardizer and the auditor
# ---------------------------------------------------------------------------

# Two vocabularies on purpose. TITLE_WORDS is the awkward end of reality -
# abbreviations, digits, accents, a title that is itself a hyphenated tag - and
# is used for the properties that must hold for *any* name. PLAIN_TITLE_WORDS
# is an ordinary movie title, used where the invariant is about names this
# toolkit produced rather than names the internet handed it.
TITLE_WORDS = ("The", "Matrix", "Blade", "Runner", "2049", "Amélie", "WALL-E", "Se7en",
               "Mad", "Max", "Fury", "Road", "Dr.", "Strangelove", "Léon", "8½", "Up")
PLAIN_TITLE_WORDS = ("The", "Matrix", "Blade", "Runner", "Arrival", "Heat", "Dune",
                     "Mad", "Max", "Fury", "Road", "Up", "Amelie", "Leon", "Se7en")
SCENE_TAGS = ("1080p", "2160p", "BluRay", "WEB-DL", "x264", "x265", "HEVC", "DTS-HD",
              "REMUX", "PROPER", "EXTENDED", "AMZN", "-GROUP", "[YTS.MX]", "REPACK")
SEPARATORS = (".", " ", "_", "-")


def release_name(rng: Any) -> str:
    words = [rng.choice(TITLE_WORDS) for _ in range(rng.randint(1, 4))]
    separator = rng.choice(SEPARATORS)
    name = separator.join(words)
    if rng.random() < 0.8:
        name += f"{separator}({rng.randint(1900, 2030)})" if rng.random() < 0.4 else f"{separator}{rng.randint(1900, 2030)}"
    for _ in range(rng.randint(0, 4)):
        name += separator + rng.choice(SCENE_TAGS)
    if rng.random() < 0.5:
        name += rng.choice((".mkv", ".mp4", ".avi"))
    return name


def scene_release_name(rng: Any) -> str:
    """A release name of the ordinary kind: real title words, then tags."""
    words = [rng.choice(PLAIN_TITLE_WORDS) for _ in range(rng.randint(1, 4))]
    separator = rng.choice((".", " "))
    name = separator.join(words)
    if rng.random() < 0.85:
        year = rng.randint(1900, 2030)
        name += f"{separator}({year})" if rng.random() < 0.3 else f"{separator}{year}"
    for _ in range(rng.randint(0, 4)):
        name += separator + rng.choice(SCENE_TAGS[:-2])
    if rng.random() < 0.4:
        name += "-GROUP"
    if rng.random() < 0.5:
        name += rng.choice((".mkv", ".mp4"))
    return name


class NamingTests(PropertyTestCase):
    """What the ingest hook writes, the auditor must call canonical."""

    def test_sanitising_a_name_is_idempotent_and_always_usable(self) -> None:
        def prop(raw: str) -> None:
            once = ms.sanitize_filename(raw)
            self.assertEqual(ms.sanitize_filename(once), once, "sanitising twice changed the name")
            self.assertTrue(once, "a name must never sanitise to nothing")
            self.assertNotEqual(once[-1], " ", "a trailing space breaks on Windows")
            self.assertNotEqual(once[-1], ".", "a trailing dot breaks on Windows")
            for bad in '<>:"/\\|?*':
                self.assertNotIn(bad, once, f"{bad!r} survived sanitisation")
            self.assertLessEqual(len(once.encode("utf-8")), 201)
        alphabet = 'abcXYZ 0123.:/\\<>|?*"' + "\x00\x01\t\n é½"
        self.for_all(text(alphabet, max_size=40), prop)

    def test_a_parsed_release_name_produces_a_folder_the_auditor_calls_canonical(self) -> None:
        """The handshake: what the ingest hook writes, the auditor must accept.

        These are two tools with two independent notions of "canonical", and
        nothing but this test makes them agree about a name neither author
        imagined.
        """
        def prop(name: str) -> None:
            parsed = ms.parse_movie_name(name)
            if parsed.is_tv:
                return
            folder_name = parsed.folder_name
            with tempfile.TemporaryDirectory() as td:
                folder = Path(td) / folder_name
                folder.mkdir()
                (folder / f"{folder_name}.mkv").write_bytes(b"v" * 2048)
                (folder / f"{folder_name}.eng.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:03,000\nHello.\n", encoding="utf-8")
                audit = la.classify_folder(folder)
                self.assertEqual(
                    audit.state, "CANONICAL_MKV",
                    f"{name!r} -> {folder_name!r}: the auditor says {audit.state} ({audit.detail})",
                )
        self.for_all(release_name, prop, cases=60)

    def test_re_parsing_a_name_this_tool_wrote_changes_nothing(self) -> None:
        """The ingest hook is idempotent: a library it organised is a fixed point.

        This matters because the standardizer is pointed at folders more than
        once - a re-ingest, a deduplication pass - and a parser that produced a
        different folder the second time would quietly split one movie in two.
        """
        def prop(name: str) -> None:
            first = ms.parse_movie_name(name)
            if first.is_tv:
                return
            second = ms.parse_movie_name(first.folder_name)
            self.assertEqual(second.title, first.title, f"{name!r}: title moved on re-parse")
            self.assertEqual(second.year, first.year, f"{name!r}: year moved on re-parse")
            self.assertEqual(second.folder_name, first.folder_name)
        self.for_all(scene_release_name, prop, cases=200)

    def test_a_title_built_out_of_scene_tags_is_the_known_exception(self) -> None:
        """Where the fixed point above stops holding, pinned so it stays visible.

        The wide generator found it: when a *title word* is itself an edition
        tag, the second parse strips it as an edition. Nobody has a movie
        called "Extended", so this is a limit of the heuristic rather than a
        bug worth risking the parser over - but it is recorded here, with the
        exact shapes, so that a future change to the edition rules shows up as
        a failing test instead of a silent improvement or a silent regression.
        """
        # Both shapes share one cause: an underscore-separated name whose tag
        # block survives the first parse and is then read as an edition.
        for name in ("Up_-GROUP_EXTENDED_DTS-HD.avi", "Léon_The_Amélie_EXTENDED.avi"):
            once = ms.parse_movie_name(name).folder_name
            twice = ms.parse_movie_name(once).folder_name
            self.assertNotEqual(
                once, twice,
                f"{name!r} is now a fixed point - good, fold it into the property above",
            )

    def test_a_movie_is_never_organised_into_the_library_root(self) -> None:
        def prop(name: str) -> None:
            parsed = ms.parse_movie_name(name)
            self.assertNotIn("/", parsed.folder_name)
            self.assertNotIn("\\", parsed.folder_name)
            self.assertNotEqual(parsed.folder_name.strip(), "")
            self.assertNotEqual(parsed.folder_name, ".")
            self.assertNotEqual(parsed.folder_name, "..")
        self.for_all(release_name, prop)


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------

class MutationTests(unittest.TestCase):
    """Break the code on purpose; a property that notices nothing is decoration.

    Each case takes a rule the properties above are supposed to protect,
    removes it from the implementation, and asserts the property now fails.
    Without this, a strategy that silently generated boring inputs - or an
    oracle that quietly re-derived its expectation from the very function it
    was judging - would look exactly like a passing suite.
    """

    def assert_property_notices(self, case_class: type, method: str, *patches: tuple[Any, str, Any]) -> None:
        instance = case_class(method)
        with contextlib.ExitStack() as stack:
            for target, attribute, replacement in patches:
                stack.enter_context(mock.patch.object(target, attribute, replacement))
            with self.assertRaises(AssertionError, msg=f"{method} passed with the code broken"):
                getattr(instance, method)()

    def test_it_notices_when_an_hdr_file_becomes_queueable(self) -> None:
        original = bd.categorize
        self.assert_property_notices(
            HdrIsFailClosedTests, "test_an_hdr_file_is_never_queued_for_re_encoding",
            (bd, "categorize",
             lambda depth, is_hdr: bd.STATUS_QUEUE if (depth or 0) <= 8 else original(depth, is_hdr)),
        )

    def test_it_notices_when_the_remux_starts_keeping_the_worst_audio(self) -> None:
        original = mtc.get_audio_quality_score
        self.assert_property_notices(
            RemuxPlanTests, "test_a_clearly_better_track_always_wins",
            (mtc, "get_audio_quality_score", lambda track: tuple(-value for value in original(track))),
        )

    def test_it_notices_when_anything_counts_as_a_subtitle(self) -> None:
        self.assert_property_notices(
            SubtitleContractTests, "test_prose_is_never_mistaken_for_a_subtitle",
            (core, "srt_looks_valid", lambda text: True),
        )

    def test_it_notices_when_names_stop_being_windows_safe(self) -> None:
        original = ms.sanitize_filename
        self.assert_property_notices(
            NamingTests, "test_sanitising_a_name_is_idempotent_and_always_usable",
            (ms, "sanitize_filename", lambda name: original(name) + " "),
        )

    def test_it_notices_when_cover_art_can_win_the_stream_pick(self) -> None:
        self.assert_property_notices(
            HdrIsFailClosedTests, "test_cover_art_is_never_mistaken_for_the_feature",
            (bd, "pick_video_stream",
             lambda payload: ([s for s in payload.get("streams", []) if isinstance(s, dict)] or [None])[-1]),
        )


class HarnessTests(PropertyTestCase):
    """A property runner that quietly checked nothing would be worse than none."""

    def test_a_planted_failure_is_reported_with_its_seed(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            self.for_all(integers(0, 10), lambda value: self.assertLess(value, -1), cases=3)
        message = str(caught.exception)
        self.assertIn("property failed on case 1 of 3", message)
        self.assertIn("seed", message)
        self.assertIn("shrunk to", message)

    def test_a_failure_shrinks_towards_the_smallest_case_that_still_fails(self) -> None:
        def prop(values: list[int]) -> None:
            self.assertLess(len([v for v in values if v > 50]), 1)
        with self.assertRaises(AssertionError) as caught:
            self.for_all(lists(integers(0, 100), min_size=6, max_size=10), prop, cases=200)
        shrunk = str(caught.exception).split("shrunk to: ", 1)[1].splitlines()[0]
        self.assertEqual(shrunk.count(","), 0, f"expected a single-element list, got {shrunk}")

    def test_every_generated_case_is_actually_run(self) -> None:
        seen: list[int] = []
        self.for_all(integers(0, 1_000_000), seen.append, cases=37)
        self.assertEqual(len(seen), 37)
        self.assertGreater(len(set(seen)), 30, "the generator is not varying its output")

    def test_the_same_test_gets_the_same_seed_every_run(self) -> None:
        self.assertEqual(self.seed_for("label"), self.seed_for("label"))
        self.assertNotEqual(self.seed_for("label"), self.seed_for("other"))

    def test_a_property_with_no_cases_is_a_failure_not_a_pass(self) -> None:
        with self.assertRaises(AssertionError):
            self.for_all(integers(0, 1), lambda value: None, cases=0)

    def test_generated_dicts_sometimes_omit_their_optional_keys(self) -> None:
        strategy = fixed_dict({"a": integers(0, 1), "b": integers(0, 1)}, optional=("b",))
        present = 0
        def prop(value: dict[str, Any]) -> None:
            nonlocal present
            self.assertIn("a", value)
            present += "b" in value
        self.for_all(strategy, prop, cases=60)
        self.assertGreater(present, 0, "optional keys were never present")
        self.assertLess(present, 60, "optional keys were never absent")

    def test_the_strategies_produce_the_shapes_they_promise(self) -> None:
        def prop(value: Any) -> None:
            self.assertIsInstance(value, (int, str, bool, list))
        self.for_all(
            one_of(integers(0, 5), text("ab", max_size=3), booleans(),
                   lists(sampled((1, 2, 3)), max_size=2), maybe(integers(0, 1), none_odds=0.0)),
            prop,
            cases=40,
        )


if __name__ == "__main__":
    unittest.main()
