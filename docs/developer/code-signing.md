# Code signing: where it sits and why

PixTagra's release pipeline is hash-locked: each stage records the bytes it saw,
and the next stage refuses to run if they changed. Signing rewrites a file, so
its position in the sequence is not a preference — it is forced by that lock.

## The rule

> Sign a file **after** it is produced and **before** any stage records its hash.

Applying that rule to this repository gives the only workable order.

## Local pipeline (unsigned, for development)

```
scripts/build_windows_rc.ps1        → dist/build-a/PixTagra/PixTagra.exe
scripts/verify_windows_rc.ps1       → .build-rc/verify-A/verification.json
                                      (records executable_sha256)
scripts/package_windows_rc.ps1      → dist/*-setup-x64.exe, *.zip
                                      → dist/PixTagra-release-identity.json
                                      (records exe, zip, installer hashes)
```

Nothing here signs anything. `signature_status` and `signature` are reported as
`NotSigned`, which is the honest state of a local build.

## Release pipeline (signed, SignPath Foundation)

```
1. build_windows_rc.ps1             → unsigned PixTagra.exe
2. upload unsigned exe as a GitHub Actions artifact
3. SignPath  ← signs PixTagra.exe, returns the signed exe
4. verify_windows_rc.ps1            → records the SIGNED exe hash         ← moved
5. package_windows_rc.ps1           → unsigned installer + zip
6. SignPath  ← signs the setup exe
7. finalize_release_identity.ps1    → rewrites the installer hash         ← added
8. upload release assets
```

Steps 4 and 7 are the two consequences worth understanding.

**Step 4 moves.** Verification must run on the signed binary, because
`package_windows_rc.ps1` compares the hash in `verification.json` against the
exe on disk and aborts when they differ. Verify the unsigned exe and that check
fails after any signing.

**Step 7 exists.** `package_windows_rc.ps1` hashes the installer immediately
after NSIS compiles it. Signing afterwards invalidates that recorded hash, so a
small finalization step recomputes the installer entry and re-verifies both
signatures. The alternative — signing inside the package script — would force an
asynchronous approval round trip into a synchronous script.

**The ZIP needs no separate step.** It is compressed from
`dist/build-a/PixTagra/`, which already contains the signed exe. Compress after
signing, never before.

**`Uninstall.exe` cannot be signed.** NSIS generates it on the target machine at
install time; no Authenticode signature can exist for a file that does not exist
until after installation. This is normal for NSIS installers and is why the
Foundation artifact configuration only covers the setup executable.

## SignPath Foundation specifics

- Application: <https://signpath.io/open-source>. A public repository with at
  least one published release is the usual prerequisite.
- The certificate subject is **SignPath Foundation**, not the project author.
  Foundation also applies a mandatory signature watermark and rejects custom
  signature descriptions.
- Only GitHub-hosted runners are accepted. Origin verification fails for
  artifacts uploaded from a developer machine.
- **Every release needs one manual approval** in the SignPath portal by an
  Approver. There is no unattended release path on the free tier.
- Signing does not remove SmartScreen warnings immediately. Reputation accrues
  per publisher certificate over downloads and time.

### Setup checklist

1. Publish the repository publicly with at least one release.
2. Apply to the Foundation programme and wait for acceptance.
3. Install the SignPath GitHub App on the repository.
4. Create a project, then two artifact configurations:

   | Slug | Signed file | Artifact contents |
   | --- | --- | --- |
   | `application` | `PixTagra.exe` | ZIP containing `PixTagra/PixTagra.exe` |
   | `installer` | `*-setup-x64.exe` | ZIP containing the setup executable |

5. Create policies `release-signing` (production) and `test-signing`.
6. Create a CI user, generate an API token, and store these as repository
   secrets: `SIGNPATH_API_TOKEN`. Store `SIGNPATH_ORGANIZATION_ID` as a
   repository variable.

## Verifying a signed release

```powershell
Get-AuthenticodeSignature .\PixTagra.exe | Select-Object Status, StatusMessage
# Expected: Status = Valid, subject = SignPath Foundation
```

The certificate must be timestamped; without a timestamp the signature expires
with the certificate and older releases stop validating.

## What is deliberately not done

- No self-signed certificate is shipped or trusted. A self-signed binary is
  indistinguishable from an unsigned one to every user outside the machine that
  trusts it, while looking "signed" to whoever built it.
- No signature is described as present when `Get-AuthenticodeSignature` reports
  `NotSigned`. The release identity file states the status it actually read.
