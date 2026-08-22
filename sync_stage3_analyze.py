# -*- coding: utf-8 -*-
r"""Stage-3 analysis: parse the mtp.sync log -> acceptance report.

Reads %APPDATA%\MichaelTVPlayer\sync_debug.log (or argv[1]) and reports,
per PHASE:
  - anchor innovation from ANCHOR rows (target - off before the step;
    stage 3 applies ONE deferred decision per arrival flush)
  - lag L(t) trajectory (one clean sample per flush)
  - caption display stops (since_show gaps), watchdog/anchor REBASEs
  - CLOCK branch histogram + divergence
  - transport events: commanded vs raw 1 s after (no-op detection)
"""
import os
import re
import sys
from collections import Counter

LOG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("APPDATA") or "", "MichaelTVPlayer", "sync_debug.log")

TS = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) ")


def tsec(line):
    m = TS.match(line)
    if not m:
        return None
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def kv(line):
    out = {}
    for m in re.finditer(r"([A-Za-z_0-9]+)=([+-]?[\d.]+)(?![\d.])", line):
        out[m.group(1)] = float(m.group(2))
    m = re.search(r"off=(-?[\d.]+)->", line)
    if m:
        out["off_before"] = float(m.group(1))
    m = re.search(r"after ([\d.]+) s blank", line)
    if m:
        out["after"] = float(m.group(1))
    return out


phases = []
rows, cues, anchors, clocks, xports, paints = [], [], [], [], [], []
rebases, snaps = [], []

with open(LOG, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        t = tsec(line)
        if t is None:
            continue
        if " PHASE " in line:
            phases.append((t, line.split(" PHASE ", 1)[1].strip()))
            continue
        body = line[13:]
        if body.startswith("TICK "):
            rows.append((t, kv(line)))
        elif body.startswith("CUE "):
            cues.append((t, kv(line), "fresh" in line.split("|")[0]))
        elif body.startswith("ANCHOR "):
            d = kv(line)
            if "off" in d and "target" in d:
                d["innov"] = d["target"] - d["off"]
            anchors.append((t, d))
        elif body.startswith("CLOCK "):
            d = kv(line)
            d["branch"] = body.split()[1]
            clocks.append((t, d))
        elif body.startswith("XPORT ") or body.startswith("XPORT1s "):
            d = kv(line)
            d["tag"] = body.split()[1]
            d["pre"] = body.startswith("XPORT ")
            xports.append((t, d))
        elif body.startswith("REBASE"):
            rebases.append((t, kv(line), line.strip()))
        elif body.startswith("EDGESNAP"):
            snaps.append((t, kv(line)))
        elif body.startswith("PAINT"):
            paints.append((t, kv(line)))


def phase_name(t):
    cur = "pre"
    for pt, name in phases:
        if t >= pt:
            cur = name.split()[0]
    return cur


print(f"log: {LOG}")
print(f"phases={len(phases)} ticks={len(rows)} cues={len(cues)} "
      f"anchors={len(anchors)} clocks={len(clocks)} xports={len(xports)} "
      f"rebases={len(rebases)} edgesnaps={len(snaps)}")

print("\n== TRANSPORT EVENTS (target vs raw 1 s after) ==")
pend = {}
for t, d in xports:
    tag = d["tag"]
    key = (tag, round(t))
    if d["pre"]:
        pend[key] = (t, d)
    else:
        pre = None
        for k in list(pend):
            if k[0] == tag and t - pend[k][0] < 5:
                pre = pend.pop(k)
        if pre:
            tp, dp = pre
            tgt = dp.get("target", -1)
            after = d.get("raw", -1)
            moved = "MOVED" if abs(after - tgt) < 8 else "NO-OP?"
            print(f"  {tag:12s} target={tgt:8.1f} raw_1s={after:8.1f} "
                  f"{moved}")

print("\n== ANCHOR innovation (deferred decisions; |innov| <= ~1.5) ==")
by_ph = {}
for t, d in anchors:
    if "innov" in d:
        by_ph.setdefault(phase_name(t), []).append(d["innov"])
for ph, vals in by_ph.items():
    sv = sorted(abs(v) for v in vals)
    n = len(sv)
    if not n:
        continue
    signed = sum(vals) / n
    print(f"  {ph:10s} n={n:4d} |innov| p50={sv[n // 2]:.2f} "
          f"p95={sv[min(n - 1, int(n * 0.95))]:.2f} max={sv[-1]:.2f} "
          f"signed_mean={signed:+.2f} "
          f"{'OK' if sv[min(n - 1, int(n * 0.95))] <= 1.5 else 'CHECK'}")

print("\n== L(t) measured CCX lag (from ANCHOR rows) ==")
for ph in dict.fromkeys(phase_name(t) for t, d in anchors):
    lags = [d["L"] for t, d in anchors
            if phase_name(t) == ph and "L" in d]
    if lags:
        print(f"  {ph:10s} n={len(lags):4d} first={lags[0]:6.1f} "
              f"avg={sum(lags) / len(lags):6.1f} last={lags[-1]:6.1f}")

print("\n== CAPTION DISPLAY STOPS (TICK since_show > 8) ==")
stops = 0
for t, d in rows:
    ss = d.get("since_show", None)
    if ss is not None and ss > 8:
        stops += 1
        if stops <= 10:
            print(f"  {phase_name(t):10s} since_show={ss:.0f}s "
                  f"cc={d.get('cc', -1):.1f} off_v={d.get('d_fr_cc', 0):+.1f}")
print(f"  total stop-ticks: {stops}")

print("\n== WATCHDOG / REBASE / EDGESNAP events ==")
for t, d, line in rebases:
    print(f"  {phase_name(t):10s} {line[13:].strip()}")
for t, d in snaps:
    print(f"  {phase_name(t):10s} EDGESNAP err={d.get('err')}")
if not rebases and not snaps:
    print("  (none)")

print("\n== CLOCK branches / divergence ==")
cph = Counter((phase_name(t), d["branch"]) for t, d in clocks)
for (ph, br), n in sorted(cph.items()):
    print(f"  {ph:10s} {br:9s} x{n}")
divs = [(phase_name(t), d["div"]) for t, d in clocks if "div" in d]
if divs:
    print(f"  div: first={divs[0][1]:+.2f} last={divs[-1][1]:+.2f} "
          f"max|div|={max(abs(v) for _, v in divs):.2f}")
