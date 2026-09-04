<#
.SYNOPSIS
  Yes, Dev engine: auto-approves Chrome's "Allow remote debugging?" consent dialog.

.DESCRIPTION
  Chrome 144+ prompts for approval on every client attach to the remote debugging
  endpoint. With several agents or automation clients attaching in parallel the
  prompts stack up, and each one blocks its client until a human clicks Allow.

  The consent dialog is its own top-level window, verified:

      hwnd=6363552  class=Chrome_WidgetWin_1  title='Allow remote debugging?'

  so finding it costs one EnumWindows sweep filtered to that class - cheap Win32
  calls, no COM, nothing that outlives the sweep. UI Automation is only touched
  once a dialog actually exists, to read the buttons and press Allow.

  That ordering matters more than it looks. This engine used to walk the UI
  Automation tree on every sweep, four times a second, whether or not a dialog
  was present. Those elements are COM objects behind managed wrappers, and their
  memory is native: it creates no managed GC pressure, so .NET never feels the
  need to collect, and nothing is ever released. Measured at ~9 MB/min, which is
  13 GB/day, and was found in the field at 51 GB after ten days. Now the common
  case - no dialog - allocates nothing at all.

  Pressing the button through UI Automation does not move the mouse and does not
  require the dialog to be focused or in the foreground, so it is safe to leave
  running while you work in another window.

  Normally driven by the tray app, which owns this process's lifetime and passes
  options as arguments. It also runs standalone.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File watcher.ps1
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File watcher.ps1 -Observe
#>
[CmdletBinding()]
param(
    [switch]$Observe,
    [int]$IntervalMs = 250,
    [string]$LogPath = "$env:LOCALAPPDATA\YesDev\yes-dev.log",
    # Title of the consent dialog window.
    [string]$DialogPattern = '(?i)^allow remote debugging\?$',
    # Button to press. Anchored so "Turn off in settings" is never hit.
    [string]$ApprovePattern = '(?i)^(allow|approve)$',
    [string[]]$BrowserProcess = @('chrome'),
    # Class of the window that hosts the dialog.
    [string]$WindowClass = 'Chrome_WidgetWin_1',
    # The tray that started us. Both mitigations - the burst guard and the
    # disarm timer - live in the tray, so an engine that outlives it is
    # approving prompts with nothing watching the rate. The field case was an
    # engine orphaned for ten days. 0 disables the check, for standalone runs.
    [int]$ParentPid = 0,
    # Restart rather than grow without bound. Nothing should reach this now, so
    # hitting it is a bug report, not routine maintenance - the tray restarts us.
    [int]$MaxPrivateMB = 400
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class YesDevWin {
    private delegate bool EnumProc(IntPtr h, IntPtr lparam);
    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumProc cb, IntPtr lparam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassNameW(IntPtr h, StringBuilder name, int count);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr h, StringBuilder text, int count);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    // EnumWindows, not FindWindowEx. Chaining FindWindowEx by passing each result
    // back as hwndChildAfter stops dead at the first owned popup that is not a
    // direct child of the desktop: measured 1 window found where EnumWindows
    // sees 56, and the consent dialog was in the 55 it missed.
    public static IntPtr[] VisibleOfClass(string cls) {
        var found = new System.Collections.Generic.List<IntPtr>();
        var name = new StringBuilder(256);
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            if (IsWindowVisible(h)) {
                name.Length = 0;
                GetClassNameW(h, name, name.Capacity);
                if (String.Equals(name.ToString(), cls, StringComparison.OrdinalIgnoreCase))
                    found.Add(h);
            }
            return true;
        }, IntPtr.Zero);
        return found.ToArray();
    }

    public static string Title(IntPtr h) {
        var sb = new StringBuilder(512);
        GetWindowTextW(h, sb, 512);
        return sb.ToString();
    }

    public static uint Pid(IntPtr h) {
        uint pid;
        GetWindowThreadProcessId(h, out pid);
        return pid;
    }
}
"@

$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $Level, $Message
    Write-Host $line
    try {
        # One generation is plenty; an engine that runs for months must not fill the disk.
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 1048576)) {
            Move-Item -Path $LogPath -Destination "$LogPath.1" -Force
        }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch { }
}

$AE      = [System.Windows.Automation.AutomationElement]
$TS      = [System.Windows.Automation.TreeScope]
$CT      = [System.Windows.Automation.ControlType]
$btnCond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::Button)

function Invoke-Element {
    param($Element)
    try {
        $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        return 'InvokePattern'
    } catch { }
    try {
        $Element.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern).DoDefaultAction()
        return 'LegacyDoDefaultAction'
    } catch { }
    return $null
}

# Every visible top-level window of $WindowClass whose title matches the dialog.
# Pure Win32: no COM, no managed allocation worth speaking of, safe to run 4x a
# second forever.
function Find-DialogWindows {
    $hits = New-Object System.Collections.ArrayList
    # One transition into native code per sweep; the enumeration happens there.
    # Only a handful of windows carry this class, so few titles are ever read.
    foreach ($h in [YesDevWin]::VisibleOfClass($WindowClass)) {
        if ([YesDevWin]::Title($h) -match $DialogPattern) { [void]$hits.Add($h) }
    }
    return ,$hits
}

# Approve one dialog window.
# Returns 'approved' | 'failed' | 'observe' | 'nomatch'.
function Approve-Dialog {
    param([IntPtr]$Hwnd)

    $host_ = $AE::FromHandle($Hwnd)
    if (-not $host_) { return 'nomatch' }

    $buttons = $host_.FindAll($TS::Descendants, $btnCond)
    $labels = @()
    $target = $null
    foreach ($b in $buttons) {
        $bn = $b.Current.Name
        if ($bn) { $labels += "'$bn'" }
        if (-not $target -and $bn -match $ApprovePattern) { $target = $b }
    }

    Write-Log "dialog found (hwnd=$Hwnd) buttons: $($labels -join ', ')"

    if ($Observe) { Write-Log "  observe mode - not clicking" 'OBSERVE'; return 'observe' }
    if (-not $target) { Write-Log "  no button matched /$ApprovePattern/ - left alone" 'WARN'; return 'nomatch' }

    $how = Invoke-Element -Element $target
    if ($how) { Write-Log "  APPROVED via $how" 'ACTION'; return 'approved' }
    Write-Log "  FAILED to invoke Allow button" 'ERROR'
    return 'failed'
}

# One engine is enough; a second would race the first onto the same dialog.
$mutex = New-Object System.Threading.Mutex($false, 'Global\YesDevEngine')
if (-not $mutex.WaitOne(0)) {
    Write-Log "another engine already holds the lock - exiting (pid=$PID)" 'WARN'
    exit 0
}

Write-Log "engine started (observe=$($Observe.IsPresent), interval=${IntervalMs}ms, browsers=$($BrowserProcess -join '+'), pid=$PID)"

$approved   = 0
$lastSeen   = @{}                    # hwnd -> last action, so one dialog is not clicked twice
$procIds    = @()
$pidsAt     = [datetime]::MinValue
$lastTidy   = [datetime]::Now
$self       = [System.Diagnostics.Process]::GetCurrentProcess()
$parent     = $null
if ($ParentPid -gt 0) {
    try { $parent = [System.Diagnostics.Process]::GetProcessById($ParentPid) }
    catch { Write-Log "parent pid $ParentPid is already gone - exiting" 'WARN'; exit 0 }
}

while ($true) {
    try {
        # HasExited reads an already-open handle, so this costs nothing and can
        # run every sweep: an orphan is caught in 250ms, not eventually.
        if ($parent -and $parent.HasExited) {
            Write-Log "tray (pid $ParentPid) has gone - exiting rather than approving unsupervised" 'WARN'
            exit 0
        }

        $hwnds = Find-DialogWindows

        if ($hwnds.Count -gt 0) {
            # Only now is the Chrome process list worth reading. Cached for a few
            # seconds either way: Get-Process allocates, and the set barely moves.
            if (((Get-Date) - $pidsAt).TotalSeconds -gt 5) {
                $procIds = @(Get-Process -Name $BrowserProcess -ErrorAction SilentlyContinue |
                             Select-Object -ExpandProperty Id)
                $pidsAt = Get-Date
            }

            foreach ($h in $hwnds) {
                try {
                    # Belt and braces: the class and title already say Chrome, but
                    # never press a button in a window that is not one of ours.
                    if ($procIds -notcontains [int][YesDevWin]::Pid($h)) { continue }

                    $key = [string]$h
                    $last = $lastSeen[$key]
                    if ($last -and ((Get-Date) - $last).TotalSeconds -lt 2) { continue }
                    $lastSeen[$key] = Get-Date

                    $result = Approve-Dialog -Hwnd $h
                    if ($result -eq 'approved') {
                        $approved++
                        Write-Log "  total approved this session: $approved"
                    } elseif ($result -eq 'failed') {
                        # Don't sit on a dialog we failed to click; sweep it again
                        # immediately rather than waiting out the dedupe window.
                        $lastSeen.Remove($key)
                    }
                } catch { }
            }

            # UI Automation elements are COM behind managed wrappers. Their memory
            # is native, so it exerts no pressure on the managed heap and nothing
            # would otherwise collect them. Only reached when a dialog appeared.
            [System.GC]::Collect()
            [System.GC]::WaitForPendingFinalizers()
        }

        if (((Get-Date) - $lastTidy).TotalSeconds -gt 60) {
            $lastTidy = Get-Date

            $cutoff = (Get-Date).AddMinutes(-5)
            foreach ($k in @($lastSeen.Keys)) {
                if ($lastSeen[$k] -lt $cutoff) { $lastSeen.Remove($k) }
            }
            # Each caught error is retained in $Error, and an ErrorRecord can hold
            # references to whatever threw - including UI Automation elements.
            $Error.Clear()

            $self.Refresh()
            $privMB = [math]::Round($self.PrivateMemorySize64 / 1MB, 1)
            if ($privMB -gt $MaxPrivateMB) {
                Write-Log "private memory ${privMB}MB over the ${MaxPrivateMB}MB ceiling - exiting so the tray restarts a clean engine" 'WARN'
                exit 0
            }
        }
    } catch {
        Write-Log "loop error: $($_.Exception.Message)" 'ERROR'
    }
    Start-Sleep -Milliseconds $IntervalMs
}
