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
for ($i=0; $i -lt 200; $i++) {
    # Production keeps a confirmation pending when the bounded log lock times out.
    # Retry that same record here too; never count an unsaved event as success.
    for ($attempt=0; ; $attempt++) {
        try { Write-Log "writer=$Writer event=$i" 'ACTION' -RequireWrite; break }
        catch {
            if ($attempt -ge 9 -or $_.Exception.Message -notmatch 'Approval (log lock timed out|event could not be persisted)') { throw }
            Start-Sleep -Milliseconds 25
        }
    }
}
'@
[IO.File]::WriteAllText($script,$body.Replace('__WRITE_LOG__',$definition.Extent.Text),[Text.Encoding]::UTF8)
$processes = @()
try {
    foreach ($writer in 1..4) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -LogPath "{1}" -Writer {2}' -f $script,$log,$writer
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
        $info.Arguments = $arguments
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.WindowStyle = 'Hidden'
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info
        [void]$process.Start()
        $processes += [pscustomobject]@{Process=$process;Output=$process.StandardOutput.ReadToEndAsync();Errors=$process.StandardError.ReadToEndAsync();Writer=$writer}
    }
    foreach ($record in $processes) {
        $process = $record.Process
        if (-not $process.WaitForExit(20000)) { throw 'Concurrent log writer timed out' }
        $record.Output.GetAwaiter().GetResult() | Set-Content (Join-Path $ResultDirectory "writer-$($record.Writer).txt")
        $record.Errors.GetAwaiter().GetResult() | Set-Content (Join-Path $ResultDirectory "writer-$($record.Writer).err")
        if ($process.ExitCode -ne 0) { throw "Log writer exited with $($process.ExitCode)" }
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
    foreach ($record in $processes) {
        if (-not $record.Process.HasExited) { $record.Process.Kill() }
        $record.Process.Dispose()
    }
}
