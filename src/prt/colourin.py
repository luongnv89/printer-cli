"""``prt colour-in``: a photo turned into black line art on white, as a colouring page.

The photo is smoothed and cut into a few flat tones, and the outlines between the tones are drawn as black lines:
the shapes come out mostly closed, so each can be coloured in. The art is fitted whole (never cropped) in a frame on
an A4 sheet, upright for a portrait photo and on its side for a landscape one; the frame is drawn flush against the
art, so the outlines that reach the photo's edge meet it and it closes the shapes the edge cuts through. Every pixel of the page is pure black or pure white, so it prints with black ink only.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from prt.layout import Cuts
from prt.paper import A4

KIND = "colouring page"
MARGIN = A4.mm(12)  # white round the frame
FRAME = 4  # px: the line round the art, flush against it (no white between) so the outlines meet it
SMOOTH = 5  # median filter size: wipes out texture (grass, fabric, skin pores) before the tones are cut
SMOOTH_PASSES = 2
TONES = 4  # flat tones the photo is cut into: more gives more, smaller shapes
TIDY = 5  # mode filter size: merges the specks left at the edges of the tones into their neighbours
LINE = 3  # min filter size: thickens the one-pixel outlines to a line a crayon can follow


def page_size(photo: Image.Image) -> tuple[int, int]:
    """A4 upright for a portrait (or square) photo, on its side for a landscape one."""
    return A4.landscape_size if photo.width > photo.height else A4.size


def _fit(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """``size`` scaled to fit inside ``box`` whole, keeping its shape; never below 1 px a side."""
    w, h = size
    scale = min(box[0] / w, box[1] / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def line_art(photo: Image.Image, size: tuple[int, int]) -> Image.Image:
    """``photo`` as black outlines on white, ``size`` px: an "L" image of pure 0 (line) and 255 (paper)."""
    grey = ImageOps.grayscale(photo)
    # The tones are found on a half-size copy: smoothing costs a quarter as much, and the shapes come out rounder.
    work = grey.resize((max(1, size[0] // 2), max(1, size[1] // 2)), Image.Resampling.LANCZOS)
    for _ in range(SMOOTH_PASSES):
        work = work.filter(ImageFilter.MedianFilter(SMOOTH))
    work = ImageOps.autocontrast(work, cutoff=1)
    step = 255 // (TONES - 1)
    work = work.point(lambda v: min(TONES - 1, v * TONES // 256) * step)
    work = work.filter(ImageFilter.ModeFilter(TIDY))
    regions = work.resize(size, Image.Resampling.NEAREST)
    edges = regions.filter(ImageFilter.FIND_EDGES)
    # The edge filter leaves the image's outermost pixels as they were, which would read as a line all round the
    # art; they are cleared (the frame drawn round the art closes the shapes instead). The outlines still reach the
    # outermost pixels where they meet the edge: the min filter below spreads them one pixel into the cleared ring.
    edges = ImageOps.expand(ImageOps.crop(edges, 1), 1, 0) if min(size) > 2 else Image.new("L", size, 0)
    return edges.point(lambda v: 0 if v else 255).filter(ImageFilter.MinFilter(LINE))


def compose(photo: Image.Image) -> tuple[Image.Image, Cuts]:
    """The colouring page of ``photo``: its line art fitted whole and centred in a frame on A4. Also returns the
    fields cut to fit (never any: the page has no text)."""
    width, height = page_size(photo)
    inner = (width - 2 * (MARGIN + FRAME), height - 2 * (MARGIN + FRAME))
    art_w, art_h = _fit(photo.size, inner)
    art = line_art(photo, (art_w, art_h))
    page = Image.new("L", (width, height), 255)
    x0, y0 = (width - art_w) // 2, (height - art_h) // 2
    page.paste(art, (x0, y0))
    ImageDraw.Draw(page).rectangle(
        (x0 - FRAME, y0 - FRAME, x0 + art_w - 1 + FRAME, y0 + art_h - 1 + FRAME), outline=0, width=FRAME
    )
    return page.convert("RGB"), {}
