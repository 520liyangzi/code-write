[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$PythonCommand = $env:POLICYKIT_PYTHON,
    [string]$PolicyHome = $env:POLICYKIT_HOME,
    [switch]$Installed,
    [string]$CodagentHome = $env:CODAGENT_HOME,
    [string]$PluginName = 'java-policy-kit',
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PolicyArguments
)

$ErrorActionPreference = 'Stop'
$isHookInvocation = $PolicyArguments -and $PolicyArguments.Count -ge 1 -and $PolicyArguments[0] -eq 'hook'
trap {
    if ($isHookInvocation) {
        [Console]::Error.WriteLine("Java Policy Hook launcher failed: $($_.Exception.Message)")
        exit 2
    }
    throw
}

if ([string]::IsNullOrWhiteSpace($PythonCommand)) {
    $PythonCommand = 'python'
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Installed) {
    if ([string]::IsNullOrWhiteSpace($CodagentHome)) {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            throw 'Cannot resolve the user profile. Pass -CodagentHome explicitly.'
        }
        $CodagentHome = Join-Path $env:USERPROFILE '.codagent'
    }
    $CodagentHome = [System.IO.Path]::GetFullPath($CodagentHome)
    $markerPath = Join-Path $CodagentHome "plugins\$PluginName\.policykit-install.json"
    if (-not (Test-Path -LiteralPath $markerPath)) {
        throw "Installed Java Policy Kit marker not found: $markerPath"
    }
    $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $markerPath | ConvertFrom-Json
    if ($marker.owner -ne 'java-policy-kit' -or [string]::IsNullOrWhiteSpace($marker.release_path)) {
        throw "Invalid Java Policy Kit install marker: $markerPath"
    }
    $PolicyHome = [System.IO.Path]::GetFullPath([string]$marker.release_path)
}
if ([string]::IsNullOrWhiteSpace($PolicyHome)) {
    $PolicyHome = Split-Path -Parent $scriptDirectory
}

$PolicyHome = [System.IO.Path]::GetFullPath($PolicyHome)
$sourceDirectory = Join-Path $PolicyHome 'src'
if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory 'policykit'))) {
    throw "Policy Kit Python sources not found: $sourceDirectory"
}

$env:POLICYKIT_HOME = $PolicyHome
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$existingPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $env:PYTHONPATH = $sourceDirectory
} else {
    $env:PYTHONPATH = "$sourceDirectory$([System.IO.Path]::PathSeparator)$existingPythonPath"
}

$workDirectory = Join-Path $PolicyHome '.policy-work'
$rulesFile = Join-Path $workDirectory 'approved-rules.json'
$searchIndex = Join-Path $workDirectory 'search-index.db'
$configFile = Join-Path $PolicyHome 'policykit.json'

# An explicit PolicyHome (including -Installed) is an isolation boundary.
# Never inherit a previous source/release's rule, receipt, or audit paths from
# the parent PowerShell session.
$env:POLICYKIT_APPROVED_RULES = $rulesFile
$env:POLICYKIT_SEARCH_INDEX = $searchIndex
$env:POLICYKIT_CONFIG = $configFile
$env:POLICYKIT_RECEIPTS_DIR = Join-Path $workDirectory 'receipts'
$env:POLICYKIT_AUDIT_DIR = Join-Path $workDirectory 'audit'

if (-not $PolicyArguments -or $PolicyArguments.Count -eq 0) {
    $PolicyArguments = @('--help')
}

& $PythonCommand -m policykit @PolicyArguments
$policyExitCode = $LASTEXITCODE
if ($policyExitCode -ne 0) {
    if ($isHookInvocation) {
        [Console]::Error.WriteLine("Java Policy Hook process failed with exit code $policyExitCode.")
        exit 2
    }
    throw "Policy Kit command failed with exit code $policyExitCode."
}
