"""Generate the PyInstaller version resource from PixTagra's single version source."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pixtagra_release import FILE_DESCRIPTION, INTERNAL_NAME, ORIGINAL_FILENAME, PRODUCT_NAME, PRODUCT_VERSION


def _version_tuple() -> tuple[int, int, int, int]:
    pieces = PRODUCT_VERSION.split(".")
    if len(pieces) != 4 or any(not piece.isdigit() for piece in pieces):
        raise ValueError("PRODUCT_VERSION must contain four numeric components")
    return tuple(int(piece) for piece in pieces)  # type: ignore[return-value]


def write_version_resource(output: Path) -> None:
    file_version = _version_tuple()
    resource = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={file_version},
    prodvers={file_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('FileDescription', '{FILE_DESCRIPTION}'),
          StringStruct('FileVersion', '{PRODUCT_VERSION}'),
          StringStruct('InternalName', '{INTERNAL_NAME}'),
          StringStruct('OriginalFilename', '{ORIGINAL_FILENAME}'),
          StringStruct('ProductName', '{PRODUCT_NAME}'),
          StringStruct('ProductVersion', '{PRODUCT_VERSION}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(resource, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_version_resource(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
