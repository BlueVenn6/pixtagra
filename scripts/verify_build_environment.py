"""Verify the local offline build environment against exact requirement pins."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import platform
import re


_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")


def pinned_requirements(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"unlocked build requirement: {line}")
        pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def verify_environment(requirements: Path, python_version: str) -> dict[str, object]:
    actual_python = platform.python_version()
    problems: list[str] = []
    if actual_python != python_version:
        problems.append("python_version")
    resolved: list[dict[str, str]] = []
    for name, expected in sorted(pinned_requirements(requirements).items()):
        try:
            actual = version(name)
        except PackageNotFoundError:
            problems.append(f"missing:{name}")
            continue
        if actual != expected:
            problems.append(f"version:{name}")
        resolved.append({"name": name, "version": actual})
    return {"ok": not problems, "python_version": actual_python, "packages": resolved, "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()
    result = verify_environment(args.requirements, args.python_version)
    if not result["ok"]:
        raise SystemExit("controlled build environment does not match requirements-build.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
