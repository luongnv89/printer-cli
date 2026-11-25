"""Turn PWG self-describing media names (RFC 5101 / PWG 5101.1) into human labels."""

from __future__ import annotations

FRIENDLY = {
    "a3": "A3",
    "a4": "A4",
    "a5": "A5",
    "a6": "A6",
    "b5": "B5",
    "letter": "Letter",
    "legal": "Legal",
    "govt-letter": "8×10",
    "index-4x6": "4×6",
    "index-5x8": "5×8",
    "5x7": "5×7",
    "photo-l": "L (3.5×5)",
    "hagaki": "Hagaki",
    "number-10": "#10 env",
    "dl": "DL env",
    "c6": "C6 env",
    "foolscap": "Foolscap",
    "hivision": "HiVision",
    "16k": "16K",
}

# User-facing shortcuts accepted by `prt print --media` and `--area`.
ALIASES = {
    "a4": "iso_a4_210x297mm",
    "a5": "iso_a5_148x210mm",
    "a6": "iso_a6_105x148mm",
    "letter": "na_letter_8.5x11in",
    "legal": "na_legal_8.5x14in",
    "4x6": "na_index-4x6_4x6in",
    "5x7": "na_5x7_5x7in",
    "8x10": "na_govt-letter_8x10in",
    "disc": "disc_12cm_18x120mm",
}

# Sizes in hundredths of a millimetre, used to match media-col-ready collections.
SIZES = {
    (21000, 29700): "A4",
    (14800, 21000): "A5",
    (10500, 14800): "A6",
    (21590, 27940): "Letter",
    (21590, 35560): "Legal",
    (10160, 15240): "4×6",
    (12700, 17780): "5×7",
    (20320, 25400): "8×10",
    (12000, 12000): "Disc",
    (8890, 12700): "L (3.5×5)",
}


def friendly(keyword: str | None) -> str | None:
    """'iso_a4_210x297mm' → 'A4'; unknown names fall back to their middle segment."""
    if not keyword:
        return None
    if keyword.startswith("disc_"):
        return "Disc"
    parts = keyword.split("_")
    if len(parts) >= 3:
        name = parts[1]
        return FRIENDLY.get(name, name.replace("-", " ").title())
    return keyword


def from_size(x: int | None, y: int | None) -> str | None:
    if x is None or y is None:
        return None
    return SIZES.get((x, y)) or SIZES.get((y, x)) or f"{x / 100:g}×{y / 100:g} mm"


def resolve_alias(name: str) -> str:
    return ALIASES.get(name.lower(), name)


def size_of(keyword: str) -> tuple[int, int] | None:
    """Media size in hundredths of mm, parsed from a PWG name like 'iso_a4_210x297mm'.

    A disc name gives the hub and outer diameters, not a width and height ('disc_12cm_18x120mm': an 18 mm hub on a
    120 mm disc), so a disc is the square around it, as printers report it in media-col-ready (120 x 120 mm).
    """
    import re

    if keyword.startswith("disc_"):
        m = re.search(r"x(\d+(?:\.\d+)?)mm$", keyword)
        if m:
            return round(float(m.group(1)) * 100), round(float(m.group(1)) * 100)
        return None
    m = re.search(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)$", keyword)
    if not m:
        return None
    x, y, unit = float(m.group(1)), float(m.group(2)), m.group(3)
    factor = 100 if unit == "mm" else 2540
    return round(x * factor), round(y * factor)
