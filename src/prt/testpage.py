"""Built-in colour test page: ink bars, gradients, grey steps and fine lines (A4 @ 150 dpi JPEG)."""

from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 1240, 1754  # A4 at 150 dpi
MARGIN = 90


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render(title: str = "printer") -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((MARGIN, MARGIN), "prt test page", fill="black", font=_font(56))
    d.text((MARGIN, MARGIN + 74), f"{title} · {time.strftime('%Y-%m-%d %H:%M')}", fill=(90, 90, 90), font=_font(26))
    y = MARGIN + 150

    # Primary ink bars (C M Y K) and secondary mixes (R G B)
    bars = [
        ("C", (0, 255, 255)),
        ("M", (255, 0, 255)),
        ("Y", (255, 255, 0)),
        ("K", (0, 0, 0)),
        ("R", (255, 0, 0)),
        ("G", (0, 160, 0)),
        ("B", (0, 0, 255)),
    ]
    bw = (W - 2 * MARGIN) // len(bars)
    for i, (label, rgb) in enumerate(bars):
        x = MARGIN + i * bw
        d.rectangle([x + 6, y, x + bw - 6, y + 220], fill=rgb)
        d.text((x + 6, y + 232), label, fill="black", font=_font(28))
    y += 300

    # Smooth gradients per channel — banding here means clogged nozzles or low ink.
    for rgb in [(0, 255, 255), (255, 0, 255), (255, 255, 0), (0, 0, 0)]:
        for x in range(W - 2 * MARGIN):
            t = x / (W - 2 * MARGIN - 1)
            col = tuple(round(255 - (255 - c) * t) for c in rgb)
            d.line([(MARGIN + x, y), (MARGIN + x, y + 70)], fill=col)
        y += 90
    y += 30

    # Grey steps 0–100 %
    steps = 11
    sw = (W - 2 * MARGIN) // steps
    for i in range(steps):
        g = round(255 * (1 - i / (steps - 1)))
        d.rectangle([MARGIN + i * sw, y, MARGIN + (i + 1) * sw - 4, y + 110], fill=(g, g, g), outline=(180, 180, 180))
        d.text((MARGIN + i * sw + 4, y + 118), f"{i * 10}%", fill="black", font=_font(20))
    y += 190

    # Fine-line pattern for alignment: 1px, 2px, 3px lines
    for width in (1, 2, 3):
        for k in range(0, W - 2 * MARGIN, 12 * width):
            d.line([(MARGIN + k, y), (MARGIN + k, y + 90)], fill="black", width=width)
        y += 120
    d.text(
        (MARGIN, H - MARGIN - 30),
        "Check: solid bars, smooth gradients, crisp lines. Streaks → run `prt maintain nozzle-check`.",
        fill=(90, 90, 90),
        font=_font(22),
    )
    return img


def render_jpeg(title: str = "printer") -> bytes:
    buf = io.BytesIO()
    render(title).save(buf, "JPEG", quality=92, dpi=(150, 150))
    return buf.getvalue()
