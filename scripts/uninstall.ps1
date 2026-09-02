[CmdletBinding()]
param(
    [string]$CodagentHome = $env:CODAGENT_HOME,
    [string]$InstallRoot,
    [string]$PluginName = 'java-policy-kit',
    [switch]$IncludeRuntime
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($CodagentHome)) {
    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        throw 'Cannot resolve the user profile. Pass -CodagentHome explicitly.'
    }
    $CodagentHome = Join-Path $env:USERPROFILE '.codagent'
}
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'Cannot resolve LOCALAPPDATA. Pass -InstallRoot explicitly.'
    }
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'CodagentJavaPolicy'
}

$CodagentHome = [System.IO.Path]::GetFullPath($CodagentHome)
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$pluginsDirectory = Join-Path $CodagentHome 'plugins'
$pluginPath = Join-Path $pluginsDirectory $PluginName
$pluginFull = [System.IO.Path]::GetFullPath($pluginPath).TrimEnd('\')
$pluginsFull = [System.IO.Path]::GetFullPath($pluginsDirectory).TrimEnd('\')
if (-not $pluginFull.StartsWith("$pluginsFull\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Plugin path must stay under $pluginsFull; actual path: $pluginFull"
}
$markerPath = Join-Path $pluginPath '.policykit-install.json'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$recoveryRoot = Join-Path $CodagentHome "backups\java-policy-kit\uninstalled-$timestamp"
$installRootOwnerMarker = Join-Path $InstallRoot '.policykit-owner.json'

if ($IncludeRuntime) {
    if (-not (Test-Path -LiteralPath $installRootOwnerMarker)) {
        throw "Runtime ownership marker missing; refusing to move InstallRoot: $installRootOwnerMarker"
    }
    $runtimeOwner = Get-Content -Raw -Encoding UTF8 -LiteralPath $installRootOwnerMarker | ConvertFrom-Json
    if ($runtimeOwner.owner -ne 'java-policy-kit-runtime') {
        throw "Runtime ownership marker does not match; refusing to move InstallRoot: $InstallRoot"
    }
}

if (Test-Path -LiteralPath $pluginPath) {
    if (-not (Test-Path -LiteralPath $markerPath)) {
        throw "Plugin directory has no Java Policy Kit marker; refusing to move it: $pluginPath"
    }
    $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $markerPath | ConvertFrom-Json
    if ($marker.owner -ne 'java-policy-kit') {
        throw "Plugin owner does not match; refusing to move it: $pluginPath"
    }
    if (Test-Path -LiteralPath $recoveryRoot) {
        throw "Recovery path already exists; retry later: $recoveryRoot"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $recoveryRoot) | Out-Null
    Move-Item -LiteralPath $pluginPath -Destination $recoveryRoot
    Write-Host "[DISABLED] Plugin moved to recoverable backup: $recoveryRoot" -ForegroundColor Green
} else {
    Write-Host "[SKIP] Plugin not found: $pluginPath" -ForegroundColor Yellow
}

if ($IncludeRuntime -and (Test-Path -LiteralPath $InstallRoot)) {
    $runtimeParent = Split-Path -Parent $InstallRoot
    $runtimeRecovery = Join-Path $runtimeParent "CodagentJavaPolicy-uninstalled-$timestamp"
    if ($InstallRoot -eq [System.IO.Path]::GetPathRoot($InstallRoot)) {
        throw "Refusing to move a filesystem root: $InstallRoot"
    }
    if (Test-Path -LiteralPath $runtimeRecovery) {
        throw "Runtime recovery path already exists; retry later: $runtimeRecovery"
    }
    Move-Item -LiteralPath $InstallRoot -Destination $runtimeRecovery
    Write-Host "[DISABLED] Runtime moved to recoverable backup: $runtimeRecovery" -ForegroundColor Green
} else {
    Write-Host '[KEPT] Runtime, approved rules, and audit reports remain. Use -IncludeRuntime to move them to recovery.' -ForegroundColor Cyan
}

Write-Host '[UNCHANGED] Codagent global MD. Remove the CODAGENT-JAVA-POLICY marked block manually if needed.' -ForegroundColor Cyan
