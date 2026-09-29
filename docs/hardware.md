# The playback chain this toolkit is engineered for

Every codec decision in this repository is tuned to **one concrete living
room**:

```
   ┌──────────────────────────────┐   ┌───────────────────────┐   ┌──────────────────────────┐
   │ Chromecast with Google TV    │   │ Samsung UN60F6350AF   │   │ Hisense AX3125H          │
   │ (HD)  — model G454V "boreal" │──▶│ 60" 1080p SDR LED TV  │──▶│ 3.1.2ch soundbar + sub   │
   │ Android TV 12 · S805X2       │   │ (2013 era, no HDR)    │   │ 440 W · DTS + Atmos      │
   └──────────────────────────────┘   └───────────────────────┘   └──────────────────────────┘
              the PLAYER                      the DISPLAY                 the SOUND sink
   HDMI forward: Chromecast → a TV HDMI input. Audio returns to the soundbar over the
   TV's HDMI (ARC) port — the as-shipped wiring, and the toolkit's default assumption.
   (The upgrade wiring — Chromecast through the bar's HDMI IN — is documented in §4.)
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
* AAC/FLAC/PCM audio ⇒ decodes locally to PCM; fine over the soundbar's HDMI
  IN (multichannel PCM works), stereo-only over plain ARC/optical.

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
the chain. **Dolby Digital (AC-3) and Dolby Digital Plus (E-AC-3)
bitstream through the entire chain with zero conversions anywhere.**

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

Two things decide the whole wiring story:

1. **Plain ARC (2013) is not eARC.** The TV's ARC/optical return paths carry
   lossy DD/DTS bitstreams or **stereo PCM only** — no multichannel PCM, no
   TrueHD/DTS-HD class streams, whatever the sink might decode. On the
   as-shipped ARC wiring this is the one material limit the toolkit
   *compensates* for: any movie whose surround is carried as multichannel
   PCM gets an AC-3 5.1 track baked in (§5), because lossy Dolby Digital is
   exactly what 2013 ARC passes. Rewiring the Chromecast through the bar's
   HDMI IN (§4) is the hardware move that removes the need.
2. Samsung's "Digital Audio Output" menu on this generation is famous for
   **greying out Dolby Digital / DTS for HDMI-originated sources** (PCM or
   "DTS Neo 2:5" only). This is compliance behaviour (EDID/HDCP), not a
   defect of any one set — and it means that on units where the menu greys
   out, every HDMI audio source collapses to stereo PCM at the bar. The
   toolkit's AC-3 policy degrades as gracefully as possible there (a clean
   stereo fold), and the upgrade wiring in §4 is the way back to full
   surround.

**As shipped, the chain is wired through ARC**: Chromecast → TV HDMI, and
the soundbar hangs off the TV's designated HDMI (ARC) port. Everything in
§5 onward assumes that wiring; §4 shows the one alternative and what flips.

## 4 · The wiring decision

| Wiring | Video limit | Audio reality on 5.1+ content |
| :--- | :--- | :--- |
| **Chromecast → TV HDMI, TV --ARC→ AX3125H (default, as shipped)** | 1080p60 | DD/DTS lossy bitstream or **stereo PCM**; DD/DTS tracks native; multichannel PCM-decodes arrive as stereo — the toolkit bakes AC-3 for exactly those |
| Chromecast → AX3125H HDMI IN → TV (upgrade wiring) | 1080p60 chain-wide | bar decodes DD/DD+/Atmos/DTS/PCM natively; player passthrough or decode; **no server work** |
| Chromecast → TV HDMI, TV --**optical**→ AX3125H | 1080p60 | same limits as ARC (no CEC, slightly worse UX) |

The toolkit's default is `tv-arc` — the soundbar hangs off the TV's HDMI
(ARC) port, as shipped. `soundbar-hdmi-in` is the supported upgrade wiring
(`--wiring soundbar-hdmi-in` or
`ORGANIZE_PLAYBACK_WIRING=soundbar-hdmi-in`) and changes exactly one rule: a
multichannel AAC/FLAC/PCM movie that is an AC-3 transcode candidate on
plain ARC/optical plays natively over HDMI IN.

## 5 · The audio codec matrix — what actually plays where

Legend: ✅ native (no server work) · ⚠️ accepted, with a caveat · ❌ never
leaves the player — the server transcodes audio on **every** play.

| Source track player | G454V player | over HDMI IN | over ARC/opt | verdict in the toolkit |
| :--- | :--- | :--- | :--- | :--- |
| Dolby Digital (AC-3) 5.1 | passthrough ✅ | ✅ decodes | ✅ | **goal format** — synthesized when missing |
| Dolby Digital Plus (E-AC-3, incl. Atmos JOC) | passthrough ✅ | ✅ decodes | ✅ (lossy DD+ on 2013 ARC) | **goal format** — best possible track on this chain |
| AAC 5.1 / stereo | decode → PCM ✅ | ✅ (multich. PCM) | ✅ stereo only | stereo = fine; **5.1+ = AC-3 candidate on the default ARC wiring** |
| FLAC / PCM / ALAC 7.1 | decode → PCM ✅ | ✅ multich. | ⚠️ stereo only | **AC-3 candidate (5.1+) on the default ARC wiring**; native multichannel only via the bar's HDMI IN |
| MP3 / Opus / Vorbis | decode → PCM ✅ | ✅ | ✅ stereo | fine |
| base 5.1 **DTS core** | ⚠️ passthrough (unofficial, works on this AMLogic build) | ✅ decodes | ⚠️ | accepted by default; `--no-dts-passthrough` transcodes it |
| **TrueHD / TrueHD Atmos** | ❌ **cannot be emitted at all** | (bar could decode — player can't send) | ❌ | **AC-3 5.1 640k synthesized** from it by audio_standardizer |
| **DTS-HD MA / HRA, DTS:X** | ❌ **cannot be emitted at all** | (same) | ❌ | **AC-3 5.1 640k synthesized** |
| WMA Pro / unknown | ❌ | ❌ | ❌ | fail-closed: reported for a human, never auto-touched |

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
* **AX3125H (as shipped, ARC wiring):** the bar's **HDMI Out (ARC)** socket
  sits on the TV's designated HDMI (ARC) lead; source = **ARC**; EQ mode
  Movie; night mode off; subwoofer paired (auto).
* **Samsung UN60F6350AF:** the soundbar's ARC lead goes into the **designated
  HDMI (ARC)** input; Anynet+ (HDMI-CEC) **on** so ARC wakes the bar and the
  TV remote drives its volume. *Settings → Sound → Audio Format*: leave it
  on whatever the set offers for HDMI inputs — if only *PCM* is selectable
  (the F-series grey-out, §3), AC-3 movies still land at the bar as clean
  stereo at worst, and the **upgrade wiring in §4** is the fix for full
  surround. *Speaker settings*: TV speaker **off** once the bar is on.

---

## Sources

Player:

* Google, *Chromecast with Google TV (HD) — tech specs* (store specs
  page/manual): HDR10/HDR10+/HLG, 1080p60, no Dolby Vision.
  <https://store.google.com/product/chromecast_google_tv_specs>
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
* Hisense AX3125H spec sheet (PDF), decoder/ports tables.
  <https://files.hisense-usa.com/storage/hisense/asset/images/66406cbb29a362.pdf>

Display:

* Samsung, *UN60F6350AF product/spec support pages* (1080p, 4× HDMI,
  optical out, ARC).
  <https://www.samsung.com/us/support/answer/ANS00077530/>
* Samsung, *Connect your soundbar / ARC support* (designated HDMI (ARC)
  port; ARC carries more channels than optical).
  <https://www.samsung.com/us/support/troubleshoot/TSG10001983/>
* Samsung UN60F6350AF user manual (mirror): "ARC is only available through
  the HDMI (ARC) port".
  <https://manualowl.com/m/Samsung/UN60F6350AF/Manual/347300>
* Community corroboration of the 2013-era Samsung greyed-out Dolby/DTS
  behaviour for HDMI sources (EDID compliance gating):
  <https://www.reddit.com/r/hometheater/comments/pkz268/> ·
  <https://www.avsforum.com/threads/.3198706/>

Playback-chain mechanics:

* Jellyfin documentation, *Codec support / Direct Play vs transcode* (the
  "server re-encodes per play" model this toolkit eliminates).
  <https://jellyfin.org/docs/general/clients/codec-support>
* DTS/PCM over HDMI vs ARC/optical return-channel limits (HDMI Licensing /
  CTA-861 summaries; see also the Samsung ARC article above).

---

[← Back to the README](../README.md) · [Tool reference](tools.md) ·
[The pipeline](pipeline.md) · [Configuration](configuration.md)
