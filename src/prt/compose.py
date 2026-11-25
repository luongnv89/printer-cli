"""``prt compose``: a page made of blocks (heading, text, image, QR code, columns, rule), for printables no other
command makes.

A page file is JSON: ``{"title": "...", "blocks": [{"type": "heading", "text": "..."}, ...]}``. Its JSON Schema
is built here by :func:`page_schema` (``prt schema page``), and :func:`check` validates a page file against that
same schema, so the schema and the checks cannot drift apart: a keyword the schema uses and :func:`check` does
not know fails the tests. After the schema, :func:`parse` checks what a schema cannot: that the page font can
print the text and that every image file can be read.

The blocks are laid out top to bottom on A4 at 150 dpi, like the other pages, and run on to a new page when one
is full (up to ``MAX_PAGES``): nothing is cut. A heading is kept with what follows it; a text block may break
between lines; any other block moves to the next page whole. A heading or ``columns`` block taller than a page,
and a page file with nothing to print (empty columns, or text the page font prints as nothing), are refused
rather than printed cut short or blank.

Image paths come from the page file, so they are untrusted: only local files are read (a web link is refused,
nothing is downloaded), only regular files of the photo formats, at most ``MAX_IMAGE_BYTES`` and
``MAX_IMAGE_PIXELS`` each and ``MAX_IMAGES`` per page. A relative path is read from the page file's folder (the
current folder for a page on stdin).
"""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn, cast

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from prt.drivers.base import PrinterError
from prt.layout import BOTTOM, CONTENT_W, _clean, _col_width, _glyphs, _lead, _printable, _wrap
from prt.testpage import H, MARGIN, W, _font

# --- limits -----------------------------------------------------------------------------------------

MAX_PAGE_FILE = 256 * 1024  # bytes of JSON
MAX_PAGES = 10  # a page file that needs more is refused, never cut short
MAX_BLOCKS = 200  # top-level blocks
MAX_CELL_BLOCKS = 50  # blocks in one column of a columns block
MIN_COLUMNS, MAX_COLUMNS = 2, 4
MAX_TITLE = 120
MAX_HEADING = 300
MAX_TEXT = 20000
MAX_CAPTION = 300
MAX_QR_DATA = 500  # characters; even 4-byte UTF-8 characters then fit a QR code (version 40, level M: 2331 bytes)
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000  # checked before an image is decoded
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF")  # Pillow decoders an image may use; no others
IMAGE_MM = (10, 260)  # an image's height, in mm
QR_MM = (15, 150)  # a QR code's width, in mm
QR_DEFAULT_MM = 40

# --- page geometry ------------------------------------------------------------------------------------

TOP = MARGIN
CONTENT_H = BOTTOM - TOP
PX_PER_MM = 150 / 25.4
GAP = 28  # between two blocks
GUTTER = 40  # between two columns
CAPTION_GAP = 10  # between an image or QR code and its caption
IMAGE_DEFAULT_H = CONTENT_H * 2 // 3  # tallest an image is drawn when the page file gives no height
QR_QUIET = 4  # modules of white around a QR code, as the standard asks
QR_MIN_MODULE = 2  # px: a QR code printed with smaller modules does not scan
HEADING_SIZES = {1: 64, 2: 46, 3: 36}
TEXT_SIZES = {"small": 22, "normal": 28, "large": 36}
CAPTION_SIZE = 22
RULE_PX = {"thin": 2, "thick": 6}
INK = (20, 20, 20)
GREY = (90, 90, 90)

BLOCK_TYPES = ("heading", "text", "image", "qr", "columns", "rule")
ALIGN = ("left", "center", "right")
NON_BLANK = r"\S"
NO_LINK = r"^(?![A-Za-z][A-Za-z0-9+.-]*://)"  # an image path is a file, never a link to download
SCHEMA_HINT = "see `prt schema page` for what a page file holds"

# --- the schema ---------------------------------------------------------------------------------------


def _string(description: str, max_length: int, non_blank: bool = True) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "description": description, "maxLength": max_length}
    if non_blank:
        prop.update(minLength=1, pattern=NON_BLANK)
    return prop


def _align(default: str) -> dict[str, Any]:
    return {"enum": list(ALIGN), "description": f"Where the block sits across its width; default {default}"}


def _caption() -> dict[str, Any]:
    return _string("Small grey line under it", MAX_CAPTION, non_blank=False)


def _block(kind: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {"type": {"const": kind}, **properties},
        "required": ["type", *required],
        "additionalProperties": False,
    }


def _leaf_blocks() -> list[dict[str, Any]]:
    """The schema of every block except ``columns``, which holds these."""
    return [
        _block(
            "heading",
            "A heading, in bold type",
            {
                "text": _string("The heading", MAX_HEADING),
                "level": {"enum": list(HEADING_SIZES), "description": "1 (largest, the default), 2 or 3"},
                "align": _align("left"),
            },
            ("text",),
        ),
        _block(
            "text",
            "Paragraphs of text; a newline starts a new paragraph. Long text runs on to the next page",
            {
                "text": _string("The text", MAX_TEXT),
                "size": {"enum": list(TEXT_SIZES), "description": "Type size: small, normal (the default) or large"},
                "align": _align("left"),
            },
            ("text",),
        ),
        _block(
            "image",
            "A picture from a local file: JPEG, PNG, WebP, GIF, BMP or TIFF, as wide as fits",
            {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": NO_LINK,
                    "description": "Path of the image file; a relative path is read from the page file's folder "
                    "(the current folder for a page on stdin). Web links are refused",
                },
                "height": {
                    "type": "number",
                    "minimum": IMAGE_MM[0],
                    "maximum": IMAGE_MM[1],
                    "description": "Most it may be tall, in mm; by default up to two thirds of the page",
                },
                "caption": _caption(),
                "align": _align("center"),
            },
            ("path",),
        ),
        _block(
            "qr",
            "A QR code",
            {
                "data": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QR_DATA,
                    "description": "What the code holds, e.g. a link or a Wi-Fi network",
                },
                "size": {
                    "type": "number",
                    "minimum": QR_MM[0],
                    "maximum": QR_MM[1],
                    "description": f"Width of the code in mm; default {QR_DEFAULT_MM}",
                },
                "caption": _caption(),
                "align": _align("center"),
            },
            ("data",),
        ),
        _block(
            "rule",
            "A horizontal line across the page or column",
            {"weight": {"enum": list(RULE_PX), "description": "thin (the default) or thick"}},
        ),
    ]


def page_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a page file; :func:`check` validates against exactly this."""
    leaves = _leaf_blocks()
    columns = _block(
        "columns",
        "Blocks side by side: one list of blocks per column. A columns block must fit on one page",
        {
            "columns": {
                "type": "array",
                "minItems": MIN_COLUMNS,
                "maxItems": MAX_COLUMNS,
                "description": f"{MIN_COLUMNS} to {MAX_COLUMNS} columns, each a list of blocks (any block but columns)",
                "items": {"type": "array", "maxItems": MAX_CELL_BLOCKS, "items": {"oneOf": leaves}},
            }
        },
        ("columns",),
    )
    blocks = [*leaves[:4], columns, leaves[4]]  # in BLOCK_TYPES order
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "prt compose page",
        "description": "A page file for `prt compose` (a file, or - for stdin), at most "
        f"{MAX_PAGE_FILE // 1024} KiB: blocks laid out top to bottom on A4, running on to more pages "
        f"as needed, up to {MAX_PAGES}. Lengths count characters. Text is printed in Latin letters: "
        "accents are printed without their marks, and characters the page font cannot print, such "
        "as emoji or Chinese, are refused. Image files must exist and be readable, at most "
        f"{MAX_IMAGE_BYTES // (1024 * 1024)} MiB and {MAX_IMAGE_PIXELS // 1_000_000} megapixels "
        f"each, and at most {MAX_IMAGES} images per page file.",
        "type": "object",
        "properties": {
            "title": _string(
                "Name of the print job, not printed; by default the first heading", MAX_TITLE, non_blank=False
            ),
            "blocks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BLOCKS,
                "description": "The blocks, top to bottom",
                "items": {"oneOf": blocks},
            },
        },
        "required": ["blocks"],
        "additionalProperties": False,
        "examples": [
            {
                "title": "Garden party",
                "blocks": [
                    {"type": "heading", "text": "Garden Party", "align": "center"},
                    {
                        "type": "text",
                        "text": "Saturday from 3 pm, at number 12.\nBring something to share.",
                        "align": "center",
                        "size": "large",
                    },
                    {"type": "rule"},
                    {
                        "type": "columns",
                        "columns": [
                            [
                                {"type": "heading", "text": "Menu", "level": 2},
                                {"type": "text", "text": "Lemonade\nStrawberry cake\nGrilled corn"},
                            ],
                            [{"type": "qr", "data": "https://example.com/rsvp", "size": 35, "caption": "Reply here"}],
                        ],
                    },
                ],
            }
        ],
    }


# --- checking a page file against the schema ---------------------------------------------------------

# Every keyword the page schema may use. A new one must be checked in _problem first: the tests compare this
# checker with a full JSON Schema validator over many page files.
KEYWORDS = frozenset(
    {
        "$schema",
        "title",
        "description",
        "examples",
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "oneOf",
    }
)
_TYPE_NAMES = {"object": "an object", "array": "a list", "string": "text", "number": "a number"}
_PATTERN_PROBLEMS = {
    NON_BLANK: "has only spaces in it",
    NO_LINK: "is a web link: give the path of a local image file (nothing is downloaded)",
}


def _is_type(value: Any, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"object": dict, "array": list, "string": str}[kind])


def _equal(a: Any, b: Any) -> bool:
    """JSON equality: true and 1 differ, 1 and 1.0 do not."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if _is_type(a, "number") and _is_type(b, "number"):
        return a == b
    return type(a) is type(b) and a == b


def _shown(value: Any) -> str:
    """A value as an error message shows it. A list or object is only named: printing it could recurse as deep
    as the page file nests it."""
    if isinstance(value, (list, dict)):
        return _TYPE_NAMES["array" if isinstance(value, list) else "object"]
    return json.dumps(value, ensure_ascii=False)


def _problem(schema: dict[str, Any], value: Any, where: str) -> str | None:
    """The first way ``value`` breaks ``schema`` (said of ``where``, e.g. ``blocks[2].text``), or None."""
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"the page schema uses {', '.join(sorted(unknown))}, which compose.check does not check")
    if "oneOf" in schema:
        options = schema["oneOf"]
        tags = [o.get("properties", {}).get("type", {}).get("const") for o in options]
        if all(isinstance(t, str) for t in tags) and len(set(tags)) == len(tags):
            # Blocks: the alternatives differ in their "type" constant, so the type picks the only one that can match.
            if not isinstance(value, dict):
                return f'{where} must be a block, e.g. {{"type": "text", "text": "..."}}'
            if "type" not in value:
                return f"{where} has no type (one of {', '.join(tags)})"
            kind = value["type"]
            if not isinstance(kind, str) or kind not in tags:
                return f"{where} has an unknown type {_shown(kind)} (use one of {', '.join(tags)})"
            problem = _problem(options[tags.index(kind)], value, where)
            if problem:
                return problem
        elif sum(_problem(o, value, where) is None for o in options) != 1:
            return f"{where} does not match exactly one of its allowed forms"
    kind = schema.get("type")
    if kind is not None and not _is_type(value, kind):
        return f"{where} must be {_TYPE_NAMES[kind]}, not {_shown(value)[:40]}"
    if "const" in schema and not _equal(value, schema["const"]):
        return f"{where} must be {_shown(schema['const'])}"
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        return f"{where} must be one of {', '.join(map(_shown, schema['enum']))}, not {_shown(value)[:40]}"
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{where} is empty"
        if len(value) > schema.get("maxLength", math.inf):
            return f"{where} is too long ({len(value)} characters, max {schema['maxLength']})"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{where} {_PATTERN_PROBLEMS.get(schema['pattern'], 'is not in the expected form')}"
    if _is_type(value, "number"):
        if value < schema.get("minimum", -math.inf):
            return f"{where} is {value}, less than {schema['minimum']}"
        if value > schema.get("maximum", math.inf):
            return f"{where} is {value}, more than {schema['maximum']}"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{where} needs at least {schema['minItems']} item{'s' if schema['minItems'] != 1 else ''}"
        if len(value) > schema.get("maxItems", math.inf):
            return f"{where} has {len(value)} items, max {schema['maxItems']}"
        if "items" in schema:
            for i, item in enumerate(value):
                problem = _problem(schema["items"], item, f"{where}[{i}]")
                if problem:
                    return problem
    if isinstance(value, dict):
        prefix = f"{where}." if where else ""
        for name in schema.get("required", ()):
            if name not in value:
                return f"{prefix}{name} is missing"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False:
            extra = [name for name in value if name not in properties]
            if extra:
                return (
                    f"{where or 'the page'} has unknown field{'s' if len(extra) != 1 else ''} "
                    f"{', '.join(map(_shown, extra))} (allowed: {', '.join(properties)})"
                )
        for name, sub in properties.items():
            if name in value:
                problem = _problem(sub, value[name], f"{prefix}{name}")
                if problem:
                    return problem
    return None


def problem(data: Any) -> str | None:
    """The first way ``data`` (a parsed page file) breaks the page schema, or None when it passes."""
    if not isinstance(data, dict):
        return 'the page must be a JSON object, e.g. {"blocks": [...]}'
    return _problem(page_schema(), data, "")


def check(data: Any, name: str = "the page file") -> None:
    """Refuse (exit code 3) a page file that does not pass the page schema."""
    found = problem(data)
    if found:
        raise PrinterError(f"{name}: {found}", hint=SCHEMA_HINT, exit_code=3)


# --- reading a page file ---------------------------------------------------------------------------


@dataclass
class Page:
    """A checked page file: its blocks, with every image already decoded (block ``"image"`` key)."""

    title: str
    blocks: list[dict[str, Any]]
    inputs: list[Path] = field(default_factory=list)  # the files it was read from: never overwritten by output


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def load(path: Path) -> Page:
    """Read and check a page file (``-`` reads stdin); refuses (exit code 3) anything that will not print."""
    from prt.booth import STDIN, read_stdin

    if str(path) == STDIN:
        name, base, inputs = "page on stdin", Path.cwd(), []
        content = read_stdin(MAX_PAGE_FILE)
        too_big = len(content) > MAX_PAGE_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"page file not found: {path}", hint="check the path", exit_code=3)
        name, base, inputs = f"page file {path.name}", path.parent, [path]
        too_big = path.stat().st_size > MAX_PAGE_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(
            f"{name} is larger than {MAX_PAGE_FILE // 1024} KiB",
            hint="put long text in fewer, shorter blocks",
            exit_code=3,
        )
    page = parse(content, name, base)
    page.inputs[:0] = inputs
    return page


def parse(content: bytes, name: str = "the page file", base: Path | None = None) -> Page:
    """A page from its JSON bytes, checked against the schema, the page font and its image files.

    ``base`` is the folder relative image paths are read from (default: the current folder).
    """
    try:
        data = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    except RecursionError as exc:
        raise PrinterError(f"{name} is not valid JSON: nested too deeply", hint=SCHEMA_HINT, exit_code=3) from exc
    except json.JSONDecodeError as exc:
        raise PrinterError(
            f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=SCHEMA_HINT, exit_code=3
        ) from exc
    except ValueError as exc:  # NaN or Infinity
        raise PrinterError(f"{name} is not valid JSON: {exc}", hint=SCHEMA_HINT, exit_code=3) from exc
    try:
        check(data, name)
    except RecursionError as exc:  # json.loads took it, but checking it nests deeper still
        raise PrinterError(f"{name} is not valid JSON: nested too deeply", hint=SCHEMA_HINT, exit_code=3) from exc
    _check_text(data, "", name)
    base = Path.cwd() if base is None else base
    count = len({_identity(path) for path in _image_paths(data["blocks"], base)})
    if count > MAX_IMAGES:  # before any image is decoded
        _too_many_images(name, count)
    images: dict[object, tuple[Path, Image.Image]] = {}
    blocks = [_ready(block, f"blocks[{i}]", base, images, name) for i, block in enumerate(data["blocks"])]
    title = " ".join(data.get("title", "").split()) or _first_heading(blocks) or "page"
    return Page(title, blocks, [path for path, _ in images.values()])


def _check_text(value: Any, where: str, name: str) -> None:
    """Refuse (exit code 3) a string that is not text: a lone UTF-16 surrogate (``"\\ud800"`` in JSON) passes the
    schema but cannot be encoded, so a QR code, a job title or a file name could not use it. ``value`` has passed
    the schema, so this nests only as deep as the schema does."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PrinterError(
                f"{name}: {where or 'the page'} is not valid text", hint=SCHEMA_HINT, exit_code=3
            ) from exc
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_text(item, f"{where}[{i}]", name)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_text(item, f"{where}.{key}" if where else key, name)


def _too_many_images(name: str, count: int) -> NoReturn:
    raise PrinterError(f"{name} has {count} images, max {MAX_IMAGES}", hint="split it into several pages", exit_code=3)


def _image_path(block: dict[str, Any], base: Path) -> Path:
    path = Path(block["path"])
    return path if path.is_absolute() else base / path


def _image_paths(blocks: list[dict[str, Any]], base: Path) -> list[Path]:
    """The path of every image block in ``blocks``, columns included, in order."""
    paths = []
    for block in blocks:
        if block["type"] == "image":
            paths.append(_image_path(block, base))
        elif block["type"] == "columns":
            paths += [p for cell in block["columns"] for p in _image_paths(cell, base)]
    return paths


def _identity(path: Path) -> object:
    """What one image file is, however its path is spelled (``a.jpg``, ``./a.jpg``, ``sub/../a.jpg``, a link
    to it): its device and inode. A path that names no regular file is its own identity."""
    try:
        info = path.stat()
    except (OSError, ValueError):
        return path
    return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else path


def _first_heading(blocks: list[dict[str, Any]]) -> str:
    return next((b["text"] for b in blocks if b["type"] == "heading" and b["text"]), "")


def _printable_text(text: str, where: str, name: str, keep_newlines: bool = False) -> str:
    value = _clean(text, keep_newlines)
    unprintable = sorted({c for c in value if _glyphs(c) is None})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(
            f"{name}: {where} has characters the page font cannot print: {shown}",
            hint="write it in Latin letters (accents are printed without their marks)",
            exit_code=3,
        )
    return value


def _ready(
    block: dict[str, Any], where: str, base: Path, images: dict[object, tuple[Path, Image.Image]], name: str
) -> dict[str, Any]:
    """A checked block with its text cleaned and its image decoded (once per file, in ``images``: identity to
    the first path naming it and its picture)."""
    ready = dict(block)
    kind = block["type"]
    if kind in ("heading", "text"):
        ready["text"] = _printable_text(block["text"], f"{where}.text", name, keep_newlines=kind == "text")
    if "caption" in block:
        ready["caption"] = _printable_text(block["caption"], f"{where}.caption", name)
    if kind == "image":
        path = _image_path(block, base)
        key = _identity(path)
        if key not in images:
            if len(images) >= MAX_IMAGES:  # a file changed since it was counted: never decode past the cap
                _too_many_images(name, len(images) + 1)
            images[key] = (path, load_image(path, f"{name}: {where}"))
        ready["image"] = images[key][1]
    if kind == "columns":
        ready["columns"] = [
            [_ready(b, f"{where}.columns[{c}][{i}]", base, images, name) for i, b in enumerate(cell)]
            for c, cell in enumerate(block["columns"])
        ]
    return ready


IMAGE_HINT = "use a JPEG, PNG, WebP, GIF, BMP or TIFF file"


def load_image(path: Path, where: str) -> Image.Image:
    """Decode an image file named by a page file, refusing (exit code 3) anything but a modest local image.

    Kept at most as large as it can print, so many images never sit in memory at full size.
    """
    if not path.is_file():
        raise PrinterError(
            f"{where}: image not found: {path}",
            hint="check the path; a relative path is read from the page file's folder",
            exit_code=3,
        )
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise PrinterError(
            f"{where}: image {path.name} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MiB",
            hint="use a smaller copy of the image",
            exit_code=3,
        )
    img: Image.Image  # Image.open gives an ImageFile; the steps below give plain Images
    try:
        with Image.open(path, formats=IMAGE_FORMATS) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise PrinterError(
                    f"{where}: image {path.name} is {img.width}x{img.height} pixels, more than "
                    f"{MAX_IMAGE_PIXELS // 1_000_000} megapixels",
                    hint="use a smaller copy of the image",
                    exit_code=3,
                )
            img.draft("RGB", (CONTENT_W, CONTENT_H))  # JPEG: decode at a reduced size when that is enough
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.has_transparency_data:  # see-through parts print as paper
                img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img.convert("RGBA"))
            img = img.convert("RGB")
    except PrinterError:
        raise
    except UnidentifiedImageError as exc:
        raise PrinterError(
            f"{where}: cannot read image {path.name}: not a supported image format", hint=IMAGE_HINT, exit_code=3
        ) from exc
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise PrinterError(f"{where}: cannot read image {path.name}: {exc}", hint=IMAGE_HINT, exit_code=3) from exc
    img.thumbnail((CONTENT_W, CONTENT_H))
    return img


# --- layout -------------------------------------------------------------------------------------------

Draw = Callable[[Image.Image, ImageDraw.ImageDraw, int, int], None]  # (page, draw, x, y): draw at that top-left


@dataclass
class Piece:
    """What a block is made of on the page: a strip of ``height`` px that is never split across pages."""

    height: int
    draw: Draw
    keep_with_next: bool = False  # a heading: start a new page rather than end one with it
    space: bool = False  # the gap between two paragraphs: dropped at the top of a page
    ink: bool = True  # False when it leaves nothing on the paper (a gap, a columns block of empty cells)


def _x(align: str, width: int, used: float) -> float:
    return {"left": 0, "center": (width - used) / 2, "right": width - used}[align]


def _lines_piece(lines: list[str], size: int, width: int, align: str, fill, bold: int = 0, **flags) -> Piece:
    font = _font(size)

    def draw(page: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int) -> None:
        for n, line in enumerate(lines):
            d.text(
                (x + _x(align, width, font.getlength(line)), y + n * _lead(size)),
                line,
                font=font,
                fill=fill,
                stroke_width=bold,
                stroke_fill=fill,
            )

    return Piece(len(lines) * _lead(size), draw, **flags)


def _caption_lines(block: dict[str, Any], width: int) -> list[str]:
    return _wrap(_printable(block.get("caption", "")), _font(CAPTION_SIZE), width)


def _with_caption(
    size: tuple[int, int], picture: Callable[[], Image.Image], block: dict[str, Any], width: int
) -> Piece:
    """A picture ``size`` px large with its caption under it. The picture is made by ``picture()`` only when the
    piece is drawn, so measuring a page file that will be refused (too many pages) builds no bitmaps."""
    align = block.get("align", "center")
    caption = _caption_lines(block, width)
    below = _lines_piece(caption, CAPTION_SIZE, width, align, GREY) if caption else None

    def draw(page: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int) -> None:
        page.paste(picture(), (round(x + _x(align, width, size[0])), y))
        if below:
            below.draw(page, d, x, y + size[1] + CAPTION_GAP)

    return Piece(size[1] + (CAPTION_GAP + below.height if below else 0), draw)


def _image(block: dict[str, Any], width: int) -> Piece:
    img = block["image"]
    caption = _caption_lines(block, width)
    room = CONTENT_H - (CAPTION_GAP + len(caption) * _lead(CAPTION_SIZE) if caption else 0)
    tallest = min(room, round(block["height"] * PX_PER_MM) if "height" in block else IMAGE_DEFAULT_H)
    scale = min(width / img.width, tallest / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return _with_caption(size, lambda: img.resize(size, Image.Resampling.LANCZOS), block, width)


def _qr_code(data: str, width: int, where: str) -> tuple[Any, int]:
    """The QR code of ``data`` and the px per module that fit it in ``width`` with its quiet zone; refuses (exit
    code 3) a code whose modules would be too small to scan. Builds no bitmap."""
    import segno

    try:
        code = segno.make(data, error="m", micro=False)
    except segno.DataOverflowError as exc:
        raise PrinterError(f"{where}: too much data for a QR code", hint="shorten the data", exit_code=3) from exc
    scale = width // cast(int, code.symbol_size(scale=1, border=QR_QUIET)[0])  # an int at scale=1
    if scale < QR_MIN_MODULE:
        raise PrinterError(
            f"{where}: too much data for a QR code {width / PX_PER_MM:.0f} mm wide",
            hint="shorten the data, or give the code more room (a larger size, or fewer columns)",
            exit_code=3,
        )
    return code, scale


def _qr_bitmap(code: Any, scale: int) -> Image.Image:
    rows = [list(row) for row in code.matrix_iter(scale=1, border=QR_QUIET)]
    modules = len(rows)
    img = Image.new("L", (modules, modules), 255)
    img.putdata([0 if dark else 255 for row in rows for dark in row])
    return img.resize((modules * scale, modules * scale), Image.Resampling.NEAREST).convert("RGB")


def qr_image(data: str, width: int, where: str) -> Image.Image:
    """A QR code of ``data`` at most ``width`` px wide, with its quiet zone; refuses (exit code 3) a code whose
    modules would be too small to scan."""
    return _qr_bitmap(*_qr_code(data, width, where))


def _qr(block: dict[str, Any], width: int, where: str) -> Piece:
    wanted = round(block.get("size", QR_DEFAULT_MM) * PX_PER_MM)
    code, scale = _qr_code(block["data"], min(wanted, width), where)  # refused here, before anything is drawn
    side = code.symbol_size(scale=scale, border=QR_QUIET)[0]
    return _with_caption((side, side), lambda: _qr_bitmap(code, scale), block, width)


def _rule(block: dict[str, Any], width: int) -> Piece:
    thickness = RULE_PX[block.get("weight", "thin")]

    def draw(page: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int) -> None:
        d.rectangle([x, y, x + width - 1, y + thickness - 1], fill=INK)

    return Piece(thickness, draw)


def _text(block: dict[str, Any], width: int) -> list[Piece]:
    size = TEXT_SIZES[block.get("size", "normal")]
    font, align = _font(size), block.get("align", "left")
    pieces: list[Piece] = []
    for para in _printable(block["text"]).split("\n"):
        lines = [_lines_piece([line], size, width, align, INK) for line in _wrap(para, font, width)]
        if lines and pieces:  # a paragraph that prints nothing adds no gap either
            pieces.append(Piece(_lead(size) // 2, lambda *_: None, space=True, ink=False))
        pieces += lines
    return pieces


def _heading(block: dict[str, Any], width: int, where: str) -> list[Piece]:
    """A heading as one piece, never split across pages; refuses (exit code 3) one taller than a page, whose last
    lines would run off the sheet."""
    size = HEADING_SIZES[int(block.get("level", 1))]
    lines = _wrap(_printable(block["text"]), _font(size), width)
    if not lines:
        return []
    piece = _lines_piece(lines, size, width, block.get("align", "left"), INK, bold=1, keep_with_next=True)
    if piece.height > CONTENT_H:
        raise PrinterError(
            f"{where}: the heading is taller than a page",
            exit_code=3,
            hint="shorten the heading, or move part of it into a text block",
        )
    return [piece]


def _stack(blocks: list[dict[str, Any]], width: int, where: str) -> Iterator[tuple[int, Piece]]:
    """The pieces of ``blocks`` laid out at ``width``, each with the gap above it (GAP before each block but the
    first). A block is measured only when its pieces are asked for, so a caller can stop early. A piece that
    leaves nothing on the paper (a columns block of empty cells) is left out, so it can never start a page."""
    started = False
    for i, block in enumerate(blocks):
        pieces = [p for p in _pieces(block, width, f"{where}[{i}]") if p.ink or p.space]
        for n, piece in enumerate(pieces):
            yield (GAP if started and n == 0 else 0), piece
            started = True


def _columns(block: dict[str, Any], width: int, where: str) -> Piece:
    cells = block["columns"]
    cell_w = _col_width(width, len(cells), GUTTER)
    stacks: list[list[tuple[int, Piece]]] = []
    for c, cell in enumerate(cells):
        stack: list[tuple[int, Piece]] = []
        height = 0
        for gap, piece in _stack(cell, cell_w, f"{where}.columns[{c}]"):
            stack.append((gap, piece))
            height += gap + piece.height
            if height > CONTENT_H:  # stop measuring: it cannot print
                raise PrinterError(
                    f"{where}: the columns block is taller than a page",
                    exit_code=3,
                    hint="split it into several columns blocks, or move long text out of it",
                )
        stacks.append(stack)
    height = max((sum(gap + p.height for gap, p in stack) for stack in stacks), default=0)
    ink = any(piece.ink for stack in stacks for _, piece in stack)

    def draw(page: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int) -> None:
        for c, stack in enumerate(stacks):
            top = y
            for gap, piece in stack:
                top += gap
                piece.draw(page, d, x + c * (cell_w + GUTTER), top)
                top += piece.height

    return Piece(height, draw, ink=ink)


def _pieces(block: dict[str, Any], width: int, where: str) -> list[Piece]:
    kind = block["type"]
    if kind == "heading":
        return _heading(block, width, where)
    if kind == "text":
        return _text(block, width)
    if kind == "image":
        return [_image(block, width)]
    if kind == "qr":
        return [_qr(block, width, where)]
    if kind == "rule":
        return [_rule(block, width)]
    return [_columns(block, width, where)]


def layout(page: Page) -> list[list[tuple[int, Piece]]]:
    """Each page's pieces with the y they start at; refuses (exit code 3) a page file needing over MAX_PAGES,
    as soon as it does, without measuring the blocks after that, and one with nothing to print, which would
    come out as a blank sheet."""
    sheets: list[list[tuple[int, Piece]]] = [[]]
    seq = _stack(page.blocks, CONTENT_W, "blocks")
    y = TOP
    following = next(seq, None)
    while following is not None:
        (gap, piece), following = following, next(seq, None)
        need = gap + piece.height
        if piece.keep_with_next and following is not None:
            need += following[0] + following[1].height
        if sheets[-1] and y + need > BOTTOM:
            if len(sheets) == MAX_PAGES:
                raise PrinterError(
                    f"the page file needs more than {MAX_PAGES} pages",
                    hint="shorten the text or print it in several page files",
                    exit_code=3,
                )
            sheets.append([])
            y = TOP
        if not sheets[-1]:
            if piece.space:
                continue
            gap = 0
        y += gap
        sheets[-1].append((y, piece))
        y += piece.height
    if not sheets[0]:
        raise PrinterError(
            "the page file has nothing to print: its blocks leave the page blank",
            exit_code=3,
            hint="add text, an image, a QR code or a rule; empty columns and text the page font "
            "prints as nothing, such as a lone accent mark, do not count",
        )
    return sheets


def compose_pages(page: Page) -> list[Image.Image]:
    """The A4 pages (150 dpi) of a checked page file."""
    pages = []
    for sheet in layout(page):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        for y, piece in sheet:
            piece.draw(img, d, MARGIN, y)
        pages.append(img)
    return pages
