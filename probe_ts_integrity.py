"""Headless TS integrity scan: sync alignment + continuity-counter errors.

Distinguishes producer damage (VLC sout wrote garbage) from ingest damage
(the provider's stream arrived with holes). Read-only, first N MB.
"""
import sys

path = sys.argv[1]
limit = 40 * 1024 * 1024
PKT = 188

bad_sync = 0
pkt_count = 0
cc_errors = 0
cc_pkts = 0
last_cc = {}

with open(path, "rb") as fh:
    buf = fh.read(limit)

for off in range(0, len(buf) - PKT, PKT):
    b = buf[off]
    pkt_count += 1
    if b != 0x47:
        bad_sync += 1
        continue
    pid = ((buf[off + 1] & 0x1F) << 8) | buf[off + 2]
    afc = (buf[off + 3] >> 4) & 0x3
    cc = buf[off + 3] & 0xF
    if afc == 0 or afc == 2:      # no payload
        continue
    cc_pkts += 1
    prev = last_cc.get(pid)
    if prev is not None and cc != ((prev + 1) & 0xF):
        cc_errors += 1
    last_cc[pid] = cc

print(f"packets={pkt_count} bad_sync={bad_sync} "
      f"({100.0 * bad_sync / max(1, pkt_count):.2f}%)  "
      f"payload_pkts={cc_pkts} cc_errors={cc_errors} "
      f"({100.0 * cc_errors / max(1, cc_pkts):.2f}%)  pids={len(last_cc)}")
