"""Exercise the real packaging entry point without building or executing a binary."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("failure", ["behavior", "latency", "hash", "commit", "dependency"])
def test_packaging_rejects_failed_or_stale_verification(tmp_path, failure):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required to exercise the Windows release entry point")
    artifact = tmp_path / "input"
    artifact.mkdir()
    exe = artifact / "PixTagra.exe"
    exe.write_bytes(b"non-executable fixture: no program will be launched")
    source_commit = "a" * 40
    report = {
        "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "source_commit": source_commit,
        "packaged_workspace_smoke": {"passed": True, "performance": {"passed": True}},
    }
    manifest = tmp_path / "payload.json"
    manifest.write_text(json.dumps([{"path": "PixTagra.exe", "size": exe.stat().st_size,
                                     "sha256": report["executable_sha256"].upper()}]), encoding="utf-8")
    report["payload_manifest"] = str(manifest)
    if failure == "behavior":
        report["packaged_workspace_smoke"]["passed"] = False
    elif failure == "latency":
        report["packaged_workspace_smoke"]["performance"]["passed"] = False
    elif failure == "hash":
        report["executable_sha256"] = "b" * 64
    elif failure == "commit":
        report["source_commit"] = "c" * 40
    else:
        (artifact / "unexpected-runtime.dll").write_bytes(b"stale runtime")
    report_file = tmp_path / "verification.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    output = tmp_path / "must-not-be-created"
    script = Path(__file__).resolve().parents[2] / "scripts/package_windows_rc.ps1"
    result = subprocess.run([
        shell, "-NoProfile", "-File", str(script), "-ArtifactRoot", str(artifact),
        "-SourceCommit", source_commit, "-VerificationReport", str(report_file),
        "-OutputDirectory", str(output),
    ], capture_output=True, timeout=30)
    assert result.returncode != 0
    expected = "Packaging is blocked" if failure in {"behavior", "latency", "dependency"} else "does not identify"
    # PowerShell 7 emits UTF-8 even when Python's Windows locale is GBK.
    # Compare the script's ASCII diagnostic directly, without locale decoding.
    assert expected.encode("ascii") in result.stderr
    assert not output.exists()
