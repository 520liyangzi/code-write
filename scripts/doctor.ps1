[CmdletBinding()]
param(
    [string]$CodagentHome = $env:CODAGENT_HOME,
    [string]$PolicyRoot,
    [string]$PythonCommand = $env:POLICYKIT_PYTHON,
    [string]$PluginName = 'java-policy-kit',
    [switch]$RequireActivated
)

$ErrorActionPreference = 'Continue'
$failures = 0

function Write-Check {
    param([bool]$Passed, [string]$Message, [bool]$Required = $true)
    if ($Passed) {
        Write-Host "[PASS] $Message" -ForegroundColor Green
    } elseif ($Required) {
        $script:failures++
        Write-Host "[FAIL] $Message" -ForegroundColor Red
    } else {
        Write-Host "[INFO] $Message" -ForegroundColor Yellow
    }
}

if ([string]::IsNullOrWhiteSpace($CodagentHome) -and -not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $CodagentHome = Join-Path $env:USERPROFILE '.codagent'
}
if ([string]::IsNullOrWhiteSpace($PythonCommand)) {
    $PythonCommand = 'python'
}
if ([string]::IsNullOrWhiteSpace($PolicyRoot)) {
    $PolicyRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$PolicyRoot = [System.IO.Path]::GetFullPath($PolicyRoot)

$pythonOk = $false
try {
    $pythonVersion = (& $PythonCommand --version 2>&1 | Out-String).Trim()
    $pythonOk = $LASTEXITCODE -eq 0 -and $pythonVersion -match 'Python\s+(\d+)\.(\d+)'
    if ($pythonOk) {
        $pythonMajor = [int]$Matches[1]
        $pythonMinor = [int]$Matches[2]
        $pythonOk = $pythonMajor -gt 3 -or ($pythonMajor -eq 3 -and $pythonMinor -ge 10)
    }
    Write-Check $pythonOk "Python 3.10+ available: $pythonVersion"
} catch {
    Write-Check $false "Python unavailable: $PythonCommand"
}

$javaText = ''
try {
    $javaText = (& java -version 2>&1 | Out-String).Trim()
    $java21 = $LASTEXITCODE -eq 0 -and $javaText -match '(version\s+"21(?:[\.]|"|\s)|openjdk\s+21(?:[\.]|\s|$))'
    Write-Check $java21 'JDK 21 available'
} catch {
    Write-Check $false 'JDK 21 unavailable'
}

try {
    $mavenText = (& mvn -version 2>&1 | Out-String).Trim()
    Write-Check ($LASTEXITCODE -eq 0) 'Maven available'
} catch {
    Write-Check $false 'Maven unavailable'
}

Write-Check (Test-Path -LiteralPath (Join-Path $PolicyRoot 'src\policykit')) "Policy Kit sources present: $PolicyRoot"
$configPresent = Test-Path -LiteralPath (Join-Path $PolicyRoot 'policykit.json')
$rulesPresent = Test-Path -LiteralPath (Join-Path $PolicyRoot '.policy-work\approved-rules.json')
$blockPresent = Test-Path -LiteralPath (Join-Path $PolicyRoot '.policy-work\GLOBAL_MD_BLOCK.md')
Write-Check $configPresent $(if ($configPresent) { 'policykit.json initialized' } else { 'policykit.json not initialized yet' }) $RequireActivated
Write-Check $rulesPresent $(if ($rulesPresent) { 'Approved rules present' } else { 'Approved rules not activated yet' }) $RequireActivated
Write-Check $blockPresent $(if ($blockPresent) { 'Global MD block generated' } else { 'Global MD block not generated yet' }) $RequireActivated

if (-not [string]::IsNullOrWhiteSpace($CodagentHome)) {
    $pluginPath = Join-Path ([System.IO.Path]::GetFullPath($CodagentHome)) "plugins\$PluginName"
    $marker = Join-Path $pluginPath '.policykit-install.json'
    $hookConfig = Join-Path $pluginPath 'hooks\hooks.json'
    $pluginPresent = Test-Path -LiteralPath $marker
    Write-Check $pluginPresent $(if ($pluginPresent) { "Codagent plugin installed: $pluginPath" } else { "Codagent plugin not installed: $pluginPath" }) $RequireActivated
    if (Test-Path -LiteralPath $hookConfig) {
        try {
            $hookBytes = [System.IO.File]::ReadAllBytes($hookConfig)
            $hasUtf8Bom = $hookBytes.Length -ge 3 -and $hookBytes[0] -eq 0xEF -and $hookBytes[1] -eq 0xBB -and $hookBytes[2] -eq 0xBF
            Write-Check (-not $hasUtf8Bom) 'hooks.json is UTF-8 without BOM'
            $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
            $hookText = $strictUtf8.GetString($hookBytes)
            $null = $hookText | ConvertFrom-Json
            Write-Check $true 'hooks.json is valid strict UTF-8 JSON'
        } catch {
            Write-Check $false 'hooks.json cannot be parsed as strict UTF-8 JSON'
        }
    } else {
        Write-Check $false 'hooks.json missing' $RequireActivated
    }
}

if ($pythonOk) {
    try {
        & (Join-Path $PolicyRoot 'scripts\policy.ps1') -PythonCommand $PythonCommand -PolicyHome $PolicyRoot doctor
        Write-Check ($LASTEXITCODE -eq 0) 'Policy Kit internal doctor passed' $RequireActivated
    } catch {
        Write-Check $false "Policy Kit internal doctor error: $($_.Exception.Message)" $RequireActivated
    }
}

Write-Host '[INFO] CodeGraph is optional and never fails doctor.' -ForegroundColor Cyan
if ($failures -gt 0) {
    throw "Doctor completed with $failures required check(s) failing."
}
Write-Host 'Doctor completed: all required checks passed.' -ForegroundColor Green
