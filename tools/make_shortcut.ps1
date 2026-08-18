$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist\MichaelTV.exe"
if (-not (Test-Path $exe)) { Write-Error "Build first: $exe not found" }
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "MichaelTV.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $exe
$s.WorkingDirectory = Split-Path -Parent $exe
$s.IconLocation = Join-Path $root "assets\icon.ico"
$s.Description = "MichaelTVPlayer"
$s.Save()
Write-Output "shortcut created: $lnk"
