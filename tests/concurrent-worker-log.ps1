param([string]$SourcePath, [string]$ResultDirectory)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $ResultDirectory -Force | Out-Null
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definition=$ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'Write-Log'}
$log = Join-Path $ResultDirectory 'shared.log'
$script = Join-Path $ResultDirectory 'writer.ps1'
$body = @'
param([string]$LogPath,[int]$Writer)
$ErrorActionPreference='Stop'
function Write-Host { param($Object) }
__WRITE_LOG__
for ($i=0; $i -lt 200; $i++) { Write-Log "writer=$Writer event=$i" 'ACTION' -RequireWrite }
'@
[IO.File]::WriteAllText($script,$body.Replace('__WRITE_LOG__',$definition.Extent.Text),[Text.Encoding]::UTF8)
$processes = @()
try {
    foreach ($writer in 1..4) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -LogPath "{1}" -Writer {2}' -f $script,$log,$writer
        $processes += Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ResultDirectory "writer-$writer.txt") -RedirectStandardError (Join-Path $ResultDirectory "writer-$writer.err")
    }
    foreach ($process in $processes) {
        if (-not $process.WaitForExit(20000)) { throw 'Concurrent log writer timed out' }
        if ($process.ExitCode) { throw "Log writer exited with $($process.ExitCode)" }
    }
    $lines = @(Get-Content $log)
    if ($lines.Count -ne 800) { throw "Expected 800 records, got $($lines.Count)" }
    $events = @($lines | ForEach-Object {
        if ($_ -notmatch '^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[ACTION\] (writer=\d event=\d+)$') { throw "Corrupt record: $_" }
        $matches[1]
    })
    if (@($events | Select-Object -Unique).Count -ne 800) { throw 'Duplicate or missing approval records' }
    Write-Output '800 distinct persisted events from four simultaneous writers passed'
} finally {
    foreach ($process in $processes) { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force } }
}
