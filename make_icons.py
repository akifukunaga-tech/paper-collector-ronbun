"""Generate PWA icons. Run once when the design changes.

Outputs to icons/:
  icon-192.png, icon-512.png, icon-180.png (apple-touch), icon-512-maskable.png, favicon.png
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "icons"
OUT.mkdir(exist_ok=True)

BG      = (11, 14, 19)      # #0b0e13
PANEL   = (20, 26, 34)      # #141a22
ACCENT  = (110, 169, 255)   # #6ea9ff
GREEN   = (74, 222, 128)    # #4ade80
MUTED   = (138, 150, 166)   # #8a96a6
STROKE  = (76, 139, 245)    # #4c8bf5


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(size: int, maskable: bool = False) -> Image.Image:
    im = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(im)
    # Rounded background so the icon looks OK when a launcher doesn't mask it.
    corner = int(size * 0.22)
    d.rounded_rectangle([0, 0, size, size], radius=corner, fill=BG)

    # Safe-zone padding: maskable icons must survive an aggressive circle mask,
    # so we shrink the actual content to a smaller safe area.
    safe = int(size * 0.20) if maskable else int(size * 0.10)
    inner = size - 2 * safe

    # Paper
    px = safe + int(inner * 0.14)
    py = safe + int(inner * 0.08)
    pw = int(inner * 0.72)
    ph = int(inner * 0.84)
    r = int(size * 0.04)
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=r, fill=PANEL, outline=STROKE, width=max(2, size // 90))

    # Lines representing a paper card content
    lx = px + int(pw * 0.12)
    lw = int(pw * 0.76)
    lh = max(2, size // 70)
    def line(top_frac: float, width_frac: float, color, tall: int = 1) -> None:
        y = py + int(ph * top_frac)
        h = lh * tall
        d.rounded_rectangle([lx, y, lx + int(lw * width_frac), y + h], radius=h // 2, fill=color)

    line(0.14, 1.00, ACCENT, tall=2)   # title
    line(0.28, 0.70, GREEN)            # keyword row
    line(0.36, 0.90, MUTED)
    line(0.44, 0.70, MUTED)

    # "10" badge
    br = int(pw * 0.17)
    bcx = px + pw // 2
    bcy = py + int(ph * 0.72)
    d.ellipse([bcx - br, bcy - br, bcx + br, bcy + br], fill=ACCENT)
    font = _font(int(br * 1.15))
    d.text((bcx, bcy), "10", font=font, fill=BG, anchor="mm")
    return im


def main() -> None:
    variants = [
        ("icon-192.png",           192, False),
        ("icon-512.png",           512, False),
        ("icon-180.png",           180, False),
        ("icon-512-maskable.png",  512, True),
        ("favicon.png",             32, False),
    ]
    for name, size, mask in variants:
        img = render(size, maskable=mask)
        img.save(OUT / name, "PNG", optimize=True)
        print(f"  {name:26} {size:4}x{size:<4} {(OUT / name).stat().st_size // 1024:5} KB")


if __name__ == "__main__":
    main()
