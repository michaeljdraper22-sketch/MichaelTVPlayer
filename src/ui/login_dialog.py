"""Account setup / connection test dialog."""

from PyQt5 import QtWidgets

from ..xtream import XtreamClient, normalize_server_url
from .worker import AsyncRunner


class LoginDialog(QtWidgets.QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("MichaelTV — Account Setup")
        self.setMinimumWidth(480)

        self.runner = AsyncRunner()
        self.runner.finished.connect(self._on_test)

        self.server_edit = QtWidgets.QLineEdit(config.server_url)
        self.server_edit.setPlaceholderText("http://your-provider.net:8080")
        self.user_edit = QtWidgets.QLineEdit(config.username)
        self.pass_edit = QtWidgets.QLineEdit(config.password)
        self.pass_edit.setEchoMode(QtWidgets.QLineEdit.Password)

        form = QtWidgets.QFormLayout()
        form.addRow("Server URL:", self.server_edit)
        form.addRow("Username:", self.user_edit)
        form.addRow("Password:", self.pass_edit)

        self.test_btn = QtWidgets.QPushButton("Test Connection")
        self.test_btn.clicked.connect(self._test)

        self.skip_btn = QtWidgets.QPushButton("Skip — no IPTV")
        self.skip_btn.setToolTip(
            "Use MichaelTV without an IPTV subscription: open local video "
            "files (File ▸ Open video…), play custom stream URLs, and keep "
            "the Stremio handoff. An account can be added any time via "
            "File ▸ Account…")
        self.skip_btn.clicked.connect(self._skip)

        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(self.test_btn)
        btns.addWidget(self.skip_btn)
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(self.save_btn)

        self.status = QtWidgets.QLabel(
            "Enter your Xtream account details — or Skip to use MichaelTV "
            "without IPTV (local video files, custom channels, Stremio).")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(48)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btns)
        layout.addWidget(self.status)

    @staticmethod
    def configure(config, parent=None) -> "LoginDialog":
        return LoginDialog(config, parent)

    def _client_creds(self):
        # normalize_server_url: pastes from chat/email can carry newlines
        # and signature dashes — one such paste reached DNS as
        # "host%0a%0a%0a-----" (GitHub issue #5)
        server = normalize_server_url(self.server_edit.text())
        return server, self.user_edit.text().strip(), self.pass_edit.text()

    def _test(self):
        server, user, pw = self._client_creds()
        if not (server and user and pw):
            self.status.setText("Please fill in all fields.")
            return
        self.test_btn.setEnabled(False)
        self.status.setText("Connecting…")
        client = XtreamClient(server, user, pw)
        self.runner.run(client.authenticate)

    def _on_test(self, result):
        self.test_btn.setEnabled(True)
        ok, val = result
        if ok == "ok":
            info = val
            self.status.setText(
                "✓ Connected as "
                f"{info.username or self.user_edit.text()}\n"
                f"Status: {info.status}    "
                f"Active: {info.active_cons}/{info.max_connections}    "
                f"Expires: {info.exp_date or '—'}"
            )
        else:
            self.status.setText(f"✗ {val}")

    def _save(self):
        server, user, pw = self._client_creds()
        if not (server and user and pw):
            QtWidgets.QMessageBox.warning(
                self, "Missing info",
                "Please fill in server, username and password.",
            )
            return
        self.config.data["server_url"] = server
        self.config.data["username"] = user
        self.config.data["password"] = pw
        # real credentials retire the skip: the account gate stays closed
        # on future launches
        self.config.data["setup_skipped"] = False
        self.config.save()
        self.accept()

    def _skip(self):
        """Account-less mode: no IPTV lists, everything else plays. The
        flag is what keeps the first-run gate from reappearing."""
        self.config.data["setup_skipped"] = True
        self.config.save()
        self.accept()
