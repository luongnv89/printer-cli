"""Bingo cards: a different 5×5 card for each player, one card per A4 page.

The words come from a bundled theme (:data:`THEMES`, picked with ``--theme``) or from a words file written for the
game (``--words-file``, ``{"title"?, "free"?, "words": [...]}``). Each card holds :data:`PER_CARD` of them round a
free centre square, and :func:`deal` gives every card in a run a different set of words, so no two players can hold
the same card. The deal is drawn from a seeded random generator: the same words, number of cards and seed give the
same cards again.

A card is the title in the bundled Caveat handwriting, a row of coloured B-I-N-G-O letters, the 5×5 grid with each
word set as large as fits its square, and "Card 3 of 8" at the foot. Letters the font lacks print without their
accents, and text it cannot print at all, such as emoji or Chinese, is refused before anything is drawn.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from prt import fonts
from prt.booth import STDIN, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import Cuts, _clean, _wrap
from prt.paper import A4
from prt.vintage import _char_in, _fit_face, _printable_face

PAPER = A4
W, H = PAPER.size  # 1240 x 1754
KIND = "bingo card"

SIZE = 5  # squares across and down
PER_CARD = SIZE * SIZE - 1  # words on one card: every square but the free centre
MIN_WORDS = PER_CARD + 1  # 24 words make only one card; 25 make 25 different ones
MAX_WORDS = 200
MAX_CARDS = 30  # cards in one run
MAX_WORD = 24  # characters in one word or phrase: at the smallest size it still fits its square
LIMITS = {"title": 60, "free": 12, "theme": 40}
MAX_BINGO_FILE = 64 * 1024
DEFAULT_FREE = "FREE"
DEFAULT_THEME = "road trip"
EXPECTED = 'expected {"title": "...", "words": ["...", "...", ...]} with at least 25 words'

# --- the words ---------------------------------------------------------------------------------

# Bundled themes, each with comfortably more than PER_CARD words so that any number of cards up to MAX_CARDS can
# be dealt different. Names are matched without regard to case, and with "-" or "_" read as a space.
THEMES: dict[str, tuple[str, ...]] = {
    "road trip": (
        "Red car", "Truck", "Motorbike", "Bridge", "Tunnel", "Cow", "Horse", "Sheep", "Gas station", "Stop sign",
        "Traffic light", "Bus", "Train", "Airplane", "Windmill", "River", "Lake", "Mountain", "Tractor",
        "Police car", "Camper van", "Bicycle", "Speed limit sign", "Road works", "Church", "Farm", "Dog in a car",
        "Rest stop", "Billboard", "Yellow car", "Fire truck", "Motorway exit", "Roundabout", "Picnic table", "Flag",
        "Toll booth",
    ),
    "beach": (
        "Seashell", "Crab", "Sandcastle", "Bucket", "Spade", "Seagull", "Lifeguard", "Sunglasses", "Beach ball",
        "Umbrella", "Towel", "Ice cream", "Boat", "Surfboard", "Starfish", "Seaweed", "Big wave", "Kite",
        "Flip-flops", "Sun hat", "Lighthouse", "Pier", "Rock pool", "Footprints", "Sunscreen", "Dog on the beach",
        "Swimsuit", "Jellyfish", "Sailboat", "Pebble", "Driftwood", "Snorkel",
    ),
    "zoo": (
        "Lion", "Tiger", "Elephant", "Giraffe", "Zebra", "Monkey", "Penguin", "Flamingo", "Hippo", "Rhino",
        "Kangaroo", "Koala", "Panda", "Gorilla", "Crocodile", "Snake", "Parrot", "Owl", "Tortoise", "Seal",
        "Meerkat", "Camel", "Brown bear", "Wolf", "Lemur", "Peacock", "Zookeeper", "Zoo map", "Feeding time",
        "Gift shop", "Polar bear", "Otter",
    ),
    "birthday": (
        "Cake", "Candles", "Balloon", "Present", "Party hat", "Wrapping paper", "Ribbon", "Birthday card",
        "Confetti", "Streamers", "Ice cream", "Cupcake", "Juice", "Pizza", "Party games", "Music", "Dancing",
        "Photo", "Party bag", "Sweets", "Crisps", "Birthday song", "Make a wish", "Banner", "Magic trick",
        "Pass the parcel", "Musical chairs", "Sandwiches", "Thank you", "Best friend", "Lemonade", "Bow",
    ),
    "space": (
        "Rocket", "Astronaut", "Moon", "Sun", "Star", "Planet", "Comet", "Meteor", "Galaxy", "Alien", "UFO",
        "Satellite", "Telescope", "Mars", "Saturn", "Jupiter", "Earth", "Space suit", "Helmet", "Launch pad",
        "Countdown", "Black hole", "Shooting star", "Space station", "Moon boot", "Crater", "Orbit", "Nebula",
        "Rover", "Asteroid", "Milky Way", "Constellation",
    ),
}


@dataclass(frozen=True)
class Bingo:
    """One game: the words the cards are dealt from (each different), the title on every card and the text of the
    free centre square."""
    words: tuple[str, ...]
    title: str = ""
    free: str = DEFAULT_FREE


FIELDS = ("title", "free", "words")
REQUIRED = ("words",)


def theme_key(theme: str) -> str:
    """How a theme name is looked up: lower case, "-" and "_" read as spaces, runs of spaces as one."""
    return " ".join(theme.replace("-", " ").replace("_", " ").split()).casefold()


def _printable_check(name: str, value: str, exit_code: int) -> None:
    face = fonts.face("hand", 40)
    unprintable = sorted({c for c in value if c != "?" and _char_in(face, c) == "?"})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(f"{name} has characters the handwriting font cannot print: {shown}",
                           hint="write it in Latin or Cyrillic letters (no emoji)", exit_code=exit_code)


def _check_text(name: str, value: str, limit: int, exit_code: int) -> str:
    value = _clean(value, keep_newlines=False)
    if len(value) > limit:
        raise PrinterError(f"{name} is too long ({len(value)} characters, max {limit})",
                           hint=f"shorten the {name} so it fits on the card", exit_code=exit_code)
    _printable_check(name, value, exit_code)
    return value


def check_bingo(game: Bingo, exit_code: int) -> Bingo:
    """Normalise whitespace and enforce the length limits, printable characters, and at least :data:`MIN_WORDS`
    words, none empty and no two the same (ignoring case and spacing)."""
    title = _check_text("title", game.title, LIMITS["title"], exit_code)
    free = _check_text("free", game.free, LIMITS["free"], exit_code) or DEFAULT_FREE
    if len(game.words) > MAX_WORDS:
        raise PrinterError(f"the bingo game has {len(game.words)} words, more than {MAX_WORDS}",
                           hint=f"list at most {MAX_WORDS} words", exit_code=exit_code)
    words: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(game.words):
        name = f"words[{i}]"
        word = _clean(raw, keep_newlines=False)
        if not word:
            raise PrinterError(f"{name} is empty", hint="give every word some text, or leave it out",
                               exit_code=exit_code)
        if len(word) > MAX_WORD:
            raise PrinterError(f"{name} is too long ({len(word)} characters, max {MAX_WORD})",
                               hint="shorten it so it fits its square", exit_code=exit_code)
        _printable_check(name, word, exit_code)
        key = word.casefold()
        if key in seen:
            raise PrinterError(f"{name} repeats words[{seen[key]}] ({word!r})",
                               hint="every word on a bingo card must be different: leave the repeat out",
                               exit_code=exit_code)
        seen[key] = i
        words.append(word)
    if len(words) < MIN_WORDS:
        raise PrinterError(f"the bingo game has {len(words)} words, fewer than {MIN_WORDS}",
                           hint=f"a card holds {PER_CARD} words, and different cards need at least {MIN_WORDS}",
                           exit_code=exit_code)
    return Bingo(words=tuple(words), title=title, free=free)


def load_bingo(path: Path) -> Bingo:
    """Read a words file: {"title"?, "free"?, "words": [...]}; ``-`` reads stdin."""
    if str(path) == STDIN:
        name = kind = "bingo words on stdin"
        content = read_stdin(MAX_BINGO_FILE)
        too_big = len(content) > MAX_BINGO_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"words file not found: {path}", hint="check the path", exit_code=3)
        name, kind = f"words file {path.name}", "words file"
        too_big = path.stat().st_size > MAX_BINGO_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MAX_BINGO_FILE // 1024} KiB",
                           hint="a words file holds a title and a list of words", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_bingo(text, name, kind)


def parse_bingo(text: str, name: str = "words file", kind: str = "words file") -> Bingo:
    """A game from its JSON text, checked as a words file is. ``name`` and ``kind`` say where it came from in error
    messages (e.g. ``words file party.json`` and ``words file``)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=EXPECTED,
                           exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=EXPECTED, exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in {kind}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    if "words" not in data:
        raise PrinterError(f"{name} is missing words", hint=EXPECTED, exit_code=3)
    words = data["words"]
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise PrinterError(f"words in {kind} must be a list of texts", hint=EXPECTED, exit_code=3)
    wrong = sorted(f for f in ("title", "free") if f in data and not isinstance(data[f], str))
    if wrong:
        raise PrinterError(f"{kind} fields must be text: {', '.join(wrong)}", hint=EXPECTED, exit_code=3)
    return check_bingo(Bingo(words=tuple(words), title=data.get("title", ""), free=data.get("free", "")),
                       exit_code=3)


def game(theme: str | None, words: Bingo | None = None) -> tuple[Bingo, str | None]:
    """The game to deal and the bundled theme it uses (None when the words came from a file).

    With ``words`` (a checked words file) its words are used for any ``theme``; else ``theme`` (the default theme
    when None) must name a bundled theme. The title is the file's, else "<Theme> Bingo", else "Bingo"."""
    if theme is not None:
        theme = _check_text("theme", theme, LIMITS["theme"], exit_code=2)
        if not theme:
            raise PrinterError("the theme is empty", hint="name a theme, e.g. --theme 'road trip'", exit_code=2)
    bundled = None
    if words is None:
        bundled = theme_key(theme if theme is not None else DEFAULT_THEME)
        if bundled not in THEMES:
            raise PrinterError(f"unknown bingo theme: {theme}",
                               hint=f"choose one of: {', '.join(THEMES)}; or give your own words with "
                                    "--words-file FILE (or - for stdin)", exit_code=2, kind="unknown_theme")
        words = Bingo(words=THEMES[bundled])
        theme = bundled
    title = words.title
    if not title:
        named = string.capwords(theme) if theme else ""
        title = named if theme_key(named).endswith("bingo") else f"{named} Bingo".strip()
    return check_bingo(Bingo(words=words.words, title=title, free=words.free), exit_code=2), bundled


def new_seed() -> int:
    """A seed for a run that was not given one; it is reported, so the run can be dealt again."""
    return random.SystemRandom().randrange(1_000_000)


SMALL_DEAL = 5000  # with at most this many different cards possible, they are listed and drawn from


def deal(count: int, n: int, seed: int) -> list[tuple[int, ...]]:
    """``n`` cards from ``count`` words, each card :data:`PER_CARD` word indices in the order they fill the grid
    (row by row, skipping the centre). No two cards hold the same set of words. The same arguments give the same
    cards; ``n`` larger than the number of different cards ``count`` words can make is refused (exit code 3)."""
    possible = math.comb(count, PER_CARD)
    if n > possible:
        raise PrinterError(f"{count} words make only {possible} different bingo cards, not {n}",
                           hint=f"add more words (each one more makes many more cards), or ask for at most "
                                f"{possible} cards", exit_code=3)
    rng = random.Random(seed)
    if possible <= SMALL_DEAL:
        # Few enough to list: each card is the words left off it, so draw n different leave-out sets.
        left_out = rng.sample(list(itertools.combinations(range(count), count - PER_CARD)), n)
        sets = [[i for i in range(count) if i not in out] for out in left_out]
    else:
        chosen: set[tuple[int, ...]] = set()
        sets = []
        while len(sets) < n:
            pick = tuple(sorted(rng.sample(range(count), PER_CARD)))
            if pick not in chosen:
                chosen.add(pick)
                sets.append(list(pick))
    for card in sets:
        rng.shuffle(card)
    return [tuple(card) for card in sets]


# --- the layout --------------------------------------------------------------------------------

MARGIN = 90
GRID = W - 2 * MARGIN  # 1060 px, about 180 mm
CELL = GRID // SIZE  # 212 px, about 36 mm
GRID_X = (W - CELL * SIZE) // 2
TITLE_TOP = 150
TITLE_SIZE, TITLE_MIN = 120, 72  # on one line; a title too long for it goes on two
TITLE_TWO, TITLE_TWO_MIN = 64, 40
HEADER_H = 150  # the B-I-N-G-O row
LETTERS = "BINGO"
LETTER_COLOURS = ((214, 48, 49), (240, 140, 0), (46, 160, 67), (33, 113, 181), (128, 60, 170))
INK = (28, 28, 36)
LINE = (60, 60, 72)
FREE_FILL = (255, 236, 150)
WORD_SIZE, WORD_MIN = 54, 28
WORD_LINES = 4
CELL_PAD = 22
FOOTER_SIZE = 34
GREY = (110, 110, 120)


def _centred(d: ImageDraw.ImageDraw, lines: list[str], font, size: int, cx: float, cy: float, fill) -> None:
    """``lines`` centred as a block on (``cx``, ``cy``)."""
    lead = round(size * 1.05)
    top = cy - lead * len(lines) / 2
    for k, line in enumerate(lines):
        d.text((cx, top + lead * k + lead / 2), line, font=font, fill=fill, anchor="mm")


def _fit_word(word: str, width: int):
    """The largest size (:data:`WORD_SIZE` down to :data:`WORD_MIN`) at which ``word`` wraps into
    :data:`WORD_LINES` lines of ``width`` without splitting a word of it; failing that, as ``vintage._fit_face``
    fits it (split by character, cut with "…" at the smallest). Returns (font, size, lines, cut)."""
    for size in range(WORD_SIZE, WORD_MIN - 1, -2):
        font = fonts.face("hand", size)
        printable = _printable_face(word, font)
        lines = _wrap(printable, font, width)
        if len(lines) <= WORD_LINES and all(font.getlength(part) <= width for part in printable.split()):
            return font, size, lines, None
    return _fit_face(word, "hand", WORD_SIZE, WORD_MIN, width, WORD_LINES)


def grid_top() -> int:
    return TITLE_TOP + TITLE_SIZE + 40 + HEADER_H


def compose_card(game: Bingo, card: tuple[int, ...], number: int, of: int) -> tuple[Image.Image, Cuts]:
    """One card: the title, the B-I-N-G-O row, the grid of the words at ``card`` (indices into ``game.words``) round
    the free square, and "Card ``number`` of ``of``" at the foot; and the texts cut with "…" to fit (``title``, or
    ``words[i]`` by the word's place in the game)."""
    page = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(page)
    cut: Cuts = {}
    font, size, lines, title_cut = _fit_face(game.title, "hand-bold", TITLE_SIZE, TITLE_MIN, GRID, 1)
    if title_cut:  # too long for one line: two smaller ones
        font, size, lines, title_cut = _fit_face(game.title, "hand-bold", TITLE_TWO, TITLE_TWO_MIN, GRID, 2)
    if title_cut:
        cut["title"] = title_cut
    _centred(d, lines, font, size, W / 2, TITLE_TOP + TITLE_SIZE / 2, INK)
    top = grid_top()
    head_top = top - HEADER_H
    letter_font = fonts.face("hand-bold", 120)
    for k, (letter, colour) in enumerate(zip(LETTERS, LETTER_COLOURS)):
        x0 = GRID_X + k * CELL
        d.rounded_rectangle((x0 + 8, head_top + 8, x0 + CELL - 8, top - 10), radius=26, fill=colour)
        d.text((x0 + CELL / 2, head_top + (HEADER_H - 2) / 2), letter, font=letter_font, fill=(255, 255, 255),
               anchor="mm")
    squares = list(card)
    centre = SIZE * SIZE // 2
    for k in range(SIZE * SIZE):
        row, col = divmod(k, SIZE)
        x0, y0 = GRID_X + col * CELL, top + row * CELL
        if k == centre:
            d.rectangle((x0, y0, x0 + CELL, y0 + CELL), fill=FREE_FILL)
            font, size, lines, _ = _fit_face(game.free, "hand-bold", 64, 30, CELL - 2 * CELL_PAD, 2)
            _centred(d, lines, font, size, x0 + CELL / 2, y0 + CELL / 2, INK)
            continue
        index = squares.pop(0)
        font, size, lines, word_cut = _fit_word(game.words[index], CELL - 2 * CELL_PAD)
        if word_cut:
            cut.setdefault(f"words[{index}]", word_cut)
        _centred(d, lines, font, size, x0 + CELL / 2, y0 + CELL / 2, INK)
    for k in range(SIZE + 1):
        width = 6 if k in (0, SIZE) else 3
        x, y = GRID_X + k * CELL, top + k * CELL
        d.line((x, top, x, top + SIZE * CELL), fill=LINE, width=width)
        d.line((GRID_X, y, GRID_X + SIZE * CELL, y), fill=LINE, width=width)
    footer = fonts.face("hand", FOOTER_SIZE)
    d.text((W / 2, top + SIZE * CELL + 70), f"Card {number} of {of}", font=footer, fill=GREY, anchor="mm")
    return page, cut


def compose_cards(game: Bingo, cards: list[tuple[int, ...]]) -> tuple[list[Image.Image], Cuts]:
    """Every card of a run, in order, and the texts cut to fit on any of them."""
    pages: list[Image.Image] = []
    cut: Cuts = {}
    for number, card in enumerate(cards, 1):
        page, card_cut = compose_card(game, card, number, len(cards))
        pages.append(page)
        for name, c in card_cut.items():
            cut.setdefault(name, c)
    return pages, cut


def to_dict(game: Bingo) -> dict[str, Any]:
    return {"title": game.title, "free": game.free, "words": list(game.words)}
