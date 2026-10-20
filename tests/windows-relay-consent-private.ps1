param([string]$SourcePath, [string]$ResultDirectory)
$ErrorActionPreference = 'Stop'
$config = Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$repo = Split-Path -Parent $SourcePath
& $config.python (Join-Path $PSScriptRoot 'windows_relay_consent.py') --repo $repo --output $ResultDirectory --interval $config.interval
exit $LASTEXITCODE
