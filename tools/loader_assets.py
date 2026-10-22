"""Make the images for the update loading screen from the app icon.

    python tools/loader_assets.py

Writes jigstream/static/loader/logo.png (the icon's navy tile, cropped) and lines.png (a white mask of
the logo's bright lines, so a moving light can be drawn only where the logo has outlines).
"""
from pathlib import Path

from PIL import Image, ImageChops

STATIC = Path(__file__).resolve().parent.parent / "jigstream" / "static"
TILE = (99, 99, 924, 924)  # the rounded navy square inside icon.png, without its drop shadow
SIZE = 480


def main():
    out = STATIC / "loader"
    out.mkdir(exist_ok=True)
    tile = Image.open(STATIC / "icon.png").convert("RGBA").crop(TILE).resize((SIZE, SIZE), Image.LANCZOS)
    tile.save(out / "logo.png", optimize=True)
    # The tile is navy (about 10,19,52); anything clearly brighter is part of the drawing.
    r, g, b = tile.convert("RGB").split()
    bright = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: max(0, min(255, (v - 70) * 3)))
    # Only the drawing inside the rounded tile; the tile's own edge gets a separate light.
    inside = tile.getchannel("A").point(lambda v: 255 if v == 255 else 0)
    mask = Image.new("RGBA", tile.size, (255, 255, 255, 0))
    mask.putalpha(ImageChops.multiply(bright, inside))
    mask.save(out / "lines.png", optimize=True)


if __name__ == "__main__":
    main()
