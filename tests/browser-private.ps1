param([string]$SourcePath,[string]$ResultDirectory)
$ErrorActionPreference='Stop'
$config=Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$browser=$null; $socket=$null; $sockets=@(); $results=New-Object System.Collections.ArrayList
$burstSize=if($config.burst_size){[int]$config.burst_size}else{1}
if($burstSize -lt 1 -or $burstSize -gt 8) { throw 'burst_size must be between 1 and 8' }
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
           $stmt.Name -in @('Find-DialogWindows','Approve-Dialog','Invoke-Element','Invoke-LegacyElement','Complete-PendingApprovals')) {
            $definition=$stmt.Extent.Text
            if($stmt.Name -eq 'Invoke-Element') { $invokeDefinition=$definition }
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
        try { $lines=[IO.File]::ReadAllLines($portFile) }
        catch [IO.IOException] { Start-Sleep -Milliseconds 100; continue }
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
        $definition=$invokeDefinition
        if($mode -eq 'forced-fallback') {
            $definition=$definition.Replace('$Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()',"throw 'Injected primary failure for fallback test'")
        }
        . ([scriptblock]::Create($definition))
        $Observe=$false; $parent=$null; $approved=0; $lastSeen=@{}; $procIds=@(); $pidsAt=[datetime]::MinValue
        $pendingApprovals=@{}
        $lastTidy=[datetime]::Now; $procIdsWarned=$false; $IntervalMs=100
        $cancel=New-Object Threading.CancellationTokenSource
        $cancel.CancelAfter(20000)
        $sockets=@();$connects=@()
        for($request=0;$request -lt $burstSize;$request++) {
            $socket=New-Object Net.WebSockets.ClientWebSocket
            $sockets += $socket
            $connects += $socket.ConnectAsync([uri]$endpoint,$cancel.Token)
        }
        $sawDialog=$false; $actions=0; $titles=@(); $labels=@(); $lastAction=[datetime]::MinValue
        $logStart=$logs.Count
        while(@($connects | Where-Object {-not $_.IsCompleted}).Count -gt 0) {
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
            }
            . $sweep
        }
        foreach($connect in $connects) { $connect.GetAwaiter().GetResult() }
        if(-not $sawDialog -or @($sockets | Where-Object {$_.State -ne 'Open'}).Count -gt 0) { throw 'Connection did not prove consent-dialog approval' }
        if($config.language -eq 'zh-CN' -and -not ($titles -match '[\u4e00-\u9fff]')) { throw 'The requested Chinese UI language was not observed' }
        foreach($socket in $sockets) {
            $bytes=[Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.getVersion"}')
            $socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult()
            $buffer=New-Object byte[] 8192
            $received=$socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$cancel.Token).GetAwaiter().GetResult()
            $response=[Text.Encoding]::UTF8.GetString($buffer,0,$received.Count) | ConvertFrom-Json
            if(-not $response.result.product) { throw 'Browser.getVersion returned no product' }
        }
        $countDeadline=[datetime]::UtcNow.AddSeconds(2)
        while($pendingApprovals.Count -gt 0 -and [datetime]::UtcNow -lt $countDeadline) { . $sweep }
        $requestLogs=@($logs | Select-Object -Skip $logStart)
        $actionEvents=@($requestLogs | Where-Object {$_ -like 'ACTION:*'}).Count
        $actions=@($requestLogs | Where-Object {$_ -like '*approval attempted via LegacyDoDefaultAction*'}).Count
        if($approved -ne $burstSize -or $actionEvents -ne $burstSize -or $pendingApprovals.Count -ne 0) {
            throw "Each connection must count once: connections=$burstSize approvals=$approved events=$actionEvents pending=$($pendingApprovals.Count)"
        }
        if($mode -eq 'forced-fallback' -and $actions -lt 1) { throw 'Fallback path was not exercised' }
        [void]$results.Add([pscustomobject]@{mode=$mode;passed=$true;connections=$burstSize;product=$response.result.product;dialog_titles=@($titles | Select-Object -Unique);button_labels=$labels;logged_approvals=$approved;native_fallback_actions=$actions})
        foreach($socket in $sockets) { $socket.Abort();$socket.Dispose() }
        $sockets=@();$socket=$null;$cancel.Dispose()
        Start-Sleep -Milliseconds 200
    }
} catch {
    [void]$results.Add([pscustomobject]@{mode='browser-test';passed=$false;error=($_ | Out-String)})
} finally {
    foreach($socket in $sockets) { $socket.Abort();$socket.Dispose() }
    if($browser -and -not $browser.HasExited) {
        # Only the PID returned when this test started its fresh-profile browser.
        & "$env:WINDIR\System32\taskkill.exe" /PID $browser.Id /T /F | Out-Null
    }
    $report=[pscustomobject]@{browser=$config.browser;language=$config.language;tests=@($results);passed=@($results | Where-Object passed).Count;failed=@($results | Where-Object {-not $_.passed}).Count;log=@($logs);candidate_titles=@($candidateTitles.Keys);profile=$profile}
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'browser-results.json')
}
if($report.failed) { exit 1 }
