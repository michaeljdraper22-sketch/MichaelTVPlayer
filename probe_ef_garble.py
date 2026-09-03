"""Headless probe of the Elite Force E05 provider (cf.534842.xyz).

Measures, against the exact URL the vod splitter was serving today:
  1. header behavior of a small Range request (status, Content-Range,
     codec strings in the MKV head),
  2. sustained throughput + connection-drop pattern from the offset where
     today's session kept dying (~206 MB),
  3. Range correctness: same bytes fetched via two different windows must
     hash identically (a wrong-offset 206 poisons the decoder -> garbling),
  4. repeat-fetch consistency (same window twice, two connections).

Pure HTTP; no video window, no audio, nothing visible.
"""
import hashlib
import socket
import time
import urllib.request

socket.setdefaulttimeout(20)   # DNS/connect/read cap (urlopen timeout
                               # does NOT cover getaddrinfo on Windows)

URL = ("http://cf.534842.xyz/series/726352471c/"
       "d809266e91/2083260.mkv")
UA = "MichaelTVPlayer/1.0"
DEATH_OFFSET = 206_126_292          # where today's session kept dying


def fetch(a, b=None, timeout=30):
    """One range GET -> (status, headers, bytes). b=None -> open-ended."""
    rng = f"bytes={a}-" if b is None else f"bytes={a}-{b}"
    req = urllib.request.Request(URL, headers={"User-Agent": UA,
                                               "Range": rng})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return getattr(r, "status", None), dict(r.headers), data


print("== 1. head probe (bytes 0-2047) ==")
try:
    st, hd, head = fetch(0, 2047)
    print(f"status={st} content-range={hd.get('Content-Range')!r} "
          f"len={len(head)} server={hd.get('Server')!r}")
    for tag in (b"V_MPEGH/ISO/HEVC", b"V_MPEG4/ISO/AVC",
                b"V_VP9", b"hvcC", b"avcC", b"HEVC", b"DVHE"):
        if tag in head:
            print(f"  head contains {tag!r}")
except Exception as exc:
    print("HEAD PROBE FAILED:", repr(exc))

print("\n== 2. sustained read from the death offset "
      f"({DEATH_OFFSET}, 20 s cap, chunked) ==")
CHUNK = 1 << 20
total = 0
deaths = 0
pos = DEATH_OFFSET
t0 = time.monotonic()
reads = []
while time.monotonic() - t0 < 20.0:
    try:
        rng = f"bytes={pos}-"
        req = urllib.request.Request(URL, headers={
            "User-Agent": UA, "Range": rng})
        with urllib.request.urlopen(req, timeout=15) as r:
            st = getattr(r, "status", None)
            if st not in (200, 206):
                print(f"  open at {pos}: HTTP {st} — abort")
                break
            cr = r.headers.get("Content-Range", "")
            if st == 206 and not cr.startswith(f"bytes {pos}-"):
                print(f"  open at {pos}: 206 but Content-Range {cr!r} "
                      f"— WRONG-OFFSET LIE")
                break
            while time.monotonic() - t0 < 20.0:
                data = r.read(CHUNK)
                if not data:
                    deaths += 1
                    break
                total += len(data)
                reads.append(len(data))
                pos += len(data)
    except Exception as exc:
        deaths += 1
        reads.append(-1)
        time.sleep(0.5)
dt = time.monotonic() - t0
print(f"  served {total/1e6:.1f} MB in {dt:.1f}s = "
      f"{total/dt/1e6:.2f} MB/s; body cuts/errors={deaths}; "
      f"reads min/med/max={min(reads) if reads else 0}/"
      f"{sorted(reads)[len(reads)//2] if reads else 0}/"
      f"{max(reads) if reads else 0}")

print("\n== 3. range correctness (overlap hash) ==")
A, N = DEATH_OFFSET, 1 << 20           # [A, A+1MB)
B = A - (512 << 10)                    # window starting 512KB earlier
try:
    stA, hdA, dA = fetch(A, A + N - 1)
    crA = hdA.get("Content-Range", "")
    stB, hdB, dB = fetch(B, B + (512 << 10) + N - 1)
    crB = hdB.get("Content-Range", "")
    print(f"  A: status={stA} cr={crA!r} len={len(dA)}")
    print(f"  B: status={stB} cr={crB!r} len={len(dB)}")
    ok_start = crA.startswith(f"bytes {A}-") and crB.startswith(f"bytes {B}-")
    print(f"  Content-Range starts match request: {ok_start}")
    tail_of_B = dB[(512 << 10): (512 << 10) + N]
    hA = hashlib.md5(dA).hexdigest()
    hB = hashlib.md5(tail_of_B).hexdigest()
    print(f"  overlap md5 A={hA} B={hB} -> "
          f"{'MATCH — offsets honored' if hA == hB else 'MISMATCH — CDN SERVES WRONG BYTES'}")
except Exception as exc:
    print("  OVERLAP PROBE FAILED:", repr(exc))

print("\n== 4. repeat consistency (same 1MB twice) ==")
try:
    _, _, x1 = fetch(A, A + N - 1)
    _, _, x2 = fetch(A, A + N - 1)
    print(f"  md5 run1={hashlib.md5(x1).hexdigest()} "
          f"run2={hashlib.md5(x2).hexdigest()} -> "
          f"{'MATCH' if x1 == x2 else 'MISMATCH — INCONSISTENT CDN DATA'}")
except Exception as exc:
    print("  REPEAT PROBE FAILED:", repr(exc))
