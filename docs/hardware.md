# The playback chain this toolkit is engineered for

Every codec decision in this repository is tuned to **one concrete living
room**:

```
   ┌──────────────────────────────┐   ┌───────────────────────┐   ┌──────────────────────────┐
   │ Chromecast with Google TV    │   │ Hisense AX3125H       │   │ Samsung UN60F6350AF      │
   │ (HD)  — model G454V "boreal" │──▶│ 3.1.2ch soundbar+sub  │──▶│ 60" 1080p SDR LED TV     │
   │ Android TV 12 · S805X2       │   │ 440 W · DTS + Atmos   │   │ (2013 era, no HDR)       │
   └──────────────────────────────┘   └───────────────────────┘   └──────────────────────────┘
              the PLAYER                      the SOUND sink               the DISPLAY
   The chain as actually cabled (`soundbar-hdmi-in`, the toolkit's DEFAULT): the
   Chromecast feeds the soundbar's HDMI IN, the bar decodes the audio and its
   HDMI OUT passes the picture through to the TV. Audio never crosses the 2013 TV.
   (The alternative — Chromecast into the TV, TV --ARC/optical--> bar — is
   supported explicitly (`--wiring tv-arc`) and documented in §4.)
```

The goal of the whole toolkit is that **every movie in the library Direct
Plays on this chain, every time** — the Jellyfin/Plex server does zero
transcoding, zero burn-in, zero remuxing. This document is the evidence that
led to every rule, link by link, with sources. The machine-readable form of
these facts is `organizekit/core/playbackchain.py`; if that table and this
document ever disagree, one of them is a bug.

---

## 1 · The player — Chromecast with Google TV (HD), model G454V

Facts (Google's own materials and device measurements; sources at the end):

| Property | Value |
| :--- | :--- |
| Model | G454V, codename "boreal" (the **HD** model, 2022; **not** the 4K model) |
| SoC | Amlogic **S805X2**, quad Cortex-A35 up to 1.8 GHz, Mali-G31 MP2, 1.5 GB RAM |
| Video ceiling | **1920×1080 @ 60 Hz max**. No 4K output at all. |
| Video decoders | H.264 (AVC), H.265 (HEVC), VP9, **AV1**, MPEG-2 — up to 1080p60 |
| HDR | HDR10, HDR10+, HLG — **Dolby Vision is NOT supported on this model** (the 4K model has it; the HD does not) |
| Audio passthrough | Dolby Digital (**AC-3**) and Dolby Digital Plus (**E-AC-3**, incl. JOC/Atmos) — the licensed, documented set |
| Audio decode | AAC/AAC-LC/HE-AAC, MP3, FLAC, Opus, Vorbis, PCM — decoded in software to PCM |
| Audio never emitted | **TrueHD (incl. TrueHD Atmos), DTS-HD MA/HR, DTS:X, WMA Pro** — Android TV/ExoPlayer cannot pass these through and refuses to decode them |

Two subtleties the research surfaced and the code encodes:

1. **Base Dolby Digital 5.1 DTS core is the unofficial extra — and the
   default wiring declines it.** Google's published passthrough list for
   this device is DD/DD+ only, but the Amlogic Android TV firmware passes
   plain 5.1 DTS core through the HDMI layer in practice (widely reported;
   see sources). That is real, but it is firmware behaviour rather than a
   contract, so on the default `soundbar-hdmi-in` wiring the toolkit
   **converts base DTS to Dolby Digital Plus** rather than depend on it.
   Three reasons, all pointing the same way on this chain:
   * it is the only unlicensed link in a path that is otherwise documented
     at every hop, and if a system update ever ends it those movies start
     transcoding server-side on every play, silently;
   * the AX3125H is **3.1.2 and has no surround speakers** — L/C/R, two
     up-firing height drivers and the sub — so a 5.1 mix is folded into that
     array whatever carries it, and the audible gap between DTS core and a
     640 kbps DD+ bed after that fold is not worth the risk;
   * DTS core runs ~1.5 Mbps against DD+ at 640 kbps, roughly **400 MB on a
     two-hour movie**, for audio the bar downmixes anyway.

   `--dts-passthrough` opts back into keeping it for anyone who trusts the
   firmware; `--no-dts-passthrough` states the default explicitly. The
   `tv-arc` alternative keeps the historical accept-DTS policy — that path
   is the documented-degraded one this install does not run (§3–§4), and
   re-deciding its DTS policy is a separate question. The table is
   `playbackchain.DTS_PASSTHROUGH_DEFAULT`.
2. **HDR10/HDR10+/HLG "play" here means tone-mapped to SDR.** The panel is a
   1080p SDR display, so the Chromecast outputs SDR after tone-mapping.
   That is a picture-preserving operation done at playback time, with zero
   generation loss — unlike a HandBrake re-encode, which is why the
   bit-depth inspector's "protect HDR, never re-encode" stand is unchanged.

Practical consequences (all encoded in the code):

* `> 1080p` video ⇒ the server transcodes on every play. A 2160p rip in this
  library is a replacement candidate, not a "maybe someday" file.
* **"1080p" is measured against the CODED ceiling, not 1080 exactly.** 1080 is
  not a multiple of 16, so an H.264 1080p stream is routinely *stored* as
  **1920×1088** with 8 lines of `frame_crop_bottom_offset` padding (MediaInfo
  shows `Stored_Height 1088` / `Sampled_Height 1080`). Depending on container
  and muxer, `ffprobe` can report either number for the same picture — so
  `classify_video()` compares against `Player.max_coded_resolution` =
  **(1920, 1088)** = the 1080p ceiling rounded up to the macroblock grid.
  1920×1088 Direct Plays (it is inside the S805X2's decode block *and* the
  TV's panel, which scales it); 1920×1090, 2048×858 and 3840×2160 are still
  oversize. Without the tolerance, ordinary 1080p movies were being flagged
  "queue a 1080p downscale" — a pointless re-encode of a file that already
  plays.
* A picture whose dimensions or codec cannot be read is **never** called
  playable: `classify_video()` fails closed to `unknown` ("review manually"),
  matching the audio side's `AUDIO_UNKNOWN`.
* **Dolby Vision video** ⇒ same: the player has no DV license; the server
  re-encodes. DV files are flagged, not kept silently.
* TrueHD/DTS-HD/DTS:X audio ⇒ the server re-encodes the audio on every
  play. This is the single most common Direct-Play breaker in real remux
  libraries, and it is what `audio_standardizer.py` was built to fix once,
  offline, losslessly-for-video.
* AAC/FLAC/PCM audio ⇒ decodes locally to PCM; on the default wiring
  (soundbar HDMI IN) multichannel PCM arrives intact, while over the explicit
  ARC alternative this TV delivers it stereo-only (§3).

## 2 · The sound — Hisense AX3125H (3.1.2ch soundbar + wireless sub, 440 W)

From Hisense's product page and spec sheet (sources at the end):

| Property | Value |
| :--- | :--- |
| Configuration | 3.1.2 channels, 440 W, wireless 6.5" subwoofer, up-firing height drivers |
| HDMI | **1× HDMI IN (4K/HDMI 3D passthrough) + 1× HDMI OUT (eARC/ARC + CEC)** |
| Audio decoding | **Dolby Digital, Dolby Digital Plus / Atmos (DD+ JOC), TrueHD, DTS, DTS-HD**, Multichannel PCM |
| Other inputs | optical (TOSLINK), Bluetooth 5.3, USB, 3.5 mm AUX |

Why this is the load-bearing device: it is the only link in the chain
licensed to decode **every** format that matters. Dolby Atmos arrives on
this chain as DD+ JOC — E-AC-3 with embedded Atmos metadata — which is
exactly what streaming services send for Atmos, and exactly what the G454V
can pass through. The bar *can* decode TrueHD and DTS-HD MA, but it never
gets the chance from this player: the Chromecast cannot emit them (see
above), and the toolkit therefore *normalizes* lossless-HD into the
native-to-every-hop formats instead of fantasizing a different player into
the chain. **On the default `soundbar-hdmi-in` wiring, Dolby Digital (AC-3)
and Dolby Digital Plus (E-AC-3) bitstream through the entire chain with zero
conversions anywhere** — player to bar, bar decodes, nothing in between. Under
the explicit `tv-arc` alternative that claim does *not* hold: the bitstream
goes to the 2013 TV first, and this TV offers PCM only for HDMI sources (§3),
so the ARC path may deliver it as stereo. See §5.

### The per-port input matrix — the evidence the default wiring rests on

Hisense's manual §1.3 ("Supported Input Audio Formats") lists support **per
port**, not per device, and the two ports this chain could use disagree. This
is the single most load-bearing table in the document, verified against the
manufacturer PDF (see Sources):

| Format | OPTICAL | HDMI ARC | HDMI eARC | **HDMI IN** ← default wiring |
| :--- | :---: | :---: | :---: | :---: |
| LPCM 2ch | ● | ● | ● | **●** |
| **LPCM 5.1ch** | — | **—** | ● | **●** |
| **LPCM 7.1ch** | — | **—** | ● | **●** |
| Dolby Digital | ● | ● | ● | **●** |
| Dolby Digital Plus | — | ● | ● | **●** |
| **Dolby Atmos – Dolby Digital Plus** | — | ● | ● | **●** |
| Dolby TrueHD | — | — | ● | **●** |
| Dolby Atmos – Dolby TrueHD | — | — | ● | **●** |
| DTS / DTS-ES / DTS 96/24 | ● | ● | ● | **●** |
| DTS-HD HR / DTS-HD MA / DTS-HD LBR / DTS:X | — | — | ● | **●** |

Read the two bold rows as the whole wiring decision. **Multichannel LPCM is
supported on HDMI IN and is *not* supported on HDMI ARC or optical.** That is
precisely why:

* on the default `soundbar-hdmi-in` wiring a 5.1/7.1 AAC, FLAC, Opus or PCM
  track is genuinely "playable as-is" — the Chromecast decodes it and the bar
  takes multichannel PCM on its HDMI IN, with no server work at all; and
* on the `tv-arc` alternative the same track cannot arrive as surround even in
  principle, independently of what the 2013 Samsung's menu offers (§3). Two
  separate limits point the same way.

Note also that "Dolby Atmos – Dolby Digital Plus" is supported on HDMI IN, so
DD+ Atmos (the only Atmos variant the G454V can emit) reaches the bar's
up-firing drivers intact on the default wiring. The bar's TrueHD / DTS-HD /
DTS:X rows are real but unreachable from *this* player — the Chromecast can
never emit those formats, which is why `audio_standardizer.py` normalizes them
into DD+ rather than pretending a different player is in the chain.

`Sink.hdmi_in_accepts` and `Sink.arc_cannot_carry` in
`organizekit/core/playbackchain.py` are this table as data, with a test
asserting the invariant that matters: **every format ARC refuses, HDMI IN
accepts** — so re-cabling through the bar really does recover the whole column.

## 3 · The display — Samsung UN60F6350AF (60" 1080p, ~2013)

From Samsung's spec page and manual (sources at the end):

| Property | Value |
| :--- | :--- |
| Panel | 1920×1080, SDR (no HDR of any kind pre-2014-ish on F-series) |
| Inputs | 4× HDMI, one designated **HDMI (ARC)** port |
| Audio out | optical (TOSLINK), plus ARC on the designated HDMI port |

Three things decide the whole wiring story:

1. **Plain ARC (2013) is not eARC.** Samsung's own ARC documentation lists
   the formats the return channel can carry as PCM (2 channel), Dolby
   Digital (up to 5.1) and DTS Digital Surround (up to 5.1) — and crucially
   not multichannel PCM. So ARC/optical can pass a *bitstream* it is given,
   but it cannot carry 5.1/7.1 PCM; the bar's multichannel-PCM capability is
   unreachable through it.
2. **What the TV offers depends on the input source — and on this unit it
   is PCM only.** The F-series e-manual says of Digital Audio Output
   (SPDIF) that "the available Digital Audio Output (SPDIF) formats may
   vary depending on the input source". With HDMI sources connected (the
   Chromecast is the source here), **this UN60F6350AF offers PCM only** —
   Dolby Digital/DTS are not selectable (**confirmed on this unit,
   2026-09**; the greyed-out-for-HDMI-sources behaviour on 2013-era
   Samsungs is widely reported). The TV therefore downmixes every HDMI
   source to stereo PCM before ARC/optical, whatever the source sent. This
   is an *input-side* limit, not a cable or soundbar problem: the same TV
   will still bitstream Dolby Digital from its own tuner and apps, which is
   why the menu is content/input dependent in the first place.
3. **Consequence for the wiring.** On ARC, 5.1+ content sourced from the
   Chromecast reaches the AX3125H as **stereo PCM** — the one material
   limit the toolkit used to *compensate* for by baking AC-3 5.1 into
   multichannel-PCM movies (§5). Cabling the Chromecast through the
   soundbar's HDMI IN instead removes the limit at the hardware level, and
   that is the wiring this chain now uses and the toolkit assumes by
   default. `tv-arc` remains supported (`--wiring tv-arc` /
   `ORGANIZE_PLAYBACK_WIRING=tv-arc`) for anyone who re-cables the old
   way, and re-enables the AC-3 compensation for multichannel
   PCM-decoded sources.

**The chain is wired through the soundbar's HDMI IN** (`soundbar-hdmi-in`):
Chromecast → AX3125H HDMI IN, AX3125H HDMI OUT → TV. Everything in §5
onward assumes that wiring; §4 shows the ARC alternative and exactly what
flips if it is selected.

## 4 · The wiring decision

| Wiring | Video limit | Audio reality on 5.1+ content |
| :--- | :--- | :--- |
| **Chromecast → AX3125H HDMI IN → TV (`soundbar-hdmi-in`, DEFAULT — how this chain is cabled)** | 1080p60 chain-wide | the bar decodes DD/DD+/Atmos/DTS/multichannel PCM itself; player passthrough or decode; **no server work** |
| Chromecast → TV HDMI, TV --**ARC**→ AX3125H (`tv-arc`, explicit alternative) | 1080p60 | the TV offers PCM only for HDMI sources on this unit, so everything arrives as **stereo PCM** at the bar; multichannel PCM-decoded movies become AC-3 transcode candidates, and the toolkit bakes AC-3 for exactly those |
| Chromecast → TV HDMI, TV --**optical**→ AX3125H | 1080p60 | same limits as ARC (no CEC, slightly worse UX) |

The toolkit's default is **`soundbar-hdmi-in`**: Chromecast into the
soundbar's HDMI IN, audio decoded by the bar, video passed through to the TV.
`tv-arc` is the supported *alternative* (`--wiring tv-arc` or
`ORGANIZE_PLAYBACK_WIRING=tv-arc`) and changes exactly one rule: a
multichannel AAC/FLAC/PCM movie that plays natively over HDMI IN becomes an
AC-3 transcode candidate on the TV's ARC/optical path, because that path
delivers it as stereo PCM (§3).

Nothing about the *video* side changes between the two: both pass the picture
through at up to 1080p60, and the G454V's decode ceiling, HDR handling and
Dolby Vision gap are identical.

## 5 · The audio codec matrix — what actually plays where

Legend: ✅ native (no server work) · ⚠️ accepted, with a caveat · ❌ never
leaves the player — the server transcodes audio on **every** play.

| Source track player | G454V player | over HDMI IN | over ARC/opt | verdict in the toolkit |
| :--- | :--- | :--- | :--- | :--- |
| Dolby Digital (AC-3) 5.1 | passthrough ✅ | ✅ decodes | ⚠️ this TV offers PCM only for HDMI sources, so ARC/opt may deliver it as stereo (§3) | kept as-is; the synthesis target only under `tv-arc` |
| Dolby Digital Plus (E-AC-3, incl. Atmos JOC) | passthrough ✅ | ✅ decodes | ⚠️ same PCM-only limit — DD+ Atmos does not survive this TV's ARC path (§3) | **goal format** — best possible track on this chain; what audio_standardizer synthesizes on the default wiring |
| AAC 5.1 / stereo | decode → PCM ✅ | ✅ (multich. PCM) | ⚠️ stereo only on this TV | stereo = fine; 5.1+ native on the **default** HDMI-IN wiring, **AC-3 candidate only under `tv-arc`** |
| FLAC / PCM / ALAC 7.1 | decode → PCM ✅ | ✅ multich. | ⚠️ stereo only on this TV | native multichannel on the **default** HDMI-IN wiring; **AC-3 candidate (5.1+) only under `tv-arc`** |
| MP3 / Opus / Vorbis | decode → PCM ✅ | ✅ | ✅ stereo | fine |
| base 5.1 **DTS core** | ⚠️ passthrough (unofficial, works on this AMLogic build) | ✅ decodes | ⚠️ same PCM-only limit (§3) | **transcoded to DD+ by default** on `soundbar-hdmi-in` (unofficial, and ~1.5 Mbps for audio a 3.1.2 bar downmixes — §1); `--dts-passthrough` keeps it; still accepted by default under `tv-arc` |
| **TrueHD / TrueHD Atmos** | ❌ **cannot be emitted at all** | (bar could decode — player can't send) | ❌ | **Dolby Digital Plus (E-AC-3) @ 640k synthesized** from it by audio_standardizer (AC-3 under `tv-arc`) |
| **DTS-HD MA / HRA, DTS:X** | ❌ **cannot be emitted at all** | (same) | ❌ | **Dolby Digital Plus (E-AC-3) @ 640k synthesized** (AC-3 under `tv-arc`) |
| WMA Pro / WMA Lossless | ❌ **cannot be emitted at all** | (same) | ❌ | **Dolby Digital Plus (E-AC-3) @ 640k synthesized** — no ExoPlayer decoder, so `ffmpeg` converts it like any other master |
| unknown / unclassifiable | ❌ | ❌ | ❌ | **fail-closed: reported for a human, never auto-touched** — and it achieves *zero* channels in the keeper ranking, so it can never outrank a track the toolkit does understand |

**Read the "over ARC/opt" column as one limit, not nine.** Every ⚠️ in it has
the same single cause: on this UN60F6350AF the digital audio output offers PCM
only for HDMI sources (§3, user-confirmed 2026-09), and §3's wording is
deliberate — the TV downmixes *every* HDMI source before ARC/optical,
**whatever the source sent**. A Dolby bitstream is not obviously exempt from
that, which is why AC-3 and DD+ Atmos are ⚠️ here rather than ✅. (ARC as a
*standard* carries PCM 2.0 / Dolby Digital 5.1 / DTS 5.1 — see the Samsung
article in Sources; what this TV's menu will offer for an HDMI input is the
narrower thing.)

One open question this matrix does not settle, stated plainly rather than
papered over: §4 has the toolkit bake AC-3 into multichannel PCM-decoded
movies under `tv-arc`, and that compensation only pays off if the TV *will*
forward a Dolby bitstream it received on HDMI despite its menu offering PCM
only. Whether the PCM-only limit applies to a received bitstream, or only to
audio the TV decoded itself, has not been measured on this unit — so the ⚠️
above is deliberately "cannot be relied on" and not "never works". Until it is
measured, do not depend on ARC for surround here: the default wiring is the
fix, because it removes the whole column from the equation by never asking the
TV to carry sound.

The keep-one-track ranking (`mkv_track_cleaner.py`) encodes exactly that
matrix — but it asks **which track can this movie END UP with?**, not "which
track plays right now?", and that question decides the order. The remux is
**irreversible**: a dropped track is gone for good, while a track that merely
needs converting can still be converted on any later run. Deleting a TrueHD
Atmos 7.1 master to keep a stereo AC-3 that plays today cannot be undone;
converting that master into a DD+ 5.1 bed can always be done. So:

1. **achievable layout** — how many channels the track reaches *on this chain*,
   via `playbackchain.achievable_channels()`. A chain-native track achieves the
   channels it carries; a transcode-bound master achieves the channels
   `audio_standardizer.py` would bake into its replacement, which is **at most
   5.1 on either wiring** — the ceiling is ffmpeg's Dolby encoders, not the
   format (`playbackchain.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS`); a wider master
   folds, it is never promised a bitstream that would fail to encode. An
   *unknown* track achieves nothing, because the toolkit fail-closes and never
   auto-touches one. This is the key that makes **TrueHD Atmos 7.1 worth more
   than an AC-3 2.0 that plays today** (6 beats 2). Tracks needing no encoder
   are untouched by the cap — FLAC 7.1 really arrives as LPCM 7.1 on the bar's
   HDMI IN, which is what keeps **FLAC 7.1 ahead of everything Dolby-bound**.
2. **band** — *plays with no server work* (AC-3 / DD+ / base DTS / anything
   decoded to PCM) beats *transcode-bound* (TrueHD / DTS-HD / DTS:X / WMA Pro)
   beats *unknown*. At an **equal achievable layout** this is the
   Direct-Play-first rule and it is unchanged: a 5.1 AC-3 still beats a 5.1
   TrueHD, because both land at 5.1 and only one of them gets there without the
   server re-encoding anything — and since 8.4.0 a 5.1 AC-3 also beats a 7.1
   TrueHD, which folds to the same bed (8.2.0 ranked the master above both on
   the false premise that its replacement would be 7.1). "Highest sample rate
   wins" is still the wrong metric; "reaches the widest layout, with the least
   server work" is the right one.
3. **Atmos** — a real DD+ Atmos track wins among equal layouts, because a 3.1.2
   bar with up-firing drivers is what it exists for. This needs no special case
   against 7.1 masters any more: both reach the same 5.1 bed, so the band above
   already keeps the Atmos track — correct, since no open encoder can synthesize
   Atmos at all (the JOC object metadata is gated behind a proprietary Dolby
   signature), so a real DD+ Atmos stream is irreplaceable, while 7.1's two
   extra channels drive nothing on a bar with no rear speakers.
4. **codec sub-tier** — the historical quality order refines the rest:
   DD+ (100) > DD (95) > base DTS core (80) > lossless-decodable (66) >
   Opus (62) > other lossy (60), and below the band DTS-HD MA / DTS:X (34) >
   DTS-HD HRA (32) > the rest (30). That lower half is also what picks the best
   *transcode source*: `audio_standardizer.py` ranks its own pool with this same
   function, so the two tools cannot disagree about which master to burn — and
   because the encoder cap makes every surround master land on the same 5.1
   bed, the source is the **highest-tier** master again rather than the widest:
   a DTS-HD MA 5.1 outranks a TrueHD 7.1 as *input* (it did not before 8.4.0,
   when the target table wrongly preserved 7.1).

The per-port table in §2 is what makes multichannel decoded audio first-class on
this wiring — LPCM 5.1/7.1 are supported on the bar's HDMI IN — which is why a
convertible master is worth keeping rather than merely tolerating.

**Keeping a master is only correct if it gets converted.** Because key 1 can
retain a track the G454V cannot emit yet, the cleaner names every such movie in
its report (`Kept audio needing audiofit`) and warns on the console and in the
log. In the pipeline `audio_standardizer.py` runs *before* the cleaner, so the
master has usually already become the DD+ track that wins on key 1 outright; a
movie in that bucket means audiofit did not run or could not — `ffmpeg` missing,
or the cleaner run standalone.

### Why Dolby Digital Plus (E-AC-3) @ 640 kbps — and AC-3 only under `tv-arc`?

* This chain is cabled `soundbar-hdmi-in`, and on that wiring audio crosses
  exactly two hops: the G454V's HDMI out and the AX3125H's HDMI IN. Dolby
  Digital Plus is **official** on both — it is on Google's published
  passthrough list for this player (Dolby Digital, Dolby Digital Plus,
  Dolby Atmos via HDMI pass-through) and on Hisense's decoder list for the
  bar. Nothing else about the target is hedged.
* E-AC-3 is strictly the better codec of the two Dolby bitstreams: a more
  efficient encode than AC-3 at any given bitrate. Width is not one of its
  advantages *on the synthesis side*: the format can carry 7.1, but no Dolby
  encoder in ffmpeg can write past 5.1 — `eac3` and `ac3` support layouts up
  to 5.1 only (they write independent frames and never the dependent
  substreams 7.1 needs), and they fail rather than downmix, so the toolkit's
  own target table caps at 5.1 on both wirings
  (`playbackchain.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS`). An existing DD+ 7.1
  stream in a file is a different matter — nothing converts it, it just
  bitstreams. 5.1/6.1/7.1+ sources all normalize to a 5.1 bed at 640 kbps;
  stereo stays stereo (192 kbps), mono mono — nothing is ever upmixed.
* 640 kbps is the bitrate every sink in this chain handles with headroom
  and the ceiling these mixes need; the transcode source is usually a
  lossless master with plenty of headroom, so the ceiling is the honest
  choice.
* **Under the explicit `tv-arc` alternative** the synthesized target stays
  AC-3 (Dolby Digital), where 5.1 has been the encoder's limit all along;
  that path routes sound through the 2013 TV, and AC-3 is the one format
  licensed-and-supported at every hop of it (ARC and optical included).
  Re-muxing an existing E-AC-3 (or AC-3) track is of course *kept* as-is on
  either wiring — it is native too, and Atmos carries through.

## 6 · The settings checklist (the human half of the chain)

* **Chromecast (Google TV):** Settings → Display & Sound →
  *Surround sound*: **Auto** (passthrough DD/DD+); *Audio output format*:
  Standard; turn *off* "match content frame rate" only if you see judder
  complaints — irrelevant to audio.
* **AX3125H (default HDMI-IN wiring):** the Chromecast sits on the bar's
  **HDMI IN** socket, and the bar's **HDMI OUT (TV eARC/ARC)** goes to a TV
  HDMI input; source = **HDMI In**; EQ mode Movie; night mode off;
  subwoofer paired (auto). *(If you ever re-cable to the TV's ARC port
  instead: TV's designated HDMI (ARC) lead into the bar's HDMI OUT, source
  = **ARC**, and expect stereo PCM from HDMI sources — see §3.)*
* **Samsung UN60F6350AF:** with the default wiring it only ever receives
  video, so its audio menu is irrelevant to the chain — leave Anynet+
  (HDMI-CEC) **on** so the TV remote and CEC wake behaviour still work, and
  TV speaker **off**. (Under the `tv-arc` alternative, *Settings → Sound →
  Digital Audio Out* must be revisited: on this unit only PCM is offered
  for HDMI sources, §3, so full surround there is not reachable.)

---

## Sources

Player:

* Google, *Chromecast with Google TV (HD) — tech specs* (store specs
  page/manual): HDR10/HDR10+/HLG, 1080p60, no Dolby Vision.
  <https://store.google.com/product/chromecast_google_tv_specs>
* Google support, *Chromecast & Google TV Streamer specifications* — the HD
  model: "Up to 1080p HDR, 60 fps", video formats HDR10/HDR10+/HLG, audio
  formats Dolby Digital / Dolby Digital Plus / **Dolby Atmos via HDMI
  pass-through** (and no Dolby Vision entry, unlike the 4K model and the
  Google TV Streamer).
  <https://support.google.com/chromecast/answer/3046409>
* Google, *Chromecast with Google TV (HD) — "G454V" regulatory and user
  manual* (documents the model id "G454V").
  <https://support.google.com/chromecast/answer/11236184>
* AFTVnews, *Google releases Chromecast HD as its 1080p streaming device for
  $29.99* — launch coverage confirming 1080p60, HDR10/HDR10+/HLG, 1.5 GB RAM,
  8 GB storage on the HD model.
  <https://www.aftvnews.com/google-releases-chromecast-hd-as-its-1080p-streaming-device-for-29-99/>
* GSMArena device sheet (S805X2 / Mali-G31 / 1.5 GB / AV1+VP9+HEVC decode).
  <https://www.gsmarena.com/google_chromecast_with_google_tv_%28hd%29-11907.php>
* Rtings senior review of the HD model: measured HDR behavior, no Dolby
  Vision, audio passthrough limits.
  <https://www.rtings.com/streaming/reviews/google/chromecast-with-google-tv-hd>
* Android Developers, *Supported media formats* (the passthrough/decode
  matrix Android TV devices share; TrueHD/DTS-HD absent).
  <https://developer.android.com/guide/topics/media/media-formats>

Soundbar:

* Hisense, *AX3125H 3.1.2ch Soundbar with Wireless Subwoofer* — product page
  (440 W, 3.1.2, HDMI IN/OUT-ARC, optical/BT/USB, Dolby Atmos + DTS
  decoding).
  <https://www.hisense-usa.com/product/ax3125h>
* Hisense AX3125H spec sheet (PDF), decoder/ports tables — HDMI Input ×1,
  HDMI eARC/CEC ×1, decoders: Dolby Atmos / Dolby TrueHD / Dolby Digital
  Plus / Dolby Digital / DTS:X / DTS-HD Master / DTS / PCM / **Multich PCM**.
  <https://files.hisense-usa.com/download/f25648883914883a> ·
  <https://files.hisense-usa.com/storage/hisense/asset/images/66406cbb29a362.pdf>
* **Hisense 3.1.2ch soundbar manual, §1.3 "Supported Input Audio Formats" —
  the PER-PORT matrix quoted in §2 above** (LPCM 5.1ch/7.1ch: HDMI eARC ● and
  HDMI IN ●, but HDMI ARC — and OPTICAL —; "Dolby Atmos – Dolby Digital Plus":
  ARC/eARC/HDMI IN all ●; TrueHD, DTS-HD HR/MA/LBR and DTS:X: eARC and HDMI IN
  only). This is the evidence the `soundbar-hdmi-in` default rests on.
  <https://files.hisense-usa.com/download/f25642eeb4a386bb>
* Hisense AX3125H user manual — "HDMI IN Socket: For connecting HDMI source
  devices, such as a DVD player, Blu-ray Disc™ player, or gaming console";
  "HDMI OUT (TV eARC/ARC) Socket: The port for connecting a TV"; input
  format table (PCM → PCM, Dolby Digital/DD+/TrueHD → Dolby, Dolby MAT →
  MPCM).
  <https://manuals.plus/hisense/ax3125h-3-1-2ch-440w-dolby-atmos-soundbar-with-wireless-subwoofer-manual>

Display:

* Samsung, *UN60F6350AF product/spec support pages* (1080p, 4× HDMI,
  optical out, ARC).
  <https://www.samsung.com/us/support/answer/ANS00077530/>
* Samsung UN60F6350AF e-manual (mirror), §Sound → Digital Audio Output:
  "Audio Format: Selects the Digital Audio output (SPDIF) format. **The
  available Digital Audio output (SPDIF) formats may vary depending on the
  input source**", and §Connections: "ARC is only available through the
  HDMI (ARC) port and only when the TV is connected to an ARC-enabled AV
  receiver."
  <https://manualowl.com/m/Samsung/UN60F6350AF/Manual/347300> ·
  <https://www.manualshelf.com/manual/samsung/un60f6350afxza/user-manual-ver10.html>
* Samsung support, *How to use HDMI ARC on Samsung Smart TV* — the formats
  ARC carries: PCM (2 channel), Dolby Digital (up to 5.1), DTS Digital
  Surround (up to 5.1); setting path for 2013-2014 F/H series.
  <https://www.samsung.com/sg/support/tv-audio-video/how-to-use-the-hdmi-arc-port-on-a-samsung-tv/> ·
  <https://www.samsung.com/latin_en/support/tv-audio-video/how-to-use-hdmi-arc-on-samsung-smart-tv/>
* Samsung support, *Change the audio format on your Samsung TV* — input vs
  output format settings; PCM output is 2.0 only; formats above Dolby
  Digital need HDMI ARC.
  <https://www.samsung.com/us/support/answer/ANS00085244/>
* Community corroboration of the 2013-era greyed-out Dolby/DTS behaviour for
  HDMI sources (the menu offers PCM, or PCM/**DTS Neo 2:5**, until a Dolby
  bitstream is actually detected on the input):
  <https://www.avsforum.com/threads/getting-a-samsung-tv-to-output-dolby-digital-5-1-through-optical-out.1509865/> ·
  <https://www.reddit.com/r/hometheater/comments/pkz268/>
* **USER-CONFIRMED (2026-09) on the actual UN60F6350AF in this chain:** with
  HDMI sources connected, the TV's digital audio output offers **PCM only**
  (Dolby Digital/DTS are not selectable). This is the direct observation the
  default wiring change rests on; it is bounded to this unit, and the
  sources above explain why it is expected behavior on this generation
  rather than a fault. No claim is made here about other units or other
  input types.

Playback-chain mechanics:

* Jellyfin documentation, *Codec support / Direct Play vs transcode* (the
  "server re-encodes per play" model this toolkit eliminates).
  <https://jellyfin.org/docs/general/clients/codec-support>
* DTS/PCM over HDMI vs ARC/optical return-channel limits (HDMI Licensing /
  CTA-861 summaries; see also the Samsung ARC article above).

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[The pipeline](pipeline.md) · [Configuration](configuration.md)
