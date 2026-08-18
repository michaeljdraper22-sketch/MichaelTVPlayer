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
- **Rewind (DVR / timeshift)** on live TV
- **Record** live TV, movies and series
- **Direct download** of movies and series (no re-encode)
- **Built-in subtitles** for movies & series (the stream's own tracks,
  with language selection — `C` cycles them)

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
  `⏪60 ⏪10 ⏯ ⏩10 | ⏮ · ⏭ LIVE · DVR/⬇ · ⏺ | CC · ⤢ · ⏱ | 🔊 ▁▁▁`
  - The row **compacts automatically** in half/quarter-screen windows
    (tighter spacing → no separators → shorter volume bar), and **clicking
    anywhere on the volume bar jumps the volume there**.
  - The controls (and the cursor) **appear when a stream starts / changes or
    the mouse moves**, and **hide after 4 s of no movement**.
  - **Subtitles (CC)** — plays the stream's own embedded subtitle tracks.
    Movies and series almost always carry a dozen+ SRT language tracks
    (English, Spanish, …); some live channels carry DVB subtitles. The
    button lights blue while a track is active; **C** cycles
    Off → track 1 → track 2 → …, and the choice is sticky by language
    across channel changes. Streams without subtitle tracks keep the
    button disabled. (DVR-chase playback uses a re-muxed buffer, where
    subtitle tracks generally don't survive — subtitles work best in
    normal live playback and movies/series.)
  - **Playback speed** (speedometer; DVR mode, movies & series): 0.125× … 4×
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
- **DVR mode (opt-in per channel)** — press **DVR** and the stream is recorded
  to a short-term buffer that you watch a few seconds behind live (Settings ▸
  Live delay). The buffer first fills for about the length of that delay (a
  small "DVR …s buffered" pill on the video shows the progress), then playback
  starts a safe distance behind the live edge. Pause is flawless, rewind is an
  instant seek, and **LIVE** jumps to the newest recorded moment. Resets to
  plain live on every channel change; the buffer is deleted when you leave
  the channel.
- **Record (⏺)** — the red button — saves the current stream to a folder of
  your choice (Settings ▸ Recording folder). Runs through the same single
  connection, even together with DVR mode — and live recordings are
  scrubbable while they record. Recordings are kept on disk.
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
- **VLC media player** — the bitness **must match** (64-bit recommended):
  https://www.videolan.org/
- Python is **not** needed to run the packaged app — only to build it or
  run from source.

## Run (packaged app — recommended)
Download **`MichaelTV.exe`** from the
[Releases](https://github.com/michaeljdraper22-sketch/MichaelTVPlayer/releases)
page (a desktop shortcut is created by `tools\make_shortcut.ps1`).
No console window, no Python install, no venv — just the app.

Build it yourself by double-clicking **`build.bat`** (needs Python 3.9+,
64-bit). The exe still needs VLC installed — or drop a full `vlc\` folder
(copy `libvlc.dll`, `libvlccore.dll` and `plugins\`) next to the exe to
make it fully self-contained.

## Run (from source, for development)
Double-click **`run.bat`**. It creates a virtual environment, installs
dependencies, and launches the app.

On first launch you'll be asked for your Xtream details:
- **Server URL** — the portal URL, e.g. `http://provider.net:8080`
- **Username**
- **Password**

Click **Test Connection** to verify before saving. Your details are stored in
`%APPDATA%\MichaelTVPlayer\settings.json`.

## Keyboard shortcuts
| Key | Action |
|-----|--------|
| Space | Pause / Resume |
| ← / → | Seek −10s / +10s |
| ↑ / ↓ | Seek −60s / +60s |
| M | Mute / Unmute |
| C | Cycle subtitles (Off → track 1 → track 2 → …) |
| Mouse wheel (over video) | Volume |
| Double-click video | Toggle fullscreen |
| ● LIVE button | Jump back to the live edge (DVR mode only) |
| F / Esc | Toggle fullscreen (Esc always exits) |
| Enter | Play selected channel/movie/episode |
| F5 | Reload channel lists |

## How DVR (timeshift) works
Playback is **live by default** — the stream plays directly, no delay, on a
single connection. Opt in per channel with the **DVR** button (or Playback ▸
DVR mode): the stream is recorded to a short-term buffer that you watch **a few
seconds behind live** (Settings ▸ Live delay). Pause is flawless, rewind is an
instant seek, and the red **● LIVE** button — the one live affordance, enabled
only in DVR mode — jumps back to the live edge. DVR (and REC) reset to OFF on
every channel change; the buffer is deleted when you leave the channel or close
the app (buffers stranded by a crash are cleaned out on the next launch).
**REC** is unchanged: it saves a permanent recording (kept on disk)
through the same single connection, even together with DVR mode.

## Adding your own channels
Channels ▸ **Add custom channel…** lets you add any stream URL
(HLS `.m3u8`, MPEG-TS, RTSP, etc.). These appear under the **➕ Custom** tab.

## Movies & Series notes
- For movies and episodes the **DVR button is replaced by a Download
  button**: it saves the original file straight into your recordings
  folder (no re-encode). Playback controls work without DVR — scrub,
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
