# -*- coding: utf-8 -*-
"""Offscreen probe: the desktop Stremio profile reader
(src/stremio_profile.py) — WebView2 localStorage LevelDB parsing and the
addon discovery layered on top.

The desktop Stremio keeps its profile (every installed addon) in the
WebView2 browser profile's Local Storage leveldb: an SSTable (.ldb) plus
a write-ahead .log, snappy-compressed blocks, Chromium's
<origin>\\x00\\x01profile localStorage keys. src/stremio_profile.py
parses all of it by hand (stdlib only) and exposes
discover_stream_addons() / priority_sort / describe_addon /
provider_from_url for the addon-import flow.

This probe pins the format code and the policy offline and
deterministically, against SYNTHETIC leveldb directories written to
TEMP (the real Stremio directory is never written to):
  [1] the raw-snappy block decompressor (literals, all three copy
      widths, overlapping copies, every error path);
  [2] SSTable record walking — internal-key seq/type split, footer
      magic, snappy blocks, truncated/corrupt tables yield nothing;
  [3] WAL record walking — WriteBatch decode, deletions, padding/torn/
      garbage records, newest-seq-wins across .ldb + .log;
  [4] the Chromium value flag-byte sniffing + transport-URL
      normalization;
  [5] provider_from_url — TorBox/Premiumize/Real-Debrid path tokens and
      base64 config segments, unknown -> "";
  [6] describe_addon labels + priority_sort's documented default order
      (Torrentio family, then Debridio; TorBox before Premiumize;
      unknowns last; stable);
  [7] discover_stream_addons end to end on the synthetic dir (the
      module's _leveldb_dirs patched): stream-only filtering, dedupe,
      profile order preserved;
  [8] LIVE — the real desktop profile on this machine, read-only
      (discovery asserts >= 1 stream addon, files copied aside, the real
      directory untouched; skipped when Stremio isn't installed).

No window, no network, no audio. leveldb is only ever read (the module
copies files aside before parsing; the live leg asserts it).
"""
import base64
import json
import os
import shutil
import struct
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import stremio_profile as sp   # noqa: E402

fails = [0]


def check(name, cond, extra=""):
    print(("  ok   " if cond else "FAIL ") + name
          + ("" if cond or not extra else "  [%s]" % extra))
    if not cond:
        fails[0] += 1


# ---- synthetic leveldb builders (mimic Chromium's layout) ----

def varint(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


_TABLE_MAGIC = 0xDB4775248B80FB57


def ldb_block(entries):
    """One leveldb block: prefix-compressed entries (shared=0, one
    restart point at offset 0 — restarts exist for binary search only,
    the sequential reader needs none) + restart array + count."""
    buf = bytearray()
    for key, value in entries:
        buf += varint(0) + varint(len(key)) + varint(len(value))
        buf += key + value
    buf += struct.pack("<I", 0)      # restart point: offset 0
    buf += struct.pack("<I", 1)      # num_restarts
    return bytes(buf)


def write_ldb(path, records, snappy=False, corrupt_snappy=False):
    """A minimal .ldb SSTable, real leveldb order:
    [data block][ctype][metablock][0x00][index][0x00][footer 48] —
    data first, so the index entry can carry the data block's handle.
    records = [(seq, etype, user_key, value)]; the internal key is
    user_key + (seq<<8 | etype) little-endian, like leveldb's."""
    data = ldb_block([(k + struct.pack("<Q", (seq << 8) | t), v)
                      for seq, t, k, v in records])
    if snappy:
        payload, ctype = snappy_literal_blob(data), 1
    elif corrupt_snappy:
        payload, ctype = b"\xff\xff\xff\x7fnot-snappy", 1
    else:
        payload, ctype = data, 0
    data_off, data_size = 0, len(payload)
    meta = ldb_block([])
    meta_off = data_off + data_size + 1          # + the ctype byte
    index = ldb_block([(b"\x00sep", varint(data_off) + varint(data_size))])
    index_off = meta_off + len(meta) + 1         # + the 0x00 ctype byte
    footer = (varint(meta_off) + varint(len(meta))
              + varint(index_off) + varint(len(index))).ljust(40, b"\x00") \
        + struct.pack("<Q", _TABLE_MAGIC)
    with open(path, "wb") as f:
        f.write(payload + bytes([ctype]) + meta + b"\x00"
                + index + b"\x00" + footer)
    return path


def wal_record(payload, rtype=1):
    """One log record: [crc (never verified by the reader)][len][type]."""
    return struct.pack("<I", 0) + struct.pack("<H", len(payload)) \
        + bytes([rtype]) + payload


def write_batch(seq, entries):
    """A leveldb WriteBatch: [seq u64][count u32] then per entry
    [etype][klen varint][key] (+ [vlen varint][value] when etype == 1;
    etype 0 = deletion)."""
    buf = bytearray(struct.pack("<Q", seq) + struct.pack("<I", len(entries)))
    for etype, key, value in entries:
        buf.append(etype)
        buf += varint(len(key)) + key
        if etype == 1:
            buf += varint(len(value)) + value
    return bytes(buf)


# snappy EMIT side (the probe compresses; the module decompresses)

def _snappy_literal(data):
    n = len(data) - 1
    if n < 60:
        return bytes([n << 2]) + data
    if n <= 0xFF:
        return bytes([60 << 2, n]) + data
    if n <= 0xFFFF:
        return bytes([61 << 2]) + struct.pack("<H", n) + data
    if n <= 0xFFFFFF:
        return bytes([62 << 2]) + struct.pack("<I", n)[:3] + data
    return bytes([63 << 2]) + struct.pack("<I", n) + data


def snappy_literal_blob(data):
    return varint(len(data)) + _snappy_literal(data)


def snappy_copy1(n, off):
    return bytes([((off >> 8) << 5) | ((n - 4) << 2) | 1, off & 0xFF])


def snappy_copy2(n, off):
    return bytes([((n - 1) << 2) | 2]) + struct.pack("<H", off)


def snappy_copy4(n, off):
    return bytes([((n - 1) << 2) | 3]) + struct.pack("<I", off)


# ---- the fake Stremio profile (mimics the real localStorage blob) ----

ORIGIN_KEY = b"_https://app.stremio.com\x00\x01profile"
OTHER_KEY = b"_https://app.stremio.com\x00\x01lastGood"


def ls_value(profile, wide=True):
    """Chromium localStorage value: 1 flag byte (0 = UTF-16LE, 1 =
    Latin-1) + text."""
    body = json.dumps(profile).encode("utf-16-le" if wide else "latin-1")
    return bytes([0 if wide else 1]) + body


B64_PM = base64.urlsafe_b64encode(b'{"provider":"premiumize"}').decode()
URL_TTB = "https://torrentio.strem.fun/torbox=TBKEY123/manifest.json"
URL_DPM = "https://addon.debridio.com/%s/manifest.json" % B64_PM
URL_TRD = "https://torrentio.strem.fun/realdebrid=RDKEY9/manifest.json"


def addon(url, name, resources=("stream",)):
    return {"manifest": {"name": name, "resources": list(resources)},
            "transportUrl": url}


FULL_PROFILE = {
    "auth": {"user": {"id": "not-looked-at"}},
    "addons": [
        addon(URL_TTB, "Torrentio"),
        addon(URL_DPM, "Debridio", resources=[{"name": "stream"}]),
        addon(URL_TRD, "Torrentio", resources={"stream": {}}),  # object form
        addon(URL_TTB, "Torrentio"),                 # duplicate transportUrl
        {"manifest": {"name": "IPTV", "resources": ["catalog"]},
         "transportUrl": "https://iptv.example.net/manifest.json"},
        {"manifest": {"name": "Ratings", "resources": []},
         "transportUrl": "https://ratings.example.net/manifest.json"},
        addon("http://127.0.0.1:11470/manifest.json", "Local addon"),
        {"manifest": {"name": "Bad scheme", "resources": ["stream"]},
         "transportUrl": "ftp://nope.example/manifest.json"},
        "junk-not-a-dict",
        {"transportUrl": URL_TTB},                   # no manifest at all
    ],
}
OLD_PROFILE = {"auth": {}, "addons": [addon(URL_TTB, "Torrentio")]}


_tmpdirs = []


def new_tmp(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _tmpdirs.append(d)
    return d


# the main synthetic dir: newest profile rides the WAL, an older one
# lives (snappy-compressed) in the SSTable, plus a truncated table, a
# corrupt-snappy table and a garbage log to prove the robustness
SYNTH = new_tmp("mtp_probe_sldb_")
batch = write_batch(200, [
    (1, ORIGIN_KEY, ls_value(FULL_PROFILE)),
    (1, OTHER_KEY, b"1"),
    (0, b"_https://app.stremio.com\x00\x01stale", b""),
])
with open(os.path.join(SYNTH, "000003.log"), "wb") as f:
    f.write(wal_record(batch)
            + wal_record(b"tiny")            # payload < 12: dropped
            + wal_record(b"junk99", rtype=9)  # future type: dropped
            + wal_record(b"pad", rtype=0)     # preallocated padding
            + struct.pack("<I", 0) + struct.pack("<H", 4000)
            + bytes([1]) + b"torn")           # torn tail: break
write_ldb(os.path.join(SYNTH, "000006.ldb"),
          [(100, 1, ORIGIN_KEY, ls_value(OLD_PROFILE)),
           (500, 1, b"_https://app.stremio.com\x00\x01quiet", b"x")],
          snappy=True)
trunc_src = os.path.join(SYNTH, "000006.ldb")
with open(trunc_src, "rb") as f:
    _full = f.read()
with open(os.path.join(SYNTH, "000005.ldb"), "wb") as f:
    f.write(_full[:int(len(_full) * 6) // 10])     # no magic: not a table
write_ldb(os.path.join(SYNTH, "000007.ldb"),
          [(1, 1, ORIGIN_KEY, b"x")], corrupt_snappy=True)
with open(os.path.join(SYNTH, "000009.log"), "wb") as f:
    f.write(os.urandom(64))                        # pure garbage
with open(os.path.join(SYNTH, "CURRENT"), "w") as f:
    f.write("MANIFEST-000001\n")                   # ignored (not .ldb/.log)

# a dir whose newest profile entry is a DELETION -> unreadable profile
DELE = new_tmp("mtp_probe_sldb_del_")
with open(os.path.join(DELE, "000004.log"), "wb") as f:
    f.write(wal_record(write_batch(10, [
        (1, ORIGIN_KEY, ls_value(OLD_PROFILE)),
        (0, ORIGIN_KEY, b""),          # the user uninstalled everything
    ])))

EMPTY = new_tmp("mtp_probe_sldb_empty_")

try:
    print("[1] snappy_decompress — raw block literals + copies")
    check("empty blob -> empty output",
          sp.snappy_decompress(varint(0)) == b"")
    check("literal-only round-trip",
          sp.snappy_decompress(snappy_literal_blob(b"hello stremio"))
          == b"hello stremio")
    long = bytes(range(256)) * 3
    check("long literal (2-byte length header)",
          sp.snappy_decompress(snappy_literal_blob(long)) == long)
    check("copy-1 RLE run (offset 1)",
          sp.snappy_decompress(varint(6) + _snappy_literal(b"a")
                               + snappy_copy1(5, 1)) == b"a" * 6)
    check("copy-1 max run (11 bytes)",
          sp.snappy_decompress(varint(12) + _snappy_literal(b"z")
                               + snappy_copy1(11, 1)) == b"z" * 12)
    check("copy-2 overlapping run (byte-by-byte copy)",
          sp.snappy_decompress(varint(12) + _snappy_literal(b"abc")
                               + snappy_copy2(9, 3)) == b"abc" * 4)
    check("copy-2 mid-window reference",
          sp.snappy_decompress(varint(10) + _snappy_literal(b"abcde")
                               + snappy_copy2(5, 5)) == b"abcdeabcde")
    check("copy-4 offset form",
          sp.snappy_decompress(varint(3) + _snappy_literal(b"xy")
                               + snappy_copy4(1, 1)) == b"xyy")
    try:
        sp.snappy_decompress(varint(99) + _snappy_literal(b"hi"))
        check("declared length mismatch raises", False)
    except ValueError:
        check("declared length mismatch raises", True)
    try:
        sp.snappy_decompress(varint(4) + snappy_copy2(4, 9))
        check("copy offset beyond the output raises", False)
    except ValueError:
        check("copy offset beyond the output raises", True)
    try:
        sp.snappy_decompress(varint(2) + snappy_copy2(2, 0))
        check("zero copy offset raises", False)
    except ValueError:
        check("zero copy offset raises", True)
    try:
        sp.snappy_decompress(b"\x80")     # continuation with no more bytes
        check("truncated varint raises", False)
    except ValueError:
        check("truncated varint raises", True)
    try:
        sp.snappy_decompress(varint(10) + _snappy_literal(b"abc"))
        check("truncated literal payload -> length mismatch raises", False)
    except ValueError:
        check("truncated literal payload -> length mismatch raises", True)
    try:
        import snappy as _snappy_lib
    except ImportError:
        _snappy_lib = None
    if _snappy_lib is not None:
        import random
        random.seed(7)
        blob = bytes(random.randrange(256) for _ in range(4096))
        check("python-snappy cross-check (library-generated block)",
              sp.snappy_decompress(_snappy_lib.compress(blob)) == blob)
    else:
        print("  ..   python-snappy not installed — hand-built blocks only")

    print("[2] SSTable (.ldb) walking — internal keys, footer, robustness")
    raw = new_tmp("mtp_probe_ldb_raw_")
    raw_p = write_ldb(os.path.join(raw, "000010.ldb"),
                      [(7, 1, ORIGIN_KEY, ls_value(OLD_PROFILE)),
                       (9, 0, OTHER_KEY, b"")])
    with open(raw_p, "rb") as f:
        entries = list(sp._table_entries(f.read()))
    check("entries decoded in order with seq/type split",
          [(e[0], e[1], e[2]) for e in entries]
          == [(7, 1, ORIGIN_KEY), (9, 0, OTHER_KEY)], repr(entries))
    check("values intact through the walk",
          entries and entries[0][3] == ls_value(OLD_PROFILE))
    check("random bytes (no footer magic) yield nothing",
          list(sp._table_entries(os.urandom(200))) == [])
    with open(os.path.join(SYNTH, "000005.ldb"), "rb") as f:
        check("truncated table (footer cut off) yields nothing",
              list(sp._table_entries(f.read())) == [])
    with open(os.path.join(SYNTH, "000006.ldb"), "rb") as f:
        sn_entries = list(sp._table_entries(f.read()))
    check("snappy-compressed data block walks like a raw one",
          [(e[0], e[2]) for e in sn_entries]
          == [(100, ORIGIN_KEY), (500,
              b"_https://app.stremio.com\x00\x01quiet")], repr(sn_entries))
    with open(os.path.join(SYNTH, "000007.ldb"), "rb") as f:
        check("corrupt snappy block skips the block (no crash)",
              list(sp._table_entries(f.read())) == [])

    print("[3] WAL (.log) walking — WriteBatch, deletions, newest-wins")
    with open(os.path.join(SYNTH, "000003.log"), "rb") as f:
        log_entries = sp._log_entries(f.read())
    check("the valid batch decodes to its three entries",
          [(e[0], e[1], e[2]) for e in log_entries]
          == [(200, 1, ORIGIN_KEY), (201, 1, OTHER_KEY),
              (202, 0, b"_https://app.stremio.com\x00\x01stale")],
          repr(log_entries))
    check("profile VALUE survives the batch",
          log_entries[0][3] == ls_value(FULL_PROFILE))
    with open(os.path.join(SYNTH, "000009.log"), "rb") as f:
        check("garbage log yields nothing",
              sp._log_entries(f.read()) == [])
    best = sp._profile_bytes(SYNTH)
    check("newest seq WINS across files (WAL 200 beats SSTable 100)",
          best == ls_value(FULL_PROFILE),
          repr(best)[:80] if best else "None")
    check("a HIGHER-seq non-profile key never shadows the profile",
          sp._decode_profile(best) == FULL_PROFILE)
    check("newest = deletion -> profile unreadable (None)",
          sp._profile_bytes(DELE) is None)
    check("empty leveldb dir -> None", sp._profile_bytes(EMPTY) is None)

    print("[4] _decode_profile + normalize_base — flag sniffing, URLs")
    check("flag 0 (UTF-16LE) decodes",
          sp._decode_profile(ls_value(OLD_PROFILE)) == OLD_PROFILE)
    check("flag 1 (Latin-1) decodes",
          sp._decode_profile(ls_value(OLD_PROFILE, wide=False))
          == OLD_PROFILE)
    check("no flag byte (bare JSON) decodes",
          sp._decode_profile(json.dumps(OLD_PROFILE).encode())
          == OLD_PROFILE)
    check("JSON array (not a dict) -> None",
          sp._decode_profile(b"\x00" + b"[1,2]".decode().encode(
              "utf-16-le")) is None)
    check("garbage bytes -> None",
          sp._decode_profile(b"\x00\x01\x02\x03") is None)
    check("empty value -> None", sp._decode_profile(b"") is None)
    check("manifest.json suffix strips",
          sp.normalize_base("https://a.example/manifest.json")
          == "https://a.example")
    check("debrid token path KEPT (keys ride in the path)",
          sp.normalize_base(URL_TTB)
          == "https://torrentio.strem.fun/torbox=TBKEY123")
    check("non-manifest path kept verbatim",
          sp.normalize_base("https://a.example/stremio/")
          == "https://a.example/stremio")
    check("non-http transport rejected",
          sp.normalize_base("ftp://a.example/manifest.json") == ""
          and sp.normalize_base("") == "")

    print("[5] provider_from_url — tokens + base64 config segments")
    check("torbox= path token -> TorBox",
          sp.provider_from_url(URL_TTB) == "TorBox")
    check("premiumize= path token -> Premiumize",
          sp.provider_from_url(
              "https://torrentio.strem.fun/premiumize=KEY/manifest.json")
          == "Premiumize")
    check("realdebrid= path token -> Real-Debrid",
          sp.provider_from_url(URL_TRD) == "Real-Debrid")
    check("base64 {provider:torbox} segment -> TorBox",
          sp.provider_from_url(
              "https://addon.debridio.com/%s/manifest.json"
              % base64.urlsafe_b64encode(b'{"provider":"torbox"}').decode())
          == "TorBox")
    check("base64 {provider:premiumize} segment -> Premiumize",
          sp.provider_from_url(URL_DPM) == "Premiumize")
    check("non-JSON base64 segment -> no provider",
          sp.provider_from_url("https://x.example/%s/m"
                               % base64.urlsafe_b64encode(
                                   b"just a long random string").decode())
          == "")
    check("tokenless URL -> no provider (falsy)",
          not sp.provider_from_url("https://weird.example/manifest.json")
          and sp.provider_from_url("") == "")

    print("[6] describe_addon + priority_sort — labels, default order")
    check("manifest name kept when it already carries the provider tag",
          sp.describe_addon(URL_TTB, "Torrentio TB") == "Torrentio TB")
    check("provider appended when the name omits it (em dash)",
          sp.describe_addon(URL_TTB, "Torrentio")
          == "Torrentio \u2014 TorBox")
    check("provider word in the name suppresses the suffix",
          sp.describe_addon(URL_DPM, "Debridio Premiumize thing")
          == "Debridio Premiumize thing")
    check("known host names the nameless addon",
          sp.describe_addon("https://torrentio.strem.fun/x", "")
          == "Torrentio")
    check("unknown host falls back to the host",
          sp.describe_addon("https://sub.example.com/m", "")
          == "sub.example.com")
    check("no host, no name -> raw URL",
          sp.describe_addon("just some text", "") == "just some text")
    entries = [
        {"url": "https://addon.debridio.com/%s/m" % B64_PM,
         "provider": "Premiumize", "name": "Debridio PM"},
        {"url": "https://torrentio.strem.fun/realdebrid=K/m",
         "provider": "Real-Debrid", "name": "Torrentio RD"},
        {"url": "https://addon.debridio.com/%s/m" % base64.urlsafe_b64encode(
            b'{"provider":"torbox"}').decode(),
         "provider": "TorBox", "name": "Debridio TB"},
        {"url": "https://torrentio.strem.fun/torbox=K/m",
         "provider": "TorBox", "name": "Torrentio TB"},
        {"url": "https://weird.example/m", "provider": "", "name": "Mystery"},
    ]
    order = [e["name"] for e in sp.priority_sort(entries)]
    check("Torrentio family first, then Debridio, TorBox before "
          "Premiumize, unknowns last",
          order == ["Torrentio TB", "Torrentio RD", "Debridio TB",
                    "Debridio PM", "Mystery"], repr(order))
    twin_a = {"url": "https://torrentio.strem.fun/torbox=A/m",
              "provider": "TorBox", "name": "Same"}
    twin_b = {"url": "https://torrentio.strem.fun/torbox=B/m",
              "provider": "TorBox", "name": "Same"}
    check("equal keys keep input order (stable)",
          [e["url"][-3] for e in sp.priority_sort([twin_a, twin_b])]
          == ["A", "B"])

    print("[7] discover_stream_addons end-to-end (synthetic leveldb dir)")
    orig_dirs = sp._leveldb_dirs
    try:
        sp._leveldb_dirs = lambda: [SYNTH]
        addons = sp.discover_stream_addons()
        got = [(a["url"], a["provider"], a["name"], a["manifest_name"])
               for a in (addons or [])]
        check("exactly the stream addons, profile order, WAL-newest",
              got == [
                  ("https://torrentio.strem.fun/torbox=TBKEY123",
                   "TorBox", "Torrentio \u2014 TorBox", "Torrentio"),
                  ("https://addon.debridio.com/%s" % B64_PM,
                   "Premiumize", "Debridio \u2014 Premiumize", "Debridio"),
                  ("https://torrentio.strem.fun/realdebrid=RDKEY9",
                   "Real-Debrid", "Torrentio \u2014 Real-Debrid",
                   "Torrentio"),
              ], repr(got))
        check("non-stream / loopback / bad-scheme / dup all filtered",
              addons is not None and len(addons) == 3)
        sp._leveldb_dirs = lambda: []
        check("no Stremio install at all -> None (caller falls back)",
              sp.discover_stream_addons() is None)
        sp._leveldb_dirs = lambda: [EMPTY]
        check("no readable profile -> None",
              sp.discover_stream_addons() is None)
    finally:
        sp._leveldb_dirs = orig_dirs

    print("[8] LIVE — the real desktop Stremio profile (read-only)")
    real_dirs = orig_dirs()
    if not real_dirs:
        print("  skipped (no Stremio WebView2 leveldb on this machine)")
    else:
        try:
            real = real_dirs[0]

            def snap(path):
                out = {}
                for name in sorted(os.listdir(path)):
                    try:
                        st = os.stat(os.path.join(path, name))
                        out[name] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        out[name] = None
                return out

            before = snap(real)
            copied = []
            _orig_copy = shutil.copyfile

            def spy_copy(src, dst, **kw):
                copied.append((src, dst))
                return _orig_copy(src, dst, **kw)

            sp.shutil.copyfile = spy_copy
            try:
                addons = sp.discover_stream_addons()
            finally:
                sp.shutil.copyfile = _orig_copy
            check("live: discovery found addons",
                  isinstance(addons, list) and len(addons) >= 1,
                  repr(len(addons or [])))
            check("live: names/providers are strings",
                  all(isinstance(a.get("name"), str)
                      and isinstance(a.get("provider"), str)
                      for a in (addons or [])))
            check("live: every file parsed from a copy OUTSIDE the real "
                  "dir (never written in place)",
                  bool(copied)
                  and all(src.startswith(real + os.sep)
                          and not dst.startswith(real)
                          for src, dst in copied), repr(copied)[:120])
            after = snap(real)
            check("live: no leveldb file lost or truncated by the read",
                  all(after.get(n) is not None and after[n][0] >= v[0]
                      for n, v in before.items()))
            for a in (addons or [])[:8]:
                print("    - %s | %s" % (a["name"],
                                         a["provider"] or "(no debrid)"))
        except Exception as exc:      # noqa: BLE001 - live leg reports
            check("live: discovery ran", False, repr(exc))
finally:
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)

print()
if fails[0]:
    print("FAILURES: %d" % fails[0])
    sys.exit(1)
print("ALL PASS")
