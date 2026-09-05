"""The offline self-tests lifted out of ``mkv_track_cleaner.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

import mkv_track_cleaner as tool
from tests.selftests import bind_to_tool


def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(normalize_language("fre") == "fr", "fre->fr")
    check(normalize_language("eng") == "en", "eng->en")
    check(normalize_language(["en", "US"][0]) == "en", "en-US")

    eng = {"id": 1, "type": "audio", "properties": {"language": "eng", "language_ietf": "en"}}
    fre = {"id": 2, "type": "audio", "properties": {"language": "fre"}}
    check(is_matching_language(eng, {"en", "eng"}), "eng match")
    check(is_matching_language(fre, {"fr", "fra"}), "fra matches fre")
    check(not is_matching_language(fre, {"en"}), "fre not en")

    sdh = {"type": "subtitles", "properties": {
        "language": "eng", "track_name": "English SDH",
        "flag_hearing_impaired": True, "flag_visual_impaired": True,
    }}
    check(not is_commentary_track(sdh, True), "SDH subtitle must be KEPT")

    dvs = {"type": "audio", "properties": {
        "language": "eng", "track_name": "English Audio Description",
        "flag_visual_impaired": True,
    }}
    check(is_commentary_track(dvs, True), "DVS audio must be DROPPED")

    comm = {"type": "audio", "properties": {"language": "eng", "track_name": "Director Commentary", "flag_commentary": True}}
    check(is_commentary_track(comm, True), "commentary audio dropped")
    cut = {"type": "audio", "properties": {"language": "eng", "track_name": "Director's Cut"}}
    check(not is_commentary_track(cut, True), "Director's Cut is not commentary")

    forced = {"type": "subtitles", "properties": {"language": "eng", "track_name": "English Forced", "flag_forced": True}}
    check(is_forced_subtitle(forced), "forced flag")
    check(not is_commentary_track(forced, True), "forced sub kept")

    und_eng = {"type": "subtitles", "properties": {"language": "und", "track_name": "English"}}
    und_unknown = {"type": "audio", "properties": {"language": "und", "track_name": ""}}
    check(is_english_named_untagged(und_eng), "untagged English by name")
    check(not is_english_named_untagged(und_unknown), "untagged unknown is not English")

    truehd = {"codec": "TrueHD", "properties": {"codec_id": "A_MLP", "audio_channels": 8, "track_name": "Atmos"}}
    aac = {"codec": "AAC", "properties": {"codec_id": "A_AAC", "audio_channels": 6}}
    check(get_audio_quality_score(truehd) > get_audio_quality_score(aac), "TrueHD Atmos > AAC 5.1")

    check(_parse_mkvmerge_progress("Progress: 45%") == 45, "plain progress")
    check(_parse_mkvmerge_progress("#GUI#progress 80%") == 80, "gui progress")
    check(_parse_mkvmerge_progress("#GUI#progress#parts=1/4") == 25, "parts progress")
    check(_parse_mkvmerge_progress("hello") is None, "no progress")

    check(not SAMPLE_NAME_RE.search("The Sampler (2012)"), "false sample")
    check(bool(SAMPLE_NAME_RE.search("Movie-sample")), "sample name")

    tmp = Path(tempfile.mkdtemp(prefix="tcc_"))
    try:
        movie = tmp / "Film (2000)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        (movie / "Film (2000).mkv").write_bytes(b"x")
        (extra / "Making-Of.mkv").write_bytes(b"y")
        (movie / "Film-sample.mkv").write_bytes(b"z")
        files, _, _ = discover_mkv_files(tmp, None, skip_extras=True)
        names = {p.name for p in files}
        check(names == {"Film (2000).mkv"}, f"discover extras/samples skipped: {names}")
        files2, _, _ = discover_mkv_files(tmp, None, skip_extras=False)
        check(any(p.name == "Making-Of.mkv" for p in files2), "include extras helper")
        hardlink_source = tmp / "seed-source.mkv"
        hardlink_target = tmp / "hardlink-target.mkv"
        hardlink_source.write_bytes(b"linked")
        hardlink_target.hardlink_to(hardlink_source)
        check(hardlink_count(hardlink_source) >= 2, "hardlink count detects seeded-style link")
        hardlink_target.unlink()
        check(hardlink_count(hardlink_source) == 1, "hardlink count clears after source removal")

        movie_srt = movie / f"Film (2000){EXTERNAL_SRT_SUFFIX}"
        movie_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8")
        external_record = validate_exact_external_english_srt(movie / "Film (2000).mkv")
        check(bool(external_record.get("valid")), f"valid exact external SRT: {external_record}")
        check(external_srt_snapshot_matches(external_record), "external SRT snapshot initial match")
        (movie / "Film (2000).en.forced.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nWrong suffix\n", encoding="utf-8",
        )
        check(
            external_record.get("path", "").endswith(f"Film (2000){EXTERNAL_SRT_SUFFIX}"),
            f"only exact {EXTERNAL_SRT_SUFFIX} qualifies",
        )
        # Legacy .en.srt is promoted to the canonical .eng.srt on validate.
        legacy_movie = tmp / "Legacy (2001)"
        legacy_movie.mkdir()
        (legacy_movie / "Legacy (2001).mkv").write_bytes(b"x")
        (legacy_movie / "Legacy (2001).en.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nEnglish dialogue\n", encoding="utf-8",
        )
        legacy_record = validate_exact_external_english_srt(legacy_movie / "Legacy (2001).mkv")
        check(bool(legacy_record.get("valid")), f"legacy .en.srt promotes: {legacy_record}")
        check(
            str(legacy_record.get("path", "")).endswith(f"Legacy (2001){EXTERNAL_SRT_SUFFIX}"),
            "promoted path is .eng.srt",
        )
        check(not (legacy_movie / "Legacy (2001).en.srt").exists(), "legacy .en.srt removed after promote")
        # A covering .eng.sdh.srt must be recorded under its OWN name: the
        # post-remux re-check re-stats the recorded path, and a stale canonical
        # path that never existed would reject an untouched valid sidecar.
        sdh_movie = tmp / "Sdh (2002)"
        sdh_movie.mkdir()
        (sdh_movie / "Sdh (2002).mkv").write_bytes(b"x")
        (sdh_movie / "Sdh (2002).eng.sdh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSDH line\n", encoding="utf-8",
        )
        sdh_record = validate_exact_external_english_srt(sdh_movie / "Sdh (2002).mkv")
        check(bool(sdh_record.get("valid")), f"covering .eng.sdh.srt qualifies: {sdh_record}")
        check(str(sdh_record.get("path", "")).endswith("Sdh (2002).eng.sdh.srt"),
              "sdh record names the file it was validated from")
        check(external_srt_snapshot_matches(sdh_record), "untouched sdh sidecar keeps its snapshot match")
        # A broken .eng.srt beside a valid .eng.sdh.srt must fall through to
        # the valid alternate rather than hiding it.
        fallthrough_movie = tmp / "Fallthrough (2003)"
        fallthrough_movie.mkdir()
        (fallthrough_movie / "Fallthrough (2003).mkv").write_bytes(b"x")
        (fallthrough_movie / "Fallthrough (2003).eng.srt").write_text(
            "<html>not a subtitle</html>", encoding="utf-8",
        )
        (fallthrough_movie / "Fallthrough (2003).eng.sdh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSDH line\n", encoding="utf-8",
        )
        fallthrough_record = validate_exact_external_english_srt(fallthrough_movie / "Fallthrough (2003).mkv")
        check(bool(fallthrough_record.get("valid")),
              f"broken .eng.srt falls through to valid .eng.sdh.srt: {fallthrough_record}")
        check(str(fallthrough_record.get("path", "")).endswith("Fallthrough (2003).eng.sdh.srt"),
              "fallthrough record names the valid .eng.sdh.srt")
        check(external_srt_snapshot_matches(fallthrough_record), "fallthrough sdh keeps its snapshot match")
        movie_srt.write_text("<html>not a subtitle</html>", encoding="utf-8")
        check(not external_srt_snapshot_matches(external_record), "changed/malformed external SRT rejects activation")
        check(not validate_exact_external_english_srt(movie / "Film (2000).mkv").get("valid"),
              "malformed external SRT does not qualify")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Accessibility and selected-track verification fixtures require no media
    # binaries; they model mkvmerge JSON directly.
    text_description = {"type": "subtitles", "codec": "SubRip/SRT", "properties": {
        "language": "eng", "track_name": "English text descriptions",
        "flag_text_descriptions": True, "flag_default": False,
    }}
    check(not is_commentary_track(text_description, True), "text-description subtitle must be KEPT")

    source_video = {"type": "video", "codec": "AVC/H.264/MPEG-4p10", "properties": {
        "codec_id": "V_MPEG4/ISO/AVC", "pixel_dimensions": "1920x1080",
        "display_dimensions": "1920x1080", "tag_number_of_frames": "240", "flag_default": True,
    }}
    source_audio = {"type": "audio", "codec": "AC-3", "properties": {
        "codec_id": "A_AC3", "language": "eng", "language_ietf": "en",
        "track_name": "English 5.1", "audio_channels": 6,
        "audio_sampling_frequency": 48000, "flag_default": False,
    }}
    source_forced = {"type": "subtitles", "codec": "SubRip/SRT", "properties": {
        "codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "English Forced",
        "flag_forced": True, "flag_default": False,
    }}
    source_info = {
        "container": {"recognized": True, "supported": True, "properties": {"duration": 10_000_000_000}},
        "tracks": [source_video, source_audio, source_forced, text_description],
        "attachments": [], "chapters": [],
    }
    verification_plan = build_verification_plan(
        source_info, source_audio, [source_forced, text_description], 4096,
    )
    output_info = json.loads(json.dumps(source_info))
    output_info["tracks"][1]["properties"]["flag_default"] = True
    check(
        track_fingerprint(output_info["tracks"][1]) == verification_plan["audio"],
        "selected audio default flag is explicit",
    )
    source_without_ietf = json.loads(json.dumps(source_info))
    source_without_ietf["tracks"][1]["properties"].pop("language_ietf")
    normalized_output = json.loads(json.dumps(output_info))
    normalized_output["tracks"][1]["properties"]["language_ietf"] = "en"
    normalized_plan = build_verification_plan(
        source_without_ietf, source_without_ietf["tracks"][1], [source_forced, text_description], 4096,
    )
    check(
        track_fingerprint(normalized_output["tracks"][1]) == normalized_plan["audio"],
        "missing source IETF tag normalizes to MKVToolNix output language tag",
    )
    aac_seven_channel_source = {"type": "audio", "codec": "AAC", "properties": {
        "codec_id": "A_AAC", "language": "eng", "audio_channels": 7,
        "audio_sampling_frequency": 24000, "default_track": True,
    }}
    aac_eight_channel_output = json.loads(json.dumps(aac_seven_channel_source))
    aac_eight_channel_output["properties"]["audio_channels"] = 8
    aac_eight_channel_output["properties"]["language_ietf"] = "en"
    aac_expected = track_fingerprint(aac_seven_channel_source, default_override=True)
    check(
        retained_audio_fingerprint_matches(track_fingerprint(aac_eight_channel_output), aac_expected),
        "AAC source channel count 7 and MKVToolNix output count 8 are accepted only when all other fields match",
    )
    aac_six_channel_source = json.loads(json.dumps(aac_seven_channel_source))
    aac_six_channel_source["properties"]["audio_channels"] = 6
    aac_six_expected = track_fingerprint(aac_six_channel_source, default_override=True)
    check(
        not retained_audio_fingerprint_matches(track_fingerprint(aac_eight_channel_output), aac_six_expected),
        "AAC channel changes other than the observed 7-to-8 representation mismatch reject the remux",
    )

    tx_tmp = Path(tempfile.mkdtemp(prefix="tcc_tx_"))
    original_runner = globals()["_run_mkvmerge"]

    def age_for_recovery(path: Path) -> None:
        aged = time.time() - ORPHAN_MIN_AGE_SECONDS - 2.0
        os.utime(path, (aged, aged))

    try:
        temp_fixture = tx_tmp / "verify-fixture.mkv"
        temp_fixture.write_bytes(b"x" * 4096)
        ok, reason = _verify_remux_info(temp_fixture, output_info, verification_plan)
        check(ok, f"fingerprint verification accepted intended output: {reason}")
        source_without_frame_stats = json.loads(json.dumps(source_info))
        source_without_frame_stats["tracks"][0]["properties"].pop("tag_number_of_frames")
        generated_frame_output = json.loads(json.dumps(output_info))
        generated_frame_plan = build_verification_plan(
            source_without_frame_stats, source_without_frame_stats["tracks"][1],
            [source_forced, text_description], 4096,
        )
        ok, reason = _verify_remux_info(temp_fixture, generated_frame_output, generated_frame_plan)
        check(ok, f"generated output-only frame statistics are accepted: {reason}")
        wrong_frame_output = json.loads(json.dumps(output_info))
        wrong_frame_output["tracks"][0]["properties"]["tag_number_of_frames"] = "241"
        ok, _ = _verify_remux_info(temp_fixture, wrong_frame_output, verification_plan)
        check(not ok, "a changed source-known video frame count rejects the remux")
        changed_output = json.loads(json.dumps(output_info))
        changed_output["tracks"][1]["properties"]["language"] = "fra"
        ok, _ = _verify_remux_info(temp_fixture, changed_output, verification_plan)
        check(not ok, "fingerprint verification rejects a wrong retained audio track")

        original = tx_tmp / "Recovery Film.mkv"
        original.write_bytes(b"source" * 1024)
        temp_path, journal, token = new_transaction_paths(original)
        check(temp_path.parent == original.parent and journal.parent == original.parent, "transaction paths are siblings")
        check(temp_path.name != original.name and _transaction_token_from_temp_name(temp_path.name) == token,
              "transaction temp names are unique and parseable")
        transaction = create_transaction(original, temp_path, token, original.stat())
        transaction["verification_plan"] = verification_plan
        temp_path.write_bytes(b"x" * 4096)
        write_transaction(journal, transaction)
        check(read_transaction(journal) is not None, "transaction journal round-trip")
        check(_source_snapshot_matches(original, transaction["source_snapshot"]), "source snapshot initial match")
        original.write_bytes(b"changed" * 1024)
        check(not _source_snapshot_matches(original, transaction["source_snapshot"]), "source snapshot detects mutation")

        # An unverified missing-original transaction must be preserved, not promoted.
        original.unlink()
        age_for_recovery(temp_path)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(temp_path.exists() and journal.exists(), "unverified orphan retained for manual review")
        cleanup_transaction_artifacts(temp_path, journal)

        # A verified journal is recoverable only after verification succeeds again.
        recovered = tx_tmp / "Recovered Film.mkv"
        recovered_temp, recovered_journal, recovered_token = new_transaction_paths(recovered)
        recovered_temp.write_bytes(b"x" * 4096)
        recovered_tx = create_transaction(recovered, recovered_temp, recovered_token, temp_fixture.stat())
        recovered_tx["verification_plan"] = verification_plan
        recovered_tx["phase"] = "verified"
        age_for_recovery(recovered_temp)
        recovered_tx["temp_snapshot"] = _source_snapshot(recovered_temp)
        write_transaction(recovered_journal, recovered_tx)
        globals()["_run_mkvmerge"] = lambda *_args, **_kwargs: (0, json.dumps(output_info), "")
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(recovered.exists() and not recovered_temp.exists() and not recovered_journal.exists(),
              "verified and rechecked orphan recovers atomically")

        # Simulate a crash after os.replace but before journal deletion.
        journal_only = tx_tmp / "Journal Only.mkv"
        journal_only.write_bytes(b"source" * 1024)
        missing_temp, stale_journal, stale_token = new_transaction_paths(journal_only)
        stale_tx = create_transaction(journal_only, missing_temp, stale_token, journal_only.stat())
        stale_tx["phase"] = "verified"
        write_transaction(stale_journal, stale_tx)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(not stale_journal.exists() and journal_only.exists(), "stale journal removed only beside intact original")

        legacy = tx_tmp / "temp_clean_legacy-missing.mkv"
        legacy.write_bytes(b"x" * 4096)
        age_for_recovery(legacy)
        cleanup_orphan_temps(tx_tmp, "stub", None)
        check(legacy.exists(), "legacy orphan without original is never auto-promoted")
    except Exception as exc:
        errors.append(f"transaction/fingerprint self-test exception: {exc}")
    finally:
        globals()["_run_mkvmerge"] = original_runner
        shutil.rmtree(tx_tmp, ignore_errors=True)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (selection + external-SRT policy + fingerprints + transactions + recovery + discovery + hardlinks)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
