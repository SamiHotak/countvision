"""The CountVision app icon: a counting line with something crossing it (arrow up = IN).

Drawn in code with Pillow so it stays sharp at every size. ``python -m countvision_desktop.icon``
writes ``assets/icon.ico`` (16-256 px, for the window, the shortcut and setup.exe) and ``icon.png``.
"""

from __future__ import annotations

from pathlib import Path

INK = (14, 24, 32, 255)
PANEL = (26, 42, 54, 255)
ACCENT = (25, 195, 177, 255)
TEXT = (220, 230, 234, 255)

ASSETS = Path(__file__).resolve().parent / "assets"


def draw(size: int = 256):
    """The icon as a Pillow image (RGBA)."""
    from PIL import Image, ImageDraw

    scale = 4  # draw big, then scale down: smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(s * 0.22)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=INK)
    # the counting line, across the doorway
    line_y = int(s * 0.58)
    thick = max(scale * 2, int(s * 0.085))
    d.rounded_rectangle((int(s * 0.14), line_y - thick // 2, int(s * 0.86), line_y + thick // 2),
                        radius=thick // 2, fill=ACCENT)
    # an arrow crossing the line upwards (a visitor coming IN)
    cx = s // 2
    shaft = max(scale * 2, int(s * 0.07))
    top = int(s * 0.2)
    head = int(s * 0.17)
    d.rectangle((cx - shaft // 2, top + head - scale, cx + shaft // 2, int(s * 0.8)), fill=TEXT)
    d.polygon([(cx, top), (cx - head, top + head + int(s * 0.02)), (cx + head, top + head + int(s * 0.02))],
              fill=TEXT)
    return img.resize((size, size), Image.LANCZOS)


def tray_image(size: int = 64):
    return draw(size)


def write_assets(folder: Path = ASSETS) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    big = draw(256)
    ico = folder / "icon.ico"
    big.save(ico, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128),
                         (256, 256)])
    png = folder / "icon.png"
    big.save(png)
    return [ico, png]


if __name__ == "__main__":
    for path in write_assets():
        print(path)
