"""Recipe cards: a recipe from the web, a saved page or a recipe file, printed as 4×6 index cards.

Sources: an http(s) link, fetched like ``prt article`` fetches one (the same link checks, redirect and size limits),
a saved HTML page, a recipe JSON file (see ``prt schema recipe``), a plain-text recipe, or ``-`` for stdin. From a
web page the recipe is read from its schema.org ``Recipe`` data (the JSON-LD most recipe sites publish for search
engines): the name, ingredients, steps, yield and times. A page without it is read with the article extractor:
the list under an "Ingredients" heading and the steps under an "Instructions", "Method", "Directions" or
"Steps" heading. A page with neither is refused (kind ``no_recipe``).

The card is a 6×4 in index card, laid out on its side on :data:`prt.paper.PHOTO_4X6` at 300 dpi: the title in
handwriting over a red rule, then the ingredients and the numbered steps on blue lines in two columns. The type
is set as large as lets the recipe fit on one card; a longer recipe runs on to more cards (up to ``MAX_CARDS``),
so nothing is ever cut. Each card is turned upright only for the printer.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from prt import article, fonts
from prt.booth import STDIN, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import _clean, _ellipsize, _wrap
from prt.paper import PHOTO_4X6
from prt.testpage import _font
from prt.vintage import _fit_face

# --- the recipe --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    title: str
    ingredients: tuple[str, ...]
    steps: tuple[str, ...]
    servings: str = ""  # e.g. "Serves 4"
    time: str = ""  # e.g. "Prep 15 min · Cook 30 min"
    source: str = ""  # site or book
    url: str = ""


FIELDS = ("title", "ingredients", "steps", "servings", "time", "source", "url")
REQUIRED = ("title", "ingredients", "steps")
LIMITS = {"title": 200, "servings": 60, "time": 80, "source": 120, "url": 500}
MAX_ITEMS = 100  # ingredients, and steps, in one recipe
MAX_ITEM = 1500  # characters in one ingredient or step
MAX_CARDS = 6  # a recipe that needs more is refused, never cut short
MAX_BYTES = article.MAX_BYTES
KIND = "recipe card"
SCHEMA_HINT = "save the recipe as a JSON file (see `prt schema recipe`) and print the file"
JSON_HINT = ('expected {"title": "...", "ingredients": ["...", "..."], "steps": ["...", "..."], "servings": "...", '
             '"time": "...", "source": "...", "url": "..."}')


def load(source: str, timeout: float) -> Recipe:
    """The recipe behind a link (``scheme://…``), on stdin (``-``) or in a local file, checked and normalised for
    the card."""
    if source == STDIN:
        return normalise(load_stdin())
    return normalise(from_link(source, timeout) if article._LINK.match(source) else load_file(Path(source)))


def _no_recipe(where: str) -> PrinterError:
    return PrinterError(f"no recipe found in {where}",
                        hint="the page has no recipe data and no Ingredients and Instructions headings; " + SCHEMA_HINT,
                        exit_code=3, kind="no_recipe")


def from_link(url: str, timeout: float) -> Recipe:
    article.check_link(url)
    page = article.fetch(url, timeout)
    host = article._site(page.url)
    if page.media_type in ("text/html", "application/xhtml+xml", ""):
        return from_html(article._decode(page.content, page.charset), page.url, host)
    if page.media_type == "text/plain":
        return from_text(article._decode(page.content, page.charset), source=host, url=page.url)
    raise PrinterError(f"cannot print {url}: it is not a web page ({page.media_type})",
                       hint="link to the recipe page itself, or " + SCHEMA_HINT, exit_code=3)


def load_file(path: Path) -> Recipe:
    """A recipe saved as JSON (.json, see ``JSON_HINT``), HTML (.html, .htm, .xhtml) or plain text (anything else)."""
    if not path.is_file():
        raise PrinterError(f"file not found: {path}",
                           hint="check the path; to fetch a web page give its full link, starting with https://",
                           exit_code=3)
    if path.stat().st_size > MAX_BYTES:
        raise PrinterError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MiB",
                           hint="save just the recipe", exit_code=3)
    suffix = path.suffix.lower()
    kind = "html" if suffix in (".html", ".htm", ".xhtml") else "json" if suffix == ".json" else "text"
    return parse(path.read_bytes(), kind, path.name)


def load_stdin() -> Recipe:
    """A recipe piped in on stdin: JSON when it starts with ``{``, HTML when it starts with ``<``, else text."""
    content = read_stdin(MAX_BYTES)
    if len(content) > MAX_BYTES:
        raise PrinterError(f"the recipe on stdin is larger than {MAX_BYTES // (1024 * 1024)} MiB",
                           hint="pipe in just the recipe", exit_code=3)
    start = content.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    kind = "json" if start == b"{" else "html" if start == b"<" else "text"
    return parse(content, kind, "stdin")


def parse(content: bytes, kind: str, name: str = "the recipe") -> Recipe:
    """A recipe from its bytes: ``kind`` is ``json``, ``html`` or ``text``; ``name`` says where it came from."""
    if kind == "html":
        return from_html(article._decode(content, None), where=name)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8", exit_code=3) from exc
    return parse_json(text, name) if kind == "json" else from_text(text, where=name)


def _items(value: str | list[str]) -> tuple[str, ...]:
    return tuple(value.split("\n") if isinstance(value, str) else value)


def parse_json(text: str, name: str = "the recipe") -> Recipe:
    """A recipe JSON file (see ``JSON_HINT``): ``ingredients`` and ``steps`` are lists of text, or one text with a
    newline between items."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=JSON_HINT,
                           exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=JSON_HINT, exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in {name}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    missing = [field for field in REQUIRED if field not in data]
    if missing:
        raise PrinterError(f"{name} is missing {' and '.join(missing)}", hint=JSON_HINT, exit_code=3)

    def is_list(value: Any) -> bool:
        return isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))
    wrong = sorted(field for field, value in data.items()
                   if not (is_list(value) if field in ("ingredients", "steps") else isinstance(value, str)))
    if wrong:
        raise PrinterError(f"fields of {name} must be text: {', '.join(wrong)}",
                           hint="ingredients and steps are lists of text (or one text, a newline between items)",
                           exit_code=3)
    return Recipe(title=data["title"], ingredients=_items(data["ingredients"]), steps=_items(data["steps"]),
                  servings=data.get("servings", ""), time=data.get("time", ""), source=data.get("source", ""),
                  url=data.get("url", ""))


# --- schema.org Recipe data ---------------------------------------------------------------------


class _LdJson(HTMLParser):
    """Collects the text of every <script type="application/ld+json"> on a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        kind = next((v or "" for k, v in attrs if k == "type"), "")
        if tag == "script" and kind.split(";")[0].strip().lower() == "application/ld+json":
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._buffer is not None:
            self.blocks.append("".join(self._buffer))
            self._buffer = None


MAX_DEPTH = 8  # how deep Recipe data is looked for inside other JSON-LD objects (@graph, mainEntity…)


def _walk(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > MAX_DEPTH:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, depth + 1)


def _types(obj: dict[str, Any]) -> set[str]:
    kind = obj.get("@type")
    return {str(k).rsplit("/", 1)[-1] for k in (kind if isinstance(kind, list) else [kind]) if k}


def recipe_data(page: str) -> dict[str, Any] | None:
    """The first schema.org Recipe object in the page's JSON-LD, or None."""
    parser = _LdJson()
    parser.feed(page)
    parser.close()
    for block in parser.blocks:
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue  # one broken block must not hide a good one
        for obj in _walk(data):
            if "Recipe" in _types(obj):
                return obj
    return None


_TAG = re.compile(r"<[^>]+>")


def _text(value: Any) -> str:
    """One line of text from a JSON-LD value: HTML entities decoded and tags dropped."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, (str, int, float))), "")
    if isinstance(value, dict):
        value = value.get("text") or value.get("name") or ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(_TAG.sub(" ", html.unescape(str(value))).split())


def _ingredients(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line for line in (_text(v) for v in value.split("\n")) if line]
    if isinstance(value, list):
        return [line for line in (_text(v) for v in value) if line]
    return []


def _instructions(value: Any, depth: int = 0) -> list[str]:
    """The steps of ``recipeInstructions``: a text (one step per line), a list of texts, HowToStep objects
    (``text``) or HowToSection / ItemList objects holding them (``itemListElement``)."""
    if depth > MAX_DEPTH:
        return []
    if isinstance(value, str):
        return [line for line in (_text(v) for v in re.split(r"\n|<br\s*/?>|</p>", value, flags=re.I)) if line]
    if isinstance(value, list):
        return [step for item in value for step in _instructions(item, depth + 1)]
    if isinstance(value, dict):
        if "itemListElement" in value:
            return _instructions(value["itemListElement"], depth + 1)
        step = _text(value.get("text") or value.get("name") or "")
        return [step] if step else []
    return []


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:\.\d+)?S)?)?$", re.I)


def duration(value: Any) -> str:
    """An ISO 8601 duration as people write it: "PT1H30M" → "1 h 30 min"; "" when it cannot be read."""
    m = _DURATION.match(_text(value))
    if not m or not any(m.groups()):
        return ""
    days, hours, minutes = (int(g or 0) for g in m.groups())
    hours += days * 24
    hours, minutes = hours + minutes // 60, minutes % 60
    parts = ([f"{hours} h"] if hours else []) + ([f"{minutes} min"] if minutes else [])
    return " ".join(parts)


def _servings(value: Any) -> str:
    texts = [_text(v) for v in (value if isinstance(value, list) else [value])]
    best = max((t for t in texts if t), key=len, default="")
    return f"Serves {best}" if best.isdigit() else best


def _times(obj: dict[str, Any]) -> str:
    prep, cook, total = (duration(obj.get(key)) for key in ("prepTime", "cookTime", "totalTime"))
    parts = ([f"Prep {prep}"] if prep else []) + ([f"Cook {cook}"] if cook else [])
    return " · ".join(parts) if parts else (f"Total {total}" if total else "")


def from_recipe_data(obj: dict[str, Any], title: str = "", source: str = "", url: str = "") -> Recipe:
    """A recipe from a schema.org Recipe object; ``title`` stands in for a missing name."""
    return Recipe(title=_text(obj.get("name")) or title, ingredients=tuple(_ingredients(
                      obj.get("recipeIngredient") if obj.get("recipeIngredient") is not None else obj.get("ingredients"))),
                  steps=tuple(_instructions(obj.get("recipeInstructions"))), servings=_servings(obj.get("recipeYield")),
                  time=_times(obj), source=source, url=url)


# --- pages without recipe data ------------------------------------------------------------------

_INGREDIENTS = re.compile(r"^\W*ingredients?\b", re.I)
_METHOD = re.compile(r"^\W*(?:instructions?|method|directions?|steps|preparation|how to make(?: it)?)\b", re.I)
HEADING_MAX = 40  # a line longer than this is never taken for a section heading
_BULLET = re.compile(r"^\s*(?:[-*•·‣–]|\d+[.)])\s+")


def _section(text: str) -> str | None:
    if len(text) > HEADING_MAX:
        return None
    return "ingredients" if _INGREDIENTS.match(text) else "steps" if _METHOD.match(text) else None


def _sections(lines: list[tuple[str, int]]) -> tuple[list[str], list[str]]:
    """Ingredients and steps from (text, heading level) lines in page order; level 0 is not a heading, 1-6 are
    h1-h6. Each section runs from its heading to the next heading of the same level or higher; a deeper heading
    inside it (e.g. "For the sauce") is left out."""
    found: dict[str, list[str]] = {"ingredients": [], "steps": []}
    current, level = None, 0
    for text, heading in lines:
        section = _section(text)
        if section and (heading or not current or section != current):
            current, level = section, heading or 7
            continue
        if heading and current and heading <= level:
            current = None
        if current and not heading:
            item = _BULLET.sub("", text).strip()
            if item:
                found[current].append(item)
    return found["ingredients"], found["steps"]


def from_html(page: str, url: str = "", host: str = "", where: str | None = None) -> Recipe:
    """The recipe on a web page: its schema.org Recipe data, else the lists under its Ingredients and
    Instructions headings (see ``_sections``)."""
    parser = article._Extractor()
    parser.feed(page)
    parser.close()
    meta = parser.meta
    title = (meta.get("og:title") or meta.get("twitter:title") or parser.page_title or parser.first_h1).strip()
    source = (meta.get("og:site_name") or meta.get("application-name") or host).strip()
    data = recipe_data(page)
    if data is not None:
        found = from_recipe_data(data, title, source, url)
        if found.ingredients and found.steps:
            return found
    lines = [(p.text, int(p.tag[1]) if re.fullmatch(r"h[1-6]", p.tag) else 0) for p in parser.paragraphs]
    ingredients, steps = _sections(lines)
    if not ingredients or not steps:
        raise _no_recipe(where or url or "the page")
    return Recipe(title=title or source, ingredients=tuple(ingredients), steps=tuple(steps), source=source, url=url)


def from_text(text: str, source: str = "", url: str = "", where: str | None = None) -> Recipe:
    """Plain text: the first line is the title, then an "Ingredients" line and a "Method" (or "Instructions",
    "Directions", "Steps") line, each followed by one item per line."""
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        raise _no_recipe(where or url or "the text")
    head, rest = lines[0], lines[1:]
    if _section(head):
        head, rest = "", lines
    ingredients, steps = _sections([(line, 0) for line in rest])
    if not ingredients or not steps:
        raise _no_recipe(where or url or "the text")
    return Recipe(title=head, ingredients=tuple(ingredients), steps=tuple(steps), source=source, url=url)


# --- normalising for the card ------------------------------------------------------------------


FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅙": "1/6", "⅛": "1/8",
             "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
_FRACTION = re.compile(r"(\d?)([" + "".join(FRACTIONS) + "])")


def _fractions(text: str) -> str:
    """Vulgar fractions as the card font prints them: "1½ cups" → "1 1/2 cups"."""
    return _FRACTION.sub(lambda m: (m[1] + " " if m[1] else "") + FRACTIONS[m[2]], text)


def normalise(recipe: Recipe) -> Recipe:
    """Clean whitespace, map the text onto the card font's glyphs and check the limits (exit code 3)."""
    def line(text: str) -> tuple[str, int]:
        printable, unknown = article._printable(_fractions(_clean(text, keep_newlines=False)))
        return " ".join(printable.split()), unknown

    fields = {}
    for name in ("title", "servings", "time", "source"):
        fields[name] = line(getattr(recipe, name))[0]
        if len(fields[name]) > LIMITS[name]:
            raise PrinterError(f"the recipe's {name} is too long ({len(fields[name])} characters, max "
                               f"{LIMITS[name]})", hint=f"shorten the {name}", exit_code=3)
    url = _clean(recipe.url, keep_newlines=False)
    if len(url) > LIMITS["url"]:
        url = ""  # a tracking-laden link is left off the card rather than refusing the recipe
    letters = unknown = 0
    lists = {}
    for name in ("ingredients", "steps"):
        items = [line(item) for item in getattr(recipe, name)]
        letters += sum(len(text.replace(" ", "")) for text, _ in items)
        unknown += sum(bad for _, bad in items)
        kept = [text for text, _ in items if text.strip("-? ")]
        if not kept:
            raise PrinterError(f"the recipe has no {name}", hint=f"give it at least one of its {name}", exit_code=3,
                               kind="no_recipe")
        if len(kept) > MAX_ITEMS:
            raise PrinterError(f"the recipe has {len(kept)} {name}, more than {MAX_ITEMS}",
                               hint="a recipe card holds one recipe; " + SCHEMA_HINT, exit_code=3)
        longest = max(len(text) for text in kept)
        if longest > MAX_ITEM:
            raise PrinterError(f"one of the recipe's {name} is {longest} characters long, more than {MAX_ITEM}",
                               hint="split it up; " + SCHEMA_HINT, exit_code=3)
        lists[name] = tuple(kept)
    if unknown * 2 > letters:
        raise PrinterError(f"the recipe is mostly in characters the card font cannot print ({unknown} of {letters})",
                           hint="the card font covers Latin letters only", exit_code=3)
    return Recipe(title=fields["title"] or fields["source"] or "Recipe", ingredients=lists["ingredients"],
                  steps=lists["steps"], servings=fields["servings"], time=fields["time"], source=fields["source"],
                  url=url)


# --- the cards ---------------------------------------------------------------------------------

W, H = PHOTO_4X6.landscape_size  # 1800 x 1200: a 6×4 in index card
MARGIN = 80
CARD = (255, 254, 249)
RED = (196, 48, 52)
BLUE = (186, 210, 234)
INK = (34, 34, 40)
GREY = (110, 110, 116)
PEN = (30, 38, 78)
TITLE_BASELINE = MARGIN + 96
META_TOP = MARGIN + 124
RULE_Y = MARGIN + 182
BODY_TOP = RULE_Y + 34
BODY_BOTTOM = H - MARGIN - 44
FOOTER_BASELINE = H - MARGIN + 6
GUTTER = 70
COL_W = (W - 2 * MARGIN - GUTTER) // 2
SIZES = (40, 36, 32)  # body type, largest first: the largest that fits the recipe on one card is used
RUN_ON_SIZE = 36  # the type of a recipe that needs more than one card anyway
HEADINGS = ("Ingredients", "Method")
BULLET = "•"  # drawn as a dot: the card font has no bullet


@dataclass(frozen=True)
class Row:
    kind: str  # head, item (first line of an ingredient or step), more (its next lines) or gap
    text: str = ""
    mark: str = ""  # the bullet or step number of an item's first line


def _lead(size: int) -> int:
    return round(size * 1.5)


def _indent(recipe: Recipe, font, size: int) -> int:
    return round(font.getlength(f"{len(recipe.steps)}.") + size * 0.5)


def rows(recipe: Recipe, size: int) -> list[Row]:
    """Every line of the recipe's body at ``size``, in reading order: the Ingredients heading and its items, a
    gap, the Method heading and the numbered steps."""
    font = _font(size)
    width = COL_W - _indent(recipe, font, size)
    out: list[Row] = []
    for heading, items, marks in ((HEADINGS[0], recipe.ingredients, [BULLET] * len(recipe.ingredients)),
                                  (HEADINGS[1], recipe.steps, [f"{n}." for n in range(1, len(recipe.steps) + 1)])):
        if out:
            out.append(Row("gap"))
        out.append(Row("head", heading))
        for item, mark in zip(items, marks):
            lines = _wrap(item, font, width)
            out += [Row("item", lines[0], mark)] + [Row("more", text) for text in lines[1:]]
    return out


def flow(body: list[Row], per_column: int) -> list[list[list[Row]]]:
    """The rows in columns of ``per_column`` rows, two columns to a card: a gap never starts a column and a
    heading never ends one."""
    columns: list[list[Row]] = []
    column: list[Row] = []
    for row in body:
        if column and (len(column) == per_column or (row.kind == "head" and len(column) == per_column - 1)):
            columns.append(column)
            column = []
        if row.kind == "gap" and not column:
            continue
        column.append(row)
    if column:
        columns.append(column)
    return [columns[i:i + 2] for i in range(0, len(columns), 2)]


def plan(recipe: Recipe) -> tuple[int, list[list[list[Row]]]]:
    """The body type size and the cards: the largest of ``SIZES`` that fits on one card, else ``RUN_ON_SIZE``
    over as many cards as it takes. Refuses (exit code 3) a recipe that needs more than ``MAX_CARDS``."""
    for size in SIZES:
        cards = flow(rows(recipe, size), (BODY_BOTTOM - BODY_TOP) // _lead(size))
        if len(cards) == 1:
            return size, cards
    cards = flow(rows(recipe, RUN_ON_SIZE), (BODY_BOTTOM - BODY_TOP) // _lead(RUN_ON_SIZE))
    if len(cards) > MAX_CARDS:
        raise PrinterError(f"the recipe needs {len(cards)} cards, more than {MAX_CARDS}",
                           hint="shorten the steps; " + SCHEMA_HINT, exit_code=3)
    return RUN_ON_SIZE, cards


def _header(d: ImageDraw.ImageDraw, recipe: Recipe, number: int) -> None:
    font, _, lines, _ = _fit_face(recipe.title, "hand-bold", 104, 60, W - 2 * MARGIN, 1)
    d.text((MARGIN, TITLE_BASELINE), lines[0], font=font, fill=PEN, anchor="ls")
    meta = " · ".join(t for t in (recipe.servings, recipe.time) if t) if number == 1 else "continued"
    if meta:
        small = _font(30)
        d.text((MARGIN + 4, META_TOP), _ellipsize(meta, small, W - 2 * MARGIN) if small.getlength(meta) >
               W - 2 * MARGIN else meta, font=small, fill=GREY)
    d.line([(MARGIN, RULE_Y), (W - MARGIN, RULE_Y)], fill=RED, width=4)


def _footer(d: ImageDraw.ImageDraw, recipe: Recipe, number: int, total: int) -> None:
    small = _font(24)
    mark = f"{number}/{total}" if total > 1 else ""
    room = W - 2 * MARGIN - (small.getlength(mark) + 40 if mark else 0)
    origin = recipe.url or recipe.source
    if origin:
        shown = origin if small.getlength(origin) <= room else _ellipsize(origin, small, room)
        d.text((MARGIN, FOOTER_BASELINE), shown, font=small, fill=GREY, anchor="ls")
    if mark:
        d.text((W - MARGIN, FOOTER_BASELINE), mark, font=small, fill=GREY, anchor="rs")


def _column(d: ImageDraw.ImageDraw, column: list[Row], x: int, recipe: Recipe, size: int) -> None:
    font, lead = _font(size), _lead(size)
    indent = _indent(recipe, font, size)
    for i, row in enumerate(column):
        baseline = BODY_TOP + (i + 1) * lead - round(lead * 0.3)
        if row.kind == "head":
            d.text((x, baseline), row.text.upper(), font=font, fill=RED, anchor="ls", stroke_width=1, stroke_fill=RED)
        elif row.kind in ("item", "more"):
            if row.mark == BULLET:
                r, cy = size * 0.13, baseline - size * 0.33
                d.ellipse([x + size * 0.15 - r, cy - r, x + size * 0.15 + r, cy + r], fill=RED)
            elif row.mark:
                d.text((x, baseline), row.mark, font=font, fill=RED, anchor="ls", stroke_width=1, stroke_fill=RED)
            d.text((x + indent, baseline), row.text, font=font, fill=INK, anchor="ls")


def compose_cards(recipe: Recipe) -> list[Image.Image]:
    """The recipe's cards, each 6×4 in on its side at 300 dpi (see ``plan``)."""
    size, cards = plan(recipe)
    lead = _lead(size)
    lines = (BODY_BOTTOM - BODY_TOP) // lead
    out = []
    for number, columns in enumerate(cards, start=1):
        card = Image.new("RGB", (W, H), CARD)
        d = ImageDraw.Draw(card)
        for k in range(1, lines + 1):  # the blue lines the text sits on
            y = BODY_TOP + k * lead - round(lead * 0.3) + round(size * 0.28)
            d.line([(MARGIN, y), (W - MARGIN, y)], fill=BLUE, width=2)
        _header(d, recipe, number)
        for column, x in zip(columns, (MARGIN, MARGIN + COL_W + GUTTER)):
            _column(d, column, x, recipe, size)
        _footer(d, recipe, number, len(cards))
        out.append(card)
    return out
