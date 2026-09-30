param([string]$SourcePath, [string]$Variant, [string]$OutputPath, [string]$ProbePath)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$source = [IO.File]::ReadAllText($SourcePath)
$defaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) {
    if ($p.Name.VariablePath.UserPath -in @('DialogPattern','ApprovePattern','WindowClass','BrowserProcess')) {
        $defaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.SafeGetValue()
    }
}
$normalizer = $ast.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -eq '$BrowserProcess'
} | Select-Object -First 1
$normalizerText = if ($normalizer) { $normalizer.Extent.Text } else { '' }
$normalize = [scriptblock]::Create($normalizerText)
# Only parameters and argument normalization are used in the -File probe.
# The original startup, desktop API, mutex, log writer, and infinite loop never run.
$probe = $ast.ParamBlock.Extent.Text + "`r`n" + $normalizerText + "`r`n" + '[pscustomobject]@{browsers=@($BrowserProcess); count=@($BrowserProcess).Count} | ConvertTo-Json -Compress'
[IO.File]::WriteAllText($ProbePath, $probe, [Text.Encoding]::ASCII)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Collections.Generic;
public class FakeWindow {
 public IntPtr Handle; public string Class; public string TitleText; public uint ProcessId; public bool Visible;
}
public static class YesDevWin {
 public static List<FakeWindow> Windows = new List<FakeWindow>();
 public static IntPtr[] VisibleOfClass(string cls) {
  var found = new List<IntPtr>();
  foreach (var w in Windows) if(w.Visible && w.Class == cls) found.Add(w.Handle);
  return found.ToArray();
 }
 public static string Title(IntPtr h) { return Windows.Find(w => w.Handle == h).TitleText; }
 public static uint Pid(IntPtr h) { return Windows.Find(w => w.Handle == h).ProcessId; }
 public static bool IsWindowVisible(IntPtr h) { var w=Windows.Find(x=>x.Handle==h); return w!=null && w.Visible; }
}
public static class FakeAutomation {
 public static object Host;
 public static object FromHandle(IntPtr h) { return Host; }
}
'@
foreach ($name in @('Find-DialogWindows','Approve-Dialog','Invoke-Element','Complete-PendingApprovals')) {
    $definition = $ast.EndBlock.Statements | Where-Object {
        $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq $name
    }
    if (-not $definition) { throw "Missing function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$loop = $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.WhileStatementAst] }
if (@($loop).Count -ne 1) { throw 'Expected exactly one watcher loop' }
$sweep = [scriptblock]::Create($loop.Body.Extent.Text.Trim().Substring(1, $loop.Body.Extent.Text.Trim().Length - 2))
$AE = [FakeAutomation]
$TS = [System.Windows.Automation.TreeScope]
$btnCond = $null
$results = New-Object System.Collections.ArrayList
$hasLocaleFix = $Variant -in @('pr2','combined','fixed')
$hasProcessFix = $Variant -in @('pr3','combined','fixed')
$zhTitle = [regex]::Unescape('\u662f\u5426\u5141\u8bb8\u8fdc\u7a0b\u8c03\u8bd5?')
$zhChromeTitle = [regex]::Unescape('\u8981\u5141\u8bb8\u8fdc\u7a0b\u8c03\u8bd5\u5417\uff1f')
$zhAllow = [regex]::Unescape('\u5141\u8bb8')
$zhCancel = [regex]::Unescape('\u53d6\u6d88')
$zhSettings = [regex]::Unescape('\u5728\u8bbe\u7f6e\u4e2d\u5173\u95ed')
function Assert-Equal($Actual, $Expected, $Message) {
    if ($Actual -ne $Expected) { throw "$Message : expected=$Expected actual=$Actual" }
}
function Write-Log { param($Message, $Level='INFO'); [void]$script:messages.Add("${Level}:$Message") }
function Start-Sleep { param($Milliseconds) }
# The native COM bridge has a separate test against a real Windows control.
function Invoke-LegacyElement {
    param($Element, [IntPtr]$DialogHwnd)
    if ($script:failLegacy) { throw 'Simulated legacy failure' }
    $script:action.DoDefaultAction()
    return $true
}
function Get-Process {
    param([string[]]$Name, $ErrorAction)
    $script:processReads++
    foreach ($n in $Name) { if ($script:processTable.ContainsKey($n)) { [pscustomobject]@{Id=$script:processTable[$n]} } }
}
function Set-Browsers([string[]]$Names) {
    $BrowserProcess = $Names
    . $normalize
    $script:BrowserProcess = @($BrowserProcess)
}
function Set-Buttons([string[]]$Names) {
    $script:buttons = @(foreach ($name in $Names) {
        $script:nextButtonId++
        $button = [pscustomobject]@{Current=[pscustomobject]@{Name=$name}; RuntimeId=[int[]]@(42,11,$script:nextButtonId)}
        $button | Add-Member ScriptMethod GetRuntimeId { return ,$this.RuntimeId }
        $button | Add-Member ScriptMethod GetCurrentPattern {
            param($Pattern)
            if ($script:failInvoke -and $Pattern -eq [System.Windows.Automation.InvokePattern]::Pattern) { throw 'Simulated Invoke failure' }
            if ($script:failLegacy -and $Pattern -eq [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern) { throw 'Simulated Legacy failure' }
            return $script:action
        }
        $button
    })
}
function Reset-Case {
    $script:DialogPattern = $defaults.DialogPattern
    $script:ApprovePattern = $defaults.ApprovePattern
    $script:WindowClass = $defaults.WindowClass
    Set-Browsers @('chrome')
    $script:Observe = $false; $script:parent = $null; $script:approved = 0
    $script:lastSeen = @{}; $script:pendingApprovals=@{}; $script:procIds = @(); $script:pidsAt = [datetime]::MinValue
    $script:procIdsWarned = $false; $script:lastTidy = [datetime]::Now
    $script:IntervalMs = 0; $script:processReads = 0
    $script:messages = New-Object System.Collections.ArrayList
    $script:processTable = @{chrome=101; msedge=202}
    $script:clicks = 0; $script:failInvoke = $false; $script:failLegacy = $false
    $script:nextButtonId=0; $script:closeOnAction=$true; $script:failFindFirst=$false
    $script:action = [pscustomobject]@{}
    $script:action | Add-Member ScriptMethod Invoke { $script:clicks++; if($script:closeOnAction) {$script:window.Visible=$false} }
    $script:action | Add-Member ScriptMethod DoDefaultAction { $script:clicks++; if($script:closeOnAction) {$script:window.Visible=$false} }
    $hostObject = [pscustomobject]@{}
    $hostObject | Add-Member ScriptMethod FindAll { param($Scope,$Condition); return $script:buttons }
    $hostObject | Add-Member ScriptMethod FindFirst {
        param($Scope,$Condition)
        if($script:failFindFirst) { throw 'Transient UIA error' }
        foreach($button in $script:buttons) { if(($button.RuntimeId -join ',') -eq ($Condition.Value -join ',')) { return $button } }
        return $null
    }
    [FakeAutomation]::Host = $hostObject
    Set-Buttons @('Turn off in settings','Cancel','Allow')
    [YesDevWin]::Windows.Clear()
    $script:window = New-Object FakeWindow
    $script:window.Handle = [IntPtr]11
    $script:window.Class = 'Chrome_WidgetWin_1'
    $script:window.TitleText = 'Allow remote debugging?'
    $script:window.ProcessId = 101; $script:window.Visible = $true
    [YesDevWin]::Windows.Add($script:window)
}
function Test-Case($Name, [scriptblock]$Body) {
    Reset-Case
    try {
        . $Body
        if (@($script:messages | Where-Object {$_ -like 'ERROR:loop error:*'}).Count) { throw ($script:messages -join '; ') }
        [void]$results.Add([pscustomobject]@{name=$Name;passed=$true})
    } catch { [void]$results.Add([pscustomobject]@{name=$Name;passed=$false;error=$_.Exception.Message;log=@($script:messages)}) }
}
Test-Case 'English Chrome approval' { . $sweep; Assert-Equal $script:clicks 1 'Click count'; Assert-Equal $approved 1 'Approval count' }
Test-Case 'English Edge with combined argv' { Set-Browsers @('chrome,msedge'); $window.ProcessId=202; . $sweep; Assert-Equal $script:clicks ([int]$hasProcessFix) 'Edge feature expectation' }
Test-Case 'English Chrome with combined argv' { Set-Browsers @('chrome,msedge'); . $sweep; Assert-Equal $script:clicks ([int]$hasProcessFix) 'Chrome feature expectation' }
Test-Case 'Simplified Chinese Chrome' { $window.TitleText=$zhTitle; Set-Buttons @($zhSettings,$zhCancel,$zhAllow); . $sweep; Assert-Equal $script:clicks ([int]$hasLocaleFix) 'Locale feature expectation' }
Test-Case 'Chrome 154 Simplified Chinese title' { $window.TitleText=$zhChromeTitle; Set-Buttons @($zhSettings,$zhCancel,$zhAllow); . $sweep; Assert-Equal $script:clicks ([int]($Variant -eq 'fixed')) 'Chrome title feature expectation' }
Test-Case 'Chinese title with extra text rejected' { $window.TitleText=$zhChromeTitle+' extra'; Set-Buttons @($zhAllow); . $sweep; Assert-Equal $script:clicks 0 'Anchored Chinese title' }
Test-Case 'Simplified Chinese Edge with combined argv' { Set-Browsers @('chrome,msedge'); $window.ProcessId=202; $window.TitleText=$zhTitle; Set-Buttons @($zhCancel,$zhAllow); . $sweep; Assert-Equal $script:clicks ([int]($hasLocaleFix -and $hasProcessFix)) 'Combined feature expectation' }
Test-Case 'Native array supports Edge' { Set-Browsers @('chrome','msedge'); $window.ProcessId=202; . $sweep; Assert-Equal $script:clicks 1 'Array input' }
Test-Case 'Whitespace and empty entries' { Set-Browsers @(' chrome, , msedge '); $window.ProcessId=202; . $sweep; Assert-Equal $script:clicks ([int]$hasProcessFix) 'Trimmed input' }
Test-Case 'Observe mode never invokes' { $Observe=$true; . $sweep; Assert-Equal $script:clicks 0 'Observe clicks' }
Test-Case 'Wrong process rejected' { $window.ProcessId=999; . $sweep; Assert-Equal $script:clicks 0 'Foreign process' }
Test-Case 'Wrong window class rejected' { $window.Class='OtherWindow'; . $sweep; Assert-Equal $script:clicks 0 'Wrong class' }
Test-Case 'Hidden window rejected' { $window.Visible=$false; . $sweep; Assert-Equal $script:clicks 0 'Hidden window' }
Test-Case 'Unrelated title rejected' { $window.TitleText='Please allow remote debugging?'; . $sweep; Assert-Equal $script:clicks 0 'Unrelated title' }
Test-Case 'Unsupported locale left alone' { $window.TitleText='Autoriser le debogage a distance ?'; . $sweep; Assert-Equal $script:clicks 0 'Unsupported locale' }
Test-Case 'Cancel and settings buttons rejected' { Set-Buttons @('Turn off in settings','Cancel','Always allow','Allow this'); . $sweep; Assert-Equal $script:clicks 0 'Unsafe button' }
Test-Case 'Chinese cancel and settings rejected' { $window.TitleText=$zhTitle; Set-Buttons @($zhCancel,$zhSettings); . $sweep; Assert-Equal $script:clicks 0 'Chinese unsafe buttons' }
Test-Case 'Legacy invocation fallback' { $script:failInvoke=$true; . $sweep; Assert-Equal $script:clicks 1 'Fallback clicks' }
Test-Case 'Both invocation methods fail' { $script:failInvoke=$true; $script:failLegacy=$true; . $sweep; Assert-Equal $script:clicks 0 'Failed invocation'; Assert-Equal $approved 0 'False success'; Assert-Equal $lastSeen.Count 0 'Retry after failure' }
Test-Case 'Repeated sweep deduplicates' { . $sweep; . $sweep; Assert-Equal $script:clicks 1 'Duplicate click'; Assert-Equal $script:processReads 1 'PID cache' }
Test-Case 'No dialogs avoids process lookup' { [YesDevWin]::Windows.Clear(); . $sweep; Assert-Equal $script:processReads 0 'Idle process lookup'; Assert-Equal $script:clicks 0 'Idle clicks' }
Test-Case 'Missing processes warns once' { $script:processTable=@{}; . $sweep; $pidsAt=[datetime]::MinValue; . $sweep; $warnings=@($script:messages | Where-Object {$_ -like 'WARN:browser list*'}).Count; Assert-Equal $warnings ([int]$hasProcessFix) 'Warning count'; Assert-Equal $script:clicks 0 'Missing process clicks' }
Test-Case 'Warning resets after recovery' { $script:closeOnAction=$false; $script:processTable=@{}; . $sweep; $script:processTable=@{chrome=101}; $pidsAt=[datetime]::MinValue; . $sweep; $script:processTable=@{}; $pidsAt=[datetime]::MinValue; . $sweep; $warnings=@($script:messages | Where-Object {$_ -like 'WARN:browser list*'}).Count; Assert-Equal $warnings (2*[int]$hasProcessFix) 'Recovered warning count' }
Test-Case 'Custom title override preserved' { $DialogPattern='^Custom consent$'; $window.TitleText='Custom consent'; . $sweep; Assert-Equal $script:clicks 1 'Custom title' }
Test-Case 'Custom button override preserved' { $ApprovePattern='^Permit$'; Set-Buttons @('Permit'); . $sweep; Assert-Equal $script:clicks 1 'Custom button' }
Test-Case 'Ignored action does not increment counter' {
    $script:closeOnAction=$false; . $sweep
    Assert-Equal $approved 0 'Pending count'
    Assert-Equal $pendingApprovals.Count 1 'Pending identity'
    Assert-Equal @($messages | Where-Object {$_ -like 'ACTION:*'}).Count 0 'Action events'
}
Test-Case 'Retries count one dismissed dialog' {
    $script:closeOnAction=$false; . $sweep
    $lastSeen['11']=(Get-Date).AddSeconds(-3); . $sweep
    Assert-Equal $script:clicks 2 'Retry attempts'
    Assert-Equal $approved 0 'No premature count'
    $window.Visible=$false; . $sweep; . $sweep
    Assert-Equal $approved 1 'One dismissal'
    Assert-Equal $pendingApprovals.Count 0 'No retained pending entries'
    Assert-Equal $lastSeen.Count 0 'Old dedup entry cleared'
    Assert-Equal @($messages | Where-Object {$_ -like 'ACTION:*'}).Count 1 'One action event'
}
Test-Case 'Delayed dismissal is counted on a later idle sweep' {
    $script:closeOnAction=$false; . $sweep; . $sweep
    Assert-Equal $approved 0 'No early dismissal'
    $window.Visible=$false; . $sweep
    Assert-Equal $approved 1 'Delayed dismissal'
}
Test-Case 'Reused HWND with new button identity is a separate dialog' {
    $script:closeOnAction=$false; . $sweep
    Set-Buttons @('Cancel','Allow'); . $sweep
    Assert-Equal $approved 1 'First dialog counted'
    Assert-Equal $script:clicks 2 'Second dialog not blocked by old dedup entry'
    $window.Visible=$false; . $sweep
    Assert-Equal $approved 2 'Second dialog counted'
}
Test-Case 'Accessibility errors are not counted as dismissals' {
    $script:closeOnAction=$false; . $sweep; $script:failFindFirst=$true; . $sweep
    Assert-Equal $approved 0 'No count from UIA error'
    Assert-Equal $pendingApprovals.Count 1 'Pending entry preserved'
}
$sha = [Security.Cryptography.SHA256]::Create()
$sourceHash = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($SourcePath))).Replace('-','')
$sha.Dispose()
$legacyTypePresent = $null -ne ('System.Windows.Automation.LegacyIAccessiblePattern' -as [type])
$report = [pscustomobject]@{variant=$Variant; powershell=$PSVersionTable.PSVersion.ToString(); legacy_pattern_type_present=$legacyTypePresent; source_sha256=$sourceHash; tests=@($results); passed=@($results | Where-Object passed).Count; failed=@($results | Where-Object {-not $_.passed}).Count}
$report | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $OutputPath
$report | Select-Object variant,powershell,passed,failed | ConvertTo-Json -Compress
if ($report.failed) { exit 1 }
