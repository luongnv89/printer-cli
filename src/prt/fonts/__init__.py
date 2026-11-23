"""Typefaces bundled for the vintage gazette (see ``prt.vintage``), each under the SIL Open Font License 1.1.

UnifrakturMaguntia (blackletter masthead), Playfair Display (headlines; one variable font, set to a weight
per role) and Old Standard TT (body, bold and italic). The licence texts sit next to the fonts as
``OFL-*.txt``.
"""

from __future__ import annotations

import io
from functools import lru_cache
from importlib import resources

from PIL import ImageFont

from prt.drivers.base import PrinterError

# role -> (file, named weight of the variable font or None)
ROLES: dict[str, tuple[str, str | None]] = {
    "masthead": ("UnifrakturMaguntia-Book.ttf", None),
    "display-bold": ("PlayfairDisplay-Variable.ttf", "Bold"),
    "display-black": ("PlayfairDisplay-Variable.ttf", "Black"),
    "body": ("OldStandard-Regular.ttf", None),
    "body-bold": ("OldStandard-Bold.ttf", None),
    "body-italic": ("OldStandard-Italic.ttf", None),
}


@lru_cache(maxsize=len({file for file, _ in ROLES.values()}))
def _data(file: str) -> bytes:
    try:
        return resources.files(__name__).joinpath(file).read_bytes()
    except OSError as exc:
        raise PrinterError(f"bundled font missing: {file}", hint="reinstall printer-cli") from exc


@lru_cache(maxsize=256)
def face(role: str, size: int) -> ImageFont.FreeTypeFont:
    """The bundled font for ``role`` at ``size`` px, never a fallback.

    Each (role, size) gets its own object: the variable Playfair font is set to a weight in place, so one
    object is never shared between two weights. Basic layout keeps the output the same with or without raqm.
    """
    file, weight = ROLES[role]
    try:
        font = ImageFont.truetype(io.BytesIO(_data(file)), size, layout_engine=ImageFont.Layout.BASIC)
        if weight:
            font.set_variation_by_name(weight)
    except OSError as exc:
        raise PrinterError(f"bundled font missing: {file} ({exc})", hint="reinstall printer-cli") from exc
    return font
