"""Bind packaging and uninstall operations to the verified distribution files."""
import argparse
import json
from pathlib import Path, PureWindowsPath
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.generate_release_manifest import collect_distribution_files


def verify_payload(root, manifest):
    expected = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
    actual = collect_distribution_files(Path(root))
    if not expected or actual != expected:
        raise ValueError("Distribution differs from the verified payload manifest")
    return expected


def uninstall_commands(files):
    """Delete only shipped files; leave user files and nonempty directories."""
    commands = []
    directories = set()
    for record in files:
        path = PureWindowsPath(record["path"])
        if path.root or path.drive or ".." in path.parts or any(c in str(path) for c in '\r\n":'):
            raise ValueError("Unsafe payload path")
        relative = str(path).replace("$", "$$")
        commands.append(f'Delete "$INSTDIR\\{relative}"')
        directories.update(parent for parent in path.parents if str(parent) != ".")
    for path in sorted(directories, key=lambda p: (-len(p.parts), str(p))):
        relative = str(path).replace("$", "$$")
        commands.append(f'RMDir "$INSTDIR\\{relative}"')
    return "\n".join(commands) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "verify", "uninstall"))
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "snapshot":
        args.manifest.write_text(json.dumps(collect_distribution_files(args.artifact_root), indent=2), encoding="utf-8")
    else:
        files = verify_payload(args.artifact_root, args.manifest)
        if args.action == "uninstall":
            if args.output is None:
                parser.error("--output is required for uninstall")
            args.output.write_text(uninstall_commands(files), encoding="utf-8-sig")


if __name__ == "__main__":
    main()
