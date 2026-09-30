param([string]$OutputPath)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'memory-soak.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count) { throw ($errors | Out-String) }
$read=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'Read-Count'}
. ([scriptblock]::Create($read.Extent.Text))
$logPath=Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))) ('observer-'+[guid]::NewGuid().ToString('N')+'.log')
$writer=[IO.File]::Open($logPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite)
$results=New-Object System.Collections.ArrayList
function Append-Bytes([string]$Text) {
    $bytes=[Text.Encoding]::UTF8.GetBytes($Text)
    $writer.Write($bytes,0,$bytes.Length);$writer.Flush()
}
function Expect-Count($Name,$Expected) {
    $actual=Read-Count
    [void]$results.Add([pscustomobject]@{name=$Name;passed=($actual -eq $Expected);expected=$Expected;actual=$actual})
}
try {
    Append-Bytes "2026-09-30 12:00:00.000 [ACTION] APPROVED; dialog dismissed`r`n"
    Expect-Count 'Read while a writer holds the file open' 1
    Append-Bytes '2026-09-30 12:00:01.000 [ACTION] APPROVED; dialog dismissed'
    Expect-Count 'Incomplete ACTION record is not counted' 1
    Append-Bytes "`r`n2026-09-30 12:00:02.000 [INFO] text containing [ACTION]`r`n"
    Expect-Count 'Complete ACTION record counts; INFO payload does not' 2
} finally { $writer.Dispose() }
$report=[pscustomobject]@{tests=@($results);passed=@($results | Where-Object passed).Count;failed=@($results | Where-Object {-not $_.passed}).Count}
$report | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $OutputPath
$report | Select-Object passed,failed | ConvertTo-Json -Compress
if($report.failed) { exit 1 }
