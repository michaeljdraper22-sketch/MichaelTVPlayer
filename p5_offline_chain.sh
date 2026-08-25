#!/bin/bash
# P5 offline verification chain: suites -> full harness -> quick x2
PY=.venv/Scripts/python.exe
cd /d/Coding/MichaelTVPlayer || exit 9

SUITES="test_always_chase test_anchor_store test_audio_tracks test_bundled_ccx test_caption_overlay test_catchup test_cuestore test_dvr_e2e test_fixes test_overlay_focus test_profanity test_startup_defaults test_sub_settings test_subtitles test_tab_resize test_temp_cleanup test_vod_series test_vod_splitter test_wedge_cluster"

echo "===== OFFLINE SUITES $(date +%H:%M:%S) ====="
for s in $SUITES; do
  echo "--- $s ---"
  $PY -X utf8 "$s.py" > "p5_out_$s.txt" 2>&1
  rc=$?
  tail -3 "p5_out_$s.txt"
  echo "[$s rc=$rc]"
done

echo "===== HARNESS FULL $(date +%H:%M:%S) ====="
$PY -X utf8 test_sync_adversarial.py > p5_harness_full.out 2>&1
echo "[full rc=$?]"
tail -8 p5_harness_full.out

echo "===== HARNESS QUICK 1 $(date +%H:%M:%S) ====="
$PY -X utf8 test_sync_adversarial.py --quick > p5_harness_quick1.out 2>&1
echo "[quick1 rc=$?]"
tail -8 p5_harness_quick1.out

echo "===== HARNESS QUICK 2 $(date +%H:%M:%S) ====="
$PY -X utf8 test_sync_adversarial.py --quick > p5_harness_quick2.out 2>&1
echo "[quick2 rc=$?]"
tail -8 p5_harness_quick2.out

echo "===== CHAIN DONE $(date +%H:%M:%S) ====="
