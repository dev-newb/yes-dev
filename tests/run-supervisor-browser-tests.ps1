param(
    [string]$Python = 'python',
    [string]$ResultDirectory = (Join-Path $env:TEMP ('yesdev-supervisor-' + [guid]::NewGuid().ToString('N')))
)
$ErrorActionPreference = 'Stop'
$pythonPath = (Get-Command $Python -ErrorAction Stop).Source
$repo = Split-Path -Parent $PSScriptRoot
$cases = @(
    @{name='fast-stall';interval=150;language='en-US';burst_size=4;inject_stall=$true},
    @{name='normal-zh';interval=250;language='zh-CN';burst_size=4},
    @{name='relaxed';interval=750;language='en-US';burst_size=4},
    @{name='observe';interval=150;language='en-US';burst_size=1;observe=$true},
    @{name='stop-blocked';interval=150;language='en-US';burst_size=1;inject_stall=$true;shutdown_stalled=$true}
)
$reports = @()
foreach ($case in $cases) {
    $directory = Join-Path ([IO.Path]::GetFullPath($ResultDirectory)) $case.name
    if (Test-Path $directory) { throw "Use a fresh result directory: $directory" }
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $case.python = $pythonPath
    $case | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $directory 'config.json')
    & "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run-private-desktop.ps1') -ScriptPath (Join-Path $PSScriptRoot 'supervisor-browser-private.ps1') -SourcePath (Join-Path $repo 'watcher.ps1') -ResultDirectory $directory -TimeoutSeconds 80
    if ($LASTEXITCODE) { throw "Supervisor browser test failed: $($case.name); see $directory" }
    $reports += Get-Content (Join-Path $directory 'supervisor-results.json') -Raw | ConvertFrom-Json
    Write-Output "$($case.name): passed"
}
$reports | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'summary.json')
Write-Output "Results: $ResultDirectory"
