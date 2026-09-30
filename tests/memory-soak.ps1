param([string]$SourcePath,[string]$ResultDirectory)
$ErrorActionPreference='Stop'
$config=Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$duration=[int]$config.duration_seconds; $idle=[int]$config.idle_seconds
if($duration -lt 30 -or $idle -lt 0 -or $idle -ge $duration) { throw 'Invalid soak duration' }
$browsers=New-Object System.Collections.ArrayList
$watcher=$null; $successes=0; $failures=0; $errorText=$null
$clock=[Diagnostics.Stopwatch]::StartNew()
$samplePath=Join-Path $ResultDirectory 'samples.jsonl'
$connectionPath=Join-Path $ResultDirectory 'connections.jsonl'
$logPath=Join-Path $ResultDirectory 'watcher.log'
$utf8=New-Object Text.UTF8Encoding($false)
function Save-Sample {
    if(-not $watcher -or $watcher.HasExited) { throw 'The isolated watcher exited during the soak' }
    $watcher.Refresh()
    $elapsed=[math]::Round($clock.Elapsed.TotalSeconds,3)
    $phase=if($elapsed -lt $idle){'idle'}else{'active'}
    $sample=[ordered]@{elapsed_seconds=$elapsed;phase=$phase;watcher_pid=$watcher.Id;
        private_bytes=$watcher.PrivateMemorySize64;working_set_bytes=$watcher.WorkingSet64;
        handles=$watcher.HandleCount;cpu_seconds=$watcher.TotalProcessorTime.TotalSeconds;
        successful_connections=$successes;failed_connections=$failures;counted_dialogs=(Read-Count)}
    [IO.File]::AppendAllText($samplePath,($sample | ConvertTo-Json -Compress)+"`n",$utf8)
    [IO.File]::WriteAllText((Join-Path $ResultDirectory 'progress.json'),($sample | ConvertTo-Json),$utf8)
}
function Read-Count {
    if(-not (Test-Path -LiteralPath $logPath)) { return 0 }
    $content=$null
    for($attempt=0;$attempt -lt 10;$attempt++) {
        try { $content=[IO.File]::ReadAllText($logPath);break }
        catch [IO.IOException] { Start-Sleep -Milliseconds 20 }
    }
    if($null -eq $content) { throw 'Test log remained locked for 200 ms' }
    $matches=[regex]::Matches($content,'total approved this session: (\d+)')
    if($matches.Count) { return [int]$matches[$matches.Count-1].Groups[1].Value }
    return 0
}
try {
    $source=[IO.File]::ReadAllText($SourcePath)
    $sha=[Security.Cryptography.SHA256]::Create()
    $sourceHash=[BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($SourcePath))).Replace('-','').ToLowerInvariant()
    $sha.Dispose()
    $mutex='Global\YesDevEngine'
    if([regex]::Matches($source,[regex]::Escape($mutex)).Count -ne 1) { throw 'Unexpected mutex source' }
    $source=$source.Replace($mutex,('Local\YesDevSoak_'+[guid]::NewGuid().ToString('N')))
    if($config.mode -eq 'forced-fallback') {
        $primary='$Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()'
        if([regex]::Matches($source,[regex]::Escape($primary)).Count -ne 1) { throw 'Unexpected primary invocation source' }
        $source=$source.Replace($primary,"throw 'Injected primary failure for fallback soak'")
    }
    $watcherCopy=Join-Path $ResultDirectory 'watcher-test-copy.ps1'
    [IO.File]::WriteAllText($watcherCopy,$source,$utf8)
    $browserConfigs=@(
        @{name='Chrome';exe=(Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe');lang='en-US'},
        @{name='Edge';exe=(Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe');lang='zh-CN'}
    )
    if($config.mode -eq 'forced-fallback') { $browserConfigs[0].lang='zh-CN';$browserConfigs[1].lang='en-US' }
    foreach($item in $browserConfigs) {
        $profile=Join-Path $ResultDirectory ('profile-'+$item.name)
        New-Item -ItemType Directory -Path $profile -Force | Out-Null
        $state=@{devtools=@{remote_debugging=@{'user-enabled'=$true}};intl=@{app_locale=$item.lang}}
        [IO.File]::WriteAllText((Join-Path $profile 'Local State'),($state | ConvertTo-Json -Depth 5),$utf8)
        $portFile=Join-Path $profile 'DevToolsActivePort'
        [IO.File]::WriteAllText($portFile,"0`n",$utf8)
        $args='--user-data-dir="{0}" --lang={1} --no-first-run --no-default-browser-check --disable-background-networking --disable-sync --disable-component-update --disable-gpu --force-renderer-accessibility --enable-features=DevToolsAcceptDebuggingConnections about:blank' -f $profile,$item.lang
        $process=Start-Process -FilePath $item.exe -ArgumentList $args -WindowStyle Hidden -PassThru
        $browser=[pscustomobject]@{name=$item.name;language=$item.lang;process=$process;endpoint=$null}
        [void]$browsers.Add($browser)
        $deadline=[datetime]::UtcNow.AddSeconds(12)
        while([datetime]::UtcNow -lt $deadline) {
            try { $lines=[IO.File]::ReadAllLines($portFile) }
            catch [IO.IOException] { Start-Sleep -Milliseconds 100; continue }
            if($lines.Length -ge 2 -and [int]$lines[0] -gt 0 -and $lines[1].StartsWith('/devtools/browser')) {
                $browser.endpoint='ws://127.0.0.1:'+$lines[0]+$lines[1];break
            }
            if($process.HasExited) { throw 'Test browser exited during startup' }
            Start-Sleep -Milliseconds 100
        }
        if(-not $browser.endpoint) { throw 'Test browser did not publish its endpoint' }
    }
    $ps="$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $watcherArgs='-NoProfile -ExecutionPolicy Bypass -File "{0}" -LogPath "{1}" -BrowserProcess chrome,msedge -ParentPid {2} -IntervalMs 250 -MaxPrivateMB 400' -f $watcherCopy,$logPath,$PID
    $watcher=Start-Process -FilePath $ps -ArgumentList $watcherArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ResultDirectory 'watcher-stdout.txt') -RedirectStandardError (Join-Path $ResultDirectory 'watcher-stderr.txt')
    $clock.Restart();$nextSample=0;$nextConnection=$idle;$index=0
    while($clock.Elapsed.TotalSeconds -lt $duration) {
        if($clock.Elapsed.TotalSeconds -ge $nextSample) { Save-Sample; $nextSample=$clock.Elapsed.TotalSeconds+5 }
        if($clock.Elapsed.TotalSeconds -lt $nextConnection) { Start-Sleep -Milliseconds 100;continue }
        $browser=$browsers[$index % $browsers.Count];$index++
        $socket=New-Object Net.WebSockets.ClientWebSocket
        $cancel=New-Object Threading.CancellationTokenSource
        $cancel.CancelAfter(12000)
        $start=$clock.Elapsed.TotalSeconds
        try {
            $task=$socket.ConnectAsync([uri]$browser.endpoint,$cancel.Token)
            while(-not $task.IsCompleted) {
                if($clock.Elapsed.TotalSeconds -ge $nextSample) { Save-Sample;$nextSample=$clock.Elapsed.TotalSeconds+5 }
                Start-Sleep -Milliseconds 50
            }
            $task.GetAwaiter().GetResult()
            $payload=[Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.getVersion"}')
            $socket.SendAsync([ArraySegment[byte]]::new($payload),[Net.WebSockets.WebSocketMessageType]::Text,$true,$cancel.Token).GetAwaiter().GetResult()
            $buffer=New-Object byte[] 8192
            $received=$socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$cancel.Token).GetAwaiter().GetResult()
            $response=[Text.Encoding]::UTF8.GetString($buffer,0,$received.Count) | ConvertFrom-Json
            if(-not $response.result.product) { throw 'Missing browser version reply' }
            $successes++
            $countDeadline=[datetime]::UtcNow.AddSeconds(3)
            while((Read-Count) -lt $successes -and [datetime]::UtcNow -lt $countDeadline) { Start-Sleep -Milliseconds 50 }
            if((Read-Count) -ne $successes) { throw "Count mismatch after $successes successful connections" }
            $record=[ordered]@{elapsed_seconds=$clock.Elapsed.TotalSeconds;browser=$browser.name;language=$browser.language;product=$response.result.product;latency_seconds=($clock.Elapsed.TotalSeconds-$start);successful_connections=$successes;counted_dialogs=(Read-Count)}
            [IO.File]::AppendAllText($connectionPath,($record | ConvertTo-Json -Compress)+"`n",$utf8)
        } catch { $failures++;throw }
        finally { $socket.Abort();$socket.Dispose();$cancel.Dispose() }
        $nextConnection=[math]::Max($nextConnection+5,$clock.Elapsed.TotalSeconds)
    }
    Save-Sample
} catch { $errorText=($_ | Out-String) }
finally {
    $count=Read-Count
    if($watcher -and -not $watcher.HasExited) { Stop-Process -Id $watcher.Id -Force }
    foreach($browser in $browsers) {
        if(-not $browser.process.HasExited) { & "$env:WINDIR\System32\taskkill.exe" /PID $browser.process.Id /T /F | Out-Null }
    }
    $report=[ordered]@{mode=$config.mode;duration_seconds=$clock.Elapsed.TotalSeconds;requested_seconds=$duration;idle_seconds=$idle;source_sha256=$sourceHash;watcher_pid=if($watcher){$watcher.Id}else{$null};successful_connections=$successes;failed_connections=$failures;counted_dialogs=$count;error=$errorText;completed=($null -eq $errorText)}
    [IO.File]::WriteAllText((Join-Path $ResultDirectory 'soak-results.json'),($report | ConvertTo-Json -Depth 5),$utf8)
}
if($errorText) { exit 1 }
