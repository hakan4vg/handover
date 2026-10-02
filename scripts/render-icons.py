"""Renders the app and extension icons from one drawing (icons/icon.svg is the
same shape). Each size is drawn on its own, supersampled, with heavier strokes
at small sizes so the arrow stays legible in the tray and the toolbar.

    python scripts/render-icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BLUE = (8, 120, 237, 255)
WHITE = (255, 255, 255, 255)
SUPERSAMPLE = 8


def stroke_for(size: int) -> float:
    """Stroke width in the 256-unit drawing."""
    if size <= 20:
        return 30
    if size <= 48:
        return 25
    return 20


def line(draw: ImageDraw.ImageDraw, points, width: float, scale: float) -> None:
    pixels = [(x * scale, y * scale) for x, y in points]
    w = width * scale
    draw.line(pixels, fill=WHITE, width=round(w), joint='curve')
    for x, y in pixels:
        draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=WHITE)


def render(size: int) -> Image.Image:
    canvas = size * SUPERSAMPLE
    scale = canvas / 256
    image = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=52 * scale, fill=BLUE)
    width = stroke_for(size)
    line(draw, [(128, 45), (128, 141)], width, scale)
    line(draw, [(91, 107), (128, 144), (165, 107)], width, scale)
    line(draw, [(68, 201), (188, 201)], width, scale)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    icons = ROOT / 'src-tauri' / 'icons'
    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 256]
    images = [render(size) for size in ico_sizes]
    images[-1].save(icons / 'icon.ico', sizes=[(s, s) for s in ico_sizes], append_images=images[:-1])
    render(512).save(icons / 'icon.png')
    for size in (16, 24, 32):
        render(size).save(icons / f'tray-{size}.png')
    render(64).save(ROOT / 'src' / 'assets' / 'app-icon.png')
    extension = ROOT / 'extension' / 'icons'
    extension.mkdir(exist_ok=True)
    for size in (16, 32, 48, 128):
        render(size).save(extension / f'icon-{size}.png')


if __name__ == '__main__':
    main()
