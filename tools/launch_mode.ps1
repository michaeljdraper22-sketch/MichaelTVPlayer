# MichaelTV launch-mode switch (dev <-> dist) for THIS machine.
#
# Flips the user's MichaelTV shortcuts between:
#   dev  = source launch  .venv\Scripts\pythonw.exe "<repo>\main.py"
#          (code changes take effect on the next app relaunch; no build)
#   dist = built exe      dist\MichaelTV.exe
#
# Reversible, per-user (Desktop + Start Menu .lnk -- never needs admin).
# The app itself keeps both integrations working in either mode: on
# every start it re-points Stremio's server.js patch and re-registers
# the .m3u "Open with" ProgId at whatever it is running from.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1                # status
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1 -Mode dev
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\launch_mode.ps1 -Mode dist
#
# Optional overrides (used by test_dev_mode.py; leave off for real flips):
#   -Root <dir> -DesktopDir <dir> -StartMenuDir <dir>

param(
    [ValidateSet("dev", "dist", "status")]
    [string]$Mode = "status",
    [string]$Root = "",
    [string]$DesktopDir = "",
    [string]$StartMenuDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }

$pyw     = Join-Path $Root ".venv\Scripts\pythonw.exe"
$mainPy  = Join-Path $Root "main.py"
$vlcDll  = Join-Path $Root "vlc\libvlc.dll"
$exe     = Join-Path $Root "dist\MichaelTV.exe"
$icon    = Join-Path $Root "assets\icon.ico"

# --- what counts as "a MichaelTV shortcut of this repo" -------------------
# (param must stay untyped: callers pass a live WshShortcut COM object --
# a [string] coercion would stringify it and .TargetPath would be $null)
function Test-MichaelTVShortcut($s) {
    $t = $s.TargetPath
    if (-not $t) { return $false }
    if ($t -ieq $exe) { return $true }                        # dist target
    if ($t -ieq $pyw) { return $true }                        # dev target
    return $false
}

$sh = New-Object -ComObject WScript.Shell

if (-not $DesktopDir)   { $DesktopDir   = [Environment]::GetFolderPath('Desktop') }
if (-not $StartMenuDir) { $StartMenuDir = [Environment]::GetFolderPath('Programs') }
$pinnedDir = "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned"

# --- status ---------------------------------------------------------------
if ($Mode -eq "status") {
    $targets = @()
    foreach ($dir in @($DesktopDir, $StartMenuDir)) {
        Get-ChildItem $dir -Recurse -Filter *.lnk -EA SilentlyContinue | ForEach-Object {
            $s = $sh.CreateShortcut($_.FullName)
            if (Test-MichaelTVShortcut $s) { $targets += [pscustomobject]@{ Path = $_.FullName; Target = $s.TargetPath; Args = $s.Arguments } }
        }
    }
    if (-not $targets) {
        Write-Output "No MichaelTV shortcuts found (Desktop: $DesktopDir, Start menu: $StartMenuDir)."
        Write-Output "Run with -Mode dev or -Mode dist to create the Desktop shortcut."
        exit 0
    }
    foreach ($t in $targets) {
        $isDev = ($t.Target -ieq $pyw)
        $modeLabel = if ($isDev) { "DEV (source)" } else { "DIST (built exe)" }
        Write-Output ("{0}" -f $t.Path)
        Write-Output ("    mode : {0}" -f $modeLabel)
        Write-Output ("    shape: `"{0}`" {1}" -f $t.Target, $t.Args)
    }
    exit 0
}

# --- prerequisites --------------------------------------------------------
if ($Mode -eq "dev") {
    $missing = @()
    if (-not (Test-Path $mainPy)) { $missing += "main.py" }
    if (-not (Test-Path $pyw))    { $missing += ".venv\Scripts\pythonw.exe" }
    if (-not (Test-Path $vlcDll)) { $missing += "vlc\libvlc.dll" }
    if ($missing) {
        Write-Output ("dev mode UNAVAILABLE in {0} (missing: {1}) -- this`ncheckout cannot run from source; staying on the dist exe." -f $Root, ($missing -join ", "))
        exit 1
    }
} else {
    if (-not (Test-Path $exe)) {
        Write-Output ("dist\MichaelTV.exe not found under {0} -- run build.bat first." -f $Root)
        exit 1
    }
}

# --- find / ensure the shortcuts we manage --------------------------------
$lnkPaths = @()

# Desktop: the daily driver -- repointed when present, CREATED when not
# (Save() below writes a fresh .lnk for a staged shortcut).
$lnkPaths += (Join-Path $DesktopDir "MichaelTV.lnk")

# Start menu: repoint only entries that already exist (never creates).
Get-ChildItem $StartMenuDir -Recurse -Filter *.lnk -EA SilentlyContinue | ForEach-Object {
    $s = $sh.CreateShortcut($_.FullName)
    if ((Test-MichaelTVShortcut $s) -and ($lnkPaths -notcontains $_.FullName)) { $lnkPaths += $_.FullName }
}

# Taskbar pins are identity-keyed by Windows; report but never rewrite.
Get-ChildItem $pinnedDir -Recurse -Filter *.lnk -EA SilentlyContinue | ForEach-Object {
    $s = $sh.CreateShortcut($_.FullName)
    if (Test-MichaelTVShortcut $s) {
        Write-Output ("NOTE: taskbar pin found ({0}) -- Windows manages pins;`nunpin and re-pin after switching if it still launches the old mode." -f $_.FullName)
    }
}

# --- apply ----------------------------------------------------------------
foreach ($lnk in $lnkPaths) {
    $s = $sh.CreateShortcut($lnk)   # loads an existing file, or stages a new one
    if ($Mode -eq "dev") {
        $s.TargetPath = $pyw
        $s.Arguments = '"{0}"' -f $mainPy
        $s.WorkingDirectory = $Root
        $s.Description = "MichaelTV (dev source launch)"
    } else {
        $s.TargetPath = $exe
        $s.Arguments = ""
        $s.WorkingDirectory = Split-Path -Parent $exe
        $s.Description = "MichaelTV"
    }
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Save()
    Write-Output ("{0} -> {1}" -f $lnk, $s.TargetPath)
}

Write-Output ""
if ($Mode -eq "dev") {
    Write-Output ("Launch mode: DEV (source). Code changes take effect on the next`napp RELAUNCH -- no build.bat needed. Stremio's `"Play in MichaelTV`" button`nand the .m3u `"Open with`" entry re-point automatically on the next app start`n(restart Stremio once to pick the button change up).")
} else {
    Write-Output ("Launch mode: DIST (built exe). After code changes, run build.bat`nBEFORE relaunching, or the exe will run stale code.")
}
