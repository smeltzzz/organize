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

Facts (Google's own materials and published specs; sources at the end, and §7
marks what is still unverified on the actual unit):

| Property | Value |
| :--- | :--- |
| Model | G454V, codename "boreal" (the **HD** model, 2022; **not** the 4K model) |
| SoC | Amlogic **S805X2**, quad Cortex-A35 up to 1.8 GHz, Mali-G31 MP2, 1.5 GB RAM |
| Video ceiling | **1920×1080 @ 60 Hz max**. No 4K output at all. |
| Video decoders | H.264 (AVC), H.265 (HEVC), VP9, **AV1**, MPEG-2 — up to 1080p60, **4:2:0 only, and profile-limited**: 8-bit H.264 *High*, HEVC *Main/Main10* (Google's documented list for this device family). **No H.264 High 10 ("Hi10P"), no 4:2:2/4:4:4** — no ARM hardware decoder exists for them |
| HDR | HDR10, HDR10+, HLG — **Dolby Vision is NOT supported on this model** (the 4K model has it; the HD does not) |
| Audio passthrough | Dolby Digital (**AC-3**) and Dolby Digital Plus (**E-AC-3**, incl. JOC/Atmos) — the licensed, documented set |
| Audio decode | AAC/AAC-LC/HE-AAC, MP3, FLAC, Opus, Vorbis, PCM — decoded in software to PCM |
| Audio never emitted | **TrueHD (incl. TrueHD Atmos), DTS-HD MA/HR, DTS:X, WMA Pro** — the device cannot pass these through, and Jellyfin's Android-TV client does not offer them for Direct Play by default (Plex forum reports show its server transcoding them; §6 covers the one Jellyfin setting that changes that, untested here) |

Two subtleties the research surfaced and the code encodes:

1. **Base 5.1 DTS core is the one grey zone — and it is UNVERIFIED on this
   unit.** Google's passthrough list for this device family is AC-3, E-AC-3,
   MPEG-H and Dolby Atmos (Google Cast docs), and Google's own support staff
   answered a DTS question with "Chromecast with Google TV devices only
   supports … Dolby Digital, Dolby Digital Plus, Dolby Atmos (pass-through)
   … If DTS worked at one point … it technically wasn't supported". Field
   reports then split: some say DTS 2.0/5.1 core passed through after the
   Android 12 update (and Kodi can do it); others get stereo PCM or
   silence (a Plex forum report from July 2025 has DTS-HD MA direct-playing
   as stereo PCM on a Chromecast with Google TV; a 2023 Hacker News poster
   found Jellyfin "incapable of DTS 5.1 passthrough … at least on GCCWGTV");
   and some say it worked before a firmware update and stopped after. The
   audit found no report specific to the **HD** model. (An earlier revision of
   this dossier called it "unofficial but real", citing a Reddit thread that
   could not be verified; that claim is withdrawn.) The toolkit still
   *accepts* base DTS by default, because converting it rewrites every DTS
   movie in the library and cannot be undone (the cleaner then drops the DTS
   track) — but it is a decision for you to make from one measurement, not an
   assumption: play a DTS 5.1 file and read
   the soundbar's display (**DTS** = passed through, **PCM** = not). If it
   says PCM, or Jellyfin's dashboard shows an audio transcode, set
   `ORGANIZE_DTS_PASSTHROUGH=0` (or run `organize audio --no-dts-passthrough`)
   and base DTS is baked into AC-3 5.1 like DTS-HD. `organize doctor` prints
   the current setting.
2. **HDR10/HDR10+/HLG "play" here means tone-mapped to SDR — on the player,
   per user reports, not per a datasheet.** The panel is a 1080p SDR
   display. Jellyfin's Android-TV client decides HDR Direct Play from the
   *decoder's* HDR support, never from the display (`deviceProfile.kt`), so
   an HDR10 file Direct Plays and the Chromecast must do the conversion.
   Users report it does on **Google TV 12 — the G454V's shipping OS** —
   looking "slightly dark, but … not discolored", whereas on Android 10 it
   crashed Kodi/VLC and made Plex transcode. That is the evidence behind
   "HDR masters are never re-encoded"; it is community-verified, not
   manufacturer-documented, so if an HDR title ever looks washed-out here
   see §7. One more consequence: **HDR video only Direct Plays if the audio
   does too** (a Plex user on a Chromecast with Google TV: "you can't direct
   stream 4K HDR on the Chromecast, you can only Direct Play. If audio needs
   transcoding, 4K HDR video will also transcode") — which is the strongest
   reason for the AC-3 policy below.

Practical consequences (all encoded in the code):

* `> 1080p` video ⇒ the server transcodes on every play. Google: "Chromecast
  with Google TV (HD) doesn't support 4K playback"; the S805X2's decoder is
  specified at 1080p60; Jellyfin users report 4K files stuttering or
  refusing to play on this model. A 2160p rip in this library is a
  replacement candidate, not a "maybe someday" file.
* **H.264 10-bit ("Hi10P"), 4:2:2 and 4:4:4 video** ⇒ same: no hardware
  decoder exists, Google lists H.264 *High* and HEVC *Main/Main10* only, and
  Jellyfin's client offers `high 10` only when a decoder reports it. Plex
  users on the Chromecast with Google TV report 10-bit x264 playing back
  corrupted until they force a transcode. These are the one class of file the bit-depth inspector used to
  call "SKIP — already 10-bit"; it now flags them as `unsupported-profile`.
  8-bit H.264 ≤1080p, by contrast, already Direct Plays — the HandBrake queue
  for 8-bit SDR is a space/banding optimisation, not a chain requirement, and
  Plex users do report some 1080p HEVC Main10 releases stuttering on this
  device family, so try a sample before converting a batch.
* Jellyfin-client specifics the toolkit does **not** enforce (they are the
  client's rules, read from `deviceProfile.kt`): 1080p H.264 must have ≤ 4
  reference frames (≤ 12 from 1200 px wide) and a level the decoder reports.
  A file outside that transcodes even though its codec "is supported".
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
| HDMI | **1× HDMI IN (4K/3D pass-through, per the spec sheet) + 1× HDMI OUT (eARC/ARC, CEC)**; auto power-on when a signal is detected on HDMI In |
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

The HDMI IN port accepts multichannel PCM — that is what makes AAC 5.1 /
FLAC 7.1 / PCM tracks "playable as-is": the Chromecast decodes them and
sends PCM to the bar. (The spec sheet lists "Multich PCM" among the decoders;
whether the bar advertises 6/8-channel LPCM in its HDMI-IN EDID — which the
Chromecast needs in order to send more than stereo — is what the spec sheet
implies, but no published EDID dump was found; §7 has the check.)

Two statements in the manual, quoted so nothing rests on memory:

* *"The unit may not be able to decode all digital audio formats from the
  input source. In this case, the unit will mute. This is NOT a defect.
  Ensure that the audio setting of the input source … is set to **PCM or
  Dolby Digital**."* The manufacturer itself names AC-3 and PCM as the safe
  formats — a second, independent reason (besides the Chromecast's own
  passthrough list) that AC-3 is the toolkit's target format.
* *"Dolby Atmos® / DTS: X is available in HDMI eARC /ARC mode."* The manual's
  input-format table sits under one heading that covers eARC/ARC **and**
  HDMI IN, and maps DD+ Atmos to "DOLBY ATMOS", so whether Atmos is rendered
  as Atmos (rather than as its DD+ 5.1 bed) on the HDMI IN is ambiguous in the
  text. It is not a Direct Play question — the stream plays either way — and
  §7 says how to read the answer off the bar's display.

The bar also has an **audio-delay control (AV SYNC, 0–200 ms in 10 ms
steps, valid for digital inputs including HDMI IN)** — the fix if speech
lags the picture because the 2013 TV's video processing is slower than the
bar's audio path.

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
| Dolby Digital (AC-3) 5.1 | passthrough ✅ | ✅ decodes | ⚠️ this TV offers PCM only for HDMI sources, so ARC/opt may deliver it as stereo (§3) | **goal format** — synthesized when missing |
| Dolby Digital Plus (E-AC-3, incl. Atmos JOC) | passthrough ✅ | ✅ decodes | ⚠️ same PCM-only limit — DD+ Atmos does not survive this TV's ARC path (§3) | **goal format** — best possible track on this chain |
| AAC 5.1 / stereo | decode → PCM ✅ | ✅ (multich. PCM) | ⚠️ stereo only on this TV | stereo = fine; 5.1+ native on the **default** HDMI-IN wiring, **AC-3 candidate only under `tv-arc`** |
| FLAC / PCM / ALAC 7.1 | decode → PCM ✅ | ✅ multich. | ⚠️ stereo only on this TV | native multichannel on the **default** HDMI-IN wiring; **AC-3 candidate (5.1+) only under `tv-arc`** |
| MP3 / Opus / Vorbis | decode → PCM ✅ | ✅ | ✅ stereo | fine |
| base 5.1 **DTS core** | ⚠️ **not on Google's list; unverified on this unit** (§1) — may reach the bar as DTS, as stereo PCM, or not at all | ✅ decodes (if it arrives) | ⚠️ same PCM-only limit (§3) | accepted by default; `ORGANIZE_DTS_PASSTHROUGH=0` / `--no-dts-passthrough` transcodes it — test first (§7) |
| **TrueHD / TrueHD Atmos** | ❌ **cannot be emitted at all** | (bar could decode — player can't send) | ❌ | **AC-3 5.1 640k synthesized** from it by audio_standardizer |
| **DTS-HD MA / HRA, DTS:X** | ❌ **cannot be emitted at all** | (same) | ❌ | **AC-3 5.1 640k synthesized** |
| WMA Pro / unknown | ❌ | ❌ | ❌ | fail-closed: reported for a human, never auto-touched |

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

The keep-one-track tier table (`mkv_track_cleaner.py`) encodes exactly that
matrix: **chain-native Dolby (E-AC-3 100, AC-3 95) > base DTS core (80) >
decode-to-PCM (60–66) > lossless-HD masters kept only as transcode sources
(30–34) > unknown (10)**. "Highest sample rate wins" is the wrong metric on this chain;
"plays without a server" is the right one.

### Why AC-3 5.1 @ 640 kbps (and not 448k, and not E-AC-3)?

* 640 kbps is the AC-3 maximum and the bitrate every sink in this chain
  handles; the transcode source is usually a lossless master with plenty of
  headroom, so the ceiling is the honest choice.
* The output **stays AC-3 rather than E-AC-3** on purpose: AC-3 is the one
  format licensed-and-supported at **every single hop forever** (ARC and
  optical included), and the 5.1 @ 640k mix is well within its design
  envelope. Re-muxing an existing E-AC-3 track is of course *kept* as-is —
  it is native too, and Atmos carries through.
* A 7.1/6.1 source folds down to 5.1 by ffmpeg's standard downmix (LFE
  kept); stereo sources stay stereo at 192 kbps — never upmixed.

## 6 · The settings checklist (the human half of the chain)

* **Chromecast (Google TV):** Settings → Display & Sound → *Audio* →
  *Surround sound*: **Auto** ("best available": it passes through whichever of
  DD/DD+ the bar advertises; the other choices reported for this model are
  None and Manual, where formats can be ticked individually). An *Audio
  output format: Standard* entry, which an earlier revision of this checklist
  asked for, could not be verified on this model — Google added an "Output
  format" menu to the Google TV **Streamer** in Nov 2024 — so ignore it if
  you do not see it. Leave *Match content dynamic range* on. *Match content
  frame rate* has no bearing on audio; if playback stutters, community reports
  for this dongle name three things that fixed it for some owners: the TV's
  **Game Mode** (switch it off for this HDMI input), the frame-rate setting
  itself, and the developer option "Disable HW overlays". If the screen
  blanks or the sound drops for a few seconds at the start of each film,
  that is typically the HDMI link re-syncing on the refresh-rate switch (more
  noticeable with a soundbar in the path), and setting it to *Never* stops
  the switch.
* **Jellyfin Android-TV app:** the per-codec *Bitstream* settings (Dolby
  Digital, Dolby Digital Plus, DTS, TrueHD) default to **Auto**, which asks
  the platform whether passthrough is available and only then offers that
  codec for Direct Play (`deviceProfile.kt` / `audioPassthrough.kt`); leave
  AC-3 and E-AC-3 on Auto. Setting DTS or TrueHD to **Enable** makes the app
  claim Direct Play regardless and fall back to its bundled FFmpeg audio
  decoder — a *possible* way to keep lossless masters (decoded to
  multichannel PCM into the bar) that this toolkit has never tried, **untested
  on this hardware**; §7 describes the experiment.
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

## 7 · What is verified, and what only your unit can tell you

Everything above was re-audited on 2026-10-01 against primary sources. This is
the honest status of each claim the toolkit's defaults lean on:

| Claim | Status | Basis |
| :--- | :--- | :--- |
| G454V: S805X2, 1.5 GB / 8 GB, 1080p60, HDR10/HDR10+/HLG, no Dolby Vision, AV1 decode | **Verified** | Google spec page + Google's device-comparison table; Android Police, CNX Software, Android TV Guide |
| Passthrough of AC-3, E-AC-3, Atmos (DD+ JOC) | **Verified** (manufacturer) | Google spec page, Google Cast docs |
| No 4K playback; profile limits (H.264 High, HEVC Main/Main10) | **Verified** (manufacturer) | Google support "Stream 4K" note; Cast docs; S805X2 decoder table |
| No hardware H.264 Hi10P decode | **Verified** (SoC family) | Kodi wiki; LibreELEC Amlogic thread; Plex reports on this dongle |
| HDR10/HLG tone-mapped to SDR on the player | **Community-verified** on Google TV 12 | Reddit/Plex user reports; not in any datasheet |
| Base DTS core passes through | **UNVERIFIED — contradicted by Google's list** | Google staff answer; conflicting field reports (§1) |
| AX3125H: 3.1.2, 440 W, 1 HDMI IN, 1 HDMI OUT (eARC/ARC), optical/AUX/USB, BT 5.3, 6.5″ sub, Atmos/TrueHD/DD+/DD/DTS:X/DTS-HD/DTS/PCM/multichannel PCM decoders | **Verified** | Hisense spec sheet + manual |
| AX3125H renders DD+ Atmos as *Atmos* on its HDMI IN | **Ambiguous** in the manual; unmeasured | §2 |
| AX3125H's HDMI-IN EDID advertises multichannel LPCM | **Implied** by the spec sheet; unmeasured | §2 |
| UN60F6350AF: 1080p, 4 HDMI (one ARC), optical out, plain ARC carrying PCM 2.0 / DD 5.1 / DTS 5.1 | **Verified** | Samsung ARC articles, e-manual, retailer spec sheets |
| This TV offers PCM only for HDMI sources | **User-confirmed (2026-09)**; corroborated | Samsung e-manual wording; AVS Forum/Reddit reports |
| TV panel is native 120 Hz ("Clear Motion Rate 240") | **Marketing figure**; no decision depends on it | retailer sheets |
| What the Jellyfin Android-TV client offers for Direct Play | **Verified from source** (master, Oct 2026) | `deviceProfile.kt`, `MediaCodecCapabilitiesTest.kt`, `audioPassthrough.kt`, `UserPreferences.kt` |
| What the Plex Android-TV client offers | **Not read** — only forum reports | — |

Three checks settle the open rows. Each takes minutes and needs only the
soundbar's front display:

1. **DTS (decides `ORGANIZE_DTS_PASSTHROUGH`).** Play a movie whose only audio
   is base DTS 5.1. The bar's display scrolls **DTS** if the Chromecast passed
   the bitstream, **PCM** if it decoded/downmixed it; check Jellyfin's
   dashboard for "Direct Play" vs an audio transcode as well. DTS + Direct
   Play → leave the default. Anything else → `ORGANIZE_DTS_PASSTHROUGH=0`,
   then rerun `organize audio --dry-run` to see how many movies it affects.
2. **Atmos and multichannel PCM (decides two assumptions at once).** Play an
   E-AC-3 Atmos (JOC) file and watch for **DOLBY ATMOS** in the first seconds
   (owners report it flashes briefly); then play a 5.1 AAC or FLAC file — the
   display should read **PCM** and you should hear surround, not stereo. If the
   second plays as stereo, the bar's HDMI-IN EDID is not advertising
   multichannel LPCM, and the compensation `--wiring tv-arc` applies (bake AC-3
   into multichannel-PCM movies) is the right setting even though the cabling
   is not ARC.
3. **HDR sanity.** The player reads the *soundbar's* EDID, not the TV's, and
   the audit found nothing documenting whether the AX3125H forwards the TV's
   capabilities or advertises HDR of its own. Play one HDR10 file: if it looks
   right (slightly dark is normal for on-device tone-mapping), nothing to do.
   If it looks washed-out or very dim, the player is sending HDR to a TV that
   cannot show it — set the Chromecast's dynamic range to SDR (the HDR and
   dynamic-range options sit under Settings → Display & Sound; exact labels
   vary by firmware).

An optional fourth experiment could make the AC-3 bake-in unnecessary: in the
Jellyfin app set *Bitstream TrueHD* and *Bitstream DTS* to **Enable** and play
a TrueHD/DTS-HD movie. If the bundled FFmpeg decoder plays it as multichannel
PCM into the bar without stutter (a 1.8 GHz Cortex-A35 is modest), lossless
masters could stay in the library. It has not been tried, it does not apply to
Plex, and until it is, the toolkit's AC-3 bake-in is the guaranteed-native
route.

---

## Sources

All links below were retrieved on 2026-10-01. Citations in earlier revisions of
this dossier that returned 404 — a Google help article for the G454V manual,
the AFTVnews article, the Rtings review, the Hisense USA product page, a second
Hisense PDF, a Samsung support article — or that resolved to the wrong page (the
GSMArena URL opened an unrelated Oppo phone; a second Samsung article id
redirected to a generic support page) or could not be retrieved (a Reddit
thread cited for DTS) were removed rather than replaced with look-alikes; every device fact they were
cited for is covered by a source below.

Player:

* Google, *Chromecast & Google TV Streamer specifications* — the HD model: "Up
  to 1080p HDR, 60 fps", HDR10/HDR10+/HLG, Dolby Digital / Dolby Digital Plus /
  Dolby Atmos via HDMI pass-through, no Dolby Vision entry.
  <https://support.google.com/chromecast/answer/3046409>
* Google Store, *Chromecast with Google TV — tech specs* (same, per version).
  <https://store.google.com/product/chromecast_google_tv_specs>
* Google TV Help, *Meet Google TV Streamer (4K)* — comparison table: HD has
  1.5 GB RAM / 8 GB storage, HDR10/HDR10+/HLG, "Dolby-encoded audio (HDMI
  passthrough)".
  <https://support.google.com/googletv/answer/15273676>
* Google, *Stream 4K Ultra HD content* — "Chromecast with Google TV (HD)
  doesn't support 4K playback."
  <https://support.google.com/chromecast/answer/7151529>
* Google for Developers, *Supported Media for Google Cast* — audio passthrough:
  AC-3, E-AC-3, MPEG-H, Dolby Atmos; Chromecast with Google TV video: H.264
  High Profile, HEVC Main and Main10.
  <https://developers.google.com/cast/docs/media>
* Google Nest Community, *Chromecast 4K with DTS?* — Google staff: "Chromecast
  with Google TV devices only supports … Dolby Digital, Dolby Digital Plus,
  Dolby Atmos (pass-through)"; DTS "technically wasn't supported".
  <https://www.googlenestcommunity.com/t5/Streaming/Chromecast-4K-with-DTS/m-p/335714>
* Android Police, *Chromecast with Google TV (HD) review* — Android 12, 1.5 GB /
  8 GB, S805X2, no Dolby Vision, AV1 decode, passthrough list.
  <https://www.androidpolice.com/chromecast-with-google-tv-hd-review/>
* CNX Software, *Chromecast with Google TV (HD) features Amlogic S805X2 CPU
  with AV1 video support*, and its S805X2 vs S805X table ("1080p60 10-bit AV1,
  H.265, VP9 P-2, H.264, AVS2, MPEG-4/2/1").
  <https://www.cnx-software.com/2022/09/23/chromecast-with-google-tv-hd-amlogic-s805x2-cpu-av1-video/> ·
  <https://www.cnx-software.com/2021/05/14/s805x2-av1-android-tv-dongles-tv-boxes-are-starting-to-show-up/>
* Android TV Guide, *Google Chromecast with Google TV (HD)* — "G454V / Boreal",
  S805X2, AV1/VP9/H.264/H.265, HDR10/HDR10+.
  <https://www.androidtv-guide.com/streaming-gaming/chromecast-google-tv-hd/>
* How-To Geek / TechRadar on the Android 14 update (March and June 2025; the
  HD model's rollout lagged the 4K's, and Google's support timeline has the HD
  receiving firmware updates into 2027).
  <https://www.howtogeek.com/chromecast-with-google-tv-android-14-rollout/> ·
  <https://www.techradar.com/televisions/streaming-devices/the-chromecast-with-google-tv-is-finally-getting-its-long-delayed-free-update-heres-whats-new>

Soundbar:

* Hisense AX3125H spec sheet (PDF) — HDMI Input ×1, HDMI eARC/CEC ×1, optical,
  AUX, USB; decoders: Dolby Atmos / TrueHD / DD+ / DD / DTS:X / DTS-HD Master /
  DTS / PCM / Multich PCM; "Auto Power On with Signal detected: AUX, ARC, HDMI
  In, Optical"; 6.5″ subwoofer.
  <https://files.hisense-usa.com/download/f25648883914883a>
* Hisense AX3125H user manual — input-format table, "set to PCM or Dolby
  Digital", "Dolby Atmos / DTS:X is available in HDMI eARC/ARC mode", AV SYNC
  0–200 ms, Bluetooth 5.3.
  <https://manuals.plus/hisense/ax3125h-3-1-2ch-440w-dolby-atmos-soundbar-with-wireless-subwoofer-manual>

Display:

* Samsung UN60F6350AF e-manual (mirrors): "ARC is only available through the
  HDMI (ARC) port"; the available Digital Audio Output (SPDIF) formats "may
  vary depending on the input source".
  <https://www.manualowl.com/m/Samsung/UN60F6350AF/Manual/347300> ·
  <https://www.manualshelf.com/manual/samsung/un60f6350afxza/user-manual-ver10.html>
* Samsung support, *How to use HDMI ARC on Samsung Smart TV* — ARC carries PCM
  (2 channel), Dolby Digital (up to 5.1), DTS Digital Surround (up to 5.1);
  setting paths for 2013–2014 F/H series.
  <https://www.samsung.com/sg/support/tv-audio-video/how-to-use-the-hdmi-arc-port-on-a-samsung-tv/> ·
  <https://www.samsung.com/latin_en/support/tv-audio-video/how-to-use-hdmi-arc-on-samsung-smart-tv/>
* Retailer specifications: the UN60F6350AF lists 4 HDMI connectors (HDMI-CEC),
  component and an optical sound out; the sibling UN60F6300 is sold as "four
  HDMI inputs, … a digital optical audio output … Clear Motion Rate of 240".
  <https://www.meetgadget.com/gadget/69008/Samsung+UN60F6350AF> ·
  <https://www.bhphotovideo.com/c/product/918920-REG/samsung_un60f6300afxza_un60f6300_60_1080p_led.html>
* Community corroboration of the greyed-out Dolby/DTS behaviour for HDMI
  sources on 2012–2013 Samsungs (PCM only, or PCM/DTS Neo 2:5):
  <https://www.avsforum.com/threads/getting-a-samsung-tv-to-output-dolby-digital-5-1-through-optical-out.1509865/> ·
  <https://www.reddit.com/r/hometheater/comments/pkz268/>
* **USER-CONFIRMED (2026-09) on the actual UN60F6350AF in this chain:** with
  HDMI sources connected, the TV's digital audio output offers **PCM only**
  (Dolby Digital/DTS are not selectable). This is the direct observation the
  default wiring rests on; it is bounded to this unit.

Playback-chain mechanics:

* Jellyfin, *Codec support* — the Direct Play vs transcode model; Android TV
  rows for H.264 10-bit, DTS ("only DTS Mono has been tested") and HDR.
  <https://jellyfin.org/docs/general/clients/codec-support/>
* Jellyfin Android-TV client source — what the client *declares* it can Direct
  Play: DTS/TrueHD only if passthrough is available or forced; `high 10` only
  if a decoder reports it; ≤ 4 reference frames at 1900 px wide; HDR gated on
  the decoder, not the display; per-codec Bitstream settings; bundled FFmpeg
  audio decoder.
  <https://github.com/jellyfin/jellyfin-androidtv/blob/master/app/src/main/java/org/jellyfin/androidtv/util/profile/deviceProfile.kt> ·
  <https://github.com/jellyfin/jellyfin-androidtv/blob/master/app/src/main/java/org/jellyfin/androidtv/util/profile/codec/audioPassthrough.kt> ·
  <https://github.com/jellyfin/jellyfin-androidtv/blob/master/app/src/main/java/org/jellyfin/androidtv/util/profile/codec/MediaCodecQuery.kt>
* DTS on the Chromecast with Google TV (field reports, conflicting):
  <https://forums.plex.tv/t/chromecast-with-google-tv-not-playing-dts-7-1-audio/642550> ·
  <https://forums.plex.tv/t/plex-ignores-dts-setting-on-chromecast-with-google-tv-pcm-output-to-receiver/924804> ·
  <https://www.reddit.com/r/Chromecast/comments/11gy2ed/chromecast_4k_2020_and_dts_format/> ·
  <https://news.ycombinator.com/item?id=37745572>
* H.264 10-bit (Hi10P): no ARM hardware decoder (Kodi wiki); Amlogic S805/S905
  cannot decode it (LibreELEC); Plex users on the Chromecast with Google TV.
  <https://kodi.wiki/view/Android_hardware> ·
  <https://forum.libreelec.tv/thread/1130-solved-s805-s905-glitchy-hardware-decoding-of-h-264-main10-profile-video/> ·
  <https://www.reddit.com/r/PleX/comments/y2blci/> ·
  <https://www.reddit.com/r/PleX/comments/19f58ze/>
* HDR on an SDR display (on-device tone-mapping works on Google TV 12; Plex:
  HDR video only Direct Plays when the audio does too):
  <https://www.reddit.com/r/Chromecast/comments/yodnsb/> ·
  <https://www.reddit.com/r/PleX/comments/18dgtqu/>
* HEVC Main10 stutter reports on this device family (Plex forum, 2025–2026) and
  an older ExoPlayer issue for HEVC Main10 + E-AC-3 through an AVR/soundbar:
  <https://forums.plex.tv/t/bug-chromecast-with-google-tv-hd-and-4k-stutters-with-hevc-main-10-1080p-movies/867668> ·
  <https://github.com/google/ExoPlayer/issues/9232>
* 4K on the HD model (Jellyfin users: stutter or refusal to play):
  <https://forum.jellyfin.org/t-chromecast-with-google-tv-hd-unable-to-play-4k-content>

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[The pipeline](pipeline.md) · [Configuration](configuration.md)
