"""Render the checked-in SVG to a multi-resolution Windows ICO."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "static" / "icon.svg"
ICO = ROOT / "static" / "icon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    source = SVG.read_bytes()
    document = pymupdf.open(stream=source, filetype="svg")
    page = document[0]
    images = []
    for size in SIZES:
        bitmap = page.get_pixmap(matrix=pymupdf.Matrix(size / 256, size / 256), alpha=True)
        images.append(Image.open(BytesIO(bitmap.tobytes("png"))).convert("RGBA"))
    # Pillow writes all requested sizes into one ICO and uses PNG for large layers.
    images[-1].save(ICO, format="ICO", sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    with Image.open(ICO) as icon:
        present = set(icon.ico.sizes())
    if present != {(size, size) for size in SIZES}:
        raise RuntimeError(f"ICO layers do not match the requested sizes: {sorted(present)}")
    document.close()
    print(f"Created {ICO.name} with {len(SIZES)} sizes")


if __name__ == "__main__":
    main()
