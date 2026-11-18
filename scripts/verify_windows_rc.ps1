[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ArtifactRoot,
    [ValidateSet("A", "B")]
    [string]$BuildId = "B",
    [string]$CorpusManifest = "",
    [switch]$RequireSigned
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ArtifactRoot = (Resolve-Path $ArtifactRoot).Path
$ExePath = Join-Path $ArtifactRoot "PixTagra.exe"
if (-not (Test-Path -LiteralPath $ExePath)) { throw "PixTagra.exe is missing from the requested artifact root." }

$VerificationRoot = Join-Path $ProjectRoot (Join-Path ".build-rc" ("verify-" + $BuildId))
if (Test-Path -LiteralPath $VerificationRoot) { Remove-Item -LiteralPath $VerificationRoot -Recurse -Force }
New-Item -ItemType Directory -Path $VerificationRoot -Force | Out-Null
$isolatedProfile = Join-Path $VerificationRoot "home"
$env:USERPROFILE = $isolatedProfile
$env:PSS_CONFIG_DIR = Join-Path $VerificationRoot "config"
$env:PSS_CONFIG_FILE = Join-Path $env:PSS_CONFIG_DIR "config.json"
$env:PSS_NO_STARTUP_DIALOG = "1"
$env:PIXTAGRA_PACKAGED_TEST_ISOLATION = "1"
New-Item -ItemType Directory -Path $isolatedProfile, $env:PSS_CONFIG_DIR -Force | Out-Null
$env:PIXTAGRA_PROBE_MANIFEST = if ($CorpusManifest) { (Resolve-Path $CorpusManifest).Path } else { "" }

function Invoke-PackagedMode([string]$Argument, [string]$OutputName) {
    $outputPath = Join-Path $VerificationRoot $OutputName
    $env:PIXTAGRA_PACKAGED_PROBE_OUTPUT = $outputPath
    $process = Start-Process -FilePath $ExePath -ArgumentList $Argument -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Packaged mode exited unsuccessfully." }
    if (-not (Test-Path -LiteralPath $outputPath)) { throw "Packaged mode did not create its redacted result." }
    return Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json
}

$selfTest = Invoke-PackagedMode "--self-test-packaged-credentials" "credential-self-test.json"
$guiSmoke = Invoke-PackagedMode "--packaged-gui-smoke" "gui-smoke.json"
$workspaceSmoke = Invoke-PackagedMode "--packaged-workspace-smoke" "workspace-smoke.json"
if ($workspaceSmoke.passed -ne $true) { throw "Packaged workspace regression checks failed." }
if ($workspaceSmoke.performance.passed -ne $true) {
    throw ("Packaged scroll performance did not meet the project budget: " + ($workspaceSmoke.performance.failures -join ", "))
}

foreach ($name in "real_credential_manager_access", "real_vault_access", "real_config_access", "real_clipboard_access", "network_requests", "hello_prompt_requested") {
    if ([int]$selfTest.$name -ne 0) { throw "Packaged credential self-test reported a real access." }
}
if ($selfTest.packaged -ne $true -or $selfTest.session_only_allowed -ne $true) { throw "Packaged credential self-test contract failed." }
foreach ($name in "real_credential_manager_access", "real_vault_access", "real_config_access", "real_clipboard_access", "network_requests", "hello_prompt_requested") {
    if ([int]$guiSmoke.$name -ne 0) { throw "Packaged GUI smoke reported a real access." }
}
$guiContractFailed = ($guiSmoke.status -ne "ok") -or
    ($guiSmoke.title -ne "PixTagra") -or
    ($guiSmoke.selectable_models_verified -ne $true) -or
    ($guiSmoke.context_menu_bound -ne $true) -or
    ($guiSmoke.date_picker_anchored -ne $true)
if ($guiContractFailed) { throw "Packaged GUI smoke contract failed." }

$signature = Get-AuthenticodeSignature -LiteralPath $ExePath
if ($signature.Status.ToString() -notin @("NotSigned", "Valid")) { throw "The executable signature is invalid." }
if ($RequireSigned -and $signature.Status.ToString() -ne "Valid") { throw "A trusted executable signature is required." }

$payloadManifest = Join-Path $VerificationRoot "payload-files.json"
& python (Join-Path $PSScriptRoot "release_payload.py") snapshot --artifact-root $ArtifactRoot --manifest $payloadManifest
if ($LASTEXITCODE -ne 0) { throw "Could not record the verified payload." }
$result = [ordered]@{
    executable = $ExePath
    executable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash.ToLowerInvariant()
    source_commit = $workspaceSmoke.source_commit
    payload_manifest = $payloadManifest
    packaged_self_test = $selfTest
    packaged_gui_smoke = $guiSmoke
    packaged_workspace_smoke = $workspaceSmoke
    signature_status = $signature.Status.ToString()
    process_exited = $true
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $VerificationRoot "verification.json") -Encoding utf8
$result | ConvertTo-Json -Compress -Depth 8
