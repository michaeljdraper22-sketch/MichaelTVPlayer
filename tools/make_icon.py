# -*- coding: utf-8 -*-
"""Generate the MichaelTVPlayer icon (assets/icon.ico + icon.png).

Design: a dark navy rounded square, a bold white "M", and a cyan play
badge in the bottom-right corner. Regenerate with:
    .venv\\Scripts\\python.exe tools\\make_icon.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets")
SIZE = 1024                       # master size everything is downscaled from
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",   # Segoe UI Bold
    r"C:\Windows\Fonts\arialbd.ttf",    # Arial Bold
    r"C:\Windows\Fonts\arial.ttf",
]


def load_font(px: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, px)
    raise SystemExit("no usable TrueType font found in C:\\Windows\\Fonts")


def rounded_gradient_bg() -> Image.Image:
    """Dark navy rounded square with a vertical gradient + soft glow."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = Image.new("RGBA", (SIZE, SIZE))
    top = (23, 34, 63)        # #17223f
    bot = (9, 13, 26)         # #090d1a
    px = grad.load()
    for y in range(SIZE):
        t = y / (SIZE - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(SIZE):
            px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.185), fill=255)
    img.paste(grad, (0, 0), mask)

    # subtle cyan glow behind the play badge corner
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [int(SIZE * 0.52), int(SIZE * 0.52), int(SIZE * 1.02), int(SIZE * 1.02)],
        fill=(41, 196, 255, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(SIZE * 0.09))
    img = Image.alpha_composite(img, glow)
    return img


def add_m(img: Image.Image) -> Image.Image:
    """The bold white M, with a soft drop shadow for depth."""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = load_font(int(SIZE * 0.48))
    bbox = d.textbbox((0, 0), "M", font=font, anchor="lt")
    h = bbox[3] - bbox[1]
    w = bbox[2] - bbox[0]
    # up and left of center, clear of the bottom-right play badge
    cx, cy = SIZE * 0.385, SIZE * 0.40
    pos = (cx - w / 2 - bbox[0], cy - h / 2 - bbox[1])
    d.text((pos[0] + SIZE * 0.010, pos[1] + SIZE * 0.016), "M",
           font=font, fill=(0, 0, 0, 140))          # shadow
    d.text(pos, "M", font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(img, layer)


def add_play_badge(img: Image.Image) -> Image.Image:
    """Cyan rounded badge with a white play triangle, bottom-right."""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d_r = SIZE * 0.135                               # badge radius
    d_cx, d_cy = SIZE * 0.755, SIZE * 0.755          # badge center
    # drop shadow
    sh = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse(
        [d_cx - d_r * 1.02, d_cy - d_r * 1.02 + SIZE * 0.014,
         d_cx + d_r * 1.02, d_cy + d_r * 1.02 + SIZE * 0.014],
        fill=(0, 0, 0, 130))
    sh = sh.filter(ImageFilter.GaussianBlur(SIZE * 0.02))
    layer = Image.alpha_composite(layer, sh)
    d = ImageDraw.Draw(layer)
    d.ellipse([d_cx - d_r, d_cy - d_r, d_cx + d_r, d_cy + d_r],
              fill=(41, 196, 255, 255), outline=(160, 230, 255, 255),
              width=max(2, int(SIZE * 0.008)))
    # play triangle (optically centered: shifted right a touch)
    t_r = d_r * 0.52
    tri = [(d_cx - t_r * 0.75, d_cy - t_r), (d_cx - t_r * 0.75, d_cy + t_r),
           (d_cx + t_r, d_cy)]
    d.polygon(tri, fill=(255, 255, 255, 255))
    return Image.alpha_composite(img, layer)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    img = add_play_badge(add_m(rounded_gradient_bg()))
    img.save(os.path.join(OUT_DIR, "icon.png"))

    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    img.save(os.path.join(OUT_DIR, "icon.ico"),
             format="ICO", sizes=sizes, append_images=[])
    print("wrote assets/icon.ico + assets/icon.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
