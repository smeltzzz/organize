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

1. **Base Dolby Digital 5.1 DTS core is the unofficial extra.** Google's
   published passthrough list for this device is DD/DD+ only, but the
   Amlogic Android TV firmware passes plain 5.1 DTS core through the HDMI
   layer in practice (widely reported; see sources). The toolkit treats base
   DTS as *acceptable but unofficial*: it is left alone by default, and
   `--no-dts-passthrough` converts it to AC-3 for anyone who distrusts it.
2. **HDR10/HDR10+/HLG "play" here means tone-mapped to SDR.** The panel is a
   1080p SDR display, so the Chromecast outputs SDR after tone-mapping.
   That is a picture-preserving operation done at playback time, with zero
   generation loss — unlike a HandBrake re-encode, which is why the
   bit-depth inspector's "protect HDR, never re-encode" stand is unchanged.

Practical consequences (all encoded in the code):

* `> 1080p` video ⇒ the server transcodes on every play. A 2160p rip in this
  library is a replacement candidate, not a "maybe someday" file.
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
| HDMI | **1× HDMI IN (4K@60 passthrough) + 1× HDMI OUT with ARC** |
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
sends PCM to the bar.

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
| base 5.1 **DTS core** | ⚠️ passthrough (unofficial, works on this AMLogic build) | ✅ decodes | ⚠️ same PCM-only limit (§3) | accepted by default; `--no-dts-passthrough` transcodes it |
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
matrix: **chain-native Dolby (100) > base DTS core (80) > decode-to-PCM
(60–66) > lossless-HD masters kept only as transcode sources (30–34) >
unknown (0)**. "Highest sample rate wins" is the wrong metric on this chain;
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
* AFTVnews, *Google's new 1080p Chromecast with Google TV (HD), model G454V* —
  confirms codename "boreal", S805X2, 1.5 GB RAM, AV1 decode as the HD
  model's distinguishing feature.
  <https://www.aftvnews.com/googles-new-1080p-chromecast-with-google-tv-hd-model-g454v-is-now-official-for-30/>
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
