param([string]$SourcePath, [string]$ResultDirectory)
$ErrorActionPreference = 'Stop'
$config = Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$repo = Split-Path -Parent $SourcePath
$browsers = @(); $sockets = @(); $supervisor = $null; $cancel = $null
$results = New-Object Collections.ArrayList
$ownedWorkers = @()
try {
    $copy = Join-Path $ResultDirectory 'app'
    New-Item -ItemType Directory -Path $copy -Force | Out-Null
    $coordinator = [IO.File]::ReadAllText((Join-Path $repo 'watcher_supervisor.py'))
    $coordinator = $coordinator.Replace('Global\\YesDevEngine', ('Global\\YesDevTest_' + [guid]::NewGuid().ToString('N')))
    [IO.File]::WriteAllText((Join-Path $copy 'watcher_supervisor.py'), $coordinator, [Text.Encoding]::UTF8)
    $worker = [IO.File]::ReadAllText($SourcePath)
    $marker = Join-Path $ResultDirectory 'stall-once'
    $injection = @'
    if ((Get-Process -Id ([int][YesDevWin]::Pid($Hwnd))).ProcessName -eq 'chrome') {
        $stallFile = $null
        try { $stallFile = [IO.File]::Open('__MARKER__', [IO.FileMode]::CreateNew, [IO.FileAccess]::Write) } catch [IO.IOException] { }
        if ($stallFile) {
            $stallFile.Dispose()
            Write-Log "INJECTED STALL hwnd=$Hwnd pid=$PID"
            [Threading.Thread]::Sleep(30000)
        }
    }
'@
    if ($config.inject_stall) {
        if (-not $worker.Contains('    $buttonId = $target.GetRuntimeId()')) { throw 'Stall injection point missing' }
        $worker = $worker.Replace('    $buttonId = $target.GetRuntimeId()',
            $injection.Replace('__MARKER__', $marker.Replace("'", "''")) + "`r`n    `$buttonId = `$target.GetRuntimeId()")
    }
    [IO.File]::WriteAllText((Join-Path $copy 'watcher.ps1'), $worker, [Text.Encoding]::UTF8)
    $log = Join-Path $ResultDirectory 'watcher.log'
    $launchArgs = '"{0}" -IntervalMs {1} -LogPath "{2}" -BrowserProcess chrome,msedge -ParentPid {3}' -f (Join-Path $copy 'watcher_supervisor.py'),$config.interval,$log,$PID
    if ($config.observe) { $launchArgs += ' -Observe' }
    $supervisor = Start-Process -FilePath $config.python -ArgumentList $launchArgs -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $ResultDirectory 'supervisor-stderr.txt')
    $cancel = New-Object Threading.CancellationTokenSource
    $cancel.CancelAfter(50000)
    $connects = @()
    foreach ($spec in @(
        @{name='chrome';exe=(Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe')},
        @{name='edge';exe=(Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')}
    )) {
        $profile = Join-Path $ResultDirectory ($spec.name + '-profile')
        New-Item -ItemType Directory -Path $profile -Force | Out-Null
        $state = @{devtools=@{remote_debugging=@{'user-enabled'=$true}};intl=@{app_locale=$config.language}}
        [IO.File]::WriteAllText((Join-Path $profile 'Local State'), ($state | ConvertTo-Json -Depth 5))
        $portFile = Join-Path $profile 'DevToolsActivePort'
        [IO.File]::WriteAllText($portFile,"0`n")
        $arguments = '--user-data-dir="{0}" --lang={1} --no-first-run --no-default-browser-check --disable-background-networking --disable-sync --disable-component-update --enable-features=DevToolsAcceptDebuggingConnections about:blank' -f $profile,$config.language
        $browser = Start-Process -FilePath $spec.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
        $browsers += $browser
        $deadline = [datetime]::UtcNow.AddSeconds(15)
        $endpoint = $null
        while ([datetime]::UtcNow -lt $deadline) {
            try { $lines = [IO.File]::ReadAllLines($portFile) } catch [IO.IOException] { Start-Sleep -Milliseconds 100; continue }
            if ($lines.Length -ge 2 -and [int]$lines[0] -gt 0 -and $lines[1].StartsWith('/devtools/browser')) {
                $endpoint = 'ws://127.0.0.1:' + $lines[0] + $lines[1]; break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $endpoint) { throw 'Fresh test browser did not publish an approval endpoint' }
        for ($i=0; $i -lt $config.burst_size; $i++) {
            $socket = New-Object Net.WebSockets.ClientWebSocket
            $sockets += $socket
            $connects += $socket.ConnectAsync([uri]$endpoint, $cancel.Token)
        }
    }
    if ($config.shutdown_stalled) {
        $deadline = [datetime]::UtcNow.AddSeconds(25)
        while (-not (Test-Path $marker) -and [datetime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
        if (-not (Test-Path $marker)) { throw 'Worker did not reach the injected stall' }
        [void]$results.Add('terminating supervisor while an approval worker is blocked')
    } elseif ($config.observe) {
        Start-Sleep -Seconds 8
        if (@($connects | Where-Object IsCompleted).Count) { throw 'Observe mode completed a connection' }
        if ((Get-Content $log -Raw) -match '\[ACTION\]') { throw 'Observe mode emitted an approval' }
        [void]$results.Add('observe mode leaves both browsers pending with no ACTION events')
    } else {
        $deadline = [datetime]::UtcNow.AddSeconds(40)
        while (@($connects | Where-Object {-not $_.IsCompleted}).Count -gt 0 -and [datetime]::UtcNow -lt $deadline) {
            if ($supervisor.HasExited) { throw 'Supervisor exited before the connections completed' }
            Start-Sleep -Milliseconds 100
        }
        foreach ($connect in $connects) { $connect.GetAwaiter().GetResult() }
        foreach ($socket in $sockets) {
            $bytes = [Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.getVersion"}')
            $socket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $cancel.Token).GetAwaiter().GetResult()
            $buffer = New-Object byte[] 8192
            $received = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $cancel.Token).GetAwaiter().GetResult()
            $response = [Text.Encoding]::UTF8.GetString($buffer,0,$received.Count) | ConvertFrom-Json
            if (-not $response.result.product) { throw 'Browser.getVersion returned no product' }
        }
        $deadline = [datetime]::UtcNow.AddSeconds(3)
        do {
            $actionLines = @(Get-Content $log | Where-Object {$_ -match '\[ACTION\]'})
            if ($actionLines.Count -eq $sockets.Count) { break }
            Start-Sleep -Milliseconds 100
        } while ([datetime]::UtcNow -lt $deadline)
        if ($actionLines.Count -ne $sockets.Count) { throw "Expected $($sockets.Count) persisted approvals; got $($actionLines.Count)" }
        if ($config.language -eq 'zh-CN' -and (Get-Content $log -Raw -Encoding UTF8) -notmatch '[\u4e00-\u9fff]') {
            throw 'Requested Chinese button labels were not observed'
        }
        [void]$results.Add("$($sockets.Count) CDP handshakes, version replies, and persisted ACTION events")
        if ($config.inject_stall) {
            $supervisorLog = Get-Content (Join-Path $ResultDirectory 'supervisor.log')
            $timeout = @($supervisorLog | Where-Object {$_ -match 'worker stalled'})
            if (-not $timeout.Count) { throw 'Injected stall was not detected' }
            if (-not ($supervisorLog -match 'legacy_first=True')) { throw 'Stall did not trigger a legacy-first retry' }
            $timeoutAt = [datetime]::ParseExact($timeout[0].Substring(0,19),'yyyy-MM-dd HH:mm:ss',$null)
            $healthyBeforeTimeout = @($actionLines | Where-Object {
                [datetime]::ParseExact($_.Substring(0,23),'yyyy-MM-dd HH:mm:ss.fff',$null) -lt $timeoutAt
            })
            if (-not $healthyBeforeTimeout.Count) { throw 'No healthy dialog completed before stall recovery' }
            [void]$results.Add('healthy prompts complete while one worker is blocked; stalled worker is killed and retried successfully')
        }
    }
    $ownedWorkers = @(Get-CimInstance Win32_Process | Where-Object {$_.ParentProcessId -eq $supervisor.Id} | Select-Object -ExpandProperty ProcessId)
    if (($config.observe -or $config.shutdown_stalled) -and $ownedWorkers.Count -eq 0) {
        throw 'Termination test requires live workers'
    }
    Stop-Process -Id $supervisor.Id -Force
    $supervisor.WaitForExit()
    Start-Sleep -Milliseconds 500
    if (@($ownedWorkers | Where-Object {Get-Process -Id $_ -ErrorAction SilentlyContinue}).Count) {
        throw 'Workers survived supervisor termination'
    }
    [void]$results.Add('worker job leaves no workers after supervisor termination')
    [pscustomobject]@{passed=$true;interval=$config.interval;language=$config.language;checks=@($results)} |
        ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'supervisor-results.json')
} catch {
    [pscustomobject]@{passed=$false;error=($_ | Out-String);checks=@($results)} |
        ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'supervisor-results.json')
    throw
} finally {
    if ($supervisor -and -not $supervisor.HasExited) { Stop-Process -Id $supervisor.Id -Force; $supervisor.WaitForExit() }
    foreach ($socket in $sockets) { $socket.Abort(); $socket.Dispose() }
    if ($cancel) { $cancel.Dispose() }
    foreach ($browser in $browsers) {
        if (-not $browser.HasExited) { & "$env:WINDIR\System32\taskkill.exe" /PID $browser.Id /T /F | Out-Null }
    }
}
