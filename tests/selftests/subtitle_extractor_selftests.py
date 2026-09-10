"""The offline self-tests for ``subtitle_extractor.py``.

The fetcher this tool replaced shipped its self-tests inside the tool; when
they moved out here the fetcher still had a scrape registry, two providers and
a refetch loop to assert against. Extraction has none of that. What is left to
assert offline - and what a broken checkout must never lose - is the
conversion and selection math that turns an embedded track into a sidecar,
plus the ledger handshake ``sync_subtitles.py`` depends on.

Each function is rebound to the tool module's namespace by
:func:`bind_to_tool`, so a body that reads or patches a module global affects
the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import subtitle_extractor as tool
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.json = json
tool.Path = Path
tool.tempfile = tempfile


def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # -- text-track conversion ---------------------------------------------
    ass = (
        "[Script Info]\n"
        "Title: demo\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello there\n"
        "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\i1}Second{\\i0} line\n"
    )
    srt = ass_to_srt(ass)
    check("00:00:01,000 --> 00:00:02,500" in srt, "ASS start/end converted to SRT timestamps")
    check("Hello there" in srt, "ASS dialogue text survives")
    check("{\\i1}" not in srt, "ASS override tags are stripped")
    check(looks_like_srt(srt), "converted ASS passes the SRT shape check")

    vtt = (
        "WEBVTT\n\n"
        "00:01.000 --> 00:02.500\n"
        "Hello there\n\n"
        "00:03.000 --> 00:04.000\n"
        "Second line\n"
    )
    srt2 = vtt_to_srt(vtt)
    check("00:00:01,000 --> 00:00:02,500" in srt2, "VTT timestamps converted to SRT")
    check(looks_like_srt(srt2), "converted VTT passes the SRT shape check")

    # -- track classification and ranking -----------------------------------
    def track(track_id: int, language: str, name: str = "", *, forced: bool = False,
              sdh: bool = False, commentary: bool = False, codec_id: str = "S_TEXT/UTF8"):
        return {
            "id": track_id, "type": "subtitles",
            "properties": {"codec_id": codec_id, "language": language,
                           "track_name": name,
                           **({"flag_forced": True} if forced else {}),
                           **({"flag_hearing_impaired": True} if sdh else {}),
                           **({"flag_commentary": True} if commentary else {})},
        }

    tracks = [
        track(3, "ger", "German"),
        track(4, "eng", "English"),
        track(5, "eng", "English (commentary)", commentary=True),
        track(6, "eng", "English SDH", sdh=True),
        track(7, "eng", "English (forced)", forced=True),
    ]
    embedded = classify_embedded_subtitle_tracks(tracks)
    check([t.track_id for t in embedded] == [4, 6],
          f"plain then SDH survive; foreign, forced and commentary drop (got {[t.track_id for t in embedded]})")
    check(all(t.kind == "text" for t in embedded), "S_TEXT/UTF8 tracks are text")
    check(embedded[0].extension == ".srt", "S_TEXT/UTF8 extracts to an .srt")

    # -- sidecar contract ---------------------------------------------------
    video = Path("/lib/Movie (2020)/Movie (2020).mkv")
    covering = covering_english_srt_paths(video)
    check(any(p.name == "Movie (2020).eng.srt" for p in covering),
          "the canonical .eng.srt path is covering")
    check(is_covering_english_sidecar(video.parent / "Movie (2020).eng.srt", video),
          "a matching .eng.srt is a covering sidecar")
    check(not is_covering_english_sidecar(video.parent / "Other (2020).eng.srt", video),
          "a foreign-stem sidecar is not covering")

    # -- the extraction ledger handshake sync_subtitles.py depends on -------
    with tempfile.TemporaryDirectory(prefix="sx_selftest_") as td:
        root = Path(td)
        library = root / "library" / "Movie (2020)"
        library.mkdir(parents=True)
        video = library / "Movie (2020).mkv"
        video.write_bytes(b"x" * 16)
        sidecar = library / "Movie (2020).eng.srt"
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
        ledger = root / "ledger.json"

        record_extracted_sidecar(
            video, sidecar,
            track=classify_embedded_subtitle_tracks([track(4, "eng")])[0],
            method="text", cue_count=1,
            sha256=sha256_text(sidecar.read_text(encoding="utf-8")),
            path=ledger,
        )
        needs = extracted_sidecar_needs_sync(
            sidecar, sha256_text(sidecar.read_text(encoding="utf-8")), path=ledger)
        check(needs, "a freshly recorded extraction needs its one sync")
        mark_extracted_sidecar_synced(sidecar, path=ledger)
        needs2 = extracted_sidecar_needs_sync(
            sidecar, sha256_text(sidecar.read_text(encoding="utf-8")), path=ledger)
        check(not needs2, "a marked sidecar is never re-synced")
        record = find_extracted_record(sidecar, path=ledger)
        check(isinstance(record, dict) and record.get("synced_utc"),
              "the ledger records the sync timestamp")

    if errors:
        print("SELF-TEST FAILED:")
        for error in errors:
            print("  -", error)
        return 1
    print("SELF-TEST PASSED (conversion + classification + sidecar contract + ledger handshake)")
    return 0


run_self_tests = bind_to_tool(tool, run_self_tests)
