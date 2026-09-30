param(
    [string]$ResultDirectory = (Join-Path $env:TEMP ('yes-dev-tests-' + [guid]::NewGuid().ToString('N'))),
    [string]$Python = 'python',
    [switch]$IncludeBrowsers,
    [ValidateRange(1,8)][int]$BurstSize=1
)
$ErrorActionPreference='Stop'
$ResultDirectory=[IO.Path]::GetFullPath($ResultDirectory)
New-Item -ItemType Directory -Path $ResultDirectory -Force | Out-Null
$repo=Split-Path -Parent $PSScriptRoot
$source=Join-Path $repo 'watcher.ps1'
$ps="$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$probe=Join-Path $ResultDirectory 'argv-probe.ps1'
& $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'watcher-regression.ps1') -SourcePath $source -Variant fixed -OutputPath (Join-Path $ResultDirectory 'regression.json') -ProbePath $probe
if($LASTEXITCODE) { throw 'Watcher regression tests failed' }
$argvTests=@()
foreach($argument in @('chrome','chrome,msedge',' chrome, , msedge ')) {
    $actual=& $ps -NoProfile -ExecutionPolicy Bypass -File $probe -BrowserProcess $argument | ConvertFrom-Json
    $expected=@($argument.Split(',') | ForEach-Object {$_.Trim()} | Where-Object {$_})
    $pass=(@($actual.browsers) -join '|') -eq ($expected -join '|')
    $argvTests += [pscustomobject]@{argument=$argument;passed=$pass;actual=$actual.browsers;expected=$expected}
}
$argvTests | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'argv.json')
if(@($argvTests | Where-Object {-not $_.passed}).Count) { throw 'Argument binding tests failed' }
& $Python (Join-Path $PSScriptRoot 'tray-launch.py') (Join-Path $ResultDirectory 'tray.json')
if($LASTEXITCODE) { throw 'Tray launch tests failed' }
& $Python (Join-Path $PSScriptRoot 'tray-log.py') (Join-Path $ResultDirectory 'tray-log.json')
if($LASTEXITCODE) { throw 'Tray log-reader tests failed' }
& $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'log-writer.ps1') -SourcePath $source -OutputPath (Join-Path $ResultDirectory 'log-writer.json')
if($LASTEXITCODE) { throw 'Concurrent log-writer tests failed' }
& $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'soak-log-reader.ps1') -OutputPath (Join-Path $ResultDirectory 'observer.json')
if($LASTEXITCODE) { throw 'Soak observer tests failed' }
& $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run-private-desktop.ps1') -ScriptPath (Join-Path $PSScriptRoot 'legacy-native.ps1') -SourcePath $source -ResultDirectory (Join-Path $ResultDirectory 'native')
if($LASTEXITCODE) { throw 'Native tests failed; inspect native/native-results.json' }
$logic=Get-Content (Join-Path $ResultDirectory 'regression.json') -Raw | ConvertFrom-Json
$tray=Get-Content (Join-Path $ResultDirectory 'tray.json') -Raw | ConvertFrom-Json
$trayLog=Get-Content (Join-Path $ResultDirectory 'tray-log.json') -Raw | ConvertFrom-Json
$logWriter=Get-Content (Join-Path $ResultDirectory 'log-writer.json') -Raw | ConvertFrom-Json
$observer=Get-Content (Join-Path $ResultDirectory 'observer.json') -Raw | ConvertFrom-Json
$native=Get-Content (Join-Path $ResultDirectory 'native\native-results.json') -Raw | ConvertFrom-Json
$browserReports=@()
if($IncludeBrowsers) {
    $browsers=@(
        @{browser='Chrome';executable=(Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe')},
        @{browser='Edge';executable=(Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')}
    )
    foreach($browser in $browsers) {
        if(-not (Test-Path -LiteralPath $browser.executable)) { throw "Browser executable not found: $($browser.executable)" }
        foreach($language in @('en-US','zh-CN')) {
            $directory=Join-Path $ResultDirectory ($browser.browser.ToLower()+'-'+$language)
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
            @{browser=$browser.browser;executable=$browser.executable;language=$language;burst_size=$BurstSize} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $directory 'config.json')
            & $ps -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run-private-desktop.ps1') -ScriptPath (Join-Path $PSScriptRoot 'browser-private.ps1') -SourcePath $source -ResultDirectory $directory -TimeoutSeconds 90
            if($LASTEXITCODE) { throw "Browser test failed: $($browser.browser), $language" }
            $browserReports += Get-Content (Join-Path $directory 'browser-results.json') -Raw | ConvertFrom-Json
            Write-Output "$($browser.browser) ${language}: normal and native fallback connections passed"
        }
    }
}
$browserPassed=0
foreach($report in $browserReports) { $browserPassed += $report.passed }
$summary=[pscustomobject]@{
    passed=($logic.passed+$argvTests.Count+$tray.passed+$trayLog.passed+$logWriter.passed+$observer.passed+$native.passed+$browserPassed)
    failed=0
    regression=$logic;arguments=$argvTests;tray=$tray;tray_log=$trayLog;log_writer=$logWriter;observer=$observer;native=$native;browsers=$browserReports
    limits='Native controls supply a test UIA provider. Browser tests, when requested, use real browsers with fresh profiles on separate desktops. No long-duration memory test is performed.'
}
$summary | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'summary.json')
$summary | Select-Object passed,failed | ConvertTo-Json -Compress
Write-Output "Results: $ResultDirectory"
