param([string]$SourcePath,[string]$ResultDirectory)
$ErrorActionPreference='Stop'
$config=Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$browser=$null; $socket=$null; $results=New-Object System.Collections.ArrayList
$logs=New-Object System.Collections.ArrayList
$candidateTitles=@{}
try {
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
    if($errors.Count) { throw ($errors | Out-String) }
    foreach($stmt in $ast.EndBlock.Statements) {
        if($stmt -is [System.Management.Automation.Language.PipelineAst] -and
           $stmt.PipelineElements[0] -is [System.Management.Automation.Language.CommandAst] -and
           $stmt.PipelineElements[0].GetCommandName() -eq 'Add-Type') {
            . ([scriptblock]::Create($stmt.Extent.Text))
        }
        if($stmt -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
           $stmt.Name -in @('Find-DialogWindows','Approve-Dialog','Invoke-Element','Invoke-LegacyElement')) {
            $definition=$stmt.Extent.Text
            if($stmt.Name -eq 'Find-DialogWindows') { $definition=$definition.Replace('function Find-DialogWindows','function Find-OriginalDialogWindows') }
            . ([scriptblock]::Create($definition))
        }
    }
    function Write-Log { param($Message,$Level='INFO'); [void]$logs.Add("${Level}:$Message") }
    function Find-DialogWindows {
        # Observe the same discovery used by the original sweep. A separate
        # before-sweep scan can miss a prompt that appears between the two calls.
        $found=Find-OriginalDialogWindows
        foreach($h in $found) {
            if([YesDevWin]::Pid($h) -ne $browser.Id) { throw 'A dialog does not belong to the test browser' }
            $script:sawDialog=$true
            $script:titles += [YesDevWin]::Title($h)
            $element=$AE::FromHandle($h)
            $script:labels=@($element.FindAll($TS::Descendants,$btnCond) | ForEach-Object {$_.Current.Name})
        }
        return ,$found
    }
    $AE=[System.Windows.Automation.AutomationElement]
    $TS=[System.Windows.Automation.TreeScope]
    $CT=[System.Windows.Automation.ControlType]
    $btnCond=New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty,$CT::Button)
    foreach($parameter in $ast.ParamBlock.Parameters) {
        if($parameter.Name.VariablePath.UserPath -in @('DialogPattern','ApprovePattern','WindowClass')) {
            Set-Variable -Name $parameter.Name.VariablePath.UserPath -Value $parameter.DefaultValue.SafeGetValue()
        }
    }
    $BrowserProcess=@('chrome,msedge')
    $normalizer=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -eq '$BrowserProcess'} | Select-Object -First 1
    . ([scriptblock]::Create($normalizer.Extent.Text))
    $profile=Join-Path $ResultDirectory 'profile'
    New-Item -ItemType Directory -Path $profile -Force | Out-Null
    $state=@{devtools=@{remote_debugging=@{'user-enabled'=$true}};intl=@{app_locale=$config.language}}
    [IO.File]::WriteAllText((Join-Path $profile 'Local State'),($state | ConvertTo-Json -Depth 5))
    # In approval mode Chrome reads this first line to select its port. 0 asks
    # for a new ephemeral port. Do not use --remote-debugging-port: it skips consent.
    $portFile=Join-Path $profile 'DevToolsActivePort'
    [IO.File]::WriteAllText($portFile,"0`n")
    $arguments='--user-data-dir="{0}" --lang={1} --no-first-run --no-default-browser-check --disable-background-networking --disable-sync --disable-component-update --disable-gpu --force-renderer-accessibility --enable-features=DevToolsAcceptDebuggingConnections about:blank' -f $profile,$config.language
    $browser=Start-Process -FilePath $config.executable -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $deadline=[datetime]::UtcNow.AddSeconds(12)
    $endpoint=$null
    while([datetime]::UtcNow -lt $deadline) {
        $lines=[IO.File]::ReadAllLines($portFile)
        if($lines.Length -ge 2 -and [int]$lines[0] -gt 0 -and $lines[1].StartsWith('/devtools/browser')) {
            $endpoint='ws://127.0.0.1:'+ $lines[0] + $lines[1]; break
        }
        if($browser.HasExited) { throw "Test browser exited: $($browser.ExitCode)" }
        Start-Sleep -Milliseconds 100
    }
    if(-not $endpoint) { throw 'Test browser did not publish an approval endpoint' }
    $loop=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.WhileStatementAst]}
    $loopText=$loop.Body.Extent.Text.Trim()
    $sweep=[scriptblock]::Create($loopText.Substring(1,$loopText.Length-2))
    foreach($mode in @('normal','forced-fallback')) {
        $Observe=$false; $parent=$null; $approved=0; $lastSeen=@{}; $procIds=@(); $pidsAt=[datetime]::MinValue
        $lastTidy=[datetime]::Now; $procIdsWarned=$false; $IntervalMs=100
        $socket=New-Object Net.WebSockets.ClientWebSocket
        $cancel=New-Object Threading.CancellationTokenSource
        $cancel.CancelAfter(12000)
        $connect=$socket.ConnectAsync([uri]$endpoint,$cancel.Token)
        $sawDialog=$false; $actions=0; $titles=@(); $labels=@(); $lastAction=[datetime]::MinValue
        while(-not $connect.IsCompleted) {
            foreach($candidate in [YesDevWin]::VisibleOfClass($WindowClass)) {
                if([YesDevWin]::Pid($candidate) -eq $browser.Id) {
                    $candidateTitles[[YesDevWin]::Title($candidate)]=$true
                }
            }
            $dialogs=Find-DialogWindows
            foreach($dialog in $dialogs) {
                if([YesDevWin]::Pid($dialog) -ne $browser.Id) { throw 'A dialog does not belong to the test browser' }
                $sawDialog=$true
                $titles += [YesDevWin]::Title($dialog)
                $hostElement=$AE::FromHandle($dialog)
                $buttons=$hostElement.FindAll($TS::Descendants,$btnCond)
                $labels=@($buttons | ForEach-Object {$_.Current.Name})
                # Match the watcher's existing two-second retry interval. Chromium
                # can discard an action sent just after a security prompt appears.
                if($mode -eq 'forced-fallback' -and ((Get-Date)-$lastAction).TotalSeconds -ge 2) {
                    $target=$buttons | Where-Object {$_.Current.Name -match $ApprovePattern} | Select-Object -First 1
                    if(-not $target) { throw 'No approval button matched in real browser dialog' }
                    $proxy=[pscustomobject]@{Actual=$target;Current=$target.Current}
                    $proxy | Add-Member ScriptMethod GetCurrentPattern { param($Pattern); throw 'Force primary failure for native fallback test' }
                    $proxy | Add-Member ScriptMethod GetRuntimeId { return ,$this.Actual.GetRuntimeId() }
                    $how=Invoke-Element -Element $proxy -DialogHwnd $dialog
                    if($how -ne 'LegacyDoDefaultAction') { throw "Native browser fallback failed: $how" }
                    $actions++
                    $lastAction=Get-Date
                }
            }
            if($mode -eq 'normal') { . $sweep } else { Start-Sleep -Milliseconds 100 }
        }
        $connect.GetAwaiter().GetResult()
        if(-not $sawDialog -or $socket.State -ne 'Open') { throw 'Connection did not prove consent-dialog approval' }
        if($config.language -eq 'zh-CN' -and -not ($titles -match '[\u4e00-\u9fff]')) { throw 'The requested Chinese UI language was not observed' }
        $bytes=[Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.getVersion"}')
        $socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult()
        $buffer=New-Object byte[] 8192
        $received=$socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$cancel.Token).GetAwaiter().GetResult()
        $response=[Text.Encoding]::UTF8.GetString($buffer,0,$received.Count) | ConvertFrom-Json
        if(-not $response.result.product) { throw 'Browser.getVersion returned no product' }
        [void]$results.Add([pscustomobject]@{mode=$mode;passed=$true;product=$response.result.product;dialog_titles=@($titles | Select-Object -Unique);button_labels=$labels;logged_approvals=$approved;native_fallback_actions=$actions})
        $socket.Abort(); $socket.Dispose(); $socket=$null; $cancel.Dispose()
        Start-Sleep -Milliseconds 200
    }
} catch {
    [void]$results.Add([pscustomobject]@{mode='browser-test';passed=$false;error=($_ | Out-String)})
} finally {
    if($socket) { $socket.Abort(); $socket.Dispose() }
    if($browser -and -not $browser.HasExited) {
        # Only the PID returned when this test started its fresh-profile browser.
        & "$env:WINDIR\System32\taskkill.exe" /PID $browser.Id /T /F | Out-Null
    }
    $report=[pscustomobject]@{browser=$config.browser;language=$config.language;tests=@($results);passed=@($results | Where-Object passed).Count;failed=@($results | Where-Object {-not $_.passed}).Count;log=@($logs);candidate_titles=@($candidateTitles.Keys);profile=$profile}
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'browser-results.json')
}
if($report.failed) { exit 1 }
