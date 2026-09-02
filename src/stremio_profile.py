"""Auto-discovery of the user's installed Stremio addons.

Stremio's desktop shell (v5) keeps its profile — every installed
addon included — inside the WebView2 browser profile embedded in the
install directory: <install>\\<shell>.WebView2\\EBChrome\\…\\Default\\
Local Storage\\leveldb\\*.ldb. The addon collection is the localStorage
map key "profile" under the web.stremio.com origin, a JSON blob:

    {"auth": {...}, "addons": [{"manifest": {...},
     "transportUrl": "https://…/manifest.json"}, ...], ...}

Reading it needs a LevelDB SSTable + WAL reader and a snappy block
decompressor — both hand-rolled below, stdlib only, because bundling
a native LevelDB binding into the PyInstaller build would be far
heavier than the format code itself. Everything here is READ-ONLY and
local: files are copied aside before parsing (LevelDB keeps its files
open while Stremio runs), nothing touches the network or the user's
Stremio account, and the auth block in the profile is never looked
at — only transportUrl + manifest name/resources are read.
"""

import base64
import json
import logging
import os
import re
import shutil
import tempfile
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# localStorage map key we want, inside Chromium's leveldb layout
# (<origin>\x00\x01<key>); any origin matches — there is only one.
_PROFILE_KEY_SUFFIX = b"\x00\x01profile"

_TABLE_MAGIC = 0xDB4775248B80FB57     # last 8 bytes of an .ldb file
_BLOCK_SNAPPY = 1

# debrid providers we can name, in URL token form; the tuple order is
# also the discovery tiebreak (the user's stated preference: TorBox
# before Premiumize)
_DEBRID_TOKENS = (("torbox", "TorBox"), ("premiumize", "Premiumize"),
                  ("realdebrid", "Real-Debrid"),
                  ("alldebrid", "AllDebrid"),
                  ("debridlink", "DebridLink"))
_PROVIDER_RANK = {"TorBox": 0, "Premiumize": 1}
# how manifests abbreviate a provider in their display names
_PROVIDER_TAGS = {"TorBox": ("tb",), "Premiumize": ("pm",),
                  "Real-Debrid": ("rd",), "AllDebrid": ("ad",),
                  "DebridLink": ("dl",)}

# addon families the user ranks by default (torrentio ahead of
# debridio — the incumbent); anything else sorts last
_FAMILY_RANK = {"torrentio.strem.fun": 0, "addon.debridio.com": 1}
_HOST_NAMES = {"torrentio.strem.fun": "Torrentio",
               "addon.debridio.com": "Debridio"}

_B64_SEGMENT_RE = re.compile(r"^[A-Za-z0-9+/_=-]{24,}$")


# ---------------------------------------------------------------------------
# format primitives: varints, snappy, SSTable blocks

def _read_varint(buf: bytes, pos: int):
    """LEB128 (leveldb varint64). Returns (value, next_pos)."""
    result = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def snappy_decompress(src: bytes) -> bytes:
    """Raw snappy block format (the .ldb block compression). Length-
    prefixed literals + back-references; overlapping copies allowed."""
    length, pos = _read_varint(src, 0)
    out = bytearray()
    end = len(src)
    while pos < end:
        tag = src[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:                      # literal
            n = tag >> 2
            if n >= 60:                    # length in the next 1-4 bytes
                extra = n - 59
                n = int.from_bytes(src[pos:pos + extra], "little")
                pos += extra
            n += 1
            out += src[pos:pos + n]
            pos += n
            continue
        if kind == 1:                      # copy, 1-byte offset
            n = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | src[pos]
            pos += 1
        elif kind == 2:                    # copy, 2-byte offset
            n = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 2], "little")
            pos += 2
        else:                              # copy, 4-byte offset
            n = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 4], "little")
            pos += 4
        if not 0 < offset <= len(out):
            raise ValueError("snappy copy offset out of range")
        for _ in range(n):                 # byte-by-byte: overlaps legal
            out.append(out[-offset])
    if len(out) != length:
        raise ValueError("snappy length mismatch")
    return bytes(out)


def _iter_block(data: bytes):
    """Yield (key, value) from a decompressed leveldb block. Entries
    are prefix-compressed; decoding sequentially needs no restart
    points (those exist for binary search only)."""
    if len(data) < 4:
        return
    restarts = int.from_bytes(data[-4:], "little")
    if restarts <= 0:
        return
    end = len(data) - 4 - 4 * restarts
    if end <= 0:
        return
    pos, prev = 0, b""
    while pos < end:
        shared, pos = _read_varint(data, pos)
        non_shared, pos = _read_varint(data, pos)
        vlen, pos = _read_varint(data, pos)
        if pos + non_shared + vlen > end:
            raise ValueError("block entry overruns block")
        key = prev[:shared] + data[pos:pos + non_shared]
        pos += non_shared
        value = data[pos:pos + vlen]
        pos += vlen
        prev = key
        yield key, value


def _read_table_block(data: bytes, offset: int, size: int):
    """One on-disk block. handle.size() covers the (possibly
    compressed) payload ONLY; the 1 compression-type byte follows it,
    then a crc we don't verify. Returns decompressed bytes, or None
    for an unsupported/failed compression."""
    payload = data[offset:offset + size]
    if len(payload) != size or offset + size >= len(data):
        return None
    ctype = data[offset + size]
    if ctype == 0:
        return payload
    if ctype == _BLOCK_SNAPPY:
        try:
            return snappy_decompress(payload)
        except (ValueError, IndexError):
            return None
    return None                      # zstd & friends: skip, not crash


def _table_entries(data: bytes):
    """Yield (seq, type, user_key, value) from an .ldb SSTable, or
    nothing when the file isn't a table we can parse (no/odd footer)."""
    if len(data) < 48 \
            or int.from_bytes(data[-8:], "little") != _TABLE_MAGIC:
        return
    try:
        pos = 0
        handles = []
        for _ in range(2):                   # metaindex, index
            off, pos = _read_varint(data[-48:], pos)
            size, pos = _read_varint(data[-48:], pos)
            handles.append((off, size))
        index = _read_table_block(data, *handles[1])
        if index is None:
            return
        for _sep, handle in _iter_block(index):
            off, pos2 = _read_varint(handle, 0)
            size, pos2 = _read_varint(handle, pos2)
            block = _read_table_block(data, off, size)
            if block is None:
                continue
            for ikey, value in _iter_block(block):
                if len(ikey) < 8:
                    continue
                # internal key = user key + (seq<<8 | type), LE uint64
                tag = int.from_bytes(ikey[-8:], "little")
                yield tag >> 8, tag & 0xFF, ikey[:-8], value
    except (ValueError, IndexError):
        return


def _log_entries(data: bytes):
    """Yield (seq, type, user_key, value) from a leveldb write-ahead
    .log: 32 KiB blocks of [crc][len][type] records, payload = a
    WriteBatch of user-level entries."""
    out = []
    for base in range(0, len(data), 32768):
        buf = data[base:base + 32768]
        pos, fragment = 0, b""
        while pos + 7 <= len(buf):
            length = int.from_bytes(buf[pos + 4:pos + 6], "little")
            rtype = buf[pos + 6]
            pos += 7
            if rtype in (5, 6, 7, 8):        # recyclable record: extra
                pos += 4                     # 4-byte log number
            if pos + length > len(buf):
                break                        # torn tail block
            payload = buf[pos:pos + length]
            pos += length
            if rtype == 0 or length == 0:
                continue                     # preallocated padding
            if rtype in (1, 5):              # full record
                fragment = payload
            elif rtype in (2, 6):            # first fragment
                fragment = payload
                continue
            elif rtype in (3, 7):            # middle fragment
                fragment += payload
                continue
            else:                            # last fragment (4/8)
                fragment += payload
            if len(fragment) < 12:
                fragment = b""
                continue
            seq = int.from_bytes(fragment[0:8], "little")
            count = int.from_bytes(fragment[8:12], "little")
            bpos, idx = 12, 0
            while bpos < len(fragment) and idx < count:
                etype = fragment[bpos]
                bpos += 1
                try:
                    klen, bpos = _read_varint(fragment, bpos)
                    key = fragment[bpos:bpos + klen]
                    bpos += klen
                    value = b""
                    if etype == 1:           # value; 0 = deletion
                        vlen, bpos = _read_varint(fragment, bpos)
                        value = fragment[bpos:bpos + vlen]
                        bpos += vlen
                except (ValueError, IndexError):
                    break
                out.append((seq + idx, etype, key, value))
                idx += 1
            fragment = b""
    return out


# ---------------------------------------------------------------------------
# the profile blob

def _profile_bytes(leveldb_dir: str):
    """The newest value stored for the localStorage 'profile' key, or
    None. Newest = highest write sequence across every .ldb table and
    .log WAL (a just-installed addon may only live in the .log until
    the memtable is flushed)."""
    workdir = tempfile.mkdtemp(prefix="mtp_stremio_ldb_")
    best = None                            # (seq, type, value)
    try:
        names = []
        for name in sorted(os.listdir(leveldb_dir)):
            if not name.endswith((".ldb", ".log")):
                continue
            src = os.path.join(leveldb_dir, name)
            dst = os.path.join(workdir, name)
            try:
                shutil.copyfile(src, dst)   # parse a stable snapshot
            except OSError:
                dst = src                   # locked differently: read live
            names.append(dst)
        for path in names:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            try:
                entries = _table_entries(data) if path.endswith(".ldb") \
                    else _log_entries(data)
                for seq, etype, key, value in entries or []:
                    if not key.endswith(_PROFILE_KEY_SUFFIX):
                        continue
                    if best is None or seq > best[0]:
                        best = (seq, etype, value)
            except Exception as exc:  # noqa: BLE001 - one odd file
                log.info("stremio profile: %s unreadable: %r",
                         os.path.basename(path), exc)
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass
    if best is None or best[1] != 1:        # missing, or newest=deletion
        return None
    return best[2]


def _decode_profile(value: bytes):
    """Chromium localStorage value: 1 flag byte (0 = UTF-16LE, 1 =
    Latin-1) + text. The flag is sniffed rather than trusted — a valid
    JSON object is the real acceptance test. Returns the dict or None."""
    if not value:
        return None
    body = value[1:] if value[0] in (0, 1) else value
    for text in (body.decode("utf-16-le", errors="ignore"),
                 body.decode("latin-1", errors="ignore")):
        try:
            profile = json.loads(text)
        except ValueError:
            continue
        if isinstance(profile, dict):
            return profile
    return None


# ---------------------------------------------------------------------------
# addon filtering, naming, ordering

def normalize_base(url: str) -> str:
    """A transport URL as stored in stremio_addons: the manifest.json
    suffix strips off, everything else must stay (debrid keys ride in
    the path). Same rules as the Config.stremio_addons setter."""
    base = str(url or "").strip().rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")].rstrip("/")
    if base.startswith(("http://", "https://")):
        return base
    return ""


def _has_stream_resource(manifest: dict) -> bool:
    """Stremio manifests declare resources either as a list of
    strings/objects (objects key them by "name") or as an object keyed
    by resource name."""
    res = manifest.get("resources")
    if isinstance(res, dict):
        return "stream" in res
    if isinstance(res, list):
        for item in res:
            if item == "stream" or (
                    isinstance(item, dict)
                    and str(item.get("name") or item.get("id") or "")
                    == "stream"):
                return True
    return False


def provider_from_url(url: str) -> str:
    """Which debrid service an addon instance is bound to, parsed from
    its URL: Torrentio carries it as a path parameter (…|torbox=KEY/),
    Debridio as a base64 JSON config segment ({"provider":"torbox"}).
    The URL is matched case-sensitively where it must be (base64) and
    insensitively everywhere else."""
    url = str(url or "")
    low = url.lower()
    for token, label in _DEBRID_TOKENS:
        if re.search(r"%s=" % token, low):
            return label
    for segment in urlparse(url).path.split("/"):
        if not _B64_SEGMENT_RE.match(segment):
            continue
        padded = segment.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        try:
            decoded = json.loads(base64.b64decode(padded))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(decoded, dict):
            prov = str(decoded.get("provider") or "").lower()
            for token, label in _DEBRID_TOKENS:
                if prov == token:
                    return label
    return ""


def describe_addon(url: str, manifest_name: str = "") -> str:
    """Human label for a list row: the addon's own manifest name when
    it has one, else the familiar host name — plus the bound debrid
    service when the name doesn't already say it (manifests use short
    tags like "TB" / "PM" for that)."""
    host = urlparse(str(url or "")).hostname or ""
    base = str(manifest_name or "").strip() \
        or _HOST_NAMES.get(host) or host or str(url)
    provider = provider_from_url(url)
    if provider:
        tags = set(_PROVIDER_TAGS.get(provider, ()))
        tags.add(provider.lower())
        tags.add(provider.replace("-", "").lower())
        words = set(re.split(r"\W+", base.lower()))
        if not words & tags:
            base = "%s \u2014 %s" % (base, provider)
    return base


def priority_sort(entries: list) -> list:
    """Default order for a first-time import: the incumbent Torrentio
    family first, then Debridio, anything else last; TorBox before
    Premiumize within a family (the user's stated preference); name as
    the quiet tiebreak. The user's own arrangement always wins after
    that first fill."""
    def key(entry):
        host = urlparse(str(entry.get("url") or "")).hostname or ""
        return (
            _FAMILY_RANK.get(host, 2),
            _PROVIDER_RANK.get(entry.get("provider") or "", 2),
            str(entry.get("name") or "").lower(),
        )
    return sorted(entries, key=key)


# ---------------------------------------------------------------------------
# where Stremio lives

def _leveldb_dirs() -> list:
    """Stremio's WebView2 localStorage leveldb directories, using the
    same install discovery as the server.js patch ( LOCALAPPDATA
    install, Program Files, registry InstallLocation)."""
    from . import streampatch
    roots, seen = [], set()
    for candidate in streampatch._server_js_candidates():
        root = os.path.dirname(candidate)
        if root and root.lower() not in seen:
            seen.add(root.lower())
            roots.append(root)
    out = []
    for root in roots:
        try:
            shells = sorted(n for n in os.listdir(root)
                            if n.endswith(".WebView2"))
        except OSError:
            continue
        for shell in shells:
            leveldb = os.path.join(root, shell, "EBWebView", "Default",
                                   "Local Storage", "leveldb")
            if os.path.isdir(leveldb):
                out.append(leveldb)
    return out


def discover_stream_addons():
    """The stream addons installed in the desktop Stremio on this
    machine: [{url, name, provider, manifest_name}] in profile order.

    None = no readable Stremio profile (not installed, or a format we
    don't parse) — callers fall back to manual entry. [] = profile
    read fine, but no stream addons installed. Catalog/meta/subtitle
    addons and loopback hosts (the local addon) are filtered out —
    only addons that can serve /stream/series/… are useful here.
    """
    for leveldb in _leveldb_dirs():
        profile = _decode_profile(_profile_bytes(leveldb))
        if profile is None:
            continue
        out, seen = [], set()
        for entry in profile.get("addons") or []:
            if not isinstance(entry, dict):
                continue
            manifest = entry.get("manifest") or {}
            if not isinstance(manifest, dict) \
                    or not _has_stream_resource(manifest):
                continue
            base = normalize_base(str(entry.get("transportUrl") or ""))
            if not base:
                continue
            host = urlparse(base).hostname or ""
            if host in ("127.0.0.1", "localhost", "::1"):
                continue
            if base in seen:
                continue
            seen.add(base)
            provider = provider_from_url(base)
            out.append({
                "url": base,
                "name": describe_addon(base,
                                       str(manifest.get("name") or "")),
                "provider": provider,
                "manifest_name": str(manifest.get("name") or ""),
            })
        return out                    # first readable profile decides
    return None
