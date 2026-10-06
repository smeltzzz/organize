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

The toolkit targets Direct Play on this reference chain using an app-neutral
Plex/Jellyfin policy for recognized formats. It prepares a compatible audio
track where the profile does not guarantee a no-transcode route; app-decoded
PCM/FLAC assumes the selected app and active HDMI route can handle it. Unknown,
MAT/MPCM, and over-envelope audio remain review-only. This is an operating
profile, not a runtime guarantee for every app or file. The notes below label
official specifications, chain measurements, and app-dependent behavior
separately. The machine-readable form is `organizekit/core/playbackchain.py`; if
that table and this document ever disagree, one of them is a bug.

---

## 1 · The player — Chromecast with Google TV (HD), model G454V

Evidence is labelled as official specification, chain measurement, or app-dependent behavior; sources at the end:

| Property | Value |
| :--- | :--- |
| Model | G454V, codename "boreal" (the **HD** model, 2022; **not** the 4K model) |
| SoC | Amlogic **S805X2**, quad Cortex-A35 up to 1.8 GHz, Mali-G31 MP2, 1.5 GB RAM |
| Video ceiling | **1920×1080 @ 60 Hz max**. No 4K output at all. |
| Video decoders | H.264 (AVC), H.265 (HEVC), VP9, **AV1**, MPEG-2 — up to 1080p60 |
| HDR | HDR10, HDR10+, HLG — **Dolby Vision is NOT supported on this model** (the 4K model has it; the HD does not) |
| Audio passthrough | **Official Google list:** Dolby Digital (AC-3), Dolby Digital Plus (E-AC-3), and Dolby Atmos via HDMI passthrough (Atmos carried here as DD+ JOC). **Chain-measured, not Google-certified:** base 5.1 DTS and the DTS core extracted from DTS-HD MA/HRA or DTS:X; the core is capped at 5.1 (see subtleties 1–2). |
| Audio decode | App/software-decoded AAC, MP3, FLAC, Opus, Vorbis and PCM/WAV may reach the HDMI sink as PCM, but support depends on the app and active route. The toolkit uses a conservative **24-bit / 48 kHz** envelope for this chain; it is not a Google-published maximum. The AX3125H manual lists LPCM 5.1/7.1 on HDMI IN, while Android's generic built-in FLAC table is mono/stereo only up to 48 kHz. **ALAC and WavPack are outside the confirmed decoder set.** |
| TrueHD path | The G454V has no supported TrueHD bitstream path. Some apps with their own decoder can software-decode TrueHD to multichannel PCM; that is app-/route-dependent, and plain PCM loses TrueHD Atmos object metadata. Plex/Jellyfin may instead ask the server to transcode. This toolkit's app-neutral profile conservatively prepares a Dolby track. |
| Dolby MAT output | **Unverified for the G454V.** Google does not list MAT or MS12 in its G454V audio specification. The AX3125H manual's input/display table lists plain Dolby MAT → MPCM and MAT-Atmos → DOLBY ATMOS, but its per-port support table does not establish that the Chromecast emits MAT. |
| Other no-guarantee formats | WMA Pro and DTS-HD LBR (DTS Express) have no supported passthrough path or backward-compatible DTS core; the toolkit prepares a server-side Dolby fallback for its app-neutral profile. |

Six distinctions the research surfaced and the code encodes:

1. **Base 5.1 DTS core plays on this chain — measured on the real hardware,
   not assumed.** Google's published list is Dolby Digital, Dolby Digital
   Plus and Atmos via HDMI passthrough; it does not list DTS. On the measured
   G454V firmware/app path in this physical HDMI-IN chain, plain 5.1 DTS core
   passes through to the bar. This is chain-specific evidence, not a general
   Amlogic/Android capability claim. 8.5.0 read that gap as a risk and converted
   base DTS to Dolby Digital Plus by default on `soundbar-hdmi-in`; the
   decision was then tested instead of debated, with a base-DTS movie played
   through Jellyfin to the G454V. **Two independent indicators agreed**:
   * the server reported **Direct Play** — no transcode at either end, so
     Jellyfin was handed a bitstream it treated as final; and
   * the **AX3125H's front panel lit its DTS indicator**, which it only does
     for a genuine DTS bitstream arriving at the bar's decoder.

   Given that, converting is irreversible loss buying nothing: 1509 kbps DTS
   core becomes a 640 kbps DD+ bed, a difference of **~870 kbps — about
   780 MB on a two-hour movie** — spent to replace audio the bar already
   decodes (its 3.1.2 array folds a 5.1 mix either way). And the decision is
   reversible at playback time from the *untouched source*: if firmware ever
   ends the passthrough, `audio_standardizer.py` converts then, from the same
   bytes, to the identical DD+ result — waiting costs one rerun, converting
   early costs the DTS track forever. The toolkit currently applies its DTS
   accept policy under both wiring selectors (`DTS_PASSTHROUGH_DEFAULT` is
   uniformly `True`), but the cited measurement validates only the physical
   HDMI-IN route; it does not prove DTS delivery over alternate TV-ARC. 8.5.0's behaviour is
   still available by asking for it with
   `--no-dts-passthrough`, which states the conversion policy explicitly.
   (The 8.5.0 default was also un-overridable where it mattered most:
   `organize run` exposes no DTS flag and forwards nothing to the audiofit
   step, so the pipeline could not opt out of the conversion at all.) See
   §5 for the verdict this feeds into the tools.
2. **DTS-HD MA / DTS-HD HRA / DTS:X play as their DTS core — confirmed on
   the real chain, 2026-10.** The G454V cannot emit the lossless HD layer:
   no DTS-HD bitstream ever leaves the box as such, and DTS:X's object
   metadata never survives at all. But DTS-HD is backward compatible *by
   design*, and the player does not drop the audio when it meets one — it
   automatically extracts the plain **DTS core** (5.1, lossy) every such
   bitstream carries and bitstreams **that**. The result, in the reporter's
   words: *the soundbar still produces surround sound, but its display reads
   `DTS` instead of `DTS:X` or `DTS-HD`*. No server transcode happens, so
   these tracks **Direct Play**, and the toolkit treats them accordingly:

   * the class is `dts-hd-core-passthrough`, native-band and tiered exactly
     with base DTS core — the delivered audio is a DTS core either way;
   * a 7.1 DTS-HD MA track therefore reaches **5.1** on this chain (the
     core's ceiling, `playbackchain.DTS_CORE_MAX_CHANNELS`), not 8;
   * `audio_standardizer.py` leaves such movies alone by default, and
     `--no-dts-passthrough` is the one way to spend the master deliberately
     (it burns the DD+/AC-3 target from the *lossless* layer, which is also
     why that flag is a bigger decision on DTS-HD than on base DTS);
   * **DTS-HD LBR / DTS Express is the exception**, and it is why the label
     must be read carefully: LBR is a separate low-bitrate decoder used for
     secondary audio, with no backward-compatible core to fall back to, so it
     stays transcode-bound like TrueHD.

   This is the same argument that reverted 8.5.0's base-DTS conversion,
   applied one level up: playing without server work beats converting, the
   master is only spent when asked, and the conversion stays available on
   any later run from the untouched source.
3. **HDR10/HDR10+/HLG "play" here means tone-mapped to SDR.** The panel is a
   1080p SDR display, so the Chromecast outputs SDR after tone-mapping.
   That is a picture-preserving operation done at playback time, with zero
   generation loss — unlike a HandBrake re-encode, which is why the
   bit-depth inspector's "protect HDR, never re-encode" stand is unchanged.
4. **The software-decode boundary is a conservative report, not a Google spec.**
   This repository uses **24-bit / 48 kHz** as its chain-specific operating
   envelope for app/software-decoded audio. Google's G454V specification does
   not publish PCM limits. Android's generic media table says built-in FLAC is
   mono/stereo up to 48 kHz, recommends 16-bit, and says support outside
   handsets/tablets may vary; it is not a complete G454V HDMI capability table.
   The actual channel layout and decode path depend on the app and active HDMI
   route. Hisense's AX3125H manual lists LPCM 5.1/7.1 on HDMI IN (not ARC or
   optical), and multichannel output from Kodi/VLC/other app decoders is
   app-dependent. A 24/96 or 24/192 file is outside the toolkit envelope, not
   proven broken: `playbackchain.exceeds_decode_ceiling()` reports it for review
   and does not transcode or change track ranking. Whether a wider stream fails,
   is resampled, or is handled by a particular app has not been established for
   every route. Bitstreamed Dolby/DTS tracks never touch this check. ALAC and
   WavPack remain `AUDIO_UNKNOWN` because they are outside the confirmed decoder
   set. The old 96-kHz claim relied on a generic Google Cast table for
   Chromecast Audio/Google Home products, not this G454V.
5. **TrueHD is not the same as “no app can play it.”** The G454V has no supported
   lossless TrueHD bitstream path and Android's generic platform table does not
   promise a TrueHD decoder. Apps with their own decoders can nevertheless
   decode TrueHD to multichannel PCM on supported routes (Kodi documents its
   own software audio engine). Plain channel-based PCM does not carry the
   TrueHD Atmos object metadata; the soundbar cannot render those objects from
   ordinary PCM. Other apps, including Plex in some client paths, may request
   server-side transcoding instead. The toolkit's app-neutral Plex/Jellyfin
   policy therefore adds a Dolby Digital Plus compatibility track; FFmpeg's
   generated E-AC-3 is **not** a JOC/Atmos encode. That policy is a predictable
   server/client strategy, not a claim that every app fails to produce audio
   from TrueHD.
6. **The soundbar's Dolby MAT input table does not prove Chromecast MAT output.**
   Android's `AudioFormat` reference describes Dolby MAT as an HDMI format that
   can carry TrueHD, channel-based PCM, or PCM with object-audio metadata; this
   explains why MAT is not interchangeable with ordinary PCM. Hisense's AX3125H
   manual §8 maps MAT to `MPCM` and MAT-Atmos to `DOLBY ATMOS`, while its §1.3
   per-port matrix does not name MAT. Google's official G454V specification says
   nothing about MAT/MS12. The toolkit therefore marks MAT and standalone MPCM
   labels unknown, rather than native PCM. This keeps the TrueHD-to-plain-PCM
   Atmos-metadata loss distinct from a possible MAT route, which remains
   unverified on G454V. Do not transfer the newer Google TV Streamer/MS12
   behavior to this device without device-specific evidence.

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
* TrueHD has no supported G454V bitstream path, but some apps can decode it
  to PCM; that loses TrueHD Atmos object metadata. Plex/Jellyfin may instead
  request server-side transcoding depending on app capabilities. The toolkit's
  app-neutral profile prepares a Dolby Digital Plus compatibility track (not
  Atmos/JOC) once, without re-encoding video. WMA Pro and DTS Express also use
  that fallback because no guaranteed native route is modeled.
* DTS-HD MA/HRA and DTS:X audio ⇒ **not** a Direct-Play breaker: the player
  extracts the DTS core such a track carries and bitstreams that (subtlety
  2), so playback needs no server work and the toolkit leaves it alone by
  default. What is lost is the HD layer on the bar's panel (it reads `DTS`)
  and any DTS:X height objects — never the surround itself.
* AAC/FLAC/PCM audio ⇒ a compatible app may decode it locally to PCM; on the
  default wiring (soundbar HDMI IN) multichannel LPCM 5.1/7.1 is accepted by
  the bar. The app and active output route determine whether it is emitted as
  multichannel; this is not a blanket Android framework guarantee. Over the
  explicit ARC alternative this TV delivers multichannel content as stereo (§3).
* App/software-decoded PCM/FLAC **past the toolkit's 24-bit / 48 kHz envelope**
  ⇒ neither promised nor converted. `audio_standardizer.py` reports it for a
  human and touches nothing (subtlety 4). That envelope is chain-specific,
  not an official G454V specification.
* ALAC / WavPack ⇒ outside the toolkit's confirmed decoder set, with no
  supported bitstream fallback modeled here. They remain `AUDIO_UNKNOWN` —
  reported, never auto-touched, and credited with **zero** achievable channels
  in the keeper ranking, so an unverified path cannot outrank a track the
  toolkit does understand.
* Any **DTS**-labelled track reaches at most **5.1** on this chain, whether it is
  a plain core or the core extracted from a DTS-HD/DTS:X master (subtleties 1
  and 2), so `achievable_channels()` caps the whole family at
  `DTS_CORE_MAX_CHANNELS`. That cap is what keeps a file whose DTS:X-ness only
  appears in the *title* — where the classifier rightly refuses to read it —
  from being credited 7.1, outranking a real DD+ 5.1 Atmos track on layout, and
  getting that Atmos track stripped by the remux.

## 2 · The sound — Hisense AX3125H (3.1.2ch soundbar + wireless sub, 440 W)

From Hisense's product page and spec sheet (sources at the end):

| Property | Value |
| :--- | :--- |
| Configuration | 3.1.2 channels, 440 W, wireless 6.5" subwoofer, up-firing height drivers |
| HDMI | **1× HDMI IN (4K/HDMI 3D passthrough) + 1× HDMI OUT (eARC/ARC + CEC)** |
| Audio decoding | **Dolby Digital, Dolby Digital Plus / Atmos (DD+ JOC), TrueHD, DTS, DTS-HD**, Multichannel PCM |
| Other inputs | optical (TOSLINK), Bluetooth 5.3, USB, 3.5 mm AUX |

Why this is the load-bearing device: it lists input decoding for **Dolby
Digital, DD+/Atmos, TrueHD, DTS/DTS-HD and multichannel PCM**. Dolby Atmos on
the officially supported G454V path arrives as DD+ JOC — E-AC-3 with embedded
Atmos metadata — which is the streaming-compatible format the player is
specified to pass through. TrueHD differs: the bar can decode it, but the
G454V has no supported TrueHD bitstream path. An app with its own decoder may
instead send channel-based PCM, losing TrueHD Atmos object metadata; the
app-neutral Plex/Jellyfin profile prepares DD+ so it does not depend on one
app's local decode behavior. A DTS-HD MA/HRA or DTS:X stream, however, arrives
as the extracted DTS core (§1, subtlety 2) — real DTS the bar decodes, with
the HD layer the only casualty. These distinctions drive the toolkit: keep
the output that is measured/officially supported, and label app-specific
alternatives rather than assuming them. **On the default `soundbar-hdmi-in`
wiring, Dolby Digital (AC-3) and Dolby Digital Plus (E-AC-3) bitstream through
the entire chain** — player to bar, bar decodes. Under the explicit `tv-arc`
alternative that claim does *not* hold: the bitstream goes to the 2013 TV
first, and this TV offers PCM only for HDMI sources (§3), so the ARC path may
deliver it as stereo. See §5.

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

* on the default `soundbar-hdmi-in` wiring the bar accepts multichannel LPCM
  on HDMI IN. A compatible app may decode a 5.1/7.1 AAC, FLAC, Opus or PCM
  source to that format, with no server work; whether a specific app emits the
  required layout is app-/route-dependent (Android's built-in FLAC table alone
  does not establish multichannel playback); and
* on the `tv-arc` alternative the same track cannot arrive as surround even in
  principle, independently of what the 2013 Samsung's menu offers (§3). Two
  separate limits point the same way.

Note also that "Dolby Atmos – Dolby Digital Plus" is supported on HDMI IN.
The officially listed Atmos passthrough path is DD+ JOC, which reached the
bar intact on this chain. Dolby MAT output is a separate, unverified question;
no other Atmos transport is assumed. The bar's TrueHD and DTS-HD/DTS:X rows
are real, but what *this* player can deliver differs: the G454V has no
supported TrueHD bitstream path. Some apps can decode TrueHD to channel-based
PCM, which loses the TrueHD Atmos object metadata; the app-neutral toolkit
profile instead prepares E-AC-3, and that generated track is not Atmos/JOC.
A DTS-HD MA/HRA or DTS:X stream arrives as the extracted **DTS core** — the
bar decodes genuine DTS, its panel reads `DTS`, and the HD layer is what the
player cannot bitstream (§1, subtlety 2). The DTS-HD rows in this table
describe the bar's own decoders; the player's measured core fallback is why
they can be reached at all.

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
   Samsungs is widely reported). This confirms the TV's PCM-only output
   setting for HDMI sources, but does not by itself prove whether a Dolby/DTS
   bitstream received over HDMI is forwarded to ARC or decoded/downmixed first;
   that bitstream case has not been measured on this unit. App-decoded
   multichannel PCM returns as stereo PCM. This is an *input-side* limit, not
   a cable or soundbar problem: the same TV can bitstream Dolby Digital from
   its own tuner and apps, which is why the menu is content/input dependent.
3. **Consequence for the wiring.** On ARC, app-decoded 5.1+ PCM sourced
   from the Chromecast returns to the AX3125H as **stereo PCM** — the
   material limit the toolkit compensates for by baking AC-3 5.1 into
   multichannel-PCM movies (§5). Whether this TV forwards or downmixes a
   Dolby/DTS bitstream received on HDMI has not been measured, so the toolkit
   does not promise surround over ARC for those formats. Cabling the
   Chromecast through the soundbar's HDMI IN removes the PCM-return limit,
   and that is the wiring this chain now uses and the toolkit assumes by
   default. `tv-arc` remains supported (`--wiring tv-arc` /
   `ORGANIZE_PLAYBACK_WIRING=tv-arc`) for anyone who re-cables the old
   way, and re-enables AC-3 compensation for multichannel PCM-decoded sources.

**The chain is wired through the soundbar's HDMI IN** (`soundbar-hdmi-in`):
Chromecast → AX3125H HDMI IN, AX3125H HDMI OUT → TV. Everything in §5
onward assumes that wiring; §4 shows the ARC alternative and exactly what
flips if it is selected.

## 4 · The wiring decision

| Wiring | Video limit | Audio reality on 5.1+ content |
| :--- | :--- | :--- |
| **Chromecast → AX3125H HDMI IN → TV (`soundbar-hdmi-in`, DEFAULT — how this chain is cabled)** | 1080p60 chain-wide | the bar accepts official Dolby passthrough, measured DTS core, and LPCM; PCM needs a compatible app/route, while the toolkit prepares its app-neutral Dolby fallbacks |
| Chromecast → TV HDMI, TV --**ARC**→ AX3125H (`tv-arc`, explicit alternative) | 1080p60 | HDMI sources expose PCM-only output on this unit; app-decoded multichannel PCM returns as stereo PCM. Dolby/DTS bitstream forwarding is unmeasured; multichannel PCM-decoded movies become AC-3 transcode candidates |
| Chromecast → TV HDMI, TV --**optical**→ AX3125H | 1080p60 | same PCM-return limit as ARC (no CEC, slightly worse UX); Dolby/DTS bitstream forwarding remains unverified |

The toolkit's default is **`soundbar-hdmi-in`**: Chromecast into the
soundbar's HDMI IN, audio decoded by the bar, video passed through to the TV.
`tv-arc` is the supported *alternative* (`--wiring tv-arc` or
`ORGANIZE_PLAYBACK_WIRING=tv-arc`) and changes exactly one rule: a
multichannel AAC/FLAC/PCM movie that plays natively over HDMI IN becomes an
AC-3 transcode candidate on the TV's ARC/optical path, because the TV returns
app-decoded multichannel PCM as stereo PCM (§3). Dolby/DTS bitstream forwarding
from HDMI to this TV's ARC output remains unmeasured and is not promised.

Nothing about the *video* side changes between the two: both pass the picture
through at up to 1080p60, and the G454V's decode ceiling, HDR handling and
Dolby Vision gap are identical.

## 5 · The audio codec matrix — what actually plays where

Legend: ✅ documented/confirmed path · ⚠️ app-/route-dependent or measured
with a caveat · ❌ no supported path in this toolkit's app-neutral profile.
“Transcode-bound” below is a conservative Plex/Jellyfin policy, not a claim that
no third-party app can decode the source.

| Source track player | G454V player | over HDMI IN | over ARC/opt | verdict in the toolkit |
| :--- | :--- | :--- | :--- | :--- |
| Dolby Digital (AC-3) 5.1 | passthrough ✅ | ✅ decodes | ⚠️ this TV offers PCM only for HDMI sources, so ARC/opt may deliver it as stereo (§3) | kept as-is; the synthesis target only under `tv-arc` |
| Dolby Digital Plus (E-AC-3, incl. Atmos JOC) | passthrough ✅ | ✅ decodes | ⚠️ HDMI-source setting is PCM-only; whether DD+ JOC is forwarded or downmixed on ARC is unmeasured, so Atmos is not guaranteed (§3) | **goal format** — best possible track on this chain; what audio_standardizer synthesizes on the default wiring |
| AAC 5.1 / stereo | app decodes → PCM ⚠️ (depends on app/profile) | ✅ HDMI IN accepts LPCM | ⚠️ this TV's HDMI-source path is PCM-only | toolkit treats app-decoded PCM as usable inside 24-bit/48 kHz; multichannel output is app/route-dependent; stereo is the safe common case |
| FLAC / LPCM 5.1–7.1 (≤ 24-bit/48 kHz) | app/software decode → PCM ⚠️; Android built-in FLAC is mono/stereo only | ✅ Hisense manual lists 5.1/7.1 LPCM on HDMI IN | ⚠️ stereo-only on this TV | toolkit treats in-envelope app-decoded PCM as usable on default HDMI-IN wiring; the manual confirms sink acceptance, while app/route support must be verified; `tv-arc` uses AC-3 candidate for multichannel |
| App/software-decoded FLAC / PCM **past 24-bit / 48 kHz** | ⚠️ outside the toolkit's conservative envelope (not Google-published) | (moot — app/decoder behavior is the question) | ⚠️ | **reported for review, untouched**: no replacement or ranking effect; wider-rate behavior depends on app/route and is not established |
| MP3 / Opus / Vorbis / WAV | app/platform decode → PCM ⚠️ | ✅ HDMI IN accepts LPCM | ⚠️ stereo over this TV's return path | app/profile-dependent; toolkit boundary applies to decoded tracks |
| ALAC / WavPack | no decoder in the confirmed profile | (n/a) | (n/a) | **`unknown` — fail-closed**; never classed `decode-to-pcm` or credited a layout |
| base 5.1 **DTS core** | ⚠️ passthrough, chain-measured on this G454V → AX3125H HDMI-IN wiring (unofficial, not Google-certified); **5.1 is the format's ceiling, so nothing in the DTS family is ever credited wider** | ✅ decodes | ⚠️ the TV's PCM-only HDMI input behavior means a DTS return path is not confirmed (§3) | toolkit accepts it by default on the measured physical chain — Jellyfin Direct Play + the AX3125H DTS indicator; do not read this as Google certification or a TV-ARC measurement; `--no-dts-passthrough` converts it to Dolby |
| **TrueHD / TrueHD Atmos** | ❌ no supported G454V bitstream path; app-specific decode → channel PCM is possible | ✅ bar manual lists TrueHD input on HDMI IN | ❌ on TV path | app-neutral profile synthesizes E-AC-3 @ 640k; plain PCM loses TrueHD Atmos objects; generated E-AC-3 is not Atmos/JOC; any MAT route is unverified |
| **DTS-HD MA / HRA, DTS:X** | ⚠️ the G454V does not bitstream the HD layer, but **extracts the DTS core** on the measured HDMI-IN chain (§1) | ✅ decodes as DTS 5.1 (panel reads `DTS`) | ⚠️ the TV's PCM-only HDMI input behavior means DTS return is not confirmed (§3) | kept by default on the measured physical HDMI-IN chain — user-confirmed 2026-10; the HD layer is lost but surround reaches the bar as DTS 5.1. `--no-dts-passthrough` converts from the HD source instead. A 7.1 master reaches 5.1 (core ceiling) |
| **DTS-HD LBR / DTS Express** | ❌ no measured passthrough and no compatible core | (same) | ❌ | **Dolby Digital Plus @ 640k fallback** in this profile (AC-3 under `tv-arc`) |
| **Dolby MAT / MAT-Atmos / MPCM label** | unverified G454V output; not in Google's G454V spec | AX3125H manual §8 maps MAT to `MPCM` and MAT-Atmos to `DOLBY ATMOS`; §1.3 port matrix omits MAT | unverified | `AUDIO_UNKNOWN`, not treated as native or ordinary PCM; Google TV Streamer/MS12 reports do not establish G454V behavior |
| WMA Pro / WMA Lossless | ❌ no confirmed decoder or passthrough path in the target profile | (same) | ❌ | **Dolby Digital Plus @ 640k fallback**; `ffmpeg` converts it like any other master |
| unknown / unclassifiable | ❌ | ❌ | ❌ | **fail-closed: reported for a human, never auto-touched** — and it achieves *zero* channels in the keeper ranking, so it can never outrank a track the toolkit does understand |

**Read the "over ARC/opt" column as one limit, not one per row.** The TV's
PCM-only digital-audio setting for HDMI sources is user-confirmed (§3,
2026-09), and app-decoded multichannel PCM returns as stereo. That does not
settle whether an HDMI Dolby/DTS bitstream is forwarded or downmixed before
ARC/optical; this unit's bitstream case has not been measured, so AC-3/DD+
Atmos and DTS return are ⚠️ rather than ✅. (ARC as a *standard* carries PCM
2.0 / Dolby Digital 5.1 / DTS 5.1 — see the Samsung article in Sources; what
this TV does with an HDMI-source bitstream is a narrower, unresolved question.)

One open question this matrix does not settle, stated plainly rather than
papered over: under `tv-arc`, the toolkit bakes AC-3 into multichannel
PCM-decoded movies, but this does not prove that the TV will forward an AC-3
bitstream it receives on HDMI. Whether the PCM-only setting also downmixes a
received Dolby/DTS bitstream has not been measured on this unit — so the ⚠️
above means "cannot be relied on", not "never works". Until measured, do not
depend on ARC for surround here: the default wiring avoids the question by
never asking the TV to carry audio.

The keep-one-track ranking (`mkv_track_cleaner.py`) encodes exactly that
matrix — but it asks **which track can this movie END UP with?**, not "which
track plays right now?", and that question decides the order. The remux is
**irreversible**: a dropped track is gone for good, while a track that merely
needs converting can still be converted on any later run. Deleting a TrueHD
Atmos 7.1 master to keep a stereo AC-3 that plays today cannot be undone;
converting that master into a DD+ 5.1 bed can always be done. So:

1. **achievable layout** — how many channels the track reaches *on this chain*,
   via `playbackchain.achievable_channels()`. A chain-native track achieves the
   channels it carries — except the DTS-HD family, which is native *by core
   fallback* and therefore achieves its core's layout, at most 5.1
   (`playbackchain.DTS_CORE_MAX_CHANNELS`); a transcode-bound master achieves the channels
   `audio_standardizer.py` would bake into its replacement, which is **at most
   5.1 on either wiring** — the ceiling is ffmpeg's Dolby encoders, not the
   format (`playbackchain.FFMPEG_DOLBY_ENCODE_MAX_CHANNELS`); a wider master
   folds, it is never promised a bitstream that would fail to encode. An
   *unknown* track achieves nothing, because the toolkit fail-closes and never
   auto-touches one. This is the key that makes **TrueHD Atmos 7.1 worth more
   than an AC-3 2.0 that plays today** (6 beats 2). Tracks needing no encoder
   are untouched by the cap — a compatible app can emit FLAC 7.1 as LPCM 7.1
   over the bar's HDMI IN because the soundbar accepts multichannel LPCM; this
   is the toolkit's app-neutral ranking policy, not proof that every app emits
   that layout. App and active-route support must be verified, and other
   apps/routes may differ.
2. **band** — under the toolkit's app-neutral profile, bitstreamed Dolby,
   measured DTS/core fallback on the physical HDMI-IN chain, and app-decoded
   PCM when that app/route supports it are treated as settled; the policy places
   masters without a guaranteed route (TrueHD / DTS-HD LBR / WMA Pro) below
   them, and unknown last. At an **equal achievable layout**, a 5.1 AC-3 still
   beats a 5.1 TrueHD under this profile: some apps decode TrueHD to PCM, but
   that path is app-dependent and loses TrueHD Atmos objects, while Plex/Jellyfin
   may request server audio transcoding. A TrueHD 7.1 master can still outrank
   narrower PCM/AC-3 by achievable layout because its prepared replacement is
   5.1; this is a keep-the-master-before-remux rule, not a claim that every app
   transcodes TrueHD. "Highest sample rate wins" is still the wrong metric;
   the ranking applies the toolkit's stated profile and measured chain facts.
3. **Atmos** — a real DD+ Atmos track wins among equal layouts, because a 3.1.2
   bar with up-firing drivers is what it exists for. This needs no special case
   against 7.1 masters any more: both reach the same 5.1 bed, so the band above
   already keeps the Atmos track — correct, since no open encoder can synthesize
   Atmos at all (the JOC object metadata is gated behind a proprietary Dolby
   signature), so a real DD+ Atmos stream is irreplaceable, while 7.1's two
   extra channels drive nothing on a bar with no rear speakers.
4. **codec sub-tier** — the historical quality order refines the rest:
   DD+ (100) > DD (95) > base DTS core **and the DTS-HD family** (80 — the
   player bits a DTS core out of those either way) > lossless-decodable (66) >
   Opus (62) > other lossy (60), and below the band the formats with no native
   path: a lossless master (TrueHD/MLP, WMA Lossless, 34) > WMA Pro (32) > the
   rest (30). That lower half is also what picks the best *transcode source*:
   `audio_standardizer.py` ranks its own pool with this same function, so the
   two tools cannot disagree about which master to burn — and because the
   encoder cap makes every surround master land on the same 5.1 bed, the
   source is the **highest-tier** master rather than the widest: a lossless
   TrueHD outranks WMA Pro as *input*. (A DTS-HD master is no longer a burn
   candidate at all: it plays as its DTS core, and only
   `--no-dts-passthrough` spends it.)

The per-port table in §2 is what makes multichannel decoded audio first-class on
this wiring — LPCM 5.1/7.1 are supported on the bar's HDMI IN — which is why a
convertible master is worth keeping rather than merely tolerating. The same
logic now covers the DTS-HD family: the bar's DTS decoder (and the player's own
core extraction) means such a master needs no conversion to play, so keeping it
is free rather than a promise.

**Keeping a master is only correct if the app-neutral fallback gets converted.**
Because key 1 can retain a track without a guaranteed route in the toolkit's
Plex/Jellyfin profile, the cleaner names every such movie in
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
  *Surround sound*: **Auto** (official Dolby passthrough; the tested DTS/core
  behavior on this G454V HDMI-IN chain is unofficial and described in §1);
  *Audio output format*: Standard; turn *off* "match content frame rate" only
  if you see judder complaints — irrelevant to audio.
  If a DTS track ever arrives at the bar as stereo, check this menu and the
  player app's own audio settings. App-specific passthrough/decode options can
  change the route; only the measured behavior described in §1 is evidence for
  this chain. Apps that decode internally may hand the bar multichannel PCM,
  which HDMI IN accepts (§2), but app and route capabilities must be checked
  individually. Do not infer those options for every client from this toolkit's
  library policy.
* **AX3125H (default HDMI-IN wiring):** the Chromecast sits on the bar's
  **HDMI IN** socket, and the bar's **HDMI OUT (TV eARC/ARC)** goes to a TV
  HDMI input; source = **HDMI In**; EQ mode Movie; night mode off;
  subwoofer paired (auto). *(If you ever re-cable to the TV's ARC port
  instead: TV's designated HDMI (ARC) lead into the bar's HDMI OUT, source
  = **ARC**, and expect stereo PCM from app-decoded multichannel sources — see §3.)*
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
* **USER-CONFIRMED (2026-10) on the actual G454V in this chain:** DTS-HD MA,
  DTS-HD HRA and DTS:X are never passed through as HD bitstreams — the player
  *automatically extracts the backward-compatible DTS core* (5.1) every such
  bitstream carries and bitstreams that instead, so the AX3125H still decodes
  surround and its display reads **DTS**, not `DTS-HD`/`DTS:X`. No server
  transcode is involved; what is lost is the HD layer and any DTS:X object
  metadata. DTS-HD LBR / DTS Express has no core and is the exception. This is
  the observation that moved the DTS-HD family out of `transcode-bound` in
  `playbackchain` (it is bounded to this chain, like every other row here).
* **USER-CONFIRMED (2026-10) for this chain, codec by codec:** on the
  physical HDMI-IN route, the G454V passes AC-3 (5.1), E-AC-3 (7.1), E-AC-3
  JOC (Atmos, decoded by the bar's 3.1.2 array), and measured DTS core (5.1);
  the AX3125H manual separately confirms HDMI IN accepts LPCM 5.1/7.1. LPCM
  and FLAC decoding/output still depend on the app and active route. Android's
  built-in FLAC decoder table is mono/stereo up to 48 kHz; multichannel decode
  is app-/route-dependent. ALAC and WavPack are absent from the confirmed
  decoder set. The toolkit's 24-bit/48-kHz envelope is a conservative,
  chain-specific policy, not a Google-published or user-measured device limit.
  This is the reading §1's subtlety 4, §5's rows and
  `Player.max_decoded_sample_rate` / `Player.undecodable_codecs` rest on.
* Android Developers, *Supported media formats* — built-in FLAC is mono/stereo
  up to 48 kHz; no Google TV-specific G454V HDMI table is provided, and the page
  warns that other form factors vary.
  <https://developer.android.com/media/platform/supported-formats>
* Android Developers, *Audio capabilities for TV* — apps should query the active
  route because encoding, channel count and sample-rate support vary by device.
  <https://developer.android.com/training/tv/playback/audio-capabilities>
* Android Developers, *AudioFormat* reference — Dolby MAT is an HDMI audio
  format that can carry TrueHD, channel PCM or PCM with object-audio metadata;
  this API reference does not establish that G454V emits MAT.
  <https://developer.android.com/reference/android/media/AudioFormat>
* Kodi, *AudioEngine* — application-owned support for TrueHD and multichannel
  PCM; this does not certify every Kodi build or route on G454V.
  <https://kodi.wiki/view/AudioEngine>
* Plex, *Direct Play and Direct Stream* — client capability can result in
  audio-only transcoding while video is copied.
  <https://support.plex.tv/articles/200250387-streaming-media-direct-play-and-direct-stream/>
* Google's Cast media page contains 96-kHz FLAC support for Chromecast Audio /
  Google Home products, not Chromecast with Google TV HD.
  <https://developers.google.com/cast/docs/media>
* Google TV Streamer/MS12 article cited only as a distinct-device comparison,
  not as evidence of G454V Dolby MAT behavior.
  <https://www.flatpanelshd.com/news.php?subaction=showfull&id=1739522759>

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
  <https://files.hisense-usa.com/download/f25648883921b2fe>
* Hisense AX3125H user manual — "HDMI IN Socket: For connecting HDMI source
  devices, such as a DVD player, Blu-ray Disc™ player, or gaming console";
  "HDMI OUT (TV eARC/ARC) Socket: The port for connecting a TV"; input
  format/display table (Dolby MAT → MPCM; MAT-Atmos → DOLBY ATMOS). Section
  1.3's supported-input matrix does not list MAT by port, so neither table
  establishes that the G454V outputs MAT.
  <https://files.hisense-usa.com/download/f25648883921b2fe>

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
  Surround (up to 5.1); the standard capability list is not proof that this
  unit forwards those bitstreams from an HDMI input. Setting path for 2013-2014
  F/H series.
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
