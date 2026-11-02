# -*- coding: utf-8 -*-
"""Dump MKV subtitle-track metadata INCLUDING FlagForced from a URL head.

Shows whether the provider's files set the real FlagForced element or
only carry 'forced' in the track Name — decides how selection must
detect forced tracks. Headless, no player.

Run: .venv\\Scripts\\python.exe tools\\dump_track_flags.py <url>
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from src.mkv_subs import (  # noqa: E402
    MkvSubParser, _vint_size, _FLAG_FORCED, _FLAG_DEFAULT)

HEAD = 1 << 19


class FlagParser(MkvSubParser):
    def _parse_track_entry(self, body):
        # capture FlagForced/FlagDefault first, then the base parse
        meta = {}
        number = None
        pos = 0
        while pos < len(body):
            eid, ilen = _vint_size(body, pos, keep_marker=True)
            if eid is None:
                break
            size, slen = _vint_size(body, pos + ilen, keep_marker=False)
            if size is None:
                break
            header = ilen + slen
            chunk = body[pos + header:pos + header + size]
            if eid == _FLAG_FORCED:
                meta["forced"] = bool(int.from_bytes(chunk, "big"))
            elif eid == _FLAG_DEFAULT:
                meta["default"] = bool(int.from_bytes(chunk, "big"))
            pos += header + size
        super()._parse_track_entry(body)
        if number is None:
            for n, m in list(self._track_meta.items()):
                m.update(meta)


def main(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "MichaelTVPlayer/1.0",
                      "Range": "bytes=0-%d" % (HEAD - 1)})
    data = urllib.request.urlopen(req, timeout=30).read()
    p = FlagParser()
    p.feed(data)
    for n in sorted(p._track_meta):
        m = p._track_meta[n]
        if m.get("type") == 0x11:
            print("sub track %s: codec=%r lang=%r name=%r forced_flag=%r "
                  "default_flag=%r" % (n, m.get("codec"), m.get("lang"),
                                       m.get("name"), m.get("forced"),
                                       m.get("default")))
    print("selected:", p._selected)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
