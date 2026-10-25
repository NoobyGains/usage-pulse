"""Composite the README hero banner, the GitHub social preview and the plugin icon from a rendered emblem.

The emblem is rendered locally with Z-Image-Turbo (prompt and seed in
assets/brand/README.md); this script only lays it out and sets the type, so every
derived picture can be rebuilt pixel for pixel.

    python scripts/make_hero.py
    python scripts/make_hero.py --bg assets/brand/emblem.png --out assets/brand/hero.png

Fonts: Unbounded ExtraBold (wordmark) and JetBrains Mono (tagline), static
instances of the SIL OFL fonts, vendored in assets/brand/fonts/. Pillow is the only dependency.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND = REPO_ROOT / "assets" / "brand"
FONTS = BRAND / "fonts"

WORD = "Usage Pulse"
SUB = "for Claude Code"
TAGLINE = "Every limit, cost and agent, live under your prompt"
FOOTLINE = "zero API calls  ·  zero dependencies  ·  10 themes  ·  Claude  ·  Codex  ·  Gemini  ·  MiniMax"

W, H = 1600, 600          # 2x of the 800x300 the README shows
BG = (6, 7, 11)
# The gradient the project has used since its first logo: cyan, indigo, fuchsia.
C_CYAN, C_INDIGO, C_FUCHSIA = (34, 211, 238), (129, 140, 248), (232, 121, 249)
GRADIENT = [(248, 250, 252), (226, 232, 240), C_CYAN, C_INDIGO, C_FUCHSIA]


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def fit(draw, text, name, start, floor, max_width):
    """The largest size from *start* down to *floor* at which *text* fits *max_width*."""
    size = start
    while size > floor:
        f = font(name, size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 1
    return font(name, floor)


def cover(im, w, h, anchor=0.5):
    scale = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    x = round((im.width - w) * anchor)
    y = (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def horizontal_gradient(w, h, stops):
    grad = Image.new("RGB", (w, 1))
    px = grad.load()
    n = len(stops) - 1
    for x in range(w):
        t = x / max(w - 1, 1) * n
        i = min(int(t), n - 1)
        f = t - i
        a, b = stops[i], stops[i + 1]
        px[x, 0] = tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
    return grad.resize((w, h))


def place_emblem(emblem_path, size=600, x=70):
    """Dark canvas, square render on the left, its right edge faded into the
    canvas so the render's own background never shows a seam."""
    canvas = Image.new("RGB", (W, H), BG)
    em = cover(Image.open(emblem_path).convert("RGB"), size, size)
    fade = Image.new("L", (size, 1), 255)
    px = fade.load()
    for i in range(size):
        t = i / (size - 1)
        px[i, 0] = 255 if t < 0.62 else round(255 * max(0.0, 1 - (t - 0.62) / 0.38))
    fade = fade.resize((size, size))
    canvas.paste(em, (x, (H - size) // 2), fade)
    return canvas


def gradient_text(canvas, text, f, xy, glow_colour, glow_radius=28, glow_strength=0.55):
    """Draw *text* filled with the brand gradient over a soft glow."""
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=f, fill=255)
    glow = mask.filter(ImageFilter.GaussianBlur(glow_radius))
    canvas = Image.composite(Image.new("RGB", canvas.size, glow_colour), canvas, glow.point(lambda v: v * glow_strength))
    bbox = mask.getbbox()
    grad = horizontal_gradient(bbox[2] - bbox[0], canvas.height, GRADIENT)
    grad_full = Image.new("RGB", canvas.size, GRADIENT[0])
    grad_full.paste(grad, (bbox[0], 0))
    return Image.composite(grad_full, canvas, mask)


def round_corners(im, radius):
    im = im.convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=radius, fill=255)
    im.putalpha(mask)
    return im


def build_banner(bg_path, out_path, text_x=720):
    canvas = place_emblem(bg_path)
    draw = ImageDraw.Draw(canvas)
    right = W - text_x - 4 - 60
    canvas = gradient_text(canvas, WORD, fit(draw, WORD, "Unbounded-ExtraBold.ttf", 118, 60, right), (text_x, 150), C_INDIGO)
    draw = ImageDraw.Draw(canvas)
    draw.text((text_x + 4, 282), SUB, font=fit(draw, SUB, "Unbounded-ExtraBold.ttf", 50, 30, right), fill=(226, 232, 240))
    draw.text((text_x + 4, 376), TAGLINE, font=fit(draw, TAGLINE, "JetBrainsMono-Medium.ttf", 29, 18, right), fill=(203, 213, 225))
    draw.text((text_x + 4, 434), FOOTLINE, font=fit(draw, FOOTLINE, "JetBrainsMono-Regular.ttf", 17, 11, right), fill=(120, 130, 150))
    canvas = round_corners(canvas, 28)
    canvas.save(out_path, optimize=True)
    return canvas


def build_social_preview(bg_path, out_path):
    """GitHub's social preview is 1280x640: emblem centred high, wordmark below."""
    canvas = Image.new("RGB", (1280, 640), BG)
    size = 400
    em = cover(Image.open(bg_path).convert("RGB"), size, size)
    fade = Image.new("L", (size, size), 0)
    ImageDraw.Draw(fade).ellipse((70, 70, size - 70, size - 70), fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(34))
    canvas.paste(em, ((1280 - size) // 2, 0), fade)
    d = ImageDraw.Draw(canvas)
    word_font = fit(d, WORD, "Unbounded-ExtraBold.ttf", 92, 50, 1100)
    tw = d.textlength(WORD, font=word_font)
    canvas = gradient_text(canvas, WORD, word_font, ((1280 - tw) / 2, 380), C_INDIGO, 24)
    d = ImageDraw.Draw(canvas)
    sf = font("Unbounded-ExtraBold.ttf", 40)
    d.text(((1280 - d.textlength(SUB, font=sf)) / 2, 496), SUB, font=sf, fill=(226, 232, 240))
    tf = font("JetBrainsMono-Medium.ttf", 24)
    d.text(((1280 - d.textlength(TAGLINE, font=tf)) / 2, 568), TAGLINE, font=tf, fill=(203, 213, 225))
    canvas.save(out_path, optimize=True)
    return canvas


def build_icon(bg_path, out_path, size=512):
    """512x512 rounded square around the bright part of the render, for plugin listings."""
    src = Image.open(bg_path).convert("RGB")
    bbox = src.convert("L").point(lambda v: 255 if v > 60 else 0).getbbox()
    cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
    half = int(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.62)
    box = (max(cx - half, 0), max(cy - half, 0), min(cx + half, src.width), min(cy + half, src.height))
    im = src.crop(box).resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=255)
    im.putalpha(mask)
    im.save(out_path)
    return box


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", default=str(BRAND / "emblem.png"))
    ap.add_argument("--out", default=str(BRAND / "hero.png"))
    ap.add_argument("--text-x", type=int, default=720)
    ap.add_argument("--social", default=str(BRAND / "social-preview.png"), help="path for the 1280x640 GitHub social preview; '' to skip")
    ap.add_argument("--icon", default=str(BRAND / "icon.png"), help="path for the 512x512 icon; '' to skip")
    a = ap.parse_args()
    banner = build_banner(a.bg, a.out, a.text_x)
    print("wrote", a.out, banner.size)
    if a.social:
        build_social_preview(a.bg, a.social)
        print("wrote", a.social)
    if a.icon:
        box = build_icon(a.bg, a.icon)
        print("wrote", a.icon, "from box", box)


if __name__ == "__main__":
    main()
