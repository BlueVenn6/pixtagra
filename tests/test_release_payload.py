import json

import pytest

from scripts.generate_release_manifest import collect_distribution_files
from scripts.release_payload import uninstall_commands, verify_payload


def test_payload_verification_rejects_changed_missing_and_extra_runtime_files(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    exe = root / "PixTagra.exe"
    dll = root / "python312.dll"
    exe.write_bytes(b"fixture")
    dll.write_bytes(b"runtime")
    manifest = tmp_path / "verified.json"
    manifest.write_text(json.dumps(collect_distribution_files(root)), encoding="utf-8")
    assert len(verify_payload(root, manifest)) == 2
    dll.write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        verify_payload(root, manifest)
    dll.unlink()
    with pytest.raises(ValueError, match="differs"):
        verify_payload(root, manifest)
    dll.write_bytes(b"runtime")
    (root / "old-plugin.dll").write_bytes(b"stale")
    with pytest.raises(ValueError, match="differs"):
        verify_payload(root, manifest)


def test_uninstaller_only_removes_shipped_files_and_empty_directories():
    commands = uninstall_commands([{"path": "PixTagra.exe"}, {"path": "_internal/legal/EULA.md"},
                                   {"path": "_internal/$name.txt"}])
    assert 'Delete "$INSTDIR\\_internal\\$$name.txt"' in commands
    assert commands.index('RMDir "$INSTDIR\\_internal\\legal"') < commands.index('RMDir "$INSTDIR\\_internal"')
    assert "/r" not in commands and "*" not in commands
    assert "$APPDATA" not in commands and "$LOCALAPPDATA" not in commands


@pytest.mark.parametrize("path", ["../secret", "C:/secret", "\\secret", "//server/share", 'bad"file', "file:stream"])
def test_uninstall_manifest_cannot_escape_install_directory(path):
    with pytest.raises(ValueError, match="Unsafe"):
        uninstall_commands([{"path": path}])
