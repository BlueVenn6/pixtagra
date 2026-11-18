"""Create a small offline dependency inventory for the Windows RC manifest."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, metadata
import json
from pathlib import Path
import re


_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
_PACKAGED_RUNTIME = {"customtkinter", "darkdetect", "packaging", "pillow", "piexif", "requests", "certifi", "charset-normalizer", "idna", "urllib3", "cryptography", "cffi", "pycparser"}


def _records(requirements: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        match = _PIN.fullmatch(raw_line.strip())
        if match is None:
            continue
        name, pinned = match.groups()
        normalized = name.lower().replace("_", "-")
        try:
            package_metadata = metadata(name)
            license_value = package_metadata.get("License-Expression") or package_metadata.get("License") or ""
            source = package_metadata.get("Home-page") or "installed distribution metadata"
        except PackageNotFoundError:
            license_value = ""
            source = "unavailable"
        records.append(
            {
                "name": name,
                "version": pinned,
                "source": source,
                "license": license_value,
                "direct": normalized in _PACKAGED_RUNTIME or normalized == "pyinstaller",
                "packaged": normalized in _PACKAGED_RUNTIME,
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = {"schema_version": 1, "components": _records(args.requirements)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
