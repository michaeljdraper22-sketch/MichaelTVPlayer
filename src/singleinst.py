"""Single-instance guard + argument forwarding.

Before this, a second MichaelTV launch (e.g. Windows opening a Stremio
playlist.m3u with us while the app is already running) spawned a fully
independent second process — second libVLC, second recorder, both writing
settings.json. Now the first instance owns a named local socket (a
Windows named pipe under the hood); later launches send it their
command-line arguments over that socket and exit. The owner plays/raises
via MainWindow.handle_handoff().
"""

import json
import logging

from PyQt5 import QtCore, QtNetwork

log = logging.getLogger("mtp.singleinst")


class SingleInstance(QtNetwork.QLocalServer):
    """Owns the app's local socket once (QLocalServer/QLocalSocket live
    in QtNetwork in PyQt5); ``received`` fires on the GUI thread with the
    args a second launch forwarded to us."""

    received = QtCore.pyqtSignal(list)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name

    def forward_if_running(self, args) -> bool:
        """If another instance already owns the socket, hand it ``args``
        and return True (the caller should exit). Otherwise take
        ownership of the socket and return False."""
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(self._name)
        if sock.waitForConnected(1500):
            payload = json.dumps([str(a) for a in (args or [])])
            try:
                sock.write(payload.encode("utf-8") + b"\n")
                sock.waitForBytesWritten(1500)
            except Exception:  # noqa: BLE001
                pass
            sock.disconnectFromServer()
            log.info("single-instance: forwarded %d args to the running "
                     "instance", len(args or []))
            return True
        # Not running — or a stale socket from a crashed run is blocking
        # listen(); clearing it is exactly what the stale case needs.
        QtNetwork.QLocalServer.removeServer(self._name)
        if not self.listen(self._name):
            log.warning("single-instance: listen failed (%s) — continuing "
                        "without the guard", self.errorString())
            return False
        self.newConnection.connect(self._on_connection)
        return False

    def _on_connection(self):
        conn = self.nextPendingConnection()
        if conn is None:
            return
        data = bytearray()
        try:
            while b"\n" not in data:
                chunk = conn.readAll()
                if chunk:
                    data.extend(chunk)
                if b"\n" in data:
                    break
                if not conn.waitForReadyRead(2000):
                    break
            line = bytes(data).split(b"\n", 1)[0]
            conn.disconnectFromServer()
            args = json.loads(line.decode("utf-8", "replace") or "[]")
            if isinstance(args, list) and args:
                self.received.emit([str(a) for a in args])
        except Exception as exc:  # noqa: BLE001
            log.warning("single-instance: bad payload: %r", exc)
        conn.deleteLater()
