param([string]$SourcePath,[string]$OutputPath)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
$definition=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'Write-Log'}
. ([scriptblock]::Create($definition.Extent.Text))
$script:hostFails=$false
function Write-Host { param($Object); if($script:hostFails) { throw 'Simulated closed console' } }
$directory=Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))
$LogPath=Join-Path $directory ('writer-'+[guid]::NewGuid().ToString('N')+'.log')
$results=New-Object System.Collections.ArrayList
function Check($Name,[scriptblock]$Body) {
    try { . $Body; [void]$results.Add([pscustomobject]@{name=$Name;passed=$true}) }
    catch { [void]$results.Add([pscustomobject]@{name=$Name;passed=$false;error=$_.Exception.Message}) }
}
function Expect($Actual,$Expected) { if($Actual -ne $Expected) { throw "Expected '$Expected', got '$Actual'" } }
[IO.File]::WriteAllText($LogPath,'')
Check 'One thousand events persist while a shared reader is open' {
    $reader=[IO.File]::Open($LogPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try { for($i=0;$i -lt 1000;$i++) { Write-Log "event $i" 'ACTION' -RequireWrite } }
    finally { $reader.Dispose() }
    Expect ([regex]::Matches([IO.File]::ReadAllText($LogPath),'\[ACTION\]').Count) 1000
}
Check 'An incompatible lock raises a required-write failure' {
    $reader=[IO.File]::Open($LogPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $failed=$false
    try { try { Write-Log 'blocked event' 'ACTION' -RequireWrite } catch { $failed=$true } }
    finally { $reader.Dispose() }
    Expect $failed $true
    Expect ([IO.File]::ReadAllText($LogPath).Contains('blocked event')) $false
}
Check 'A required event persists after the lock is removed' {
    Write-Log 'recovered event' 'ACTION' -RequireWrite
    Expect ([regex]::Matches([IO.File]::ReadAllText($LogPath),'recovered event').Count) 1
}
Check 'A reader that blocks rotation does not block appending' {
    [IO.File]::WriteAllText($LogPath,(('x'*1048580)+"`r`n"))
    $reader=[IO.File]::Open($LogPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
    try { Write-Log 'rotation blocked but event retained' 'ACTION' -RequireWrite }
    finally { $reader.Dispose() }
    Expect ([IO.File]::ReadAllText($LogPath).Contains('rotation blocked but event retained')) $true
    Write-Log 'next event after rotation' 'ACTION' -RequireWrite
    Expect ([IO.File]::ReadAllText($LogPath+'.1').Contains('rotation blocked but event retained')) $true
    Expect ([IO.File]::ReadAllText($LogPath).Contains('next event after rotation')) $true
}
Check 'UTF-8 labels round-trip' {
    $text=[regex]::Unescape('\u5141\u8bb8')
    Write-Log $text
    Expect ([IO.File]::ReadAllText($LogPath).Contains($text)) $true
}
Check 'A console failure does not duplicate a persisted event' {
    $script:hostFails=$true
    try { Write-Log 'console-failure-event' 'ACTION' -RequireWrite }
    finally { $script:hostFails=$false }
    Expect ([regex]::Matches([IO.File]::ReadAllText($LogPath),'console-failure-event').Count) 1
}
$report=[pscustomobject]@{tests=@($results);passed=@($results | Where-Object passed).Count;failed=@($results | Where-Object {-not $_.passed}).Count}
$report | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $OutputPath
$report | Select-Object passed,failed | ConvertTo-Json -Compress
if($report.failed) { exit 1 }
