# -*- coding: utf-8 -*-
"""Dump the active monitor's EDID: model name, preferred timing, and the
HDMI VSDB max TMDS clock (the link-capability cap that explains 4K@30)."""
import struct
import winreg

BASE = r"SYSTEM\CurrentControlSet\Enum\DISPLAY\KTC0000"


def dtd_hz(b):
    px = struct.unpack("<H", b[0:2])[0] * 10          # kHz
    if not px:
        return None
    hbl = b[6] + (b[5] & 0xF0) * 16
    vbl = b[9] + (b[5] & 0x0F) * 256
    hact = b[4] | ((b[6 + 1] if False else 0))
    hact = b[2] | ((b[4] & 0xF0) << 4)
    vact = b[5] & 0x0F, b[3] | ((b[4] & 0x0F) << 8)
    vact = b[3] | ((b[4] & 0x0F) << 8)
    if hbl * vbl == 0:
        return None
    return (hact, vact, round(px * 1000 / (hbl * vbl), 1), px / 1000.0)


def parse(tag, edid):
    print("===", tag, "len", len(edid))
    e = edid[:128]
    name = ""
    for d in (54, 72, 90, 108):
        blk = e[d:d + 18]
        if blk[0] == 0 and blk[1] == 0 and blk[3] in (0xFC, 0xFF):
            name = blk[5:18].rstrip(b"\n\x00").decode("ascii", "replace")
    print("  name:", name)
    pref = dtd_hz(e[54:72])
    print("  preferred DTD:", pref)
    if len(edid) >= 256:
        cta = edid[128:256]
        if cta[:2] == b"\x02\x03":
            print("  CTA-861 rev", cta[2], "-", cta[3], "DTDs")
            i = 4
            dtd_count = 0
            while i < 128 and cta[i] != 0:
                # data block collection
                blklen = cta[i] & 0x1F
                tagv = cta[i] >> 5
                if tagv == 2:      # Video Data Block
                    for j in range(i + 1, i + 1 + blklen):
                        vic = cta[j]
                        if vic <= 64:
                            print("    VIC", vic)
                if tagv == 3:      # Vendor-Specific Data Block
                    ouis = " ".join("%02X" % b for b in cta[i + 1:i + 4])
                    if cta[i + 1:i + 4] == b"\x00\x0D\xC0" or \
                            cta[i + 1:i + 4] == b"\xC0\x0D\x00":
                        pass_ = cta[i + 4:i + 4 + blklen]
                        if blklen >= 6:
                            tmds = (pass_[3] << 8 | pass_[4]) * 5
                            print("    HDMI VSDB: max TMDS", tmds, "MHz")
                i += 1 + blklen
            # detailed timings after the DBCs
            while i + 18 <= 128 and cta[i] not in (0,) and dtd_count < 8:
                t = dtd_hz(cta[i:i + 18])
                if t:
                    print("    CTA DTD:", t)
                dtd_count += 1
                i += 18


for sub in ("1&8713bca&0&UID0", "5&bc9d68a&0&UID41216"):
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           BASE + "\\" + sub + "\\Device Parameters")
        edid = winreg.QueryValueEx(k, "EDID")[0]
        parse(sub, edid)
    except OSError as exc:
        print(sub, "err", exc)
