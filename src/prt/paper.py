"""The sheets prt lays pages out on, and how each one is sent to the printer.

A :class:`Paper` ties a page's pixel size to what the printer is asked for: the media size, the input tray and the
media type. A4 from the main tray is what booth, card, comic, colour-in, article, gazette and compose print on; the
4×6 photo paper from the photo tray is what ``prt strip``, ``prt instant``, ``prt recipe`` and ``prt postcard`` print
on, and a
printable CD/DVD in the disc tray is what ``prt disc`` prints on. Another size (an A6 card) is one more
:class:`Paper`: the layout draws on ``paper.size``, :func:`prt.render.send_pages` sends with ``paper.options()`` and
:func:`prt.guard.estimate` scales the ink by ``paper.ink_scale``. ``paper.duplex`` says whether a two-sided page
(``prt postcard``) may be left to the printer's own duplexer.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prt.models import PrintOptions

if TYPE_CHECKING:
    from PIL import Image

MM_PER_INCH = 25.4
A4_AREA_MM2 = 210 * 297  # the page a cartridge's rated yield is counted in


@dataclass(frozen=True)
class Paper:
    """One sheet: its size, the printer media and tray it is printed on, and the resolution pages are drawn at."""

    name: str  # what prt calls it, e.g. "4x6"
    label: str  # what a person reads, e.g. "4×6"
    width_mm: float  # upright: the short side
    height_mm: float
    media: str  # `prt print --media` name or PWG keyword (see media.ALIASES)
    tray: str | None = None  # media-source; None lets the printer pick, as `prt print` does
    media_type: str | None = None  # e.g. "photographic"; None lets the printer pick
    dpi: int = 150
    # Whether a printer that reports two-sided printing can turn this sheet over itself. Inkjet duplexers take
    # plain paper from the main tray, not photo or card stock from the photo tray, which is turned by hand.
    duplex: bool = False
    # What a prompt tells the person to load, when "<label> paper in the <tray> tray" does not fit (a disc).
    load: str | None = None

    @property
    def size(self) -> tuple[int, int]:
        """The upright page in pixels at ``dpi``."""
        return (round(self.width_mm / MM_PER_INCH * self.dpi), round(self.height_mm / MM_PER_INCH * self.dpi))

    @property
    def landscape_size(self) -> tuple[int, int]:
        """The page on its side in pixels at ``dpi``."""
        width, height = self.size
        return height, width

    @property
    def ink_scale(self) -> float:
        """How much of an A4 page one sheet of this paper is, for the ink estimate.

        The estimate averages the ink over every pixel of the page, white ones included, so a page that prints only
        part of its sheet (a disc label, white outside the disc and over its hub) is counted by the part it prints.
        """
        return self.width_mm * self.height_mm / A4_AREA_MM2

    @property
    def loaded(self) -> str:
        """Where the paper goes, for a prompt: e.g. "4×6 paper in the photo tray"."""
        if self.load:
            return self.load
        return f"{self.label} paper in the {self.tray} tray" if self.tray else f"{self.label} paper"

    def mm(self, length: float) -> int:
        """``length`` millimetres in pixels at ``dpi``."""
        return round(length / MM_PER_INCH * self.dpi)

    def options(self, job_name: str) -> PrintOptions:
        """What one page is submitted with: this paper's media size, tray and media type."""
        return PrintOptions(job_name=job_name, media=self.media, tray=self.tray, media_type=self.media_type)

    def for_printer(self, page: Image.Image) -> Image.Image:
        """The page as the printer takes it, upright: a page laid out on its side is turned a quarter turn
        counter-clockwise (IPP's "landscape"), so it reads once the sheet is turned clockwise."""
        from PIL import Image

        return page.transpose(Image.Transpose.ROTATE_90) if page.width > page.height else page

    def to_bytes(self, page: Image.Image, fmt: str) -> bytes:
        """The page as a ``pdf`` (one page of this size), ``png`` or ``jpeg`` file, tagged with ``dpi``."""
        buf = io.BytesIO()
        if fmt == "jpeg":
            page.save(buf, "JPEG", quality=92, dpi=(self.dpi, self.dpi))
        elif fmt == "pdf":
            page.save(buf, "PDF", resolution=self.dpi, quality=92)
        elif fmt == "png":
            page.save(buf, "PNG", dpi=(self.dpi, self.dpi))
        else:
            raise ValueError(f"unknown page format {fmt!r}")
        return buf.getvalue()

    def to_jpeg(self, page: Image.Image) -> bytes:
        """The bytes that are printed: the page upright, as a JPEG."""
        return self.to_bytes(self.for_printer(page), "jpeg")

    def to_dict(self) -> dict[str, object]:
        """What a command's ``-f json`` line says about the paper."""
        return {"size": self.name, "media": self.media, "tray": self.tray}


A4 = Paper("a4", "A4", 210, 297, "a4", duplex=True)
# 4×6 in photo paper in the photo tray, drawn at 300 dpi: a photo print is looked at from close up.
PHOTO_4X6 = Paper("4x6", "4×6", 101.6, 152.4, "4x6", tray="photo", media_type="photographic", dpi=300)

# A printable 12 cm CD/DVD in the disc tray, drawn at 300 dpi on the 120 mm square around it (see prt.disc).
DISC = Paper(
    "disc",
    "CD/DVD",
    120,
    120,
    "disc",
    tray="disc",
    media_type="disc",
    dpi=300,
    load="a printable CD/DVD in the disc tray, printable side up",
)

PAPERS = {paper.name: paper for paper in (A4, PHOTO_4X6, DISC)}
