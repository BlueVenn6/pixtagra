[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory = "dist",
    [switch]$RequireSigned
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$out = if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $root $OutputDirectory }
$identityPath = Join-Path $out "PixTagra-release-identity.json"
if (-not (Test-Path -LiteralPath $identityPath)) {
    throw "Missing release identity: $identityPath. Run package_windows_rc.ps1 first."
}
$identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json

# Signing rewrites the installer, so the hash recorded at packaging time no
# longer describes the shipped file. Recompute it from the file on disk and
# refuse to continue when the installer is still unsigned, because publishing a
# stale hash would misrepresent the artifact.
foreach ($field in @("executable", "archive", "installer")) {
    if (-not (Test-Path -LiteralPath $identity.$field)) {
        throw "Release artifact is missing: $($identity.$field)"
    }
}

$exeSignature = (Get-AuthenticodeSignature -LiteralPath $identity.executable).Status.ToString()
$installerSignature = (Get-AuthenticodeSignature -LiteralPath $identity.installer).Status.ToString()
if ($exeSignature -ne "Valid") { throw "The executable is not signed with a trusted certificate: $exeSignature" }
if ($installerSignature -ne "Valid") { throw "The installer is not signed with a trusted certificate: $installerSignature" }

$updated = [ordered]@{
    source_commit = $identity.source_commit
    version = $identity.version
    executable = $identity.executable
    executable_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $identity.executable).Hash.ToLowerInvariant()
    archive = $identity.archive
    archive_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $identity.archive).Hash.ToLowerInvariant()
    installer = $identity.installer
    installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $identity.installer).Hash.ToLowerInvariant()
    verification_report = $identity.verification_report
    signature = $exeSignature
    installer_signature = $installerSignature
    timestamp = (Get-Date).ToUniversalTime().ToString("o")
}

# A hash that changed means the bytes changed after verification. The zip is
# compressed from the artifact root, so a signed executable must be re-zipped
# before this point; catching it here is cheaper than shipping the mismatch.
if ($updated.executable_sha256 -ne $identity.executable_sha256) {
    throw ("Executable changed after packaging: recorded {0}, found {1}. Rebuild the archive from the signed executable." -f $identity.executable_sha256, $updated.executable_sha256)
}

$updated | ConvertTo-Json | Set-Content -LiteralPath $identityPath -Encoding utf8
$updated | ConvertTo-Json -Compress
