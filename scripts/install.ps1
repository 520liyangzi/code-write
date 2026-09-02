[CmdletBinding()]
param(
    [string]$CodagentHome = $env:CODAGENT_HOME,
    [string]$InstallRoot,
    [string]$PythonCommand = $env:POLICYKIT_PYTHON,
    [string]$PluginName = 'java-policy-kit',
    [switch]$Update,
    [switch]$SkipDoctor
)

$ErrorActionPreference = 'Stop'

function Resolve-AbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $childFull = (Resolve-AbsolutePath $Child).TrimEnd('\')
    $parentFull = (Resolve-AbsolutePath $Parent).TrimEnd('\')
    if (-not $childFull.StartsWith("$parentFull\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be under $parentFull; actual path: $childFull"
    }
}

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
if ([string]::IsNullOrWhiteSpace($PythonCommand)) {
    $PythonCommand = 'python'
}

$CodagentHome = Resolve-AbsolutePath $CodagentHome
$InstallRoot = Resolve-AbsolutePath $InstallRoot
$sourceRoot = Resolve-AbsolutePath (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$pluginSource = Join-Path $sourceRoot 'codagent-plugin'
$approvedRules = Join-Path $sourceRoot '.policy-work\approved-rules.json'
$globalBlock = Join-Path $sourceRoot '.policy-work\GLOBAL_MD_BLOCK.md'
$installRootMarker = Join-Path $InstallRoot '.policykit-owner.json'

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'src\policykit'))) {
    throw "This is not a complete Java Policy Kit directory: $sourceRoot"
}
if (-not (Test-Path -LiteralPath $approvedRules)) {
    throw 'No activated policy found. Run policy.ps1 prepare, review REVIEW_ME.md, and then run activate.'
}
if (-not (Test-Path -LiteralPath $globalBlock)) {
    throw "Generated GLOBAL_MD_BLOCK.md not found: $globalBlock"
}

if (Test-Path -LiteralPath $InstallRoot) {
    if (Test-Path -LiteralPath $installRootMarker) {
        $rootOwner = Get-Content -Raw -Encoding UTF8 -LiteralPath $installRootMarker | ConvertFrom-Json
        if ($rootOwner.owner -ne 'java-policy-kit-runtime') {
            throw "InstallRoot ownership marker does not match: $installRootMarker"
        }
    } else {
        $existing = @(Get-ChildItem -Force -LiteralPath $InstallRoot)
        if ($existing.Count -gt 0) {
            throw "InstallRoot is not empty and has no Java Policy Kit ownership marker: $InstallRoot. Choose a dedicated directory."
        }
    }
}

& $PythonCommand --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Cannot run Python: $PythonCommand"
}
& $PythonCommand -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 2)'
if ($LASTEXITCODE -ne 0) {
    throw 'Java Policy Kit requires Python 3.10 or newer.'
}

$releaseId = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseBase = Join-Path $InstallRoot 'releases'
$releasePath = Join-Path $releaseBase $releaseId
$pluginsDirectory = Join-Path $CodagentHome 'plugins'
$pluginTarget = Join-Path $pluginsDirectory $PluginName
$backupDirectory = Join-Path $CodagentHome "backups\java-policy-kit\$releaseId"
$stagePath = Join-Path $pluginsDirectory ".$PluginName-stage-$releaseId"

Assert-ChildPath -Child $releasePath -Parent $InstallRoot -Label 'Release path'
Assert-ChildPath -Child $pluginTarget -Parent $pluginsDirectory -Label 'Plugin path'
Assert-ChildPath -Child $stagePath -Parent $pluginsDirectory -Label 'Staging path'

if (Test-Path -LiteralPath $releasePath) {
    throw "Release path already exists; retry later: $releasePath"
}
if (Test-Path -LiteralPath $stagePath) {
    throw "Staging path already exists; inspect it before retrying: $stagePath"
}
if (Test-Path -LiteralPath $pluginTarget) {
    $markerPath = Join-Path $pluginTarget '.policykit-install.json'
    if (-not (Test-Path -LiteralPath $markerPath)) {
        throw "Plugin path exists but is not owned by this installer. It will not be overwritten: $pluginTarget. Use -PluginName."
    }
    $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $markerPath | ConvertFrom-Json
    if ($marker.owner -ne 'java-policy-kit') {
        throw "Plugin ownership marker does not match; refusing to overwrite: $pluginTarget"
    }
    if (-not $Update) {
        throw "$PluginName is already installed. Re-run with -Update; the old version will be backed up first."
    }
}

New-Item -ItemType Directory -Force -Path $InstallRoot, $releasePath, $pluginsDirectory | Out-Null
if (-not (Test-Path -LiteralPath $installRootMarker)) {
    $rootMarkerJson = [ordered]@{
        owner = 'java-policy-kit-runtime'
        schema_version = 1
        created_at = (Get-Date).ToString('o')
    } | ConvertTo-Json
    Write-Utf8NoBom -Path $installRootMarker -Content $rootMarkerJson
}

$releaseSource = Join-Path $releasePath 'src'
New-Item -ItemType Directory -Force -Path $releaseSource | Out-Null
Copy-Item -Recurse -LiteralPath (Join-Path $sourceRoot 'src\policykit') -Destination $releaseSource
Copy-Item -Recurse -LiteralPath (Join-Path $sourceRoot 'scripts') -Destination $releasePath
Copy-Item -Recurse -LiteralPath $pluginSource -Destination $stagePath
Copy-Item -LiteralPath (Join-Path $sourceRoot 'instruction.md') -Destination $releasePath
if (Test-Path -LiteralPath (Join-Path $sourceRoot 'pyproject.toml')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'pyproject.toml') -Destination $releasePath
}
if (Test-Path -LiteralPath (Join-Path $sourceRoot 'policykit.json')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'policykit.json') -Destination $releasePath
}

$releaseWork = Join-Path $releasePath '.policy-work'
New-Item -ItemType Directory -Force -Path $releaseWork | Out-Null
foreach ($artifactName in @('approved-rules.json', 'search-index.db', 'GLOBAL_MD_BLOCK.md')) {
    $artifact = Join-Path (Join-Path $sourceRoot '.policy-work') $artifactName
    if (Test-Path -LiteralPath $artifact) {
        Copy-Item -LiteralPath $artifact -Destination $releaseWork
    }
}

$policyScript = Join-Path $releasePath 'scripts\policy.ps1'
function New-HookCommand {
    param([Parameter(Mandatory = $true)][string]$EventName)
    return ('powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonCommand "{1}" -PolicyHome "{2}" hook {3}' -f $policyScript, $PythonCommand, $releasePath, $EventName)
}

$hookTemplate = Join-Path $stagePath 'hooks\hooks.template.json'
$hookConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $hookTemplate | ConvertFrom-Json
$hookConfig.hooks.PreToolUse[0].hooks[0].command = New-HookCommand 'pre-edit'
$hookConfig.hooks.PreToolUse[1].hooks[0].command = New-HookCommand 'pre-shell'
$hookConfig.hooks.PostToolUse[0].hooks[0].command = New-HookCommand 'post-edit'
$hookConfig.hooks.Stop[0].hooks[0].command = New-HookCommand 'stop'
$hookJson = $hookConfig | ConvertTo-Json -Depth 12
Write-Utf8NoBom -Path (Join-Path $stagePath 'hooks\hooks.json') -Content $hookJson

$runtimeReference = Join-Path $stagePath 'skills\java-policy\references\runtime.md'
if (-not (Test-Path -LiteralPath $runtimeReference)) {
    throw "Skill runtime reference not found: $runtimeReference"
}
$searchCommand = ('powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonCommand "{1}" -PolicyHome "{2}" search' -f $policyScript, $PythonCommand, $releasePath)
$runtimeText = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeReference
if (-not $runtimeText.Contains('__POLICYKIT_SEARCH_COMMAND__')) {
    throw "Skill runtime reference has no command placeholder: $runtimeReference"
}
$runtimeText = $runtimeText.Replace('__POLICYKIT_SEARCH_COMMAND__', $searchCommand)
Write-Utf8NoBom -Path $runtimeReference -Content $runtimeText

$installMarker = [ordered]@{
    owner = 'java-policy-kit'
    version = '0.1.0'
    installed_at = (Get-Date).ToString('o')
    release_path = $releasePath
    source_path = $sourceRoot
}
$installMarkerJson = $installMarker | ConvertTo-Json
Write-Utf8NoBom -Path (Join-Path $stagePath '.policykit-install.json') -Content $installMarkerJson

$oldPluginMoved = $false
try {
    if (Test-Path -LiteralPath $pluginTarget) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupDirectory) | Out-Null
        Move-Item -LiteralPath $pluginTarget -Destination $backupDirectory
        $oldPluginMoved = $true
    }
    Move-Item -LiteralPath $stagePath -Destination $pluginTarget
} catch {
    if ($oldPluginMoved -and -not (Test-Path -LiteralPath $pluginTarget) -and (Test-Path -LiteralPath $backupDirectory)) {
        Move-Item -LiteralPath $backupDirectory -Destination $pluginTarget
    }
    throw
}

$releaseManifest = [ordered]@{
    owner = 'java-policy-kit'
    installed_at = (Get-Date).ToString('o')
    codagent_home = $CodagentHome
    plugin_path = $pluginTarget
    previous_plugin_backup = if ($oldPluginMoved) { $backupDirectory } else { $null }
}
$releaseManifestJson = $releaseManifest | ConvertTo-Json
Write-Utf8NoBom -Path (Join-Path $releasePath 'install.json') -Content $releaseManifestJson

Write-Host "[OK] Plugin: $pluginTarget" -ForegroundColor Green
Write-Host "[OK] Runtime: $releasePath" -ForegroundColor Green
if ($oldPluginMoved) {
    Write-Host "[BACKUP] Previous plugin: $backupDirectory" -ForegroundColor Yellow
}
Write-Host '[UNCHANGED] Codagent global MD and other user configuration.' -ForegroundColor Cyan
Write-Host 'Next: copy the marked block below into the Codagent global MD:'
Write-Host (Join-Path $releaseWork 'GLOBAL_MD_BLOCK.md') -ForegroundColor Cyan

if (-not $SkipDoctor) {
    & (Join-Path $releasePath 'scripts\doctor.ps1') -CodagentHome $CodagentHome -PolicyRoot $releasePath -RequireActivated
}
