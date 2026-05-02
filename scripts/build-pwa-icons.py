#!/usr/bin/env python3
"""Regenerate PWA PNG icons from admin/app/static/icon.svg.

Run from repo root after editing the SVG. The PNG outputs are committed
so CI does not have to render them, but this script is the source of
truth for icon sizing and maskable safe-zone padding.

Inputs:
  admin/app/static/icon.svg

Outputs:
  admin/app/static/icon-192.png             192x192 transparent
  admin/app/static/icon-512.png             512x512 transparent
  admin/app/static/icon-maskable-512.png    512x512 paper-cream w/ 40% safe zone
  admin/app/static/apple-touch-icon-180.png 180x180 paper-cream w/ rounded mask

Requires librsvg (apt install librsvg2-bin) or, on macOS, qlmanage falls
back automatically. Pillow does the resize/composite.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "admin/app/static/icon.svg"
OUT_DIR = ROOT / "admin/app/static"
PAPER = (251, 248, 243, 255)  # #FBF8F3 paper-cream — matches marketing/login bg


def render_svg_to_png(svg: Path, out: Path, size: int = 1024) -> None:
    """Render SVG to PNG at given size. Prefer rsvg-convert; fall back to qlmanage on macOS."""
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size), str(svg), "-o", str(out)],
            check=True,
        )
        return
    if shutil.which("qlmanage") and sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["qlmanage", "-t", "-s", str(size), "-o", tmp, str(svg)],
                check=True, capture_output=True,
            )
            generated = Path(tmp) / f"{svg.name}.png"
            if not generated.exists():
                raise RuntimeError(f"qlmanage produced no output for {svg}")
            shutil.copy(generated, out)
        return
    raise RuntimeError("Need rsvg-convert or qlmanage (macOS) to render SVG.")


def main() -> int:
    if not SVG_PATH.exists():
        print(f"missing {SVG_PATH}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "master.png"
        render_svg_to_png(SVG_PATH, master, size=1024)
        src = Image.open(master).convert("RGBA")

    for size in (192, 512):
        img = src.resize((size, size), Image.LANCZOS)
        out = OUT_DIR / f"icon-{size}.png"
        img.save(out, optimize=True)
        print(f"wrote {out.relative_to(ROOT)}")

    # Maskable: paw at 60% on paper-cream, leaving 40% safe zone for adaptive masks
    canvas = Image.new("RGBA", (512, 512), PAPER)
    inner_size = int(512 * 0.60)
    inner = src.resize((inner_size, inner_size), Image.LANCZOS)
    offset = ((512 - inner_size) // 2, (512 - inner_size) // 2)
    canvas.paste(inner, offset, inner)
    out = OUT_DIR / "icon-maskable-512.png"
    canvas.save(out, optimize=True)
    print(f"wrote {out.relative_to(ROOT)}")

    # Apple touch icon: iOS applies its own rounded mask, so paw at 75% on paper-cream
    canvas = Image.new("RGBA", (180, 180), PAPER)
    inner_size = int(180 * 0.75)
    inner = src.resize((inner_size, inner_size), Image.LANCZOS)
    offset = ((180 - inner_size) // 2, (180 - inner_size) // 2)
    canvas.paste(inner, offset, inner)
    out = OUT_DIR / "apple-touch-icon-180.png"
    canvas.save(out, optimize=True)
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
