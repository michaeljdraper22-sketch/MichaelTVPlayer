Get-CimInstance Win32_Process -Filter "Name='ccextractorwinfull.exe'" |
    ForEach-Object {
        $parent = Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue
        if (-not $parent) {
            Stop-Process -Id $_.ProcessId -Force
            Write-Output ("killed orphan CCX " + $_.ProcessId)
        } else {
            Write-Output ("kept CCX " + $_.ProcessId + " (parent " + $parent.ProcessName + ")")
        }
    }
