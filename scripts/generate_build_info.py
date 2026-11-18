"""Create bundle-only non-secret source identity for a controlled RC build."""

from __future__ import annotations

import argparse
from pathlib import Path


def _safe_value(value: str) -> str:
    if not value or any(character not in "0123456789abcdef-" for character in value.lower()):
        raise ValueError("build identity contains unsupported characters")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()

    source_commit = _safe_value(args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        '"""Generated build identity; not a credential or user-data source."""\n\n'
        f'SOURCE_COMMIT = "{source_commit}"\n',
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
