param([string]$SourcePath, [string]$ResultDirectory)
$ErrorActionPreference = 'Stop'
$config = Get-Content (Join-Path $ResultDirectory 'config.json') -Raw | ConvertFrom-Json
$repo = Split-Path -Parent $SourcePath
$extra = @()
if ($config.multiplex) { $extra += '--multiplex' }
if ($config.browser) { $extra += @('--browser', $config.browser) }
& $config.python (Join-Path $PSScriptRoot 'windows_relay_consent.py') --repo $repo --output $ResultDirectory --interval $config.interval @extra
exit $LASTEXITCODE
