[CmdletBinding()]
param(
    [ValidateSet("A", "B")]
    [string]$BuildId = "A",
    [switch]$SkipSourceVerification
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildRoot = Join-Path $ProjectRoot ".build-rc"
$IsolationRoot = Join-Path $BuildRoot ("isolation-" + $BuildId)
$WorkPath = Join-Path $ProjectRoot (Join-Path "build" ("rc-" + $BuildId.ToLowerInvariant()))
$DistPath = if ($BuildId -eq "A") { Join-Path $ProjectRoot "dist\build-a" } else { Join-Path $ProjectRoot "dist" }
$ArtifactRoot = Join-Path $DistPath "PixTagra"

function Assert-ProjectPath([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the project root."
    }
    return $fullPath
}

function Reset-GeneratedDirectory([string]$Path) {
    $fullPath = Assert-ProjectPath $Path
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullPath -Force | Out-Null
}

$statusLines = @(git -C $ProjectRoot status --short)
$unexpectedChanges = @($statusLines | Where-Object {
    $_ -notmatch '^\?\? (118450a7-bf04-410e-adea-57fa39932a98|bac4c35f-e7c2-4f13-8216-69acb1209d66)\.png$'
})
if ($unexpectedChanges.Count -gt 0) {
    throw "The controlled build requires a clean tracked source tree."
}
git -C $ProjectRoot diff --check
if ($LASTEXITCODE -ne 0) { throw "git diff --check failed." }

Push-Location $ProjectRoot
try {
    & python "scripts/verify_build_environment.py" --requirements "requirements-build.txt" --python-version "3.12.10"
    if ($LASTEXITCODE -ne 0) { throw "Pinned build environment verification failed." }

    New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
    if (-not $SkipSourceVerification) {
        & python -m py_compile "pixtagra.py" "pixtagra_credentials.py" "pixtagra_credential_migration.py" "pixtagra_windows_hello.py" "pixtagra_credential_policy.py" "pixtagra_live_credentials.py" "pixtagra_packaged_selftest.py" "pixtagra_release.py"
        if ($LASTEXITCODE -ne 0) { throw "Source compilation failed." }
        $testOutput = Join-Path $BuildRoot ("source-tests-" + $BuildId + "-" + (git rev-parse --short HEAD).Trim())
        & python -m pytest -q -p no:cacheprovider --basetemp $testOutput
        if ($LASTEXITCODE -ne 0) { throw "Source regression test suite failed." }
    }

    Reset-GeneratedDirectory $IsolationRoot
    Reset-GeneratedDirectory $WorkPath
    if ($BuildId -eq "A") {
        Reset-GeneratedDirectory $DistPath
    }
    elseif (-not (Test-Path -LiteralPath $DistPath)) {
        New-Item -ItemType Directory -Path (Assert-ProjectPath $DistPath) -Force | Out-Null
    }
    Reset-GeneratedDirectory $ArtifactRoot

    $isolatedProfile = Join-Path $IsolationRoot "home"
    $env:USERPROFILE = $isolatedProfile
    $env:PSS_CONFIG_DIR = Join-Path $IsolationRoot "config"
    $env:PSS_CONFIG_FILE = Join-Path $env:PSS_CONFIG_DIR "config.json"
    $env:PSS_NO_STARTUP_DIALOG = "1"
    $env:PIXTAGRA_PACKAGED_TEST_ISOLATION = "1"
    New-Item -ItemType Directory -Path $isolatedProfile, $env:PSS_CONFIG_DIR -Force | Out-Null

    $sourceCommit = (git rev-parse HEAD).Trim()
    $baselineTag = (git describe --tags --abbrev=0).Trim()
    $generatedDir = Join-Path $IsolationRoot "generated"
    New-Item -ItemType Directory -Path $generatedDir -Force | Out-Null
    $versionFile = Join-Path $generatedDir "PixTagra.version"
    $buildInfoFile = Join-Path $generatedDir "pixtagra_build_info.py"
    & python "scripts/generate_version_resource.py" --output $versionFile
    if ($LASTEXITCODE -ne 0) { throw "Version resource generation failed." }
    & python "scripts/generate_build_info.py" --output $buildInfoFile --source-commit $sourceCommit
    if ($LASTEXITCODE -ne 0) { throw "Build identity generation failed." }

    $env:PIXTAGRA_GENERATED_DIR = $generatedDir
    $env:PIXTAGRA_VERSION_FILE = $versionFile
    & python -m PyInstaller --noconfirm --clean --workpath $WorkPath --distpath $DistPath "PixTagra.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $exePath = Join-Path $ArtifactRoot "PixTagra.exe"
    if (-not (Test-Path -LiteralPath $exePath)) { throw "Expected one-folder executable was not created." }
    $archiveList = Join-Path $IsolationRoot "archive-list.txt"
    & pyi-archive_viewer --list $exePath | Out-File -LiteralPath $archiveList -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller archive listing failed." }

    $artifactRelative = if ($BuildId -eq "A") { "dist/build-a/PixTagra" } else { "dist/PixTagra" }
    $result = [ordered]@{
        build_id = $BuildId
        source_commit = $sourceCommit
        source_baseline_tag = $baselineTag
        artifact_root = $artifactRelative
        archive_list = (Join-Path ".build-rc" (Join-Path ("isolation-" + $BuildId) "archive-list.txt"))
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $IsolationRoot "build-result.json") -Encoding utf8
    $result | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
