# MichaelTV

**Current release: v1.1** — grab **`MichaelTV.exe`** from the
[Releases](https://github.com/michaeljdraper22-sketch/MichaelTVPlayer/releases)
page, or build it yourself with `build.bat` (see below).

(now branded **MichaelTV**; settings/logs still live in `%APPDATA%\MichaelTVPlayer`)
Built and tested on **Windows 11** — there is currently **no intent for
macOS or Android development**.

This project is specifically intended for use with an **8K Strong**
subscription. Login is currently only available through the Xtream Codes
API ("extreme code") credentials supplied by your provider.

A VLC-powered IPTV player for **Xtream Codes API** accounts — your own
personal "Smarters"-style player. **VLC (libvlc) is
at the heart of the project**: all playback, DVR/timeshift and recording run
through a single VLC connection.

## Standout features
- **Pause/Resume** live TV
- **Rewind (DVR / timeshift)** on live TV — always on, every channel
- **Record** live TV, movies and series
- **Direct download** of movies and series (no re-encode)
- **Unified subtitles** — live-TV closed captions and movies/series text
  tracks render through one app-styled overlay (`C` cycles them)

## Features
- **Dark mode by default** (theme applied on every launch) and a **black
  title bar** on Windows 10/11
- **Live TV, Movies (VOD) and Series** browsing with categories + live search
- **Now / Next EPG** (TV guide) for the current live channel
- **Real VLC playback** (libvlc) embedded in the window
- **Single stream connection** — playback, DVR and recording all
  run on ONE connection to your provider (works with 1-connection accounts).
- **On-video playback controls** — small white icons that float directly on
  the video (no background boxes at all):
  `⏪60 ⏪10 ⏯ ⏩10 | ⏮ · ⏭ LIVE · ⏺/⬇ | CC · ≋ · ⤢ · ⏱ | 🔊 ▁▁▁`
  - The row **compacts automatically** in half/quarter-screen windows
    (tighter spacing → no separators → shorter volume bar), and **clicking
    anywhere on the volume bar jumps the volume there**.
  - The controls (and the cursor) **appear when a stream starts / changes or
    the mouse moves**, and **hide after 4 s of no movement**. The whole
    on-video layer (controls, captions, notes) is an owned helper window:
    it floats above the player but **sinks below other applications'
    windows and the taskbar** — with the player in the background
    nothing of it ever paints over your other apps.
  - **Subtitles (CC)** — text subtitles render through the app's own
    overlay: live-TV **closed captions** (read from the always-on DVR
    buffer) and the embedded text tracks of movies & series (SRT, plus
    ASS/SSA flattened to plain text — those almost always carry a dozen+
    languages) share **one caption style**, set in the app. Live cues are
    timed by ARRIVAL against the app's own clock (the provider's PTS
    timeline drifts against VLC's, so CCExtractor's own timestamps are
    only used for relative spacing) — this keeps live captions in sync
    and lets a mid-session engage skip the back of the buffer instead of
    replaying it. The caption reader spins up in PARALLEL with the
    channel's startup fill, so captions are ready about when the video
    starts, and the arrival anchor is smoothed across cues so burst
    jitter never wiggles individual captions. The button
    lights blue while a track is active; **C** cycles
    Off → English (when the stream has an English track) → the remaining
    tracks, and the choice is sticky by language
    across channel changes. English is the default language throughout:
    a file whose text tracks are all labeled non-English never shows a
    foreign track automatically. Turning captions off keeps the live reader
    alive, so turning them back on is instant. Image-based tracks
    (DVB/PGS subtitles) are
    handed to VLC's own renderer instead — bitmaps can't be restyled.
    Streams without subtitle tracks keep the button disabled.
  - **Subtitle settings…** (bottom of the CC track menu) — the **delay
    applies instantly** (± 0.25 s per click), and while the app overlay
    renders the captions the visual style (font, size, vertical position,
    text / background / outline colors, background opacity, outline
    thickness) applies **live as you adjust it** — move the position
    slider or pick a color and the captions on the video follow
    immediately, no OK in between; a preview line stands in during quiet
    moments so the style is always visible. OK just closes; **Cancel
    reverts the style** to its dialog-open state. For VLC-rendered image
    tracks a style change restarts playback in place when you OK
    (movies resume where they were; live
    restarts at the edge, the DVR buffer with it). Size defaults to a
    concrete 40 px; set 0 for Auto (scales with the video height).
  - **Subtitles park in the black bar** (windowed playback) — when the
    video letterboxes with empty black space below the picture, the
    captions sit inside that bar (centered in it, like a pro player)
    instead of over the picture's bottom edge; fullscreen keeps the
    classic over-the-video placement, and a manual vertical-position
    raise still lifts them out. Turn it off with **"Use the black bar
    below the video"** in Subtitle settings. VLC-rendered image tracks
    always render over the picture (they can't be moved at runtime).
  - **Popup cards, one style** — CC, audio, scale and speed all open the
    same Stremio-style dark-glass picker card above their button,
    wherever the window sits on screen (the cards live in the on-video
    overlay and anchor to the button itself; the old QMenus mislanded
    far to the right once the window was moved or snapped). **Clicking
    the same button again closes its card**, Esc or a click outside
    works too, and long lists (the speed ladder) scroll inside the card.
  - **Audio tracks** (waveform icon) — opens the **track picker card**
    over the video (Stremio-style: dark glass panel, one row per
    language with a checkmark on the selection, Auto first with its
    "English when available" sub-label, dimmed loading state that
    fills itself the moment the track list arrives). Programs carried
    in several languages list their dubs here; **English is the
    default**: a stream
    with an English track selects it automatically (the moment the
    player has parsed the track list — a few seconds into a movie's
    load), streams without one keep the provider's own audio (audio is
    never muted by the picker). **A** cycles Auto (English) → tracks,
    **Esc** or a click outside closes the card, and a manual pick lasts
    for the current
    program only — every new program starts back at Auto/English.
  - **Playback speed** (speedometer; live rewind, movies & series): 0.125× … 4×
    (VLC mutes audio above ~4×, so the list stops there). Fast-forward
    drops back to 1× automatically at the live edge.
  - **Jump to beginning** (⏮, next to LIVE) rewinds to the start of the DVR
    buffer, or restarts a movie/episode; **LIVE** (⏭) jumps to the live
    edge — in plain live mode too (it resumes at the edge, and reconnects
    the stream if a long pause killed it). For movies/series it skips to
    the end.
  - **Video scaling** (⤢): Fit (default) / Stretch / Crop to fill.
  - **Time scrubber** with current time on the left and total time on the
    right — appears in DVR / Record mode (and for movies). **Click anywhere
    on it to jump straight to that point** (hold to fine-tune); the handle
    swells when the cursor is over it. Timestamps are tracked locally and
    tick smoothly even when the stream's own timestamps misbehave.
  - Every button can be switched on/off in **Settings ▸ Playback controls…**
- **Live TV always runs in DVR chase mode** — every channel is recorded to
  a short-term buffer that you watch a few seconds behind live (default 5 s,
  Settings ▸ Live delay). A small "Buffering…" pill covers the couple of
  extra seconds a channel takes to start; pause is then flawless, rewind is
  an instant seek, and **LIVE** jumps to the newest recorded moment (a safe
  5 s behind the write head, which also keeps captions flowing there). The
  buffer is deleted when you leave the channel; if the recorder can't get
  data (blocked provider, network), playback falls back to the direct live
  stream for that channel.
- **Record (⏺)** — the red button — saves the current stream to a folder of
  your choice (Settings ▸ Recording folder). Runs through the same single
  connection as the live buffer — and live recordings are
  scrubbable while they record. Recordings are kept on disk.
- **Profanity filter** (Settings ▸ Profanity filter…, off by default) —
  mutes the audio during profanity, and when the app-rendered captions are
  showing, masks the matched words on screen too (`hell` → `****`) — one
  word list for everything. **Live TV** reads the channel's
  **closed captions** from the always-on live buffer (playback runs your
  Live-delay setting behind live, at least 5 s; captions physically trail
  speech).
  **Movies & series** work with no playback delay: the app relays the
  single provider connection through localhost, peels the embedded
  subtitle text from the local cache and mutes ahead of the dialogue
  (seeks stay smooth — one connection restart per jump). The relay's
  startup (seek-index prefetch, track discovery) runs in the background
  so opening a movie doesn't stall the app; a resume/engage opens
  exactly one provider stream, landed by VLC's own seek at the resume
  position; and switching subtitle language re-anchors the reader
  at the playback position immediately (the earlier history is
  backfilled in the background). Subtitles do
  not need to be on. Editable word list with three match
  levels per word (**Exact**: `dog` → `*** in the doghouse`;
  **Partial**: `*** in the ***house`; **Whole**: `*** in the
  ********`), mute padding before/after each word, a **mute lead**
  (live-caption lag compensation) and a sync offset. The **Mute the
  whole line** option keeps the audio muted for the entire time a
  filtered word is anywhere in the subtitle (instead of only around
  the word itself — catches everything at the cost of muting more),
  and **Reset all settings** restores the factory defaults (enable
  state, timing, whole-line mode and the word list) in one click.
  No external tools
  are needed for movies & series; live TV needs **CCExtractor**, which
  ships bundled with the app (an installed copy is used instead when
  present). Coverage: live CC and VOD text tracks (SRT/ASS-style);
  image subtitles (PGS/DVB) carry no text and aren't filtered.
- **Save favorites** and keep your own **custom stream URLs**
- **Recently played** list + account status/expiry in the status bar
- **Zen mode (View ▸ Hide controls, or ≡ button / H)** — hides the menu bar,
  status bar, channel list AND the control bar so the video fills the window;
  move the cursor to the bottom to reveal controls.
- **Resizable window + Windows Snap** — very small minimum size so you can snap
  to halves/quarters (up to 4) or tile next to another player (e.g. VLC).
- **Fullscreen** with auto-hiding controls — double-click or **F** toggles;
  **Esc** exits.
- **🌍 Countries filter** (Countries ▸ Filter by Country) — one tab each for
  **Live TV, Movies and Series**: tick the countries/regions you want;
  saved automatically (survives restarts) and applied to both the category
  list and the full "All" view.
- **Adjustable network cache** — Playback ▸ Network cache size (0–50,000 ms)
- **Enter** plays the selected item; **mouse wheel** over the video = volume

## Requirements
- **Windows 10/11**
- **VLC media player** — only needed as a **one-time source** for the private
  runtime (see *VLC isolation* below); the app does not use your installed
  VLC at run time. https://www.videolan.org/
- Python is **not** needed to run the packaged app — only to build it or
  run from source.

## VLC isolation (important)
MichaelTV ships a **private VLC runtime** in `vlc\` (a full copy of a VLC
install: `libvlc.dll`, `libvlccore.dll`, `plugins\`) — in the project root
for source runs, and in `dist\vlc\` next to the built exe (`build.bat`
copies it automatically). The app loads DLLs and plugins **only** from that
copy and passes `--ignore-config` to every `vlc.Instance`, so it **never
reads or writes** your shared `%APPDATA%\vlc` config. Whatever the app does
(caching, subtitle rendering, decoder settings) cannot affect the VLC media
player installed on the machine — and vice versa. If the `vlc\` folder is
missing, the app falls back to the installed VLC (still with
`--ignore-config`). To refresh the private copy, delete `vlc\`, re-copy any
VLC install into it, then run `vlc\vlc-cache-gen.exe vlc\plugins`. Verify
with `.venv\Scripts\python.exe tools\verify_vlc_isolation.py`.

## Run (packaged app — recommended)
Download **`MichaelTV.exe`** from the
[Releases](https://github.com/michaeljdraper22-sketch/MichaelTVPlayer/releases)
page (a desktop shortcut is created by `tools\make_shortcut.ps1`).
No console window, no Python install, no venv — just the app.

Build it yourself by double-clicking **`build.bat`** (needs Python 3.9+,
64-bit). The build bundles the private VLC runtime into `dist\vlc\`, so the
exe is fully self-contained and never touches the installed VLC.

## Run (from source, for development)
Double-click **`run.bat`**. It creates a virtual environment, installs
dependencies, and launches the app.

On first launch you'll be asked for your Xtream details:
- **Server URL** — the portal URL, e.g. `http://provider.net:8080`
- **Username**
- **Password**

Click **Test Connection** to verify before saving. Your details are stored in
`%APPDATA%\MichaelTVPlayer\settings.json`.

### Manual acceptance — unified subtitles
Before shipping changes to the caption pipeline, run this checklist by hand
(automated counterparts: `test_caption_overlay.py` and
`tools\e2e_unified_captions.py`):

1. **One size & position across sources.** Leave the window at one size and
   play, in turn, a **16:9 live channel**, an **SRT-subtitled movie** and an
   **ASS-subtitled movie** (e.g. from an anime category), turning captions on
   in each. In all three the captions must show the **same size and bottom
   position relative to the picture** — including a letterboxed (e.g. 2.35:1)
   movie, where captions shrink and rise with the picture, not the window.
2. **Style changes apply live.** With captions showing in any of the
   three sources: CC menu ▸ **Subtitle settings…** — drag the vertical
   position slider, change font, size, colors, background, outline. The
   captions on the video follow **while you adjust** — no restart, no
   rebuild, no OK needed, playback position unchanged; Cancel puts the
   style back.
3. **Profanity masks & mutes everywhere.** Enable the filter with a word
   that actually occurs, and confirm in **all three** sources that the word
   is masked on screen (`hell` → `****`) **and** the audio mutes around it.

## Keyboard shortcuts
| Key | Action |
|-----|--------|
| Space | Pause / Resume |
| ← / → | Seek −10s / +10s |
| ↑ / ↓ | Seek −60s / +60s |
| M | Mute / Unmute |
| C | Cycle subtitles (Off → English first → remaining tracks) |
| A | Cycle audio track (Auto (English) → tracks) |
| Mouse wheel (over video) | Volume |
| Double-click video | Toggle fullscreen |
| ● LIVE button | Jump back to the live edge / end of the movie |
| F / Esc | Toggle fullscreen (Esc always exits) |
| Enter | Play selected channel/movie/episode |
| F5 | Reload channel lists |

## How live rewind (timeshift) works
Live TV **always** plays through a DVR chase buffer on a single connection:
the stream is recorded to a short-term buffer and you watch it **a few
seconds behind live** (default 5 s, Settings ▸ Live delay) — that trade buys
a flawless pause, instant rewinds, the profanity filter's caption cushion and
unified app-rendered captions. Channels may take a couple of extra seconds to
start while the buffer fills (a small "Buffering…" pill covers it). The red
**● LIVE** button jumps back to the live edge (a safe 5 s behind the write
head). The buffer is deleted when you leave the channel or close the app
(buffers stranded by a crash are cleaned out on the next launch); if the
recorder can't get data at all, playback falls back to the direct live
stream for that channel.
**REC** is unchanged: it saves a permanent recording (kept on disk)
through the same single connection.

## Adding your own channels
Channels ▸ **Add custom channel…** lets you add any stream URL
(HLS `.m3u8`, MPEG-TS, RTSP, etc.). These appear under the **➕ Custom** tab.

## Movies & Series notes
- For movies and episodes the **REC button is replaced by a Download
  button**: it saves the original file straight into your recordings
  folder (no re-encode). Playback controls work like on live TV — scrub,
  **⏮ restart**, **⏭ skip to the end** and the **speed** button are all
  enabled, and recording works like on live TV.
- The **Movies** and **Series** tabs open in the **first category** (fast).
  Choose **All** in the dropdown to load the entire library — with big
  providers that's a huge download, so it can take a minute (the status
  line warns you while it loads).
- Clicking a **series** opens an episodes window (seasons expand in order,
  episodes sorted by number); double-click or right-click ▸ Play to watch.
  Whole series and individual episodes can be favorited like channels.
- All stream requests identify themselves to the provider as
  `MichaelTVPlayer/1.0` — some CDNs block unknown player agents, which used
  to make movies fail to start.

## Troubleshooting
- **"VLC is required" / no video** — install VLC; use 64-bit VLC for 64-bit Python.
- **"Invalid username or password"** — make sure the Server URL is just
  `host:port` (no extra path), and use the Xtream API credentials.
- **Empty channel list** — use **File ▸ Reload all lists** (F5) and check connectivity.
- **Reporting a bug / freeze** — attach the log file
  `%APPDATA%\MichaelTVPlayer\player.log` (recreated on every launch; it holds
  the last events before the problem).
