# -*- coding: utf-8 -*-
"""Throwaway P0 diagnostic: attach a file handler to mtp.sync and run the
adversarial harness (default: scenario a only). Not part of any suite."""
import logging
import logging.handlers
import os
import sys

os.environ["MTP_SYNC_LOG"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

h = logging.handlers.RotatingFileHandler(
    "p0_sync_diag.log", maxBytes=32 * 1024 * 1024, backupCount=1,
    encoding="utf-8")
h.setFormatter(logging.Formatter("%(message)s"))
lg = logging.getLogger("mtp.sync")
lg.setLevel(logging.DEBUG)
lg.propagate = False
lg.addHandler(h)
lg.info("=== p0 diag harness run ===")

import test_sync_adversarial as harn  # noqa: E402

rc = harn.main()
lg.info("=== p0 diag end rc=%s ===", rc)
sys.exit(rc)
