[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArtifactRoot,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [Parameter(Mandatory = $true)][string]$VerificationReport,
    [string]$OutputDirectory = "dist",
    [string]$NsisCompiler = "C:\Program Files (x86)\NSIS\makensis.exe",
    [switch]$RequireSigned
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifact = (Resolve-Path $ArtifactRoot).Path
$exe = Join-Path $artifact "PixTagra.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Missing packaged executable: $exe" }
$verification = Get-Content -LiteralPath $VerificationReport -Raw | ConvertFrom-Json
$verifiedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
if ($verification.source_commit -ne $SourceCommit -or $verification.executable_sha256 -ne $verifiedHash) {
    throw "Verification report does not identify this exact executable and source commit."
}
if ($verification.packaged_workspace_smoke.passed -ne $true -or
    $verification.packaged_workspace_smoke.performance.passed -ne $true) {
    throw "Packaging is blocked: workspace behavior and scroll performance must both pass."
}
& python (Join-Path $PSScriptRoot "release_payload.py") verify --artifact-root $artifact --manifest $verification.payload_manifest
if ($LASTEXITCODE -ne 0) { throw "Packaging is blocked: the verified payload has changed." }
$signature = (Get-AuthenticodeSignature -LiteralPath $exe).Status.ToString()
if ($signature -notin @("NotSigned", "Valid")) { throw "Invalid executable signature." }
if ($RequireSigned -and $signature -ne "Valid") { throw "Sign the executable before runtime verification; a trusted signature is required." }
if (-not (Test-Path -LiteralPath $NsisCompiler)) { throw "NSIS compiler is required to create the installer." }
$out = if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $root $OutputDirectory }
New-Item -ItemType Directory -Path $out -Force | Out-Null
$version = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw "Executable has no valid release version." }
$prefix = "PixTagra-" + $version + "-" + $SourceCommit.Substring(0, 12)
$zip = Join-Path $out ($prefix + "-windows-x64.zip")
$installer = Join-Path $out ($prefix + "-setup-x64.exe")
$generated = Join-Path $root (".build-rc\installer-" + $SourceCommit.Substring(0, 12))
New-Item -ItemType Directory -Path $generated -Force | Out-Null
$uninstallFiles = Join-Path $generated "uninstall-files.nsh"
& python (Join-Path $PSScriptRoot "release_payload.py") uninstall --artifact-root $artifact --manifest $verification.payload_manifest --output $uninstallFiles
if ($LASTEXITCODE -ne 0) { throw "Uninstall file manifest generation failed." }
$licenseFile = Join-Path $generated "license.txt"
$legal = Join-Path $artifact "_internal\legal"
# NSIS displays UTF-16 text without depending on the system ANSI code page.
$licenseText = @("EULA", "PRIVACY", "REFUND") | ForEach-Object { Get-Content -LiteralPath (Join-Path $legal ($_ + ".md")) -Raw -Encoding utf8 }
[IO.File]::WriteAllText($licenseFile, ($licenseText -join "`r`n`r`n"), [Text.Encoding]::Unicode)
& $NsisCompiler /V2 "/DPayload=$artifact" "/DReleaseVersion=$version" "/DSourceCommit=$SourceCommit" "/DInstallerOutput=$installer" "/DLicenseFile=$licenseFile" "/DUninstallFiles=$uninstallFiles" (Join-Path $PSScriptRoot "pixtagra_installer.nsi")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installer)) { throw "NSIS installer compilation failed." }
$installerSignature = (Get-AuthenticodeSignature -LiteralPath $installer).Status.ToString()
if ($RequireSigned -and $installerSignature -ne "Valid") { throw "Installer created, but trusted signing is still required before release." }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $artifact "*") -DestinationPath $zip -CompressionLevel Optimal
$result = [ordered]@{
    source_commit=$SourceCommit; version=$version; executable=$exe; executable_sha256=$hash
    archive=$zip; archive_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    installer=$installer; installer_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    verification_report=(Resolve-Path $VerificationReport).Path
    signature=$signature; installer_signature=$installerSignature
    timestamp=(Get-Date).ToUniversalTime().ToString("o")
}
$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $out "PixTagra-release-identity.json") -Encoding utf8
$result | ConvertTo-Json -Compress
