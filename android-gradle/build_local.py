#!/usr/bin/env python3
"""Cold-build the native Gradle/Chaquopy Android app ON WINDOWS.

    python android-gradle/build_local.py            # everything
    python android-gradle/build_local.py tools      # toolchain + core only
    python android-gradle/build_local.py gradle     # prepare + gradle + apk

Everything lands in android-gradle/.tools/ (git-ignored); no admin, no
WSL, no Docker, no CI.  All steps are idempotent and resumable — a run
interrupted mid-download or mid-build can simply be re-run and skips
everything already on disk.

Steps:
  1. JDK 17 (Temurin zip)            -> .tools/jdk
  2. Gradle 8.4 (dist zip)           -> .tools/gradle/gradle-8.4
  3. Android cmdline-tools + SDK     -> .tools/sdk  (licenses accepted,
     platform-tools + platforms;android-34 + build-tools;34.0.0)
  4. Python 3.11 (official installer, per-user /quiet) -> .tools/python311
     — Chaquopy REQUIRES buildPython to match the target Python's
     major.minor (the app targets 3.11; the machine's default 3.12 is
     not acceptable to Chaquopy).
  5. local.properties (sdk.dir, forward slashes)
  6. prepare_core.py (copies src/ core, rewrites version, py_compile)
  7. RELEASE KEYSTORE (once): if keystore.properties is absent, generate
     michaeltv-release.jks with a random password via the toolchain JDK's
     keytool and write keystore.properties (both git-ignored).  KEEP BOTH
     FILES — Android only installs an update over an install signed with
     the SAME key; losing them means uninstall/reinstall on the phone.
  8. gradle --no-daemon :app:assembleRelease (assembleDebug when no
     keystore.properties)  (JAVA_HOME=.tools/jdk)
  9. copy app-release.apk -> MichaelTV-<version>.apk + sha256
     (debug builds keep the -debug suffix)

Stdlib only.
"""

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

AG_DIR = Path(__file__).resolve().parent
REPO_ROOT = AG_DIR.parent
TOOLS = AG_DIR / ".tools"
DL_DIR = TOOLS / "downloads"

JDK_URL = ("https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/"
           "jdk/hotspot/normal/eclipse?project=jdk")
GRADLE_URL = "https://services.gradle.org/distributions/gradle-8.4-bin.zip"
CMDLINE_TOOLS_URL = ("https://dl.google.com/android/repository/"
                     "commandlinetools-win-11076708_latest.zip")
# Last 3.11 with a Windows binary installer.
PY311_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

SDK_PACKAGES = ("platform-tools", "platforms;android-34", "build-tools;34.0.0")

IS_WIN = (os.name == "nt")
JAVA = "java.exe" if IS_WIN else "java"
GRADLE_LAUNCHER = "gradle.bat" if IS_WIN else "gradle"
SDKMANAGER = "sdkmanager.bat" if IS_WIN else "sdkmanager"


def log(msg):
    print(msg, flush=True)


def fail(msg):
    print("build_local: ERROR: %s" % msg, file=sys.stderr, flush=True)
    sys.exit(1)


# ----------------------------------------------------------------------
# downloads / unzip

def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log("[skip] already downloaded: %s (%d MB)"
            % (dest.name, dest.stat().st_size >> 20))
        return dest
    tmp = dest.with_name(dest.name + ".part")
    log("[down] %s" % url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "MichaelTVPlayer-build/1.0"})
    try:
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, t0 = 0, time.time()
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    log("       %3d%%  %d/%d MB  (%.0fs)"
                        % (done * 100 // total, done >> 20, total >> 20,
                           time.time() - t0))
                elif done % (16 << 20) < (1 << 20):
                    log("       %d MB  (%.0fs)"
                        % (done >> 20, time.time() - t0))
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        fail("download failed (%s): %r" % (url, exc))
    os.replace(tmp, dest)
    log("[ok  ] %s (%d MB)" % (dest.name, dest.stat().st_size >> 20))
    return dest


def unzip(zip_path, dest_dir):
    log("[zip ] %s -> %s" % (zip_path.name, dest_dir))
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)


def run(cmd, env=None, input_text=None, timeout_s=1800, name=None):
    name = name or cmd[0]
    log("[run ] %s" % " ".join(str(c) for c in cmd[:6]))
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], env=env, input=input_text,
            capture_output=True, text=True, timeout=timeout_s,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        fail("%s timed out after %ds" % (name, timeout_s))
    tail = (proc.stdout or "").strip().splitlines()[-12:]
    for line in tail:
        log("       %s" % line)
    if proc.returncode not in (0, 3010):
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        fail("%s failed with exit code %d" % (name, proc.returncode))
    return proc


# ----------------------------------------------------------------------
# 1. JDK 17

def jdk_dir():
    return TOOLS / "jdk"


def ensure_jdk():
    if (jdk_dir() / "bin" / JAVA).is_file():
        log("[skip] JDK 17 at %s" % jdk_dir())
        return jdk_dir()
    z = download(JDK_URL, DL_DIR / "jdk-17-temurin-windows-x64.zip")
    unpack = TOOLS / "jdk-unpack"
    shutil.rmtree(unpack, ignore_errors=True)
    unzip(z, unpack)
    inner = [d for d in unpack.iterdir()
             if d.is_dir() and d.name.startswith("jdk-")]
    if len(inner) != 1:
        fail("JDK zip layout unexpected (found %d jdk-* dirs)" % len(inner))
    if jdk_dir().exists():
        shutil.rmtree(jdk_dir())
    inner[0].rename(jdk_dir())
    shutil.rmtree(unpack, ignore_errors=True)
    if not (jdk_dir() / "bin" / JAVA).is_file():
        fail("JDK unpack incomplete")
    log("[ok  ] JDK 17 at %s" % jdk_dir())
    return jdk_dir()


# ----------------------------------------------------------------------
# 2. Gradle 8.4

def gradle_exe():
    return TOOLS / "gradle" / "gradle-8.4" / "bin" / GRADLE_LAUNCHER


def ensure_gradle():
    if gradle_exe().is_file():
        log("[skip] Gradle 8.4 at %s" % gradle_exe())
        return gradle_exe()
    z = download(GRADLE_URL, DL_DIR / "gradle-8.4-bin.zip")
    gdir = TOOLS / "gradle"
    shutil.rmtree(gdir, ignore_errors=True)
    unzip(z, gdir)
    if not gradle_exe().is_file():
        fail("Gradle unpack incomplete")
    log("[ok  ] Gradle 8.4 at %s" % gradle_exe())
    return gradle_exe()


# ----------------------------------------------------------------------
# 3. Android SDK (cmdline-tools restructure + licenses + packages)

def sdk_dir():
    return TOOLS / "sdk"


def sdkmanager_exe():
    return sdk_dir() / "cmdline-tools" / "latest" / "bin" / SDKMANAGER


def sdk_env(jdk):
    env = os.environ.copy()
    env["JAVA_HOME"] = str(jdk)
    return env


def ensure_sdk(jdk):
    if not sdkmanager_exe().is_file():
        z = download(CMDLINE_TOOLS_URL, DL_DIR / "commandlinetools-win.zip")
        unpack = TOOLS / "sdk-unpack"
        shutil.rmtree(unpack, ignore_errors=True)
        unzip(z, unpack)
        inner = unpack / "cmdline-tools"
        if not inner.is_dir():
            fail("cmdline-tools zip layout unexpected")
        ct = sdk_dir() / "cmdline-tools"
        ct.mkdir(parents=True, exist_ok=True)
        target = ct / "latest"
        if target.exists():
            shutil.rmtree(target)
        inner.rename(target)
        shutil.rmtree(unpack, ignore_errors=True)
        if not sdkmanager_exe().is_file():
            fail("cmdline-tools unpack incomplete")
        log("[ok  ] cmdline-tools at %s" % sdkmanager_exe())
    else:
        log("[skip] cmdline-tools present")

    env = sdk_env(jdk)
    if not (sdk_dir() / "licenses" / "android-sdk-license").is_file():
        run([sdkmanager_exe(), "--licenses"], env=env, input_text="y\n" * 50,
            name="sdkmanager --licenses")
        log("[ok  ] SDK licenses accepted")
    else:
        log("[skip] SDK licenses already accepted")

    have = ((sdk_dir() / "platform-tools" / ("adb.exe" if IS_WIN else "adb")).is_file()
            and (sdk_dir() / "platforms" / "android-34" / "android.jar").is_file()
            and (sdk_dir() / "build-tools" / "34.0.0").is_dir())
    if not have:
        run([sdkmanager_exe()] + list(SDK_PACKAGES), env=env,
            input_text="y\n" * 10, name="sdkmanager packages")
        log("[ok  ] SDK packages installed")
    else:
        log("[skip] SDK packages already installed")


def write_local_properties():
    p = AG_DIR / "local.properties"
    p.write_text("sdk.dir=%s\n" % sdk_dir().resolve().as_posix(),
                 encoding="utf-8")
    log("[ok  ] local.properties (sdk.dir=%s)" % sdk_dir().resolve().as_posix())


# ----------------------------------------------------------------------
# 4. Python 3.11 buildPython (Chaquopy: major.minor must match target 3.11)

def py311_dir():
    return TOOLS / "python311"


def python_version(exe):
    try:
        proc = subprocess.run(
            [str(exe), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def ensure_build_python():
    """Return a Python 3.11 command path for Chaquopy's buildPython."""
    # anything already suitable on the machine?
    candidates = [py311_dir() / "python.exe", Path(sys.executable or "."),
                  shutil.which("python"), shutil.which("py")]
    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        ver = python_version(cand)
        if ver == "3.11":
            found = str(Path(cand).resolve()).replace("\\", "/")
            log("[ok  ] buildPython: %s (3.11)" % found)
            return found

    log("[....] no Python 3.11 found — installing per-user (quiet) "
        "into %s" % py311_dir())
    installer = download(PY311_URL, DL_DIR / "python-3.11.9-amd64.exe")
    run([installer, "/quiet",
         "TargetDir=%s" % py311_dir(),
         "InstallAllUsers=0", "PrependPath=0",
         "Include_launcher=0", "InstallLauncherAllUsers=0",
         "Include_test=0", "Include_doc=0", "Include_dev=0",
         "Include_symbols=0", "Include_debug=0", "Include_pip=1",
         "SimpleInstall=1"],
        name="python-3.11.9 installer")
    exe = py311_dir() / "python.exe"
    if python_version(exe) != "3.11":
        fail("Python 3.11 install failed (%s missing)" % exe)
    found = str(exe.resolve()).replace("\\", "/")
    log("[ok  ] buildPython: %s (3.11)" % found)
    return found


# ----------------------------------------------------------------------
# 6. core preparation

def read_app_version():
    text = (REPO_ROOT / "src" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        fail('no APP_VERSION in src/config.py')
    return m.group(1)


def run_prepare_core():
    if not sys.executable:
        fail("no running interpreter to launch prepare_core.py")
    run([sys.executable, AG_DIR / "prepare_core.py"], name="prepare_core.py")


# ----------------------------------------------------------------------
# 7/8/9. release keystore, gradle build + apk

KEYSTORE_FILE = "michaeltv-release.jks"
KEYSTORE_ALIAS = "michaeltv"


def release_mode():
    return (AG_DIR / "keystore.properties").is_file()


def ensure_release_keystore(jdk):
    """First run: generate the release keystore + keystore.properties."""
    props = AG_DIR / "keystore.properties"
    if props.is_file():
        log("[skip] release keystore configured (%s)" % props.name)
        return
    ks = AG_DIR / KEYSTORE_FILE
    if ks.exists():
        fail("%s exists but keystore.properties is missing — recreate "
             "keystore.properties pointing at it (the key cannot be "
             "regenerated)" % ks.name)
    password = secrets.token_urlsafe(18)
    keytool = jdk / "bin" / ("keytool.exe" if IS_WIN else "keytool")
    run([keytool, "-genkeypair", "-v",
         "-keystore", ks, "-alias", KEYSTORE_ALIAS,
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
         "-storepass", password, "-keypass", password,
         "-dname", "CN=MichaelTV, O=MichaelTV, C=US"],
        name="keytool")
    props.write_text(
        "storeFile=%s\nstorePassword=%s\nkeyAlias=%s\nkeyPassword=%s\n"
        % (KEYSTORE_FILE, password, KEYSTORE_ALIAS, password),
        encoding="utf-8")
    log("[ok  ] release keystore created: %s (alias %s)"
        % (ks.name, KEYSTORE_ALIAS))
    log("       KEEP %s and %s — updates must be signed with the SAME"
        % (ks.name, props.name))
    log("       key, losing them means uninstall/reinstall on the phone.")


def run_gradle(jdk, build_python):
    env = os.environ.copy()
    env["JAVA_HOME"] = str(jdk)
    env["GRADLE_USER_HOME"] = str((AG_DIR / ".gradle").resolve())
    task = ":app:assembleRelease" if release_mode() else ":app:assembleDebug"
    cmd = [gradle_exe(), "--no-daemon", "--console=plain",
           "-p", AG_DIR, task,
           "-PbuildPython=" + build_python]
    log("[run ] %s" % " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], env=env,
                            cwd=str(AG_DIR))
    rc = proc.wait()
    if rc != 0:
        fail("gradle failed with exit code %d" % rc)


def finalize_apk():
    version = read_app_version()
    if release_mode():
        src = (AG_DIR / "app" / "build" / "outputs" / "apk" / "release"
               / "app-release.apk")
        dst = AG_DIR / ("MichaelTV-%s.apk" % version)
    else:
        src = (AG_DIR / "app" / "build" / "outputs" / "apk" / "debug"
               / "app-debug.apk")
        dst = AG_DIR / ("MichaelTV-%s-debug.apk" % version)
    if not src.is_file():
        fail("APK not found at %s" % src)
    shutil.copyfile(src, dst)
    sha = hashlib.sha256(dst.read_bytes()).hexdigest()
    log("[ok  ] APK ready")
    log("       path   : %s" % dst)
    log("       size   : %d bytes (%.1f MB)"
        % (dst.stat().st_size, dst.stat().st_size / (1024 * 1024)))
    log("       sha256 : %s" % sha)
    return dst


# ----------------------------------------------------------------------

def main():
    step = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    if step not in ("all", "tools", "gradle"):
        fail("usage: build_local.py [all|tools|gradle]")

    t0 = time.time()
    jdk = ensure_jdk()
    ensure_gradle()
    ensure_sdk(jdk)
    write_local_properties()
    build_python = ensure_build_python()
    run_prepare_core()
    ensure_release_keystore(jdk)
    if step == "tools":
        log("[done] toolchain + core prepared (gradle skipped) "
            "(%.0fs)" % (time.time() - t0))
        return
    run_gradle(jdk, build_python)
    finalize_apk()
    log("[done] build complete (%.0fs)" % (time.time() - t0))


if __name__ == "__main__":
    main()
