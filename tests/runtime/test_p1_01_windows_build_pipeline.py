"""Offline contract tests for the controlled Windows RC build pipeline."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pixtagra_packaged_selftest import run_packaged_credential_self_test
from pixtagra_release import PRODUCT_NAME, PRODUCT_VERSION


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_metadata_is_single_safe_source():
    assert PRODUCT_NAME == "PixTagra"
    assert PRODUCT_VERSION == "0.9.1.0"


def test_packaged_self_test_uses_only_memory_fakes_and_redacts_secret():
    result = run_packaged_credential_self_test()
    encoded = json.dumps(result, sort_keys=True)

    assert result["packaged"] is True
    assert result["credential_modules_imported"] is True
    assert result["session_only_allowed"] is True
    assert result["automatic_persistent_writes"] == 0
    assert result["legacy_fallback_count"] == 0
    assert result["active_leases_after_shutdown"] == 0
    assert result["hello_prompt_requested"] == 0
    assert result["real_credential_manager_access"] == 0
    assert result["real_vault_access"] == 0
    assert result["real_config_access"] == 0
    assert result["real_clipboard_access"] == 0
    assert result["network_requests"] == 0
    assert "SYNTHETIC" not in encoded


def test_packaged_self_test_imports_every_credential_layer():
    result = run_packaged_credential_self_test()

    assert result["credential_modules_imported"] is True
    assert result["credential_manager_ctypes_definition"] is True
    assert result["migration_constructed"] is True
    assert result["live_bridge_constructed"] is True


def test_packaged_self_test_preserves_hello_delayed_load_boundary():
    result = run_packaged_credential_self_test()

    assert result["hello_delayed_load_boundary"] is True
    assert result["hello_prompt_requested"] == 0


def test_packaged_self_test_uses_session_only_and_closes_lease():
    result = run_packaged_credential_self_test()

    assert result["session_only_allowed"] is True
    assert result["active_leases_after_shutdown"] == 0


def test_packaged_self_test_never_uses_persistent_or_legacy_paths():
    result = run_packaged_credential_self_test()

    assert result["automatic_persistent_writes"] == 0
    assert result["legacy_fallback_count"] == 0


def test_packaged_self_test_reports_all_real_accesses_as_zero():
    result = run_packaged_credential_self_test()

    for key in (
        "real_credential_manager_access",
        "real_vault_access",
        "real_config_access",
        "real_clipboard_access",
        "network_requests",
    ):
        assert result[key] == 0


def test_version_resource_is_generated_from_single_source(tmp_path):
    generator = _load_script("generate_version_resource.py")
    output = tmp_path / "PixTagra.version"

    generator.write_version_resource(output)

    text = output.read_text(encoding="utf-8")
    assert "ProductName', 'PixTagra'" in text
    assert f"FileVersion', '{PRODUCT_VERSION}'" in text
    assert f"ProductVersion', '{PRODUCT_VERSION}'" in text
    assert "CompanyName" not in text


def test_version_resource_keeps_file_and_product_versions_identical(tmp_path):
    generator = _load_script("generate_version_resource.py")
    output = tmp_path / "PixTagra.version"

    generator.write_version_resource(output)

    text = output.read_text(encoding="utf-8")
    assert text.count(PRODUCT_VERSION) == 2
    version_tuple = tuple(int(part) for part in PRODUCT_VERSION.split("."))
    assert f"filevers={version_tuple}" in text
    assert f"prodvers={version_tuple}" in text
    assert "OriginalFilename', 'PixTagra.exe'" in text


def test_spec_and_build_scripts_avoid_developer_absolute_paths_and_exclude_sources():
    spec_text = (ROOT / "PixTagra.spec").read_text(encoding="utf-8")
    build_text = (ROOT / "scripts" / "build_windows_rc.ps1").read_text(encoding="utf-8")
    verify_text = (ROOT / "scripts" / "verify_windows_rc.ps1").read_text(encoding="utf-8")

    for text in (spec_text, build_text, verify_text):
        # Any developer home directory would leak a machine-specific path into
        # the shipped build, so the guard matches the pattern, not one name.
        assert not re.search(r"[A-Za-z]:\\\\Users\\\\[^\\\\\"']+\\\\", text)
        assert "CredReadW" not in text
        assert "RequestVerificationForWindowAsync" not in text
    assert "collect_all(\"customtkinter\")" in spec_text
    assert "pixtagra_live_credentials" in spec_text
    assert "tests" not in spec_text.lower()
    assert "docs/audit" not in spec_text.lower()
    assert "USERPROFILE" in build_text
    assert "PSS_CONFIG_DIR" in build_text
    assert "--self-test-packaged-credentials" in verify_text
    assert "--packaged-gui-smoke" in verify_text


def test_spec_declares_all_credential_runtime_modules():
    spec_text = (ROOT / "PixTagra.spec").read_text(encoding="utf-8")

    for module in (
        "pixtagra_credentials",
        "pixtagra_credential_migration",
        "pixtagra_credential_policy",
        "pixtagra_live_credentials",
        "pixtagra_windows_hello",
        "pixtagra_packaged_selftest",
    ):
        assert module in spec_text


def test_spec_is_one_folder_windowed_and_excludes_test_frameworks():
    spec_text = (ROOT / "PixTagra.spec").read_text(encoding="utf-8")

    assert "COLLECT(" in spec_text
    assert "console=False" in spec_text
    assert 'excludes=["pytest", "unittest", "pydoc"]' in spec_text
    assert "Path(SPEC).resolve().parent" in spec_text
    assert "Path(__file__)" not in spec_text


def test_build_script_uses_clean_output_and_offline_environment_verification():
    build_text = (ROOT / "scripts" / "build_windows_rc.ps1").read_text(encoding="utf-8")

    assert "--noconfirm --clean" in build_text
    assert "verify_build_environment.py" in build_text
    assert "git -C $ProjectRoot diff --check" in build_text
    assert "PIXTAGRA_PACKAGED_TEST_ISOLATION" in build_text
    assert "--specpath" not in build_text


def test_build_script_preserves_build_a_while_building_b():
    build_text = (ROOT / "scripts" / "build_windows_rc.ps1").read_text(encoding="utf-8")

    assert 'if ($BuildId -eq "A") {' in build_text
    assert "dist/build-a/PixTagra" in build_text
    assert "dist/PixTagra" in build_text


def test_verify_script_only_runs_explicit_packaged_modes_with_isolation():
    verify_text = (ROOT / "scripts" / "verify_windows_rc.ps1").read_text(encoding="utf-8")

    assert "PIXTAGRA_PACKAGED_TEST_ISOLATION" in verify_text
    assert "Get-AuthenticodeSignature" in verify_text
    assert "NotSigned" in verify_text
    assert "selectable_models_verified" in verify_text
    assert "context_menu_bound" in verify_text
    assert "date_picker_anchored" in verify_text
    assert '$guiSmoke.status -ne "ok"' in verify_text
    assert "Packaged GUI smoke reported a real access." in verify_text
    assert "--request-verification" not in verify_text
    assert "--acknowledge-visible-prompt" not in verify_text


def test_windows_rc_powershell_scripts_parse_without_errors():
    for path in sorted((ROOT / "scripts").glob("*.ps1")):
        escaped_path = str(path).replace("'", "''")
        command = (
            "$tokens = $null; $errors = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}', "
            "[ref]$tokens, [ref]$errors); "
            "if ($errors.Count -ne 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, f"{path.name} failed to parse"


def test_main_module_keeps_packaged_modes_explicit():
    source = (ROOT / "pixtagra.py").read_text(encoding="utf-8")

    assert '"--self-test-packaged-credentials" in sys.argv' in source
    assert '"--packaged-gui-smoke" in sys.argv' in source
    assert "PIXTAGRA_PACKAGED_TEST_ISOLATION" in source
    assert "app.quit()" in source
    assert "exit_guard = threading.Timer" in source
    assert "threading.Timer(30.0" in source
    assert 'native_key_entry.bind("<Button-3>")' in source
    assert '"selectable_models_verified"' in source
    assert '"visible_model_count"' in source
    assert '"date_picker_anchored"' in source
    assert 'os._exit(1)' in source
    assert '"gui_contract_failed"' in source
    assert '"layout_checks"' in source
    assert 'GUI layout did not stabilize' in source


def test_locked_build_requirements_pin_runtime_and_packaging_dependencies():
    lines = (ROOT / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
    pins = {line.split("==", 1)[0].lower(): line for line in lines if "==" in line}

    for package in ("customtkinter", "pillow", "piexif", "requests", "cryptography", "pyinstaller"):
        assert package in pins
    assert all(">=" not in line for line in pins.values())


def test_build_environment_checker_rejects_the_wrong_python_version():
    checker = _load_script("verify_build_environment.py")

    result = checker.verify_environment(ROOT / "requirements-build.txt", "0.0.0")

    assert result["ok"] is False
    assert "python_version" in result["problems"]


def test_build_requirements_include_pyinstaller_transitive_build_tools():
    lines = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")

    for package in ("pyinstaller-hooks-contrib==", "altgraph==", "pefile==", "pywin32-ctypes==", "setuptools=="):
        assert package in lines


def test_manifest_hashes_payload_and_redacts_absolute_paths(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "PixTagra.exe").write_bytes(b"synthetic executable")
    (artifact / "_internal").mkdir()
    (artifact / "_internal" / "runtime.dat").write_bytes(b"runtime")

    manifest = manifest_tool.build_release_manifest(
        artifact_root=artifact,
        source_commit="a" * 40,
        baseline_tag="state-b-p0-01-3c2",
        build_command="scripts/build_windows_rc.ps1 -BuildId B",
        test_total=423,
        packaged_self_test={"packaged": True},
        packaged_gui_smoke={"title": "PixTagra"},
        scan_result={"secret_sentinel_hits": [], "absolute_path_hits": []},
        comparison={"bit_identical": False, "different_files": ["PixTagra.exe"]},
        signature_status="unsigned",
        build_dependencies=[{"name": "PyInstaller", "version": "6.20.0"}],
    )
    encoded = json.dumps(manifest, sort_keys=True)

    assert manifest["product"] == "PixTagra"
    assert manifest["signature_status"] == "unsigned"
    assert manifest["files"][0]["path"] == "PixTagra.exe"
    assert len(manifest["files"][0]["sha256"]) == 64
    assert str(tmp_path) not in encoded
    assert "C:\\Users" not in encoded


def test_manifest_includes_only_relative_distribution_paths(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "PixTagra.exe").write_bytes(b"exe")
    (artifact / "_internal").mkdir()
    (artifact / "_internal" / "data.bin").write_bytes(b"data")

    records = manifest_tool.collect_distribution_files(artifact)

    assert [record["path"] for record in records] == ["PixTagra.exe", "_internal/data.bin"]


def test_manifest_writer_does_not_put_host_paths_in_readable_output(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "PixTagra.exe").write_bytes(b"exe")
    manifest = manifest_tool.build_release_manifest(
        artifact_root=artifact,
        source_commit="b" * 40,
        baseline_tag="state-b-p0-01-3c2",
        build_command="scripts/build_windows_rc.ps1 -BuildId B",
        test_total=423,
        packaged_self_test={},
        packaged_gui_smoke={},
        scan_result={},
        comparison={},
        signature_status="unsigned",
        build_dependencies=[],
    )
    json_path = tmp_path / "release-manifest.json"
    text_path = tmp_path / "release-manifest.txt"

    manifest_tool.write_manifest(manifest, json_path, text_path)

    assert str(tmp_path) not in text_path.read_text(encoding="utf-8")
    assert "Signature: unsigned" in text_path.read_text(encoding="utf-8")


def test_release_tree_scan_flags_files_but_not_legitimate_identifier_text(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "api_key_notes.txt").write_text("api_key is an identifier", encoding="utf-8")
    (artifact / "keys.vault").write_bytes(b"not real")

    scan = manifest_tool.scan_release_tree(artifact, project_root=ROOT)

    assert scan["forbidden_file_paths"] == ["keys.vault"]
    assert scan["identifier_hits"] == ["api_key_notes.txt"]
    assert scan["secret_sentinel_hits"] == []


def test_release_tree_scan_flags_secret_sentinel_without_exposing_content(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "suspect.bin").write_bytes(b"prefix TEST_KEY_DO_NOT_USE suffix")

    scan = manifest_tool.scan_release_tree(artifact, project_root=ROOT)

    assert scan["secret_sentinel_hits"] == ["suspect.bin"]
    assert "TEST_KEY_DO_NOT_USE" not in json.dumps(scan)


def test_release_tree_scan_flags_embedded_project_path_without_reporting_value(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "suspect.bin").write_bytes(str(ROOT).encode("utf-8"))

    scan = manifest_tool.scan_release_tree(artifact, project_root=ROOT)

    assert scan["absolute_path_hits"] == ["suspect.bin"]
    assert str(ROOT) not in json.dumps(scan)


def test_release_tree_scan_classifies_generic_unix_user_literals_without_calling_them_host_paths(tmp_path):
    manifest_tool = _load_script("generate_release_manifest.py")
    artifact = tmp_path / "PixTagra"
    artifact.mkdir()
    (artifact / "stdlib-example.bin").write_bytes(b"splitroot('C:/Users/Barney')")

    scan = manifest_tool.scan_release_tree(artifact, project_root=ROOT)

    assert scan["absolute_path_hits"] == []
    assert scan["generic_unix_path_marker_hits"] == ["stdlib-example.bin"]
    assert "Barney" not in json.dumps(scan)


def test_manifest_comparison_never_claims_bit_identity_when_hashes_differ():
    manifest_tool = _load_script("generate_release_manifest.py")
    first = {"files": [{"path": "PixTagra.exe", "sha256": "a", "size": 1}]}
    second = {"files": [{"path": "PixTagra.exe", "sha256": "b", "size": 1}]}

    comparison = manifest_tool.compare_release_manifests(first, second)

    assert comparison["bit_identical"] is False
    assert comparison["different_files"] == ["PixTagra.exe"]


def test_manifest_comparison_reports_bit_identity_only_for_same_file_records():
    manifest_tool = _load_script("generate_release_manifest.py")
    record = {"path": "PixTagra.exe", "sha256": "same", "size": 1}

    comparison = manifest_tool.compare_release_manifests({"files": [record]}, {"files": [dict(record)]})

    assert comparison["file_set_identical"] is True
    assert comparison["bit_identical"] is True
