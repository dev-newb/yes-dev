<#
.SYNOPSIS
  Yes, Dev engine: auto-approves Chrome's "Allow remote debugging?" consent dialog.

.DESCRIPTION
  Chrome 144+ prompts for approval on every client attach to the remote debugging
  endpoint. With several agents or automation clients attaching in parallel the
  prompts stack up, and each one blocks its client until a human clicks Allow.

  The dialog is a Views bubble hosted *inside* the browser window, which is why
  watchers that scan only top-level windows never find it:

      Window  Chrome_WidgetWin_1   "<tab title> - Google Chrome"
        Pane    BrowserRootView
          Pane    Chrome_WidgetWin_1  "Allow remote debugging?"   <-- dialog host
            NonClientView > BubbleFrameView > DialogClientView > ButtonRowContainer
              Button  MdTextButton    "Allow" / "Cancel" / "Turn off in settings"

  So this walks two levels down from each browser frame - no full-tree scan, no
  scraping of page content (web pages have their own "Allow" buttons) - and
  invokes the Allow button through UI Automation. That does not move the mouse
  and does not require the dialog to be focused or in the foreground, so it is
  safe to leave running while you work in another window.

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
    # Title of the consent dialog host window.
    [string]$DialogPattern = '(?i)^allow remote debugging\?$',
    # Button to press. Anchored so "Turn off in settings" is never hit.
    [string]$ApprovePattern = '(?i)^(allow|approve)$',
    [string[]]$BrowserProcess = @('chrome')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $Level, $Message
    Write-Host $line
    try { Add-Content -Path $LogPath -Value $line -Encoding utf8 } catch { }
}

$AE      = [System.Windows.Automation.AutomationElement]
$TS      = [System.Windows.Automation.TreeScope]
$CT      = [System.Windows.Automation.ControlType]
$AnyCond = [System.Windows.Automation.Condition]::TrueCondition
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

# Approve one dialog host element. Returns $true if a click was issued.
function Approve-Dialog {
    param($Host_, [string]$Where)

    $buttons = $Host_.FindAll($TS::Descendants, $btnCond)
    $labels = @()
    $target = $null
    foreach ($b in $buttons) {
        $bn = $b.Current.Name
        if ($bn) { $labels += "'$bn'" }
        if (-not $target -and $bn -match $ApprovePattern) { $target = $b }
    }

    Write-Log "dialog found ($Where) buttons: $($labels -join ', ')"

    if ($Observe) { Write-Log "  observe mode - not clicking" 'OBSERVE'; return $false }
    if (-not $target) { Write-Log "  no button matched /$ApprovePattern/ - left alone" 'WARN'; return $false }

    $how = Invoke-Element -Element $target
    if ($how) { Write-Log "  APPROVED via $how" 'ACTION'; return $true }
    Write-Log "  FAILED to invoke Allow button" 'ERROR'
    return $false
}

# One engine is enough; a second would race the first onto the same dialog.
$mutex = New-Object System.Threading.Mutex($false, 'Global\YesDevEngine')
if (-not $mutex.WaitOne(0)) {
    Write-Log "another engine already holds the lock - exiting (pid=$PID)" 'WARN'
    exit 0
}

Write-Log "engine started (observe=$($Observe.IsPresent), interval=${IntervalMs}ms, browsers=$($BrowserProcess -join '+'), pid=$PID)"

$approved = 0
$lastSeen = @{}   # runtime-id -> last action, avoids re-clicking a dialog mid-teardown

while ($true) {
    try {
        $procIds = @(Get-Process -Name $BrowserProcess -ErrorAction SilentlyContinue |
                     Select-Object -ExpandProperty Id)
        if ($procIds.Count -gt 0) {
            foreach ($top in $AE::RootElement.FindAll($TS::Children, $AnyCond)) {
                try {
                    $tc = $top.Current
                    if ($procIds -notcontains $tc.ProcessId) { continue }

                    # Candidate hosts: the frame's own children (BrowserRootView and,
                    # in some Chrome builds, the dialog directly), plus one level down.
                    $candidates = New-Object System.Collections.ArrayList
                    if ($tc.Name -match $DialogPattern) { [void]$candidates.Add($top) }

                    foreach ($lvl1 in $top.FindAll($TS::Children, $AnyCond)) {
                        try {
                            if ($lvl1.Current.Name -match $DialogPattern) { [void]$candidates.Add($lvl1); continue }
                            if ($lvl1.Current.ClassName -notmatch 'BrowserRootView|RootView') { continue }
                            foreach ($lvl2 in $lvl1.FindAll($TS::Children, $AnyCond)) {
                                try {
                                    if ($lvl2.Current.Name -match $DialogPattern) { [void]$candidates.Add($lvl2) }
                                } catch { }
                            }
                        } catch { }
                    }

                    foreach ($host_ in $candidates) {
                        try {
                            # A torn-down bubble can linger in the tree; never click a hidden one.
                            if ($host_.Current.IsOffscreen) { continue }
                            $rid = ($host_.GetRuntimeId() -join '.')
                            $last = $lastSeen[$rid]
                            if ($last -and ((Get-Date) - $last).TotalSeconds -lt 2) { continue }
                            $lastSeen[$rid] = Get-Date

                            if (Approve-Dialog -Host_ $host_ -Where "hwnd=$($tc.NativeWindowHandle) '$($tc.Name)'") {
                                $approved++
                                Write-Log "  total approved this session: $approved"
                            }
                        } catch { }
                    }
                } catch { }
            }

            if ($lastSeen.Count -gt 200) {
                $cutoff = (Get-Date).AddMinutes(-5)
                foreach ($k in @($lastSeen.Keys)) { if ($lastSeen[$k] -lt $cutoff) { $lastSeen.Remove($k) } }
            }
        }
    } catch {
        Write-Log "loop error: $($_.Exception.Message)" 'ERROR'
    }
    Start-Sleep -Milliseconds $IntervalMs
}
