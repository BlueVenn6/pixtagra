"""Generate redacted manifests and offline scans for a PixTagra Windows RC."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pixtagra_release import PRODUCT_NAME, PRODUCT_VERSION


_FORBIDDEN_FILE_NAMES = {
    "keys.vault",
    "keys.salt",
    "credential-migration-state.json",
    "config.json",
}
_FORBIDDEN_PARTS = {".git", "tests", "docs", "audit", ".pytest_cache"}
_SECRET_SENTINELS = (b"TEST_KEY_DO_NOT_USE", b"OTHER_TEST_KEY_DO_NOT_USE")
_GENERIC_UNIX_USER_MARKER = b"/Users/"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def collect_distribution_files(artifact_root: Path) -> list[dict[str, object]]:
    root = artifact_root.resolve()
    records: list[dict[str, object]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def _developer_path_markers(project_root: Path) -> tuple[bytes, ...]:
    """Derive host-specific path markers without embedding a username in source."""
    root = project_root.resolve()
    markers = {str(root).encode("utf-8", errors="ignore")}
    parts = root.parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in {"users", "home"}:
            markers.add(str(Path(*parts[: index + 2])).encode("utf-8", errors="ignore"))
    return tuple(marker for marker in markers if marker)


def scan_release_tree(artifact_root: Path, *, project_root: Path) -> dict[str, list[str]]:
    """Report only relative paths and categories, never matched content or host paths."""
    root = artifact_root.resolve()
    developer_path_markers = _developer_path_markers(project_root)
    forbidden_file_paths: list[str] = []
    secret_sentinel_hits: list[str] = []
    absolute_path_hits: list[str] = []
    generic_unix_path_marker_hits: list[str] = []
    authorization_marker_hits: list[str] = []
    identifier_hits: list[str] = []

    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        parts = set(path.relative_to(root).parts)
        if path.name in _FORBIDDEN_FILE_NAMES or parts.intersection(_FORBIDDEN_PARTS):
            forbidden_file_paths.append(relative)
        content = path.read_bytes()
        if any(marker in content for marker in _SECRET_SENTINELS):
            secret_sentinel_hits.append(relative)
        if any(marker in content for marker in developer_path_markers):
            absolute_path_hits.append(relative)
        if _GENERIC_UNIX_USER_MARKER in content:
            generic_unix_path_marker_hits.append(relative)
        if b"Authorization:" in content:
            authorization_marker_hits.append(relative)
        if path.suffix.lower() in {".txt", ".json", ".py"} and b"api_key" in content.lower():
            identifier_hits.append(relative)

    return {
        "forbidden_file_paths": forbidden_file_paths,
        "secret_sentinel_hits": secret_sentinel_hits,
        "absolute_path_hits": absolute_path_hits,
        "generic_unix_path_marker_hits": generic_unix_path_marker_hits,
        "authorization_marker_hits": authorization_marker_hits,
        "identifier_hits": identifier_hits,
    }


def compare_release_manifests(first: dict[str, Any], second: dict[str, Any]) -> dict[str, object]:
    first_files = {item["path"]: item for item in first.get("files", [])}
    second_files = {item["path"]: item for item in second.get("files", [])}
    first_paths = set(first_files)
    second_paths = set(second_files)
    common = sorted(first_paths.intersection(second_paths))
    different = [
        path
        for path in common
        if first_files[path].get("sha256") != second_files[path].get("sha256")
        or first_files[path].get("size") != second_files[path].get("size")
    ]
    only_first = sorted(first_paths.difference(second_paths))
    only_second = sorted(second_paths.difference(first_paths))
    return {
        "file_set_identical": not only_first and not only_second,
        "bit_identical": not only_first and not only_second and not different,
        "only_build_a": only_first,
        "only_build_b": only_second,
        "different_files": different,
    }


def _safe_dependencies(dependencies: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "name": str(item.get("name", "")),
            "version": str(item.get("version", "")),
            "license": str(item.get("license", "")),
            "direct": bool(item.get("direct", False)),
            "packaged": bool(item.get("packaged", False)),
        }
        for item in dependencies
    ]


def build_release_manifest(
    *,
    artifact_root: Path,
    source_commit: str,
    baseline_tag: str,
    build_command: str,
    test_total: int,
    packaged_self_test: dict[str, object],
    packaged_gui_smoke: dict[str, object],
    scan_result: dict[str, object],
    comparison: dict[str, object],
    signature_status: str,
    build_dependencies: list[dict[str, object]],
    archive_listing_sha256: str = "",
    warning_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    files = collect_distribution_files(artifact_root)
    executable = next((item for item in files if item["path"] == "PixTagra.exe"), None)
    if executable is None:
        raise ValueError("PixTagra.exe is missing from the release tree")
    return {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "source_commit": source_commit,
        "source_baseline_tag": baseline_tag,
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "build_timestamp_not_part_of_reproducibility": True,
        "python_version": platform.python_version(),
        "operating_system": platform.system(),
        "architecture": platform.machine(),
        "pyinstaller_version": _package_version("PyInstaller"),
        "build_command": build_command,
        "signature_status": signature_status,
        "signed": False,
        "distribution_root": "PixTagra",
        "executable_sha256": executable["sha256"],
        "files": files,
        "test_total": int(test_total),
        "packaged_self_test": packaged_self_test,
        "packaged_gui_smoke": packaged_gui_smoke,
        "secret_path_scan": scan_result,
        "build_comparison": comparison,
        "archive_listing_sha256": archive_listing_sha256,
        "warning_summary": warning_summary or {"present": False, "line_count": 0},
        "build_dependencies": _safe_dependencies(build_dependencies),
    }


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "unavailable"


def write_manifest(manifest: dict[str, object], output_json: Path, output_text: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    lines = [
        f"Product: {manifest['product']}",
        f"Version: {manifest['version']}",
        f"Source commit: {manifest['source_commit']}",
        f"Baseline tag: {manifest['source_baseline_tag']}",
        f"Signature: {manifest['signature_status']}",
        f"Executable SHA-256: {manifest['executable_sha256']}",
        f"Payload files: {len(manifest['files'])}",
        f"Bit-identical A/B: {manifest['build_comparison'].get('bit_identical', False)}",
    ]
    output_text.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _read_json(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_dependencies(path: Path | None) -> list[dict[str, object]]:
    if path is None:
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", line.strip())
        if match:
            records.append({"name": match.group(1), "version": match.group(2), "license": "", "direct": True, "packaged": True})
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-text", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--baseline-tag", required=True)
    parser.add_argument("--build-command", required=True)
    parser.add_argument("--test-total", required=True, type=int)
    parser.add_argument("--packaged-self-test", type=Path)
    parser.add_argument("--packaged-gui-smoke", type=Path)
    parser.add_argument("--scan-result", type=Path)
    parser.add_argument("--comparison-output", type=Path)
    parser.add_argument("--compare-with", type=Path)
    parser.add_argument("--signature-status", default="unsigned")
    parser.add_argument("--build-requirements", type=Path)
    parser.add_argument("--archive-list", type=Path)
    parser.add_argument("--warnings-file", type=Path)
    args = parser.parse_args()

    scan = _read_json(args.scan_result) if args.scan_result else scan_release_tree(args.artifact_root, project_root=ROOT)
    comparison: dict[str, object] = {}
    provisional = {"files": collect_distribution_files(args.artifact_root)}
    if args.compare_with:
        comparison = compare_release_manifests(_read_json(args.compare_with), provisional)
        if args.comparison_output:
            args.comparison_output.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    archive_hash = sha256_file(args.archive_list) if args.archive_list and args.archive_list.exists() else ""
    warning_summary = {"present": bool(args.warnings_file and args.warnings_file.exists()), "line_count": 0}
    if args.warnings_file and args.warnings_file.exists():
        warning_summary["line_count"] = len(args.warnings_file.read_text(encoding="utf-8", errors="replace").splitlines())
    manifest = build_release_manifest(
        artifact_root=args.artifact_root,
        source_commit=args.source_commit,
        baseline_tag=args.baseline_tag,
        build_command=args.build_command,
        test_total=args.test_total,
        packaged_self_test=_read_json(args.packaged_self_test),
        packaged_gui_smoke=_read_json(args.packaged_gui_smoke),
        scan_result=scan,
        comparison=comparison,
        signature_status=args.signature_status,
        build_dependencies=_read_dependencies(args.build_requirements),
        archive_listing_sha256=archive_hash,
        warning_summary=warning_summary,
    )
    write_manifest(manifest, args.output_json, args.output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
