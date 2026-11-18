"""Derive the Windows ICO from the unchanged official artwork using Pillow."""

import io
from pathlib import Path
import struct

from PIL import Image


def generate_icon(branding):
    branding = Path(branding)
    with Image.open(branding / "PixTagra_App_Icon.png") as original:
        if original.size[0] != original.size[1]:
            raise ValueError("Official icon artwork must be square; do not crop it")
        buffer = io.BytesIO()
        original.save(buffer, format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    data = bytearray(buffer.getvalue())
    count = struct.unpack_from("<H", data, 4)[0]
    entries = [data[6 + 16 * i:6 + 16 * (i + 1)] for i in range(count)]
    # Tk uses the first ICO image on this Windows runtime. Keep every frame
    # intact, but put the largest first to avoid enlarging the 16px artwork.
    entries.sort(key=lambda entry: entry[0] or 256, reverse=True)
    data[6:6 + 16 * count] = b"".join(entries)
    output = branding / "PixTagra_App_Icon.ico"
    output.write_bytes(data)
    return output


if __name__ == "__main__":
    print(generate_icon(Path(__file__).resolve().parents[1] / "assets" / "branding"))
