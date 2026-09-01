"""Watch the browser's Downloads folder for Stremio playlist handoffs.

Why this exists (2026-09): on Windows, Stremio's stream-list handoff —
"Play in external player: M3U Playlist" — is just a browser download:
clicking a stream saves a tiny ``playlist.m3u`` (one stream URL, see
stremio-core's ``get_m3u_data_uri``) into the Downloads folder. Getting
that file to *open* in MichaelTV through Windows is a dead end: Win11's
``.m3u`` UserChoice is tamper-locked (UserChoiceLatest/UCPD) and owned
by the Store Media Player here, and the browser's download bar needs a
manual click anyway.

But MichaelTV is normally already running while the user browses Stremio
— so instead of waiting for the OS to hand us the file, we take it: the
moment a Stremio-shaped playlist lands in Downloads, it plays here. No
file-association fight, no download-bar click, works identically for
Stremio web (any browser) and the Stremio desktop app (WebView2 saves
to the same folder).

Matching is deliberately strict — filename ``playlist*.m3u(.m3u8)``
(the exact name Stremio's download anchor uses, plus the dedupe
variants Chrome/Edge/Firefox append), small file, and a parseable
stream URL inside — so unrelated IPTV playlists a user might download
are left alone. Consumed files are renamed ``*.mtpdone`` so they can't
replay and the plain name stays free for the next handoff.
"""

import logging
import os
import re

from PyQt5 import QtCore

from . import stremio

log = logging.getLogger("mtp.watchfolder")

# Stremio's download anchor is always named "playlist.m3u"; browsers
# dedupe as "playlist (1).m3u" (Chrome/Edge) / "playlist-1.m3u" (Firefox)
_NAME_RE = re.compile(
    r"^playlist(?: \(\d+\))?(?:-\d+)?\.m3u8?$", re.IGNORECASE)
_MAX_BYTES = 64 * 1024      # a Stremio handoff is ~100 bytes
_RESCAN_MS = 600           # let the download's final rename settle
_MAX_ROUNDS = 4            # ~2.5s total before giving up on a file


def downloads_dir() -> str:
    """The user's real Downloads folder (browsers' default save target)."""
    try:
        path = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.DownloadLocation)
        if path and os.path.isdir(path):
            return os.path.normcase(os.path.normpath(path))
    except Exception:  # noqa: BLE001
        pass
    return os.path.normcase(os.path.normpath(
        os.path.join(os.path.expanduser("~"), "Downloads")))


class DownloadsWatcher(QtCore.QObject):
    """Plays Stremio playlist downloads through ``handoff`` (emit payload
    is the argv-style list ``[playlist_path]`` — exactly what
    ``MainWindow.handle_handoff`` already consumes)."""

    handoff = QtCore.pyqtSignal(list)

    def __init__(self, directory: str = "", parent=None):
        super().__init__(parent)
        self.dir = os.path.normcase(os.path.normpath(directory)) \
            if directory else downloads_dir()
        self._seen = {}          # normcased path -> (mtime_ns, size)
        self._rounds = 0
        self._watcher = QtCore.QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._rescan_timer = QtCore.QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(_RESCAN_MS)
        self._rescan_timer.timeout.connect(self._rescan)
        self._started = False

    # ---- lifecycle ----

    def start(self) -> bool:
        """Baseline-scan (existing files are marked seen, never played)
        and begin watching. Returns False if the folder can't be watched."""
        if self._started:
            return True
        if not os.path.isdir(self.dir):
            log.warning("watchfolder: downloads folder not found: %s",
                        self.dir)
            return False
        base = 0
        for name in os.listdir(self.dir):
            if _NAME_RE.match(name):
                st = self._stat(
                    os.path.normcase(os.path.join(self.dir, name)))
                if st:
                    self._seen[os.path.normcase(
                        os.path.join(self.dir, name))] = st
                    base += 1
        if base:
            log.info("watchfolder: %d existing playlist file(s) marked "
                     "seen (baseline, not replayed)", base)
        if not self._watcher.addPath(self.dir):
            log.warning("watchfolder: cannot watch %s", self.dir)
            return False
        self._started = True
        log.info("watchfolder: watching %s for Stremio playlist "
                 "downloads", self.dir)
        return True

    def stop(self) -> None:
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        self._rescan_timer.stop()
        self._started = False

    # ---- internals ----

    @staticmethod
    def _stat(path: str):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _on_dir_changed(self, _path: str) -> None:
        if not self._rescan_timer.isActive():
            self._rescan_timer.start()

    def _rescan(self) -> None:
        """One pass over new playlist-shaped files. "New" means the
        name matches AND (mtime, size) changed — so a reused filename
        (the .mtpdone rename frees Stremio's playlist.m3u name) still
        hands off. Unreadable or mid-write files earn the next round;
        after _MAX_ROUNDS the candidates are marked seen so a bad file
        can't loop forever."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        candidates = []
        for name in names:
            if not _NAME_RE.match(name):
                continue
            path = os.path.normcase(os.path.join(self.dir, name))
            st = self._stat(path)
            if st is None or self._seen.get(path) == st:
                continue
            candidates.append((path, st))
        if not candidates:
            self._rounds = 0
            return
        self._rounds += 1
        retry = False
        for path, st in candidates:
            if st[1] > _MAX_BYTES:
                self._seen[path] = st              # not a Stremio handoff
                continue
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as f:
                    text = f.read(_MAX_BYTES)
            except OSError:
                if self._rounds < _MAX_ROUNDS:
                    retry = True                    # still being written
                    continue
                self._seen[path] = st
                continue
            url = stremio.parse_m3u(text) if text.endswith("\n") else ""
            if url:
                self._consume(path, url, st)
            elif self._rounds < _MAX_ROUNDS:
                retry = True                        # partial so far
            else:
                log.info("watchfolder: %s has no stream URL — ignored",
                         os.path.basename(path))
                self._seen[path] = st
        if retry and not self._rescan_timer.isActive():
            self._rescan_timer.start()
        elif not retry:
            self._rounds = 0

    def _consume(self, path: str, url: str, st) -> None:
        self._seen[path] = st
        log.info("watchfolder: Stremio playlist download picked up: "
                 "%s -> %s", os.path.basename(path), url)
        # the URL (not the path) rides the signal: the file is renamed
        # right after, and MainWindow.handle_handoff takes URLs too
        self.handoff.emit([url])
        try:
            os.rename(path, path + ".mtpdone")    # can't replay; name
        except OSError as exc:                    # freed for the next one
            log.info("watchfolder: rename of %s failed (%r) — kept, "
                     "marked seen", os.path.basename(path), exc)
