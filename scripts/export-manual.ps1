[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$PythonCommand = $env:POLICYKIT_PYTHON
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

function Assert-RequiredFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function Assert-RequiredDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label not found: $Path"
    }
}

function Test-SameOrChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $candidateFull = (Resolve-AbsolutePath $Candidate).TrimEnd('\')
    $parentFull = (Resolve-AbsolutePath $Parent).TrimEnd('\')
    return $candidateFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidateFull.StartsWith("$parentFull\", [System.StringComparison]::OrdinalIgnoreCase)
}

$sourceRoot = Resolve-AbsolutePath (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $sourceRoot 'manual-package'
}
$outputRoot = Resolve-AbsolutePath $OutputDirectory

if (Test-Path -LiteralPath $outputRoot) {
    if (-not (Test-Path -LiteralPath $outputRoot -PathType Container)) {
        throw "Output path exists and is not a directory: $outputRoot"
    }
    $existingOutput = @(Get-ChildItem -Force -LiteralPath $outputRoot)
    if ($existingOutput.Count -gt 0) {
        throw "Output directory is not empty; refusing to overwrite anything: $outputRoot"
    }
}

if ([string]::IsNullOrWhiteSpace($PythonCommand)) {
    $PythonCommand = 'python'
}
$pythonCommandInput = $PythonCommand
try {
    $pythonCandidates = @(Get-Command -Name $PythonCommand -CommandType Application -ErrorAction Stop)
    if ($pythonCandidates.Count -eq 0) {
        throw 'No application command was resolved.'
    }
    $resolvedPython = [string]$pythonCandidates[0].Source
    if ([string]::IsNullOrWhiteSpace($resolvedPython)) {
        $resolvedPython = [string]$pythonCandidates[0].Definition
    }
    if ([string]::IsNullOrWhiteSpace($resolvedPython)) {
        throw 'The resolved command has no executable path.'
    }
    $PythonCommand = Resolve-AbsolutePath $resolvedPython
    if (-not (Test-Path -LiteralPath $PythonCommand -PathType Leaf)) {
        throw "The resolved executable does not exist: $PythonCommand"
    }
    & $PythonCommand --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python returned exit code $LASTEXITCODE"
    }
    & $PythonCommand -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 2)'
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.10 or newer is required.'
    }
} catch {
    throw "Cannot resolve '$pythonCommandInput' to one Python 3.10+ executable. Pass -PythonCommand with an executable path. $($_.Exception.Message)"
}

$policySource = Join-Path $sourceRoot 'src\policykit'
$policyScriptSource = Join-Path $sourceRoot 'scripts\policy.ps1'
$configSource = Join-Path $sourceRoot 'policykit.json'
$hookTemplate = Join-Path $sourceRoot 'codagent-plugin\hooks\hooks.template.json'
$skillsSource = Join-Path $sourceRoot 'codagent-plugin\skills'
$workSource = Join-Path $sourceRoot '.policy-work'
$approvedSource = Join-Path $workSource 'approved-rules.json'
$indexSource = Join-Path $workSource 'search-index.db'
$globalBlockSource = Join-Path $workSource 'GLOBAL_MD_BLOCK.md'
$skillNames = @('java-policy', 'java-review', 'java-policy-authoring')

foreach ($recursiveSource in @($policySource, $skillsSource)) {
    if (Test-SameOrChildPath -Candidate $outputRoot -Parent $recursiveSource) {
        throw "Output directory cannot be the same as or inside a recursively copied source: $recursiveSource"
    }
}

Assert-RequiredDirectory -Path $policySource -Label 'Policy Kit Python package'
Assert-RequiredFile -Path $policyScriptSource -Label 'Policy Kit PowerShell runner'
Assert-RequiredFile -Path $configSource -Label 'Policy Kit configuration'
Assert-RequiredFile -Path $hookTemplate -Label 'Hook template'
Assert-RequiredDirectory -Path $skillsSource -Label 'Skill source directory'
Assert-RequiredFile -Path $approvedSource -Label 'Activated approved-rules.json'
Assert-RequiredFile -Path $indexSource -Label 'Activated search-index.db'
Assert-RequiredFile -Path $globalBlockSource -Label 'Activated GLOBAL_MD_BLOCK.md'
foreach ($skillName in $skillNames) {
    $skillDirectory = Join-Path $skillsSource $skillName
    Assert-RequiredDirectory -Path $skillDirectory -Label "Skill '$skillName'"
    Assert-RequiredFile -Path (Join-Path $skillDirectory 'SKILL.md') -Label "Skill manifest '$skillName'"
}

try {
    $null = Get-Content -Raw -Encoding UTF8 -LiteralPath $configSource | ConvertFrom-Json
} catch {
    throw "policykit.json is not valid JSON: $($_.Exception.Message)"
}
try {
    $approvedData = Get-Content -Raw -Encoding UTF8 -LiteralPath $approvedSource | ConvertFrom-Json
} catch {
    throw "approved-rules.json is not valid JSON: $($_.Exception.Message)"
}
$approvedRules = @($approvedData.rules)
if ($approvedRules.Count -eq 0) {
    throw "approved-rules.json contains no activated rules: $approvedSource"
}
$notApproved = @($approvedRules | Where-Object { $_.status -ne 'approved' })
if ($notApproved.Count -gt 0) {
    throw "approved-rules.json contains $($notApproved.Count) rule(s) that are not approved. Re-run activate before exporting."
}
if ((Get-Item -LiteralPath $indexSource).Length -eq 0) {
    throw "search-index.db is empty: $indexSource"
}
$globalBlockText = Get-Content -Raw -Encoding UTF8 -LiteralPath $globalBlockSource
if ([string]::IsNullOrWhiteSpace($globalBlockText)) {
    throw "GLOBAL_MD_BLOCK.md is empty: $globalBlockSource"
}

# Reuse the runtime validator so an approved JSON / SQLite bundle mismatch is
# rejected before any deployment directory is created.
& $policyScriptSource -PythonCommand $PythonCommand -PolicyHome $sourceRoot doctor
if ($LASTEXITCODE -ne 0) {
    throw "Policy Kit doctor rejected the activated bundle. Re-run activate before exporting."
}

try {
    $hookConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $hookTemplate | ConvertFrom-Json
    $null = $hookConfig.hooks.PreToolUse[0].hooks[0].command
    $null = $hookConfig.hooks.PreToolUse[1].hooks[0].command
    $null = $hookConfig.hooks.PostToolUse[0].hooks[0].command
    $null = $hookConfig.hooks.Stop[0].hooks[0].command
} catch {
    throw "Hook template is invalid or incomplete: $($_.Exception.Message)"
}

$runtimeTemplateSource = Join-Path $skillsSource 'java-policy\references\runtime.md'
$checklistTemplateSource = Join-Path $sourceRoot 'scripts\templates\COPY_CHECKLIST.zh-CN.md'
Assert-RequiredFile -Path $runtimeTemplateSource -Label 'java-policy runtime command template'
Assert-RequiredFile -Path $checklistTemplateSource -Label 'Chinese manual-copy checklist template'
$runtimeTemplateText = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeTemplateSource
if (-not $runtimeTemplateText.Contains('__POLICYKIT_SEARCH_COMMAND__')) {
    throw "java-policy runtime template has no search command placeholder: $runtimeTemplateSource"
}

if (-not (Test-Path -LiteralPath $outputRoot)) {
    New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
}
$skillsOutput = Join-Path $outputRoot 'skills'
$hooksOutput = Join-Path $outputRoot 'hooks'
$runtimeOutput = Join-Path $outputRoot 'runtime'
$runtimeSourceOutput = Join-Path $runtimeOutput 'src'
$runtimeScriptsOutput = Join-Path $runtimeOutput 'scripts'
$runtimeWorkOutput = Join-Path $runtimeOutput '.policy-work'
New-Item -ItemType Directory -Force -Path $skillsOutput, $hooksOutput, $runtimeSourceOutput, $runtimeScriptsOutput, $runtimeWorkOutput | Out-Null

foreach ($skillName in $skillNames) {
    Copy-Item -Recurse -LiteralPath (Join-Path $skillsSource $skillName) -Destination (Join-Path $skillsOutput $skillName)
}
Copy-Item -Recurse -LiteralPath $policySource -Destination (Join-Path $runtimeSourceOutput 'policykit')
Copy-Item -LiteralPath $policyScriptSource -Destination (Join-Path $runtimeScriptsOutput 'policy.ps1')
Copy-Item -LiteralPath $configSource -Destination (Join-Path $runtimeOutput 'policykit.json')
Copy-Item -LiteralPath $approvedSource -Destination (Join-Path $runtimeWorkOutput 'approved-rules.json')
Copy-Item -LiteralPath $indexSource -Destination (Join-Path $runtimeWorkOutput 'search-index.db')
Copy-Item -LiteralPath $globalBlockSource -Destination (Join-Path $runtimeWorkOutput 'GLOBAL_MD_BLOCK.md')
Copy-Item -LiteralPath $globalBlockSource -Destination (Join-Path $outputRoot 'CLAUDE_MD_BLOCK.md')

$runtimePolicyScript = Join-Path $runtimeScriptsOutput 'policy.ps1'
function New-HookCommand {
    param([Parameter(Mandatory = $true)][string]$EventName)
    return ('powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonCommand "{1}" -PolicyHome "{2}" hook {3}' -f $runtimePolicyScript, $PythonCommand, $runtimeOutput, $EventName)
}

$hookConfig.hooks.PreToolUse[0].hooks[0].command = New-HookCommand 'pre-edit'
$hookConfig.hooks.PreToolUse[1].hooks[0].command = New-HookCommand 'pre-shell'
$hookConfig.hooks.PostToolUse[0].hooks[0].command = New-HookCommand 'post-edit'
$hookConfig.hooks.Stop[0].hooks[0].command = New-HookCommand 'stop'
$hookJson = $hookConfig | ConvertTo-Json -Depth 12
$hooksPath = Join-Path $hooksOutput 'hooks.json'
Write-Utf8NoBom -Path $hooksPath -Content $hookJson

$searchCommand = ('powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonCommand "{1}" -PolicyHome "{2}" search' -f $runtimePolicyScript, $PythonCommand, $runtimeOutput)
$runtimeReference = Join-Path $skillsOutput 'java-policy\references\runtime.md'
$runtimeReferenceText = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeReference
$runtimeReferenceText = $runtimeReferenceText.Replace('__POLICYKIT_SEARCH_COMMAND__', $searchCommand)
Write-Utf8NoBom -Path $runtimeReference -Content $runtimeReferenceText

$checklistTemplate = Get-Content -Raw -Encoding UTF8 -LiteralPath $checklistTemplateSource
$checklist = $checklistTemplate.Replace('__OUTPUT_ROOT__', $outputRoot)
$checklist = $checklist.Replace('__POLICY_SCRIPT__', $runtimePolicyScript)
$checklist = $checklist.Replace('__RUNTIME_ROOT__', $runtimeOutput)
$checklist = $checklist.Replace('__PYTHON_COMMAND__', $PythonCommand)
Write-Utf8NoBom -Path (Join-Path $outputRoot 'COPY_CHECKLIST.md') -Content $checklist

$markerData = [ordered]@{
    owner = 'java-policy-kit-manual-package'
    schema_version = 1
    generated_at = (Get-Date).ToString('o')
    output_directory = $outputRoot
    policy_version = [string]$approvedData.policy_version
    approved_rule_count = $approvedRules.Count
    runtime_directory = $runtimeOutput
    python_command = $PythonCommand
    approved_rules_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $runtimeWorkOutput 'approved-rules.json')).Hash.ToLowerInvariant()
    search_index_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $runtimeWorkOutput 'search-index.db')).Hash.ToLowerInvariant()
    global_block_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $runtimeWorkOutput 'GLOBAL_MD_BLOCK.md')).Hash.ToLowerInvariant()
}
$markerPath = Join-Path $outputRoot '.policykit-manual-package.json'
Write-Utf8NoBom -Path $markerPath -Content ($markerData | ConvertTo-Json)

$generatedHookText = Get-Content -Raw -Encoding UTF8 -LiteralPath $hooksPath
$generatedRuntimeText = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeReference
$generatedChecklistText = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $outputRoot 'COPY_CHECKLIST.md')
if ($generatedHookText -match '__POLICYKIT_[A-Z_]+__') {
    throw "Generated hooks.json still contains a command placeholder: $hooksPath"
}
if ($generatedRuntimeText.Contains('__POLICYKIT_SEARCH_COMMAND__')) {
    throw "Generated runtime.md still contains a command placeholder: $runtimeReference"
}
if ($generatedChecklistText -match '__[A-Z0-9_]+__') {
    throw "Generated COPY_CHECKLIST.md still contains a placeholder: $(Join-Path $outputRoot 'COPY_CHECKLIST.md')"
}
try {
    $null = $generatedHookText | ConvertFrom-Json
} catch {
    throw "Generated hooks.json is invalid: $($_.Exception.Message)"
}
$markerBytes = [System.IO.File]::ReadAllBytes($markerPath)
if ($markerBytes.Length -ge 3 -and $markerBytes[0] -eq 0xEF -and $markerBytes[1] -eq 0xBB -and $markerBytes[2] -eq 0xBF) {
    throw "Manual package marker unexpectedly contains a UTF-8 BOM: $markerPath"
}

Write-Host "[OK] Manual package: $outputRoot" -ForegroundColor Green
Write-Host "[OK] Activated rules: $($approvedRules.Count)" -ForegroundColor Green
Write-Host '[UNCHANGED] No Codagent directory was read or modified.' -ForegroundColor Cyan
Write-Host "Next: follow $(Join-Path $outputRoot 'COPY_CHECKLIST.md')" -ForegroundColor Cyan
