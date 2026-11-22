# -*- coding: utf-8 -*-
"""Offline tests for the Android APK's Python bridge (bridge.py).

Covers the Nov 2026 Live TV work:
  [1] country tokens come from the SHARED src/filters.py helper
  [2] categories carry their country; channels carry category + country
  [3] "all channels" browsing (country filter) + session cache
  [4] text search: case-insensitive, multi-word, country-narrowed, capped
  [5] favorites: toggle, persist to settings.json, survive a relaunch
  [6] live URL pair for the native ExoPlayer screen (HLS + TS fallback)
  [7] UI guard: pressing Enter in the live search box can only render
      results — no playback call exists in that path

No network: the Xtream client is a stub.  Run:

    .venv\\Scripts\\python.exe test_android_bridge.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
AG_DIR = os.path.join(ROOT, "android-gradle")
PY_DIR = os.path.join(AG_DIR, "app", "src", "main", "python")
CORE_DIR = os.path.join(PY_DIR, "michaeltv_core")
INDEX_HTML = os.path.join(AG_DIR, "app", "src", "main", "assets", "www",
                          "index.html")

# the core modules are generated; make sure a fresh checkout has them
if not os.path.isfile(os.path.join(CORE_DIR, "filters.py")):
    subprocess.run([sys.executable, os.path.join(AG_DIR, "prepare_core.py")],
                   check=True)
sys.path.insert(0, PY_DIR)

import bridge as apk_bridge  # noqa: E402
from michaeltv_core.filters import extract_country  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("   [%s]" % detail) if detail else ""))


CATS = [
    {"category_id": "1", "category_name": "USA | News"},
    {"category_id": "2", "category_name": "UK : Sports"},
    {"category_id": "3", "category_name": "France \u00bb Cinema"},
]

DATA = [
    {"stream_id": 101, "name": "CNN", "stream_icon": "http://x/cnn.png",
     "category_id": "1"},
    {"stream_id": 102, "name": "Fox News", "stream_icon": "",
     "category_id": "1"},
    {"stream_id": 201, "name": "Sky Sports News", "stream_icon": "",
     "category_id": "2"},
    {"stream_id": 301, "name": "Canal+", "stream_icon": "",
     "category_id": "3"},
]


class FakeClient:
    """The slice of XtreamClient the bridge uses, with call counters."""

    def __init__(self):
        self.category_calls = 0
        self.stream_calls = []          # category_id per live_streams call
        self.timeout = 20

    def authenticate(self):
        from types import SimpleNamespace
        return SimpleNamespace(status="Active", exp_date="0",
                               max_connections="2", active_cons="0",
                               is_trial="0", username="u")

    def live_categories(self):
        self.category_calls += 1
        return [dict(c) for c in CATS]

    def live_streams(self, category_id=None):
        self.stream_calls.append(category_id)
        if category_id is None:
            return [dict(r) for r in DATA]
        return [dict(r) for r in DATA
                if str(r["category_id"]) == str(category_id)]

    def live_url(self, stream_id, ext="ts"):
        return "http://panel.example/live/u/p/%s.%s" % (stream_id, ext)


def fresh(tmp):
    b = apk_bridge.Bridge(tmp)
    fake = FakeClient()
    b._xt_client = lambda: fake
    return b, fake


def call(b, cmd, args=None):
    return json.loads(b.rpc(cmd, json.dumps(args or {})))


def main():
    tmp = tempfile.mkdtemp(prefix="mtp-bridge-")
    b, fake = fresh(tmp)

    print("[1] country tokens use the desktop app's shared helper")
    check("'USA | News' -> USA", extract_country("USA | News") == "USA")
    check("'AR : Sports' -> AR", extract_country("AR : Sports") == "AR")
    check("'UK - News' -> UK", extract_country("UK - News") == "UK")
    check("empty stays empty", extract_country("") == "")

    print("[2] categories + channels carry country/category")
    cats = call(b, "xt_categories")["categories"]
    check("three categories returned", len(cats) == 3)
    check("category countries extracted",
          [c["country"] for c in cats] == ["USA", "UK", "France"])
    rows = call(b, "xt_channels", {"category_id": "1"})["channels"]
    check("one category returns its own channels", len(rows) == 2)
    check("channels inherit their category name",
          all(r["category"] == "USA | News" for r in rows))
    check("channels inherit their country",
          all(r["country"] == "USA" for r in rows))
    check("channel rows keep stream_id/name/logo",
          {r["stream_id"] for r in rows} == {101, 102}
          and any(r["logo"] for r in rows))

    print("[3] all-channels browsing + session cache")
    every = call(b, "xt_channels", {"category_id": "", "country": ""})
    check("whole library returned", every["total"] == 4 and every["all"])
    uk = call(b, "xt_channels", {"category_id": "", "country": "UK"})
    check("country filter narrows to that country",
          [r["stream_id"] for r in uk["channels"]] == [201])
    check("country filter reads category_id=None from the provider",
          fake.stream_calls[-1] is None)
    before = fake.stream_calls.count(None)
    call(b, "xt_channels", {"category_id": "", "country": "France"})
    check("the big downloaded list is cached for the session",
          fake.stream_calls.count(None) == before)

    print("[4] live channel text search (results only)")
    r = call(b, "xt_search_channels", {"q": "news"})
    check("search is case-insensitive and matches names + categories",
          r["total"] == 3)          # CNN (category), Fox News, Sky Sports News
    r = call(b, "xt_search_channels", {"q": "FOX news"})
    check("every word must match",
          [c["stream_id"] for c in r["channels"]] == [102])
    r = call(b, "xt_search_channels", {"q": "news", "country": "UK"})
    check("search honours the country filter",
          [c["stream_id"] for c in r["channels"]] == [201])
    r = call(b, "xt_search_channels", {"q": "a", "limit": 2})
    check("result rows are capped but the total is honest",
          len(r["channels"]) <= 2 and r["total"] > 2)
    check("empty query returns nothing",
          call(b, "xt_search_channels", {"q": "   "})["total"] == 0)
    check("prefix matches sort first",
          call(b, "xt_search_channels", {"q": "canal"})["channels"][0]
          ["stream_id"] == 301)

    print("[5] favorite channels persist")
    t = call(b, "xt_fav_toggle", {"stream_id": 101, "name": "CNN",
                                  "logo": "http://x/cnn.png",
                                  "category": "USA | News"})
    check("first tap stars the channel", t["fav"] is True
          and [f["stream_id"] for f in t["favorites"]] == [101])
    check("stored with name/logo/category for the Favorites list",
          t["favorites"][0]["name"] == "CNN"
          and t["favorites"][0]["logo"] == "http://x/cnn.png"
          and t["favorites"][0]["category"] == "USA | News")
    call(b, "xt_fav_toggle", {"stream_id": 201, "name": "Sky Sports News"})
    check("second favorite appends",
          [f["stream_id"] for f in call(b, "xt_auth")["favorites"]] == [101, 201])
    t = call(b, "xt_fav_toggle", {"stream_id": 101})
    check("second tap unstars it",
          t["fav"] is False
          and [f["stream_id"] for f in t["favorites"]] == [201])
    # star -> unstar -> star: the list must never duplicate the channel
    call(b, "xt_fav_toggle", {"stream_id": 101, "name": "CNN"})
    call(b, "xt_fav_toggle", {"stream_id": 101, "name": "CNN"})
    call(b, "xt_fav_toggle", {"stream_id": 101, "name": "CNN"})
    check("repeated star taps cannot duplicate a channel",
          [f["stream_id"] for f in call(b, "get_settings")["settings"]
           ["live_favorites"]].count(101) == 1)
    saved = json.load(open(os.path.join(tmp, "settings.json"),
                           encoding="utf-8"))
    check("favorites are written to settings.json",
          [f["stream_id"] for f in saved["live_favorites"]] == [201, 101])
    b2, _ = fresh(tmp)
    check("favorites survive an app relaunch",
          [f["stream_id"] for f in call(b2, "get_settings")["settings"]
           ["live_favorites"]] == [201, 101])
    # the Settings screen's Save posts the form fields only — favorites
    # must not be wiped by an unrelated settings save
    call(b, "set_settings", {"addons": ["https://torrentio.strem.fun"],
                             "resolution_pref": "1080", "size_demote_gb": 0,
                             "xtream_server": "", "xtream_username": "",
                             "xtream_password": ""})
    check("saving Settings keeps the favorites",
          [f["stream_id"] for f in call(b, "get_settings")["settings"]
           ["live_favorites"]] == [201, 101])
    bad = call(b, "xt_fav_toggle", {"stream_id": "nope"})
    check("a bad stream_id is a clean error, not a crash",
          "error" in bad)

    print("[6] live URL pair for the native player")
    u = call(b, "xt_url", {"stream_id": 101})
    check("primary URL is HLS (m3u8)", u["url"].endswith("/101.m3u8"))
    check("progressive .ts fallback is supplied",
          u["url_ts"].endswith("/101.ts"))

    print("[6b] live filter + DVR settings persist across relaunch")
    call(b, "set_settings", {"live_country": "USA", "live_category": "2",
                             "live_mode": "favs", "dvr_minutes": 60,
                             "cast_with_dvr": False,
                             "cast_bg_buffer": False})
    s = call(b, "get_settings")["settings"]
    check("live country/category/mode saved",
          s["live_country"] == "USA" and s["live_category"] == "2"
          and s["live_mode"] == "favs", str(
              (s["live_country"], s["live_category"], s["live_mode"])))
    check("DVR/cast settings saved",
          s["dvr_minutes"] == 60 and s["cast_with_dvr"] is False
          and s["cast_bg_buffer"] is False, str(
              (s["dvr_minutes"], s["cast_with_dvr"], s["cast_bg_buffer"])))
    b3, _ = fresh(tmp)
    s3 = call(b3, "get_settings")["settings"]
    check("all of it survives an app relaunch",
          s3["live_country"] == "USA" and s3["live_category"] == "2"
          and s3["live_mode"] == "favs" and s3["dvr_minutes"] == 60
          and s3["cast_with_dvr"] is False
          and s3["cast_bg_buffer"] is False, str(s3))
    call(b3, "set_settings", {"resolution_pref": "720"})
    s4 = call(b3, "get_settings")["settings"]
    check("a Settings save keeps the live filter/DVR choices",
          s4["resolution_pref"] == "720" and s4["live_country"] == "USA"
          and s4["live_mode"] == "favs" and s4["dvr_minutes"] == 60)
    call(b3, "set_settings", {"dvr_minutes": 7})
    check("an invalid DVR window falls back to 30 min",
          call(b3, "get_settings")["settings"]["dvr_minutes"] == 30)

    print("[6c] relay start maps local/cast URLs; failures fall back direct")
    class FakeRelay:
        token = "tok"
        local_url = "http://127.0.0.1:1/s/t/live.m3u8"
        cast_url = "http://192.168.1.20:1/s/t/live.m3u8"
        def status(self):
            return {"buffered_s": 20.0, "window_s": 1800}

    orig_start = apk_bridge.live_relay.start
    apk_bridge.live_relay.start = lambda *a, **k: FakeRelay()
    try:
        out = call(b, "xt_live_start", {"stream_id": 101})
        check("relay URLs are handed to the player",
              out.get("relay") is True
              and out["url"].startswith("http://127.0.0.1:")
              and out["cast_url"].startswith("http://192.168.1.20:"),
              str(out))
        check("direct HLS + .ts fallbacks travel with the relay URL",
              out["direct"].endswith("/101.m3u8")
              and out["url_ts"].endswith("/101.ts"))
        check("cast/DVR preferences travel too",
              out["cast_with_dvr"] is False
              and out["cast_bg_buffer"] is False)
        check("the relay session token is returned for safe teardown",
              out.get("session") == "tok", str(out))

        def boom(*a, **k):
            raise apk_bridge.live_relay.RelayError("no HLS for this panel")
        apk_bridge.live_relay.start = boom
        out = call(b, "xt_live_start", {"stream_id": 101})
        check("a relay failure still starts direct live",
              out.get("relay") is False
              and out["url"].endswith("/101.m3u8")
              and "no HLS" in out.get("error", ""), str(out))
    finally:
        apk_bridge.live_relay.start = orig_start

    print("[7] Enter in the live search box can only render results")
    html = open(INDEX_HTML, encoding="utf-8").read()
    check("Enter is wired to searchLive() only",
          "event.key==='Enter'" in html
          and "searchLive();" in html
          and "playChannel" not in html.split("function searchLive")[1]
          .split("function isFav")[0])
    check("search handler never calls the player",
          "openPlayer" not in html.split("function searchLive")[1]
          .split("function isFav")[0])
    check("country filter + favorites UI exist",
          "live-country" in html and "live-cat" in html
          and "favToggleAt" in html and "xt_search_channels" in html)
    check("playback goes through the native player bridge",
          "playNative" in html)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


main()
