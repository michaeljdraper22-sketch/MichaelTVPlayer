Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class DM {
  [DllImport("user32.dll")] public static extern bool EnumDisplaySettings(string dn, int i, ref DEVMODE dm);
  [DllImport("user32.dll")] public static extern bool EnumDisplayDevices(string dn, int i, ref DISPLAY_DEVICE dd, int flags);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)] public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
    public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra;
    public int dmFields; public int dmPositionX; public int dmPositionY; public int dmDisplayOrientation;
    public int dmDisplayFixedOutput; public short dmColor; public short dmDuplex; public short dmYResolution;
    public short dmTTOption; public short dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
    public short dmLogPixels; public int dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight;
    public int dmDisplayFlags; public int dmDisplayFrequency;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)] public struct DISPLAY_DEVICE {
    public int cb;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string DeviceName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceString;
    public int StateFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceID;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceKey;
  }
}
'@
$dd = New-Object DM+DISPLAY_DEVICE
$dd.cb = [Runtime.InteropServices.Marshal]::SizeOf($dd)
for ($i=0; [DM]::EnumDisplayDevices($null, $i, [ref]$dd, 0); $i++) {
  if ($dd.StateFlags -band 1) {
    Write-Host ("ACTIVE: " + $dd.DeviceName + " | " + $dd.DeviceString)
    $dm = New-Object DM+DEVMODE
    $dm.dmSize = [Runtime.InteropServices.Marshal]::SizeOf($dm)
    if ([DM]::EnumDisplaySettings($dd.DeviceName, -1, [ref]$dm)) {
      Write-Host ("  CURRENT : " + $dm.dmPelsWidth + "x" + $dm.dmPelsHeight + " @ " + $dm.dmDisplayFrequency + "Hz")
    }
    $modes = @{}
    for ($m=0; [DM]::EnumDisplaySettings($dd.DeviceName, $m, [ref]$dm); $m++) {
      if ($dm.dmPelsWidth -eq 3840 -and $dm.dmPelsHeight -eq 2160) { $modes[$dm.dmDisplayFrequency] = $true }
    }
    Write-Host ("  4K MODES AVAILABLE: " + (($modes.Keys | Sort-Object) -join ", "))
  }
}
