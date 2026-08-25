"""Control: playback connection A ALONE - when does the provider cut it?

The first probe showed A surviving a concurrent download but hitting EOF
~0.6 s after the download closed at ~21 MB read. If A alone also EOFs at
a similar byte count, the provider simply caps/chunks response sizes and
reconnects are routine; if A alone runs long past that, the EOF was
triggered by the download connection's lifecycle.
"""

import sys
import time
import urllib.request

BASE = ("http://cf.534842.xyz/streaming/timeshift.php"
        "?username=726352471c&password=d809266e91"
        "&stream=497001&start=2026-08-25:02-15&duration=285&extension=ts")
UA = "MichaelTVPlayer/1.0"


def main():
    t0 = time.time()
    req = urllib.request.Request(BASE, headers={"User-Agent": UA,
                                                "Range": "bytes=5000000-"})
    got = 0
    with urllib.request.urlopen(req, timeout=30) as r:
        print("status:", r.status, "content-range:",
              r.headers.get("Content-Range"))
        bucket = 0
        t_bucket = time.time()
        while got < 200 * 1048576:
            chunk = r.read(256 * 1024)
            if not chunk:
                print(f"{time.time() - t0:6.1f}s  EOF at {got / 1048576:.1f} MB")
                return
            bucket += len(chunk)
            got += len(chunk)
            now = time.time()
            if now - t_bucket >= 2.0:
                print(f"{now - t0:6.1f}s  {bucket / 1048576:.2f} MB/2s "
                      f"(total {got / 1048576:.1f} MB)")
                bucket = 0
                t_bucket = now
            time.sleep(max(0.0, len(chunk) / 1048576.0 / 0.8 - 0.02))
    print(f"reached cap: {got / 1048576:.1f} MB, no EOF")


if __name__ == "__main__":
    sys.exit(main())
