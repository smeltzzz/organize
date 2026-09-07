"""The offline self-tests lifted out of ``subtitle_fetcher.py``.

These assertions used to ship inside the tool itself. They are unchanged; only
their address is different. Each function is rebound to the tool module's
namespace by :func:`bind_to_tool`, so a body that reads or patches a module
global (``globals()["_movie_upgrade_decision"] = ...``,  ``global CFG``)
affects the tool exactly as it did when it lived there.

``tests/test_selftests.py`` runs them as part of the normal unit suite.
"""

from __future__ import annotations

from unittest import mock

import subtitle_fetcher as tool
from tests.selftests import bind_to_tool

# The bodies below resolve their names in the tool's namespace. A few of the
# names they need had no other user in the tool once the self-tests moved out,
# and dead imports do not belong in a shipped file — so they are supplied from
# here, where the dependency is visible.
tool.mock = mock


def run_scrape_self_tests(errors: list[str]) -> None:
    """Offline self-test: registry invariants, every parser, breaker, chain."""

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(tuple(SCRAPE_SOURCES.keys()) == SCRAPE_PROVIDER_ORDER, "registry keys follow the documented order")
    check(all(src.key in SCRAPE_PROVIDER_LABELS for src in SCRAPE_SOURCES.values()), "every source has a label")
    check(len(SCRAPE_SOURCES) == 7, "exactly seven scraped sources are registered")

    identity = SourceIdentity("The Father", 2020, scrape_normalize_title("The Father"))

    # --- Subf2m: search result year match + movie page + zip --------------
    subf2me_search = (
        b"<html><body><div class=\"search-result\">"
        b"<h2 class=\"exact\">The Father</h2><ul>"
        b"<li><a href=\"/subtitles/111\">The Father (2019)</a></li>"
        b"<li><a href=\"/subtitles/222\">The Father (2020)</a></li>"
        b"</ul></div></body></html>"
    )
    subf2me_movie = (
        b"<html><body><ul>"
        b"<li class=\"item\"><li>playWEB</li>"
        b"<a class=\"download icon-download\" href=\"/subtitles/222/en/999\"></a></li>"
        b"</ul></body></html>"
    )
    subf2me_dl_page = b"<html><body><div class=\"download\"><a href=\"/dl/file.zip\">get</a></div></body></html>"
    def make_zip(name: str, payload: bytes) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, payload)
        return buf.getvalue()

    SRT = b"1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"
    subf2me_zip = make_zip("sub.utf.srt", SRT)

    class FakeT:
        def __init__(self, routes: dict[str, bytes]) -> None:
            self.routes = routes
            self.calls: list[str] = []

        def _route(self, url: str) -> bytes:
            best: tuple[int, bytes] | None = None
            for prefix, payload in self.routes.items():
                if url.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), payload)
            if best is None:
                raise ScrapeSourceError(f"unrouted {url}")
            return best[1]

        def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
            self.calls.append(url)
            return self._route(url)

        def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
            self.calls.append("POST " + url)
            return self._route(url)

    t = FakeT({
        "https://subf2m.co/subtitles/searchbytitle": subf2me_search,
        "https://subf2m.co/subtitles/222/en": subf2me_movie,
        "https://subf2m.co/subtitles/222/en/999": subf2me_dl_page,
        "https://subf2m.co/dl/file.zip": subf2me_zip,
    })
    src = Subf2meSource()
    cands = src.search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "/subtitles/222", "subf2m search keeps the right-year entry only")
    raw = src.fetch(cands[0], t)
    check(raw == SRT, "subf2m fetch extracts the UTF-8 entry from the zip")

    # --- Podnapisi: JSON search + year filter + zip ------------------------
    podnapisi_payload = (
        b'{"data":[{"id":77,"releases":["The.Father.2020.1080p.BluRay.x264-GRP"],'
        b'"custom_releases":[],"movie":{"title":"The Father","year":"2020"}},'
        b'{"id":78,"releases":[],"custom_releases":[],"movie":{"title":"The Father","year":"2019"}}],'
        b'"page":"1","all_pages":"1"}'
    )
    t = FakeT({
        "https://www.podnapisi.net/subtitles/search/advanced": podnapisi_payload,
        "https://www.podnapisi.net/subtitles/77/download": make_zip("77.srt", SRT),
    })
    cands = PodnapisiSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "77" and cands[0].release.startswith("The.Father.2020"),
          "podnapisi keeps English year-matching subtitles only")
    check(PodnapisiSource().fetch(cands[0], t) == SRT, "podnapisi fetch unzips the single-file archive")

    # --- Addic7ed: search + completed English rows + Referer ---------------
    addic7ed_search = b"<html><body><b>1 results found</b><a href=\"movie/555\">x</a></body></html>"
    addic7ed_movie = (
        b"<html><body><div>Deadpool 2 (2018) <small>...</small></div>"
        b"<table><tr><td class=\"version\">Version 1080p x264-KILLERS,</td></tr></table>"
        b"<table><tr><td class=\"language\">English</td><td>Completed</td><td>"
        b"<a href=\"/sd/9001\"><strong>Download</strong></a> 123 Downloads</td></tr>"
        b"<tr><td class=\"language\">English (Hearing Impaired)</td><td>Completed</td><td>"
        b"<a href=\"/sd/9002\"><strong>most updated</strong></a> 5 Downloads</td></tr>"
        b"<tr><td class=\"language\">Fran\xc3\xa7ais</td><td>Completed</td><td>"
        b"<a href=\"/sd/9003\"><strong>Download</strong></a> 900 Downloads</td></tr>"
        b"<tr><td class=\"language\">English</td><td>% Completed</td><td>"
        b"<a href=\"/sd/9004\"><strong>Download</strong></a> 400 Downloads</td></tr></table>"
        b"<a href=\"/show/12\">show</a></body></html>"
    )
    t = FakeT({
        "https://www.addic7ed.com/srch.php": addic7ed_search,
        "https://www.addic7ed.com/movie/555": addic7ed_movie,
        "https://www.addic7ed.com/sd/": SRT,
    })
    ident_dp = SourceIdentity("Deadpool 2", 2018, scrape_normalize_title("Deadpool 2"))
    cands = Addic7edSource().search(ident_dp, t)
    check(len(cands) == 2 and all(c.feature_title == "Deadpool 2" for c in cands),
          "addic7ed keeps English rows only and drops the incomplete (% Completed) row")
    check(all(c.extra.get("referer", "").endswith("/show/12") for c in cands), "addic7ed captures the movie referer")
    check(Addic7edSource().fetch(cands[0], t) == SRT, "addic7ed fetch returns the raw SRT")

    # --- SubSource: direct slug + English rows + API link -------------------
    subsource_movie = (
        b"<html><body><table>"
        b"<tr><td><a href=\"/subtitle/the-father-2020/english/501\">English</a></td>"
        b"<td><a href=\"/subtitle/the-father-2020/english/501\">The.Father.2020.1080p</a></td></tr>"
        b"<tr><td><a href=\"/subtitle/the-father-2020/french/502\">French</a></td></tr>"
        b"</table></body></html>"
    )
    t = FakeT({
        "https://subsource.net/subtitles/the-father-2020": subsource_movie,
        "https://subsource.net/subtitle/the-father-2020/english/501": (
            b"<html><a href=\"https://api.subsource.net/v1/subtitle/download/abc123\">Download</a></html>"
        ),
        "https://api.subsource.net/v1/subtitle/download/abc123": SRT,
    })
    cands = SubSourceSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id.endswith("/english/501"), "subsource finds the English file row")
    check(SubSourceSource().fetch(cands[0], t) == SRT, "subsource fetch follows the API download link")

    # --- Subsunacs: POST search + language guard + getentry ------------------
    subsunacs_search = (
        b"<html><body><table><tr>"
        b"<td><a href=\"/subtitles/The_Father-9001/\">The Father</a> <span>(2020)</span></td>"
        b"</tr></table></body></html>"
    )
    subsunacs_page_en = (
        "<html><h1>The Father (2020)</h1>Език: Английски"
        "<a href=\"https://subsunacs.net/getentry.php?id=9001&amp;ei=0\">srt</a></html>"
    ).encode()
    t = FakeT({
        "https://subsunacs.net/search.php": subsunacs_search,
        "https://subsunacs.net/subtitles/The_Father-9001/": subsunacs_page_en,
        "https://subsunacs.net/getentry.php": SRT,
    })
    cands = SubsunacsSource().search(identity, t)
    check(len(cands) == 1 and cands[0].feature_year == 2020, "subsunacs parses the search row and year")
    check(SubsunacsSource().fetch(cands[0], t) == SRT, "subsunacs fetch verifies English and downloads the entry")
    t2 = FakeT({
        "https://subsunacs.net/search.php": subsunacs_search,
        "https://subsunacs.net/subtitles/The_Father-9001/": (
            "<html><h1>The Father (2020)</h1>Език: Български</html>"
        ).encode(),
    })
    try:
        SubsunacsSource().fetch(cands[0], t2)
        check(False, "subsunacs must reject a Bulgarian subtitle page")
    except CandidateRejected:
        pass

    # --- YIFY: search cards + English rows + zip ----------------------------
    yify_search = (
        b"<html><body>"
        b"<div class=\"media\"><div class=\"media-body\">"
        b"<h3 class=\"media-heading\" itemprop=\"name\">The Father</h3>"
        b"<span class=\"movinfo-section\">2020<small>year</small></span>"
        b"<a href=\"/movie-imdb/tt111\">go</a></div></div>"
        b"<div class=\"media\"><div class=\"media-body\">"
        b"<h3 class=\"media-heading\" itemprop=\"name\">The Father</h3>"
        b"<span class=\"movinfo-section\">2019<small>year</small></span>"
        b"<a href=\"/movie-imdb/tt222\">go</a></div></div>"
        b"</body></html>"
    )
    yify_movie = (
        b"<html><tbody>"
        b"<tr data-id=\"1\"><span class=\"sub-lang\">Bulgarian</span>"
        b"<td class=\"rating-cell\">4</td><a href=\"/subtitles/77\">x</a></tr>"
        b"<tr data-id=\"2\"><span class=\"sub-lang\">English</span>"
        b"<td class=\"rating-cell\">2</td><a href=\"/subtitles/88\">x</a></tr>"
        b"<tr data-id=\"3\"><span class=\"sub-lang\">English</span>"
        b"<td class=\"rating-cell\">5</td><a href=\"/subtitles/99\">x</a></tr>"
        b"</tbody></html>"
    )
    t = FakeT({
        "https://yifysubtitles.ch/search": yify_search,
        "https://yifysubtitles.ch/movie-imdb/tt111": yify_movie,
        "https://yifysubtitles.ch/subtitle/99.zip": make_zip("88.srt", SRT),
    })
    cands = YifySubtitlesSource().search(identity, t)
    check(len(cands) == 2 and cands[0].file_id == "/movie-imdb/tt111"
          and cands[1].feature_year == 2019,
          "yify search returns the movie cards with their years")
    picked = pick_candidates(identity, cands, limit=SCRAPE_MAX_CANDIDATES_PER_SOURCE)
    check(len(picked) == 1 and picked[0].file_id == "/movie-imdb/tt111",
          "year filtering keeps only the right-year card")
    check(YifySubtitlesSource().fetch(cands[0], t) == SRT, "yify fetch picks the highest-rated English row")

    # --- Subs.sab.bz: POST search + Cyrillic guard ---------------------------
    subsab_search = (
        b"<html><body><table><tr>"
        b"<td><a href=\"http://subs.sab.bz/index.php?s=x&amp;act=download&amp;attach_id=4242\">The Father (2020)</a></td>"
        b"</tr></table></body></html>"
    )
    cyrillic = "1\n00:00:01,000 --> 00:00:03,000\nздравей свят\n\n".encode()
    t = FakeT({
        "http://subs.sab.bz/index.php?act=download": SRT,
        "http://subs.sab.bz/index.php?": subsab_search,
    })
    cands = SubsSabSource().search(identity, t)
    check(len(cands) == 1 and cands[0].file_id == "4242", "subs.sab.bz captures the attach id")
    check(SubsSabSource().fetch(cands[0], t) == SRT, "subs.sab.bz fetch accepts an English SRT")
    t2 = FakeT({
        "http://subs.sab.bz/index.php?act=download": cyrillic,
        "http://subs.sab.bz/index.php?": subsab_search,
    })
    try:
        SubsSabSource().fetch(cands[0], t2)
        check(False, "subs.sab.bz must reject a Cyrillic payload")
    except CandidateRejected:
        pass

    # --- selection + breaker + chain ------------------------------------------
    mixed = [
        ScrapeCandidate(provider="x", file_id="a", feature_title="The Father", feature_year=2020, downloads=10),
        ScrapeCandidate(provider="x", file_id="b", feature_title="Totally Different", feature_year=2020),
        ScrapeCandidate(provider="x", file_id="c", feature_title="The Father", feature_year=2019),
    ]
    picked = pick_candidates(identity, mixed)
    check([c.file_id for c in picked] == ["a"], "selection requires title match and year match")

    chain = ScrapeChain(keys=(PROVIDER_SUBF2ME,), transport=FakeT({}))
    for _ in range(BREAKER_HARD_FAILURES):
        try:
            chain.search(PROVIDER_SUBF2ME, identity)
        except SourceUnavailable:
            pass
    check(chain.health[PROVIDER_SUBF2ME].disabled, "three hard failures disable the source")
    try:
        chain.search(PROVIDER_SUBF2ME, identity)
        check(False, "disabled source must not be searched")
    except SourceUnavailable:
        pass

    cap_chain = ScrapeChain(keys=(PROVIDER_SUBF2ME,), transport=FakeT({}),
                            search_caps={PROVIDER_SUBF2ME: 1}, reserved={PROVIDER_SUBF2ME: 1})
    try:
        cap_chain.search(PROVIDER_SUBF2ME, identity)
        check(False, "exhausted search cap must refuse the source")
    except SourceUnavailable as exc:
        check("cap" in str(exc), "cap exhaustion is named in the reason")

    # chain: first source dead, second source delivers
    ok_routes = {
        "https://www.podnapisi.net/subtitles/search/advanced": podnapisi_payload,
        "https://www.podnapisi.net/subtitles/77/download": make_zip("77.srt", SRT),
    }
    reasons: list[tuple[str, str]] = []
    # A FakeT with only the podnapisi routes hard-fails every subf2me request
    # ("unrouted"), so the chain must fail over to podnapisi.
    mixed_chain = ScrapeChain(
        keys=(PROVIDER_SUBF2ME, PROVIDER_PODNAPISI),
        transport=FakeT(ok_routes),
    )
    got = run_scrape_chain(
        identity, keys=(PROVIDER_SUBF2ME, PROVIDER_PODNAPISI), chain=mixed_chain,
        on_reason=lambda k, r: reasons.append((k, r)),
    )
    check(got[1] == PROVIDER_PODNAPISI and got[2] == SRT, "chain fails over to the next live source")
    check(any(k == PROVIDER_SUBF2ME for k, _ in reasons), "the failed source's verdict is reported")


def run_extract_self_tests(errors: list[str]) -> None:
    """Offline self-tests for embedded-subtitle extraction.

    Everything here is local: the external binaries are replaced with a fake
    ``subprocess.run`` that serves a canned ``mkvmerge -J`` payload and writes
    a canned ASS track, so no MKVToolNix, no Tesseract, and no media file is
    needed.
    """

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    this_module = sys.modules[__name__]

    def fake_binaries(_name: str, explicit: str | None = None) -> str:
        return f"fake-{_name}"

    saved_ledger = os.environ.get(EXTRACTED_LEDGER_ENV)
    with tempfile.TemporaryDirectory(prefix="extract_selftest_") as tmpdir:
        tmp = Path(tmpdir)
        os.environ[EXTRACTED_LEDGER_ENV] = str(tmp / "extracted.json")
        try:
            # ---- 1. ASS/SSA and WebVTT conversion --------------------------
            ass = (
                "[Script Info]\nTitle: demo\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
                "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,"
                "100,0,0,1,2,2,2,10,10,10,1\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\i1}Hello there\\NGeneral Kenobi\n"
                "Dialogue: 0,0:00:05.00,0:00:06.25,Default,,0,0,0,,Second line\n"
                "Comment: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,not shown\n"
            )
            converted = ass_to_srt(ass)
            check("00:00:01,500 --> 00:00:03,000" in converted, f"ASS timing converted: {converted!r}")
            check("Hello there\nGeneral Kenobi" in converted,
                  f"ASS override block and \\N line break handled: {converted!r}")
            check("Second line" in converted, "ASS second cue kept")
            check("not shown" not in converted, "ASS Comment lines never become cues")
            check(converted.index("Hello there") < converted.index("Second line"),
                  "ASS cues stay in time order")
            check(converted.startswith("1\n"), "ASS output is renumbered from 1")

            ssa = (
                "[Script Info]\n\n[V4 Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
                "SecondaryColour, TertiaryColour, BackColour, Bold, Italic, BorderStyle, Outline, "
                "Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,1,2,2,2,"
                "10,10,10,0,1\n\n"
                "[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: Marked=0,0:00:02.00,0:00:04.00,Default,,0,0,0,,SSA cue\n"
            )
            check("SSA cue" in ass_to_srt(ssa), "SSA v4 column order parsed (no Layer column)")

            vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello VTT\n\n"
                   "00:00:04.000 --> 00:00:05.500 align:start\nSecond VTT\n")
            vtt_text = vtt_to_srt(vtt)
            check("00:00:01,000 --> 00:00:03,000" in vtt_text and "Hello VTT" in vtt_text,
                  f"WebVTT converted: {vtt_text!r}")

            messy = ("5\r\n00:00:01,000 --> 00:00:02,000\r\nfirst\r\n\r\n"
                     "9\r\n00:00:03,000 --> 00:00:04,000\r\nsecond\r\n")
            fixed = normalize_extracted_srt(messy)
            check(fixed.startswith("1\n00:00:01,000 --> 00:00:02,000\nfirst\n\n2\n"),
                  f"cues renumbered: {fixed!r}")
            check("\r" not in fixed, "CRLF normalized away")

            # ---- 2. quality gate -------------------------------------------
            good = render_srt_cues([
                (f"00:00:{index:02d},000", f"00:00:{index:02d},900", "This is a line of English dialogue")
                for index in range(1, 31)
            ])
            ok, reason = extracted_subtitle_quality(good)
            check(ok, f"a complete English track passes ({reason})")

            short = render_srt_cues([("00:00:01,000", "00:00:02,000", "Only line")])
            ok, reason = extracted_subtitle_quality(short)
            check(not ok and "signs/songs-only" in reason, f"a one-cue track is refused ({reason})")

            foreign = render_srt_cues([
                ("00:00:01,000", "00:00:02,000", "Это предложение на русском языке")
                for _ in range(30)
            ])
            ok, reason = extracted_subtitle_quality(foreign)
            check(not ok and "not Latin-script" in reason, f"a Cyrillic track is refused ({reason})")

            noise = render_srt_cues([
                ("00:00:01,000", "00:00:02,000", "||| ~~~ ### ||| ~~~") for _ in range(30)
            ])
            ok, reason = extracted_subtitle_quality(noise, method="ocr")
            check(not ok and "noise" in reason, f"OCR noise is refused ({reason})")

            salad = render_srt_cues([
                ("00:00:01,000", "00:00:02,000", "Qwx zp vfg blrt mnk jklqwerty") for _ in range(30)
            ])
            ok, reason = extracted_subtitle_quality(salad)
            check(not ok and "does not read as English" in reason,
                  f"word salad is refused ({reason})")

            # ---- 3. track classification -----------------------------------
            tracks: list[dict[str, Any]] = [
                {"id": 2, "type": "subtitles",
                 "properties": {"codec_id": "S_HDMV/PGS", "language": "eng", "track_name": "English"}},
                {"id": 3, "type": "subtitles",
                 "properties": {"codec_id": "S_TEXT/ASS", "language": "eng",
                                "track_name": "English (SDH)", "flag_hearing_impaired": True}},
                {"id": 4, "type": "subtitles",
                 "properties": {"codec_id": "S_TEXT/UTF8", "language": "fre", "track_name": "French"}},
                {"id": 5, "type": "subtitles",
                 "properties": {"codec_id": "S_TEXT/UTF8", "language": "eng",
                                "track_name": "English forced", "flag_forced": True}},
                {"id": 6, "type": "subtitles",
                 "properties": {"codec_id": "S_TEXT/UTF8", "language": "eng", "track_name": "Commentary"}},
                {"id": 7, "type": "audio", "properties": {"codec_id": "A_AC3", "language": "eng"}},
                {"id": 8, "type": "subtitles",
                 "properties": {"codec_id": "S_VOBSUB", "language": "und", "track_name": "English"}},
                {"id": 9, "type": "subtitles",
                 "properties": {"codec_id": "S_KATE", "language": "eng"}},
            ]
            picked = classify_embedded_subtitle_tracks(tracks)
            check([item.track_id for item in picked] == [3, 2, 8],
                  f"text first, then PGS then VobSub; forced/commentary/foreign dropped: "
                  f"{[item.track_id for item in picked]}")
            check(picked[0].sdh, "the SDH flag is carried through")
            check(picked[0].kind == "text" and picked[1].kind == "image", "text outranks image")
            check(classify_embedded_subtitle_tracks([]) == [], "no tracks yields no candidates")

            # ---- 4. OCR backends -------------------------------------------
            pgsrip_backend = OcrBackend(OCR_BACKEND_PGSRIP, "pgsrip + Tesseract", ("pgsrip",),
                                        frozenset({"PGS"}), output_mode="sibling")
            check(pgsrip_backend.build_command(Path("/tmp/4.sup"), Path("/tmp/4.srt"),
                                               track_id=4, language="eng")
                  == ["pgsrip", "-l", "en", str(Path("/tmp/4.sup"))],
                  "pgsrip argv is built correctly (language filter, no output flag)")
            check(OCR_BACKEND_AUTO_ORDER[0] == OCR_BACKEND_PGSRIP,
                  "pgsrip is the first backend auto-detection tries")

            sup_backend = OcrBackend(OCR_BACKEND_SUP2SRT, "sup2srt + Tesseract", ("sup2srt",),
                                     frozenset({"PGS"}))
            check(sup_backend.build_command(Path("/tmp/3.sup"), Path("/tmp/3.srt"),
                                            track_id=3, language="eng")
                  == ["sup2srt", "-l", "eng", "-o",
                      str(Path("/tmp/3.srt")), str(Path("/tmp/3.sup"))],
                  "sup2srt argv is built correctly")
            se_backend = OcrBackend(OCR_BACKEND_SUBTITLEEDIT, "Subtitle Edit", ("SubtitleEdit",),
                                    frozenset({"PGS", "VOBSUB"}), output_mode="sibling")
            se_argv = se_backend.build_command(Path("/tmp/3.sup"), Path("/tmp/out.srt"),
                                               track_id=3, language="eng")
            check(se_argv[:4] == ["SubtitleEdit", "/convert", str(Path("/tmp/3.sup")), "srt"],
                  f"Subtitle Edit argv: {se_argv}")
            check(se_backend.result_path(Path("/tmp/3.sup"), Path("/tmp/out.srt")) == Path("/tmp/3.srt"),
                  "Subtitle Edit writes beside its input")
            pg_backend = OcrBackend(OCR_BACKEND_PGSTOSRT, "PgsToSrt",
                                    ("dotnet", "/opt/PgsToSrt.dll"), frozenset({"PGS"}))
            check("--tesseractlanguage" in pg_backend.build_command(
                Path("/tmp/3.sup"), Path("/tmp/o.srt"), track_id=3, language="en"),
                "PgsToSrt is given a Tesseract language")
            custom_backend = OcrBackend(OCR_BACKEND_CUSTOM, "custom", ("/opt/ocr.sh",),
                                        frozenset({"PGS"}), arg_template=("{input}", "{output}"))
            check(custom_backend.build_command(Path("/tmp/3.sup"), Path("/tmp/o.srt"),
                                               track_id=3, language="en")
                  == ["/opt/ocr.sh", str(Path("/tmp/3.sup")), str(Path("/tmp/o.srt"))],
                  "custom template is expanded")
            check(not sup_backend.supports_track(picked[2]),
                  "sup2srt refuses a VobSub track it cannot read")
            with tempfile.TemporaryDirectory() as ocr_dir:
                ocr_root = Path(ocr_dir)
                src = ocr_root / "track4.sup"
                src.write_bytes(b"pgs")
                expected = ocr_root / "track4.srt"
                check(find_sibling_srt(src, expected) is None,
                      "no output file means the OCR pass produced nothing")
                (ocr_root / "track4.eng.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
                check(find_sibling_srt(src, expected) is not None,
                      "a backend that renames its output is still found")
                expected.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
                check(find_sibling_srt(src, expected) == expected,
                      "the documented output name always wins")

            backend, note = detect_ocr_backend(OCR_BACKEND_NONE)
            check(backend is None and "disabled" in note, "--ocr-backend none disables OCR")
            backend, note = detect_ocr_backend(OCR_BACKEND_CUSTOM, explicit_bin="",
                                               arg_template="{input} {output}")
            check(backend is None and "--ocr-bin" in note,
                  "a custom backend without a binary explains itself")

            # ---- 5. extraction record (read by sync_subtitles.py) ----------
            video = tmp / "Movie (2020)" / "Movie (2020).mkv"
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b"movie-bytes")
            sidecar = tmp / "Movie (2020)" / "Movie (2020).eng.srt"
            sidecar.write_text(good, encoding="utf-8")
            track = EmbeddedSubtitleTrack(track_id=3, codec_id="S_TEXT/ASS", language="eng",
                                          name="English", kind="text", extension=".ass")
            check(record_extracted_sidecar(video, sidecar, track=track, method="text",
                                           cue_count=30, sha256=sha256_text(good)),
                  "an extraction is recorded durably")
            found = find_extracted_record(sidecar, sha256_text(good))
            check(found is not None and found["track_id"] == 3 and found["method"] == "text",
                  "the record reads back")
            check(find_extracted_record(sidecar, "stale-hash") is None,
                  "a sidecar replaced since extraction is no longer trusted")
            check(find_extracted_record(tmp / "Other (2021)" / "Other (2021).eng.srt") is None,
                  "an unknown sidecar has no record")

            # ---- 6. one movie, end to end, with faked binaries -------------
            movie_dir = tmp / "library" / "Fake (2021)"
            movie_dir.mkdir(parents=True)
            movie = movie_dir / "Fake (2021).mkv"
            movie.write_bytes(b"mkv-bytes")
            dest = movie.with_name("Fake (2021).eng.srt")
            probe_payload = json.dumps({"tracks": [
                {"id": 0, "type": "video", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
                {"id": 1, "type": "audio",
                 "properties": {"codec_id": "A_TRUEHD", "language": "eng"}},
                {"id": 2, "type": "subtitles",
                 "properties": {"codec_id": "S_TEXT/ASS", "language": "eng", "track_name": "English"}},
            ]})

            def fake_run(command: Sequence[str], **_kwargs: Any) -> Any:
                argv = [str(part) for part in command]
                if "-J" in argv:
                    return subprocess.CompletedProcess(argv, 0, probe_payload.encode("utf-8"), b"")
                if len(argv) > 1 and argv[1] == "tracks":
                    target = argv[-1].split(":", 1)[1]
                    Path(target).write_text(ass, encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with mock.patch.object(subprocess, "run", fake_run), \
                    mock.patch.object(this_module, "find_mkvtoolnix_binary", fake_binaries):
                outcome = extract_embedded_english_srt(movie, dest, ExtractOptions(min_cues=2))
            check(outcome.ok, f"a text track is extracted end to end ({outcome.detail})")
            check(outcome.method == "text" and outcome.cue_count == 2,
                  f"end-to-end outcome reports the method and cue count ({outcome.cue_count})")
            check(dest.is_file() and dest.read_text(encoding="utf-8") == ass_to_srt(ass),
                  "the sidecar is written from the embedded track")
            check(find_extracted_record(dest, sha256_text(ass_to_srt(ass))) is not None,
                  "the end-to-end extraction is recorded for the sync step")

            dest.unlink()
            with mock.patch.object(subprocess, "run", fake_run), \
                    mock.patch.object(this_module, "find_mkvtoolnix_binary", fake_binaries):
                dry = extract_embedded_english_srt(movie, dest,
                                                   ExtractOptions(min_cues=2, dry_run=True))
            check(dry.ok and not dest.exists(),
                  "a dry run previews the extraction without writing anything")

            image_payload = json.dumps({"tracks": [
                {"id": 4, "type": "subtitles",
                 "properties": {"codec_id": "S_HDMV/PGS", "language": "eng"}},
            ]})

            def fake_run_image(command: Sequence[str], **_kwargs: Any) -> Any:
                argv = [str(part) for part in command]
                if "-J" in argv:
                    return subprocess.CompletedProcess(argv, 0, image_payload.encode("utf-8"), b"")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with mock.patch.object(subprocess, "run", fake_run_image), \
                    mock.patch.object(this_module, "find_mkvtoolnix_binary", fake_binaries):
                image_only = extract_embedded_english_srt(
                    movie, dest, ExtractOptions(min_cues=2, ocr_backend=OCR_BACKEND_NONE))
            check(not image_only.ok and not dest.exists(),
                  "an image-only movie with OCR disabled does not create a sidecar")
            check("OCR is disabled" in (image_only.unavailable_reason + image_only.detail),
                  f"the image-only fall-through names the reason "
                  f"({image_only.unavailable_reason or image_only.detail})")

            missing_payload = json.dumps({"tracks": [
                {"id": 1, "type": "audio", "properties": {"codec_id": "A_AC3", "language": "eng"}},
            ]})

            def fake_run_none(command: Sequence[str], **_kwargs: Any) -> Any:
                argv = [str(part) for part in command]
                if "-J" in argv:
                    return subprocess.CompletedProcess(argv, 0, missing_payload.encode("utf-8"), b"")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with mock.patch.object(subprocess, "run", fake_run_none), \
                    mock.patch.object(this_module, "find_mkvtoolnix_binary", fake_binaries):
                no_track = extract_embedded_english_srt(movie, dest, ExtractOptions(min_cues=2))
            check(not no_track.ok and "no English subtitle track" in no_track.unavailable_reason,
                  f"a movie with no English track falls through to the providers "
                  f"({no_track.unavailable_reason})")

            with mock.patch.object(this_module, "find_mkvtoolnix_binary", lambda *_a, **_k: None):
                no_tools = extract_embedded_english_srt(movie, dest, ExtractOptions(min_cues=2))
            check(not no_tools.ok and "MKVToolNix" in no_tools.unavailable_reason,
                  "a machine without MKVToolNix reports the install, not a crash")
        finally:
            if saved_ledger is None:
                os.environ.pop(EXTRACTED_LEDGER_ENV, None)
            else:
                os.environ[EXTRACTED_LEDGER_ENV] = saved_ledger


def run_self_tests() -> int:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # Deterministic hash: 128 KiB of incrementing bytes.
    blob = bytes(i & 0xFF for i in range(MIN_HASH_SIZE))
    digest = moviehash_bytes(blob)
    check(len(digest) == 16 and all(c in "0123456789abcdef" for c in digest), f"hash format {digest}")
    # Same bytes → same hash
    check(moviehash_bytes(blob) == digest, "hash stable")
    # Size change changes hash
    blob2 = blob + b"\x00"
    check(moviehash_bytes(blob2[:MIN_HASH_SIZE]) == digest, "same first/last 128k")

    payload = {
        "data": [
            {"attributes": {
                "moviehash_match": False, "download_count": 99999,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en", "release": "wrong",
                "files": [{"file_id": 1, "file_name": "fuzzy.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 12, "from_trusted": True,
                "ratings": 8.5, "votes": 8,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en", "release": "hashy",
                "files": [{"file_id": 2, "file_name": "Knowing.2009.BluRay.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 500,
                "machine_translated": True, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 3, "file_name": "mt.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 9000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "fr",
                "files": [{"file_id": 4, "file_name": "wrong-language.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 1000,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": True, "foreign_parts_only": False,
                "language": "eng",
                "files": [{"file_id": 5, "file_name": "sdh.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 1000,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": True,
                "language": "english",
                "files": [{"file_id": 6, "file_name": "forced.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 500,
                "ratings": 6.5, "votes": 10,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 7, "file_name": "Knowing.2009.1080p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 300, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 8, "file_name": "Knowing.2009.2160p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 9999, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 9, "file_name": "Inception.2010.1080p.BluRay.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 50000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 10, "file_name": "Knowing.2009.720p.WEB.ENG.srt"}],
            }},
            {"attributes": {
                "moviehash_match": True, "download_count": 7000, "from_trusted": True,
                "ratings": 10, "votes": 100,
                "machine_translated": False, "ai_translated": False,
                "hearing_impaired": False, "foreign_parts_only": False,
                "language": "en",
                "files": [{"file_id": 11, "file_name": "Knowing.2010.1080p.BluRay.ENG.srt"}],
            }},
        ]
    }
    cands = parse_candidates(payload)
    hash_identity = MovieIdentity("Knowing", 2009, "knowing")
    # Downloads-first: without an identity the most-downloaded Blu-ray release
    # wins even though it names another movie; with the movie identity the
    # Inception upload and the wrong-year Knowing upload drop out and the
    # 2009 Knowing release with the most downloads wins. The 50k-download WEB
    # release never qualifies.
    pick = pick_candidate(cands, Config())
    check(pick is not None and pick.file_id == 9, f"downloads-first pick {pick}")
    pick_named = pick_candidate(cands, Config(), identity=hash_identity)
    check(pick_named is not None and pick_named.file_id == 7, f"named downloads-first pick {pick_named}")
    web_candidate = next(candidate for candidate in cands if candidate.file_id == 10)
    check(pick_candidate([web_candidate], Config()) is None,
          "non-Blu-ray release must not be auto-selected")
    inception_candidate = next(candidate for candidate in cands if candidate.file_id == 9)
    check(pick_candidate([inception_candidate], Config(), identity=hash_identity) is None,
          "release for another movie must not be picked")
    check(pick_candidate([inception_candidate], Config(),
                         identity=MovieIdentity("Inception", 2010, "inception")) is not None,
          "title/year-matched release is selectable")
    wrong_year = next(candidate for candidate in cands if candidate.file_id == 11)
    check(pick_candidate([wrong_year], Config(), identity=hash_identity) is None,
          "wrong release year must not be picked")
    hi_named = Candidate(
        file_id=50, release="Knowing.2009.1080p.BluRay.SDH.srt",
        moviehash_match=True, downloads=40, votes=0, rating=0.0, trusted=False,
        hearing_impaired=True, machine_translated=False, ai_translated=False,
        foreign_parts_only=False, language="en",
    )
    check(pick_candidate([hi_named], Config(), identity=hash_identity) is not None,
          "SDH/HI candidates are allowed when the release otherwise qualifies")
    check(pick_candidate([candidate for candidate in cands if candidate.foreign_parts_only], Config()) is None,
          "forced/foreign-part candidates must be excluded")
    check(pick_candidate([candidate for candidate in cands if not candidate.moviehash_match], Config()) is None,
          "no hash match → none")
    os_pick = next(candidate for candidate in cands if candidate.file_id == 7)
    subdl_pick = next(candidate for candidate in cands if candidate.file_id == 8)
    web_pick = next(candidate for candidate in cands if candidate.file_id == 10)
    # Equal sources: when both providers offer a qualifying release, the
    # most-downloaded one wins regardless of provider.
    pooled, pooled_provider, _pooled_method, pooled_reason = pick_pooled_candidates(
        [(subdl_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match"),
         (os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match")],
        hash_identity,
    )
    check(pooled is not None and pooled.file_id == 7 and pooled_provider == PROVIDER_OPENSUBTITLES,
          f"pool picks the most-downloaded release across providers ({pooled_reason})")
    # A non-qualifying (WEB) release from one provider never beats a
    # qualifying release from the other.
    pooled2, provider2, _method2, reason2 = pick_pooled_candidates(
        [(web_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match"),
         (os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match")],
        hash_identity,
    )
    check(pooled2 is not None and pooled2.file_id == 7 and provider2 == PROVIDER_OPENSUBTITLES,
          f"non-qualifying provider release loses to the qualifying one ({reason2})")
    # An unbroken cross-provider tie is held for review, not defaulted.
    twin_pick = Candidate(**os_pick.__dict__)
    tied_pool, _p, _m, tied_reason = pick_pooled_candidates(
        [(os_pick, PROVIDER_OPENSUBTITLES, "hash", "moviehash match"),
         (twin_pick, PROVIDER_SUBDL, "subdl-release", "subdl release match")],
        hash_identity,
    )
    check(tied_pool is None and "review" in tied_reason,
          f"cross-provider ties remain review-only ({tied_reason})")

    sample = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "Hello\n"
    )
    check(looks_like_srt(sample), "srt detect")
    check(not looks_like_srt("<html>nope</html>"), "html not srt")

    subdl_identity = MovieIdentity("Knowing", 2009, "knowing")
    subdl_candidate = Candidate(
        file_id="subdl:fixture", release="Knowing.2009.1080p.BluRay",
        moviehash_match=False, downloads=0, votes=0, rating=0.0, trusted=False,
        hearing_impaired=False, machine_translated=False, ai_translated=False,
        foreign_parts_only=False, language="en", feature_title="Knowing", feature_year=2009,
        subdl_match_score=0.92,
    )
    subdl_pick, _subdl_reason = pick_subdl_identity_candidate([subdl_candidate], subdl_identity)
    check(subdl_pick == subdl_candidate, "SubDL unique title/year fallback")
    subdl_release_pick, _subdl_release_reason = pick_subdl_identity_candidate(
        [subdl_candidate], subdl_identity, require_release_match_score=True,
    )
    check(subdl_release_pick == subdl_candidate, "SubDL confident release match")

    def ident_candidate(file_id, release, downloads, rating, votes, trusted):
        return Candidate(
            file_id=file_id, release=release, moviehash_match=False, downloads=downloads,
            votes=votes, rating=rating, trusted=trusted, hearing_impaired=False,
            machine_translated=False, ai_translated=False, foreign_parts_only=False,
            language="en", feature_title="Knowing", feature_year=2009,
        )

    popular_id = ident_candidate(21, "Knowing.2009.1080p.BluRay.ENG.srt", 300, 8.5, 25, False)
    elite_id = ident_candidate(22, "Knowing.2009.2160p.BluRay.ENG.srt", 100, 10.0, 50, True)
    web_id = ident_candidate(23, "Knowing.2009.720p.WEB.ENG.srt", 9999, 10.0, 100, True)
    twin_id = ident_candidate(24, "Knowing.2009.1080p.BluRay.OTHER-GROUP.srt", 300, 8.5, 25, False)
    identity_pick, identity_reason = pick_identity_candidate([elite_id, popular_id, web_id], subdl_identity)
    check(identity_pick is not None and identity_pick.file_id == 21,
          f"identity downloads-first pick {identity_pick} ({identity_reason})")
    check(pick_identity_candidate([web_id], subdl_identity)[0] is None,
          "non-Blu-ray release must not pass the identity policy")
    tied_pick, tied_reason = pick_identity_candidate([popular_id, twin_id], subdl_identity)
    check(tied_pick is None and "review" in tied_reason,
          f"tied download counts still held for review ({tied_reason})")
    # No quality floor: a popular-but-unvoted Blu-ray release is auto-selected.
    fresh_id = ident_candidate(25, "Knowing.2009.1080p.BluRay.ENG.srt", 120, 0.0, 0, False)
    fresh_pick, fresh_reason = pick_identity_candidate([fresh_id], subdl_identity)
    check(fresh_pick is not None and fresh_pick.file_id == 25,
          f"popular-but-unvoted subtitle is auto-selected ({fresh_reason})")
    wrong_year_id = ident_candidate(26, "Knowing.2010.1080p.BluRay.ENG.srt", 9999, 10.0, 100, True)
    check(pick_identity_candidate([wrong_year_id], subdl_identity)[0] is None,
          "wrong release year must not pass the identity policy")
    check(
        normalize_subdl_download_url("/subtitle/fixture/file") == "https://dl.subdl.com/subtitle/fixture/file",
        "SubDL relative download URL is constrained",
    )
    try:
        normalize_subdl_download_url("https://example.invalid/subtitle/fixture")
        errors.append("untrusted SubDL URL unexpectedly accepted")
    except ValueError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="subf_"))
    try:
        movie = tmp / "Knowing (2009)"
        extra = movie / "Featurettes"
        extra.mkdir(parents=True)
        vid = movie / "Knowing (2009).mkv"
        with vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (extra / "Making-Of.mkv").write_bytes(b"x")
        sidecar = movie / f"Knowing (2009){EXTERNAL_SRT_SUFFIX}"
        sidecar.write_text(sample, encoding="utf-8")
        (movie / f"Another Movie (2009){EXTERNAL_SRT_SUFFIX}").write_text(sample, encoding="utf-8")
        (movie / "Knowing (2009).eng.ass").write_text("[Script Info]", encoding="utf-8")
        with (movie / "Knowing (2009).mp4").open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        found = discover_videos(tmp, 300 * 1024 * 1024)
        check(found == [vid], f"discover {found}")
        check(has_english_sidecar(movie, "Knowing (2009)") == sidecar, "exact existing English SRT")
        check(not is_english_srt_sidecar(movie / f"Another Movie (2009){EXTERNAL_SRT_SUFFIX}", "Knowing (2009)"),
              "neighboring movie subtitle must not block download")
        check(not is_english_srt_sidecar(movie / "Knowing (2009).eng.ass", "Knowing (2009)"),
              "non-SRT sidecar must not count as direct-play policy output")

        guarded = movie / f"Guarded{EXTERNAL_SRT_SUFFIX}"
        atomic_write_text(guarded, sample, replace=False)
        try:
            atomic_write_text(guarded, "1\\n00:00:00,000 --> 00:00:01,000\\nreplacement\\n", replace=False)
            errors.append("create-only sidecar write unexpectedly replaced destination")
        except FileExistsError:
            pass
        check(guarded.read_text(encoding="utf-8") == sample, "create-only sidecar retains existing content")
        check(not list(movie.glob(f".Guarded{EXTERNAL_SRT_SUFFIX}.partial.*")), "create-only sidecar leaves no temp")

        # Legacy .en.srt is promoted to the canonical .eng.srt on inspect.
        legacy_movie = tmp / "Legacy Film (2010)"
        legacy_movie.mkdir()
        legacy_vid = legacy_movie / "Legacy Film (2010).mkv"
        with legacy_vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (legacy_movie / "Legacy Film (2010).en.srt").write_text(sample, encoding="utf-8")
        status, path, detail, _reason = inspect_existing_sidecars(legacy_vid)
        check(status == "covered", f"legacy .en.srt promotes to covered: {status} {detail}")
        check(path is not None and path.name.endswith(EXTERNAL_SRT_SUFFIX), f"promoted path {path}")
        check(not (legacy_movie / "Legacy Film (2010).en.srt").exists(), "legacy .en.srt removed after promote")

        sdh_movie = tmp / "Sdh Film (2011)"
        sdh_movie.mkdir()
        sdh_vid = sdh_movie / "Sdh Film (2011).mkv"
        with sdh_vid.open("wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        (sdh_movie / "Sdh Film (2011).eng.sdh.srt").write_text(sample, encoding="utf-8")
        sdh_status, sdh_path, sdh_detail, _sdh_reason = inspect_existing_sidecars(sdh_vid)
        check(sdh_status == "covered", f".eng.sdh.srt covers the movie: {sdh_status} {sdh_detail}")
        check(sdh_path is not None and sdh_path.name.endswith(".eng.sdh.srt"), f"sdh covering path {sdh_path}")

        snapshot = video_snapshot(vid)
        with vid.open("ab") as fh:
            fh.write(b"changed")
        check(not video_snapshot_matches(vid, snapshot), "video snapshot detects change")
        try:
            decode_subtitle_bytes(gzip.compress(b"x" * (MAX_SUBTITLE_BYTES + 1)))
            errors.append("oversized gzip subtitle unexpectedly accepted")
        except ValueError:
            pass

        bad_cfg = QueueConfig(
            library=movie, report_file=movie / "report.txt", log_file=tmp / "log.txt",
        )
        check(bool(validate_compact_config(bad_cfg)), "report-inside-library validation")

        # The normal workflow is limited to a log and a report. Verify that a
        # durable quota/retry checkpoint can be reconstructed from the log alone.
        ledger_log = tmp / "subtitle_fetcher.log"
        ledger_state = new_state(tmp)
        ledger_day = day_ledger(ledger_state, "2026-01-02")
        ledger_day["download_requests_reserved"] = 1
        ledger_state["movies"]["fixture"] = {
            "path": str(vid), "status": "reserved", "attempts": 1,
        }
        ledger_state["_dirty_movies"].add("fixture")
        persist_state(ledger_state, ledger_log)
        recovered_ledger = load_state(ledger_log, tmp)
        check(
            recovered_ledger["days"].get("2026-01-02", {}).get("download_requests_reserved") == 1,
            "log ledger recovers reserved download count",
        )
        check(
            recovered_ledger["movies"].get("fixture", {}).get("status") == "reserved",
            "log ledger recovers pending movie status",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- vendored scraping sources: adapter/chain self-tests --------------
    run_scrape_self_tests(errors)

    # ---- scraping fallback tier (queue wiring) ----------------------------
    check(active_scrape_sources(QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"))) == (),
          "scraping tier is off by default in bare QueueConfig")
    cfg_scrape = QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"), scrape_daily_cap=20)
    check(active_scrape_sources(cfg_scrape) == SCRAPE_PROVIDER_ORDER,
          "scraping tier enables all seven sources in failover order")
    check(active_scrape_sources(QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"),
                                            scrape_daily_cap=20, skip_sources=("subf2me",)))
          == SCRAPE_PROVIDER_ORDER[1:],
          "skip_sources removes one source")
    check(provider_daily_cap(cfg_scrape, "subf2me") == 20
          and provider_reservation_field("subf2me") == "subf2me_search_requests_reserved"
          and provider_success_field("subf2me") == "subf2me_successful_downloads"
          and provider_label("subf2me") == SCRAPE_PROVIDER_LABELS["subf2me"],
          "scraping keys map onto the generic quota helpers")
    scrape_only_cfg = QueueConfig(library=Path("/x"), log_file=None, report_file=Path("/r"),
                                  scrape_daily_cap=20)
    scrape_only_cfg.library = Path(__file__).parent  # an existing directory
    check(validate_compact_config(scrape_only_cfg) == [],
          "a scraping-only configuration (no API keys) is valid")
    dead_cfg = QueueConfig(library=Path(__file__).parent, log_file=None,
                           report_file=Path("/r"))
    check(any("scraping sources enabled" in e for e in validate_compact_config(dead_cfg)),
          "no API keys and no scraping sources is rejected")

    class _FakeScrapeT(ScrapeTransport):
        def __init__(self, routes: dict[str, bytes]) -> None:
            super().__init__(gap=0.0)
            self.routes = routes

        def _route(self, url: str) -> bytes:
            best: tuple[int, bytes] | None = None
            for prefix, payload in self.routes.items():
                if url.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), payload)
            if best is None:
                raise ScrapeSourceError(f"unrouted {url}")
            return best[1]

        def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
            return self._route(url)

        def post(self, url: str, form: dict[str, str], *, headers: dict[str, str] | None = None) -> bytes:
            try:
                return self._route(url)
            except ScrapeSourceError as exc:
                raise ScrapeSourceError(f"unrouted POST {url}") from exc

    sample_srt = "1\n00:00:01,000 --> 00:00:03,000\nhello\n\n2\n00:00:04,000 --> 00:00:06,000\nworld\n"
    import io as _io
    import zipfile as _zf
    _buf = _io.BytesIO()
    with _zf.ZipFile(_buf, "w", _zf.ZIP_DEFLATED) as _z:
        _z.writestr("The.Father.2020.utf.srt", sample_srt.encode("utf-8"))
    subf2me_zip = _buf.getvalue()
    subf2me_routes = {
        "https://subf2m.co/subtitles/searchbytitle": (
            b"<html><body><div class=\"search-result\"><h2 class=\"close\">close</h2>"
            b"<ul><li><a href=\"/subtitles/222\">The Father (2020)</a></li>"
            b"<li><a href=\"/subtitles/333\">The Father (2019)</a></li></ul></div></body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/subtitles/222/en": (
            b"<html><body><ul><li class=\"item\"><li>playWEB</li>"
            b"<a class=\"download icon-download\" href=\"/subtitles/222/en/999\"></a></li></ul>"
            b"</body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/subtitles/222/en/999": (
            b"<html><body><div class=\"download\"><a href=\"/dl/file.zip\">zip</a>"
            b"</div></body></html>"
        ).decode("utf-8").encode("utf-8"),
        "https://subf2m.co/dl/file.zip": subf2me_zip,
    }

    def run_scrape_queue(routes: dict[str, bytes], tmp: Path | None = None
                         ) -> tuple[list[JobResult], dict[str, Any], Path]:
        """Run one queue over a one-movie scraping-only library.

        Pass a previous tmp dir to run again over the same library and ledger
        (the same-UTC-day retry gate).
        """
        if tmp is None:
            tmp = Path(tempfile.mkdtemp(prefix="scrape-selftest-"))
        library = tmp / "library"
        movie = library / "The Father (2020)"
        if not movie.exists():
            movie.mkdir(parents=True)
            (movie / "The Father (2020).mkv").write_bytes(b"v" * 64)
        cfg = QueueConfig(
            library=library, log_file=tmp / "fetcher.log", report_file=tmp / "report.txt",
            scrape_daily_cap=20, min_movie_size_mb=0,
        )
        with mock.patch.object(sys.modules[__name__], "make_scrape_transport",
                               return_value=_FakeScrapeT(routes)):
            results, summary = queue_run(cfg)
        return results, summary, tmp

    results, summary, tmp = run_scrape_queue(subf2me_routes)
    sidecar = tmp / "library" / "The Father (2020)" / "The Father (2020).eng.srt"
    check(len(results) == 1 and results[0].status == "download"
          and results[0].reason == REASON_DOWNLOADED,
          "scraping tier downloads when every API source is absent")
    check(sidecar.exists() and sidecar.read_text(encoding="utf-8").startswith("1\n00:00:01"),
          "scraped SRT is written under the canonical sidecar name")
    check(summary.get("scrape_successful_downloads", {}).get("subf2me") == 1,
          "the scraping success is metered per source in the summary")
    check(summary.get("coverage_covered") == 1 and summary.get("coverage_total") == 1,
          "coverage counts the scraped movie as covered")
    check(all(summary.get("scrape_sources_enabled") and k in summary["scrape_sources_enabled"]
              for k in SCRAPE_PROVIDER_ORDER),
          "the summary names every enabled scraping source")
    report_text = build_report(results, QueueConfig(
        library=tmp / "library", log_file=tmp / "fetcher.log", report_file=tmp / "report.txt",
        scrape_daily_cap=20, min_movie_size_mb=0), summary)
    check("1/1 (100.0%)" in report_text and "Subf2m.co" in report_text,
          "the report shows 100% coverage and the scraping sources")
    shutil.rmtree(tmp, ignore_errors=True)

    results2, _summary2, tmp2 = run_scrape_queue({})
    check(len(results2) == 1 and results2[0].status == "review"
          and results2[0].reason == REASON_REVIEW,
          "a movie no scraping source can cover is held for review")
    detail2 = results2[0].detail
    for key in SCRAPE_PROVIDER_ORDER:
        check(scrape_provider_label(key) in detail2,
              f"the review detail names the verdict of {key}")
    state2 = load_state(tmp2 / "fetcher.log", tmp2 / "library")
    check(any(str(rec.get("scrape_failed_utc_day") or "") == utc_day() and rec.get("scrape_failed")
              for rec in state2["movies"].values()),
          "the scraping failure is persisted for the next-UTC-day retry")

    results3, _summary3, _tmp3 = run_scrape_queue({}, tmp=tmp2)
    check(len(results3) == 1 and results3[0].status == "skip"
          and results3[0].reason == REASON_QUOTA
          and "already exhausted" in results3[0].detail,
          "a movie exhausted today is not offered to the scraping tier twice")
    shutil.rmtree(tmp2, ignore_errors=True)

    run_extract_self_tests(errors)

    if errors:
        print("SELF-TEST FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SELF-TEST PASSED (hash + OpenSubtitles/SubDL picks + SRT safety + discovery + "
          "transaction guards + scraping fallback tier + embedded extraction)")
    return 0

# Rebind every moved function to the tool's namespace, then publish it back on
# the module so the bodies can call each other exactly as they used to.
run_scrape_self_tests = bind_to_tool(tool, run_scrape_self_tests)
tool.run_scrape_self_tests = run_scrape_self_tests
run_extract_self_tests = bind_to_tool(tool, run_extract_self_tests)
tool.run_extract_self_tests = run_extract_self_tests
run_self_tests = bind_to_tool(tool, run_self_tests)
tool.run_self_tests = run_self_tests
