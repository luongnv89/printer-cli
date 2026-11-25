"""Puzzle pages: a sudoku, a word search, a crossword or a maze, one puzzle per A4 page, and its solution.

Every puzzle is built by prt, never drawn by hand: :func:`sudoku` makes a grid with exactly one solution,
:func:`wordsearch` hides words from a bundled theme or a words file, :func:`crossword` lays out the answers of a
crossword file (``{"title"?, "entries": [{"answer", "clue"}, ...]}``) so that they cross, and :func:`maze` digs a
maze with one way through. The sudoku, word search and maze are drawn from a seeded random generator: the same
options and seed give the same puzzle again. A crossword's layout depends on its entries alone.

Each puzzle carries its ``solution`` as plain JSON data (:meth:`Puzzle.answers`), so whoever printed it, an agent
included, can check a filled-in sheet against it later: ``prt puzzle … -f json`` reports it and ``--answers FILE``
saves it. The page itself says which puzzle it is (kind, difficulty and seed) at the foot, so a scan of it is
enough to make the same puzzle, and its solution, again.
"""

from __future__ import annotations

import json
import random
import string
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, cast

from PIL import Image, ImageDraw

from prt import fonts
from prt.booth import STDIN, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import Cuts, _clean, _cut, _ellipsize, _in_source, _wrap
from prt.paper import A4
from prt.vintage import _char_in, _fit_face, _printable_face

PAPER = A4
W, H = PAPER.size  # 1240 x 1754

KINDS = ("sudoku", "wordsearch", "crossword", "maze")
NAMES = {"sudoku": "sudoku", "wordsearch": "word search", "crossword": "crossword", "maze": "maze"}  # catalog kinds
TITLES = {"sudoku": "Sudoku", "wordsearch": "Word Search", "crossword": "Crossword", "maze": "Maze"}
DIFFICULTIES = ("easy", "medium", "hard")
DEFAULT_DIFFICULTY = "medium"
LIMITS = {"title": 60, "theme": 40}
MAX_PUZZLE_FILE = 64 * 1024


def new_seed() -> int:
    """A seed for a run that was not given one; it is reported and printed, so the puzzle can be made again."""
    return random.SystemRandom().randrange(1_000_000)


@dataclass(frozen=True)
class Puzzle:
    """One puzzle, ready to draw: its ``kind`` (see :data:`KINDS`), its title, how it was made (``seed`` and
    ``difficulty``, None for a crossword) and ``solution``, the answers as plain JSON data. ``layout`` holds what
    the page draws (the givens, the letter grid, the numbered cells or the maze walls)."""

    kind: str
    title: str
    seed: int | None
    difficulty: str | None
    solution: dict[str, Any]
    layout: Any = field(repr=False, compare=False)
    theme: str | None = None  # a word search's bundled theme (None from a words file)

    def answers(self) -> dict[str, Any]:
        """What ``-f json`` reports and ``--answers`` saves: enough to check a filled-in sheet without the page."""
        out = {"kind": self.kind, "title": self.title, "seed": self.seed, "difficulty": self.difficulty}
        if self.kind == "wordsearch":
            out["theme"] = self.theme  # the seed makes the same puzzle only from the same words
        return {**out, "solution": self.solution}

    def label(self) -> str:
        """The line at the foot of the page that says which puzzle this is, e.g. ``sudoku · medium · seed 4821``."""
        parts = [NAMES[self.kind]]
        if self.theme:
            parts.append(self.theme)
        if self.difficulty:
            parts.append(self.difficulty)
        if self.seed is not None:
            parts.append(f"seed {self.seed}")
        if self.kind == "crossword":
            entries = len(self.solution["across"]) + len(self.solution["down"])
            parts.append(f"{entries} clues")
        return " · ".join(parts)


def check_title(title: str | None, kind: str) -> str:
    """The title to print: ``title`` cleaned and checked (exit code 2: it comes from a flag), else the kind's."""
    if title is None:
        return TITLES[kind]
    value = _clean(title, keep_newlines=False)
    _check_text("title", value, LIMITS["title"], exit_code=2)
    return value


def _check_text(name: str, value: str, limit: int, exit_code: int) -> None:
    if len(value) > limit:
        raise PrinterError(
            f"{name} is too long ({len(value)} characters, max {limit})",
            hint=f"shorten the {name}",
            exit_code=exit_code,
        )
    _check_printable(name, value, "hand", exit_code)


def _check_printable(name: str, value: str, role: str, exit_code: int) -> None:
    face = fonts.face(role, 40)
    unprintable = sorted({c for c in value if c != "?" and _char_in(face, c) == "?"})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise PrinterError(
            f"{name} has characters the page font cannot print: {shown}",
            hint="write it in Latin letters (no emoji)",
            exit_code=exit_code,
        )


def letters_of(text: str) -> str:
    """The grid letters of a word or answer: its Latin letters in capitals, accents dropped, everything else (spaces,
    hyphens, apostrophes) left out; "Dog in a car" is ``DOGINACAR``."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return "".join(c for c in plain.upper() if "A" <= c <= "Z")


def _only_letters(text: str) -> bool:
    """True when ``text`` holds nothing but letters (any accents), spaces, hyphens and apostrophes."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return all(("A" <= c <= "Z") or c in " -'’" for c in plain.upper())


# --- reading a puzzle file -----------------------------------------------------------------------------------------


def _read(path: Path, what: str) -> tuple[str, str]:
    """The text of a puzzle file (``-`` reads stdin), and the name it goes by in messages."""
    if str(path) == STDIN:
        name = f"{what} on stdin"
        content = read_stdin(MAX_PUZZLE_FILE)
        too_big = len(content) > MAX_PUZZLE_FILE
    else:
        if not path.is_file():
            raise PrinterError(f"{what} not found: {path}", hint="check the path", exit_code=3)
        name = f"{what} {path.name}"
        too_big = path.stat().st_size > MAX_PUZZLE_FILE
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(
            f"{name} is larger than {MAX_PUZZLE_FILE // 1024} KiB", hint=f"a {what} is short", exit_code=3
        )
    try:
        return content.decode("utf-8"), name
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc


def _object(text: str, name: str, fields: tuple[str, ...], expected: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(
            f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=expected, exit_code=3
        ) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=expected, exit_code=3)
    unknown = sorted(set(data) - set(fields))
    if unknown:
        raise PrinterError(
            f"unknown field(s) in {name}: {', '.join(unknown)}",
            hint="allowed fields: " + ", ".join(fields),
            exit_code=3,
        )
    if "title" in data and not isinstance(data["title"], str):
        raise PrinterError(f"title in {name} must be text", hint=expected, exit_code=3)
    return data


def _file_title(data: dict[str, Any]) -> str:
    title = _clean(data.get("title", ""), keep_newlines=False)
    _check_text("title", title, LIMITS["title"], exit_code=3)
    return title


# --- sudoku --------------------------------------------------------------------------------------------------------

SUDOKU_GIVENS = {"easy": 38, "medium": 31, "hard": 26}  # the fewest clues digging may leave; it may stop sooner
ALL_DIGITS = 0b1111111110  # bits 1..9


def _box(i: int) -> int:
    r, c = divmod(i, 9)
    return r // 3 * 3 + c // 3


def _solve(grid: list[int], limit: int, rng: random.Random | None = None) -> int:
    """Count the solutions of ``grid`` (0 for an empty square) up to ``limit``. With ``limit`` 1 the first one is
    left in ``grid``; ``rng`` shuffles the order digits are tried in, which makes a random full grid from an empty
    one. Squares are filled most-constrained first, so a puzzle with one solution is settled in milliseconds."""
    rows, cols, boxes = [0] * 9, [0] * 9, [0] * 9
    for i, v in enumerate(grid):
        if v:
            bit = 1 << v
            rows[i // 9] |= bit
            cols[i % 9] |= bit
            boxes[_box(i)] |= bit
    empty = [i for i, v in enumerate(grid) if not v]
    found = 0

    def search() -> bool:
        nonlocal found
        best, best_mask, best_n = -1, 0, 10
        for i in empty:
            if grid[i]:
                continue
            mask = ALL_DIGITS & ~(rows[i // 9] | cols[i % 9] | boxes[_box(i)])
            n = bin(mask).count("1")
            if n < best_n:
                best, best_mask, best_n = i, mask, n
                if n <= 1:
                    break
        if best < 0:
            found += 1
            return found >= limit
        digits = [d for d in range(1, 10) if best_mask >> d & 1]
        if rng is not None:
            rng.shuffle(digits)
        r, c, b = best // 9, best % 9, _box(best)
        for d in digits:
            bit = 1 << d
            grid[best] = d
            rows[r] |= bit
            cols[c] |= bit
            boxes[b] |= bit
            if search():
                return True
            rows[r] &= ~bit
            cols[c] &= ~bit
            boxes[b] &= ~bit
            grid[best] = 0
        return False

    search()
    return found


@dataclass(frozen=True)
class SudokuLayout:
    givens: tuple[int, ...]  # 81 squares row by row, 0 where the player writes


def sudoku(seed: int, difficulty: str = DEFAULT_DIFFICULTY, title: str = TITLES["sudoku"]) -> Puzzle:
    """A 9×9 sudoku with exactly one solution: a random full grid, then squares emptied in a random order for as
    long as the puzzle keeps one solution, down to :data:`SUDOKU_GIVENS` clues."""
    rng = random.Random(f"sudoku:{seed}")
    full = [0] * 81
    _solve(full, 1, rng)
    puzzle = list(full)
    target = SUDOKU_GIVENS[difficulty]
    givens = 81
    squares = list(range(81))
    rng.shuffle(squares)
    for i in squares:
        if givens <= target:
            break
        puzzle[i] = 0
        if _solve(list(puzzle), 2) == 1:
            givens -= 1
        else:
            puzzle[i] = full[i]

    def rows(cells: list[int]) -> list[str]:
        return ["".join(str(v) if v else "." for v in cells[r * 9 : r * 9 + 9]) for r in range(9)]

    solution = {
        "rows": "ABCDEFGHI",
        "columns": "123456789",
        "puzzle": rows(puzzle),
        "grid": rows(full),
        "blanks": 81 - givens,
    }
    return Puzzle("sudoku", title, seed, difficulty, solution, SudokuLayout(tuple(puzzle)))


# --- word search ---------------------------------------------------------------------------------------------------

# The directions words may run in, by difficulty: easy reads only forwards and down, hard any of the eight ways.
DIRECTIONS = {
    "right": (0, 1),
    "down": (1, 0),
    "down-right": (1, 1),
    "up-right": (-1, 1),
    "left": (0, -1),
    "up": (-1, 0),
    "up-left": (-1, -1),
    "down-left": (1, -1),
}
SEARCH_DIRECTIONS = {
    "easy": ("right", "down"),
    "medium": ("right", "down", "down-right", "up-right"),
    "hard": tuple(DIRECTIONS),
}
SEARCH_SIZE = {"easy": 10, "medium": 13, "hard": 15}  # squares across and down
SEARCH_WORDS = {"easy": 8, "medium": 12, "hard": 16}  # words hidden from a theme
MAX_SEARCH = 15  # the largest grid; a words file's longest word sets the size when longer than the difficulty's
MIN_LETTERS, MAX_LETTERS = 3, MAX_SEARCH
MIN_SEARCH_WORDS, MAX_SEARCH_WORDS = 3, 20
MAX_SEARCH_WORD = 24  # characters as written, spaces included
SEARCH_FILL = 0.5  # the words may cover at most this share of the grid's squares, so they always fit
SEARCH_FIELDS = ("title", "words")
SEARCH_EXPECTED = 'expected {"title": "...", "words": ["...", "...", "..."]}'


@dataclass(frozen=True)
class WordList:
    """The words a word search hides, as they are listed under the grid, and its title ("" for the default)."""

    words: tuple[str, ...]
    title: str = ""


def check_words(words: list[str], name: str, exit_code: int) -> tuple[str, ...]:
    """Normalise whitespace and check every word: :data:`MIN_LETTERS` to :data:`MAX_LETTERS` letters, nothing but
    letters, spaces, hyphens and apostrophes, and no two with the same letters."""
    if not MIN_SEARCH_WORDS <= len(words) <= MAX_SEARCH_WORDS:
        raise PrinterError(
            f"{name} has {len(words)} words; a word search takes {MIN_SEARCH_WORDS} to {MAX_SEARCH_WORDS}",
            hint="list fewer or more words",
            exit_code=exit_code,
        )
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(words):
        where = f"words[{i}]"
        word = _clean(raw, keep_newlines=False)
        if len(word) > MAX_SEARCH_WORD:
            raise PrinterError(
                f"{where} is too long ({len(word)} characters, max {MAX_SEARCH_WORD})",
                hint="shorten it",
                exit_code=exit_code,
            )
        if not _only_letters(word):
            raise PrinterError(
                f"{where} ({word!r}) has characters other than letters, spaces, hyphens and apostrophes",
                hint="a word search hides letters only: spell numbers out",
                exit_code=exit_code,
            )
        letters = letters_of(word)
        if not MIN_LETTERS <= len(letters) <= MAX_LETTERS:
            raise PrinterError(
                f"{where} ({word!r}) has {len(letters)} letters; each word needs {MIN_LETTERS} to {MAX_LETTERS}",
                hint="leave it out or pick another word",
                exit_code=exit_code,
            )
        _check_printable(where, word, "body", exit_code)
        if letters in seen:
            raise PrinterError(
                f"{where} repeats words[{seen[letters]}] ({word!r})",
                hint="every word must be different: leave the repeat out",
                exit_code=exit_code,
            )
        seen[letters] = i
        out.append(word)
    return tuple(out)


def load_words(path: Path) -> WordList:
    """Read a word search words file: {"title"?, "words": [...]}; ``-`` reads stdin (exit code 3 when bad)."""
    text, name = _read(path, "words file")
    return parse_words(text, name)


def parse_words(text: str, name: str = "words file") -> WordList:
    data = _object(text, name, SEARCH_FIELDS, SEARCH_EXPECTED)
    words = data.get("words")
    if words is None:
        raise PrinterError(f"{name} is missing words", hint=SEARCH_EXPECTED, exit_code=3)
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise PrinterError(f"words in {name} must be a list of texts", hint=SEARCH_EXPECTED, exit_code=3)
    return WordList(check_words(words, name, exit_code=3), _file_title(data))


def theme_words(theme: str) -> tuple[str, str]:
    """The bundled theme ``theme`` names (the bingo themes) and its title, e.g. ``("space", "Space Word Search")``;
    an unknown theme exits with code 2, kind ``unknown_theme``."""
    from prt import bingo

    key = bingo.theme_key(theme)
    if key not in bingo.THEMES:
        raise PrinterError(
            f"unknown word search theme: {theme}",
            hint=f"choose one of: {', '.join(bingo.THEMES)}; or give your own words with "
            "--words-file FILE (or - for stdin)",
            exit_code=2,
            kind="unknown_theme",
        )
    return key, f"{key.title()} Word Search"


def _search_size(words: tuple[str, ...], difficulty: str) -> int:
    return max(SEARCH_SIZE[difficulty], *(len(letters_of(w)) for w in words))


def _place_words(
    letters: list[str], size: int, directions: tuple[str, ...], rng: random.Random, required: bool
) -> list[tuple[int, int, int, str]] | None:
    """Hide ``letters`` in a ``size`` grid, longest first, each where it fits (crossing others on a shared letter).
    Returns (index, row, col, direction) per hidden word, or None when ``required`` and one would not fit."""
    grid: dict[tuple[int, int], str] = {}
    placed: list[tuple[int, int, int, str]] = []
    for index in sorted(range(len(letters)), key=lambda k: -len(letters[k])):
        word = letters[index]
        spots = []
        for name in directions:
            dr, dc = DIRECTIONS[name]
            for r in range(size):
                for c in range(size):
                    end_r, end_c = r + dr * (len(word) - 1), c + dc * (len(word) - 1)
                    if not (0 <= end_r < size and 0 <= end_c < size):
                        continue
                    cells = [(r + dr * k, c + dc * k) for k in range(len(word))]
                    if all(grid.get(cell, ch) == ch for cell, ch in zip(cells, word)):
                        spots.append((r, c, name, cells))
        if not spots:
            if required:
                return None
            continue
        r, c, name, cells = rng.choice(spots)
        for cell, ch in zip(cells, word):
            grid[cell] = ch
        placed.append((index, r, c, name))
    return placed


@dataclass(frozen=True)
class SearchLayout:
    grid: tuple[str, ...]  # the rows of letters
    words: tuple[str, ...]  # as listed under the grid, alphabetically


def wordsearch(
    seed: int,
    difficulty: str = DEFAULT_DIFFICULTY,
    words: WordList | None = None,
    theme: str | None = None,
    title: str | None = None,
) -> Puzzle:
    """A word search: the words of ``words`` (a checked words file; all of them are hidden) or else
    :data:`SEARCH_WORDS` of the bundled ``theme`` (picked at random), hidden in a square of letters running in the
    difficulty's :data:`SEARCH_DIRECTIONS`, the other squares filled with random letters."""
    rng = random.Random(f"wordsearch:{seed}")
    key = None
    if words is None:
        from prt import bingo

        key, default_title = theme_words(theme or bingo.DEFAULT_THEME)
        size = SEARCH_SIZE[difficulty]
        pool = [w for w in bingo.THEMES[key] if MIN_LETTERS <= len(letters_of(w)) <= size]
        rng.shuffle(pool)
        chosen = pool[: SEARCH_WORDS[difficulty]]
    else:
        named = _clean(theme or "", keep_newlines=False)
        if named:
            _check_text("theme", named, LIMITS["theme"], exit_code=2)
        default_title = words.title or (f"{string.capwords(named)} Word Search" if named else TITLES["wordsearch"])
        chosen = list(words.words)
        size = _search_size(words.words, difficulty)
        while sum(len(letters_of(w)) for w in chosen) > SEARCH_FILL * size * size and size < MAX_SEARCH:
            size += 1
        if sum(len(letters_of(w)) for w in chosen) > SEARCH_FILL * size * size:
            raise PrinterError(
                f"the words have {sum(len(letters_of(w)) for w in chosen)} letters, too many for a "
                f"{MAX_SEARCH}×{MAX_SEARCH} word search",
                hint=f"use fewer or shorter words (at most {int(SEARCH_FILL * MAX_SEARCH**2)} letters in all)",
                exit_code=3,
            )
    letters = [letters_of(w) for w in chosen]
    placed = None
    for _ in range(50):  # a fresh try from an empty grid; with the grid at most half full one always works
        placed = _place_words(letters, size, SEARCH_DIRECTIONS[difficulty], rng, required=words is not None)
        if placed is not None:
            break
    if placed is None:
        raise PrinterError(
            "the words could not all be hidden in the grid",
            hint="use fewer or shorter words, or --difficulty hard",
            exit_code=3,
        )
    grid = [[""] * size for _ in range(size)]
    hidden: list[dict[str, Any]] = []
    for index, r, c, name in sorted(placed):
        dr, dc = DIRECTIONS[name]
        cells = []
        for k, ch in enumerate(letters[index]):
            grid[r + dr * k][c + dc * k] = ch
            cells.append([r + dr * k + 1, c + dc * k + 1])
        hidden.append(
            {
                "word": chosen[index],
                "letters": letters[index],
                "row": r + 1,
                "col": c + 1,
                "direction": name,
                "cells": cells,
            }
        )
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    rows = tuple("".join(ch or rng.choice(alphabet) for ch in row) for row in grid)
    listed = tuple(sorted((h["word"] for h in hidden), key=str.casefold))
    solution = {"size": size, "grid": list(rows), "words": hidden}
    return Puzzle("wordsearch", title or default_title, seed, difficulty, solution, SearchLayout(rows, listed), key)


# --- crossword -----------------------------------------------------------------------------------------------------

MIN_ENTRIES, MAX_ENTRIES = 2, 20
MAX_ANSWER = 15  # letters
MAX_ANSWER_TEXT = 30  # characters as written
MAX_CLUE = 90
MAX_CROSSWORD = 21  # squares across or down
CROSSWORD_FIELDS = ("title", "entries")
ENTRY_FIELDS = ("answer", "clue")
CROSSWORD_EXPECTED = 'expected {"title": "...", "entries": [{"answer": "...", "clue": "..."}, ...]}'


@dataclass(frozen=True)
class Entry:
    answer: str  # as written, e.g. "Buzz Aldrin"
    clue: str


@dataclass(frozen=True)
class CrosswordFile:
    entries: tuple[Entry, ...]
    title: str = ""


DEFAULT_CROSSWORD = CrosswordFile(
    title="Space Crossword",
    entries=(
        Entry("Moon", "It goes round the Earth once a month"),
        Entry("Rocket", "It carries astronauts up into space"),
        Entry("Astronaut", "A person who travels into space"),
        Entry("Saturn", "The planet with the brightest rings"),
        Entry("Star", "The Sun is one of these"),
        Entry("Orbit", "The path one body takes round another"),
        Entry("Comet", "An icy visitor with a long glowing tail"),
        Entry("Mars", "The red planet"),
        Entry("Telescope", "You look through it to see far-off stars"),
    ),
)


def check_crossword(file: CrosswordFile, name: str, exit_code: int) -> CrosswordFile:
    """Normalise whitespace and enforce the limits: :data:`MIN_ENTRIES` to :data:`MAX_ENTRIES` entries, each answer
    2 to :data:`MAX_ANSWER` letters (spaces, hyphens and apostrophes allowed, and left out of the grid) and no two
    the same, each clue up to :data:`MAX_CLUE` characters, and every character printable."""
    if not MIN_ENTRIES <= len(file.entries) <= MAX_ENTRIES:
        raise PrinterError(
            f"{name} has {len(file.entries)} entries; a crossword takes {MIN_ENTRIES} to {MAX_ENTRIES}",
            hint="add or remove entries",
            exit_code=exit_code,
        )
    entries = []
    seen: dict[str, int] = {}
    for i, entry in enumerate(file.entries):
        where = f"entries[{i}]"
        answer = _clean(entry.answer, keep_newlines=False)
        clue = _clean(entry.clue, keep_newlines=False)
        if len(answer) > MAX_ANSWER_TEXT:
            raise PrinterError(
                f"{where}.answer is too long ({len(answer)} characters, max {MAX_ANSWER_TEXT})",
                hint="shorten it",
                exit_code=exit_code,
            )
        if not _only_letters(answer):
            raise PrinterError(
                f"{where}.answer ({answer!r}) has characters other than letters, spaces, hyphens and apostrophes",
                hint="a crossword answer is letters only: spell numbers out",
                exit_code=exit_code,
            )
        letters = letters_of(answer)
        if not 2 <= len(letters) <= MAX_ANSWER:
            raise PrinterError(
                f"{where}.answer ({answer!r}) has {len(letters)} letters; an answer needs 2 to {MAX_ANSWER}",
                hint="pick another answer",
                exit_code=exit_code,
            )
        if not clue:
            raise PrinterError(f"{where}.clue is empty", hint="write a clue for every answer", exit_code=exit_code)
        if len(clue) > MAX_CLUE:
            raise PrinterError(
                f"{where}.clue is too long ({len(clue)} characters, max {MAX_CLUE})",
                hint="shorten the clue",
                exit_code=exit_code,
            )
        _check_printable(f"{where}.clue", clue, "body", exit_code)
        _check_printable(f"{where}.answer", answer, "body", exit_code)
        if letters in seen:
            raise PrinterError(
                f"{where}.answer repeats entries[{seen[letters]}].answer ({answer!r})",
                hint="every answer must be different",
                exit_code=exit_code,
            )
        seen[letters] = i
        entries.append(Entry(answer, clue))
    return CrosswordFile(tuple(entries), file.title)


def load_crossword(path: Path) -> CrosswordFile:
    """Read a crossword file: {"title"?, "entries": [{"answer", "clue"}, ...]}; ``-`` reads stdin."""
    text, name = _read(path, "crossword file")
    return parse_crossword(text, name)


def parse_crossword(text: str, name: str = "crossword file") -> CrosswordFile:
    data = _object(text, name, CROSSWORD_FIELDS, CROSSWORD_EXPECTED)
    raw = data.get("entries")
    if raw is None:
        raise PrinterError(f"{name} is missing entries", hint=CROSSWORD_EXPECTED, exit_code=3)
    if not isinstance(raw, list):
        raise PrinterError(f"entries in {name} must be a list", hint=CROSSWORD_EXPECTED, exit_code=3)
    entries = []
    for i, item in enumerate(raw):
        where = f"entries[{i}]"
        if not isinstance(item, dict):
            raise PrinterError(f"{where} must be an object", hint=CROSSWORD_EXPECTED, exit_code=3)
        unknown = sorted(set(item) - set(ENTRY_FIELDS))
        if unknown:
            raise PrinterError(
                f"unknown field(s) in {where}: {', '.join(unknown)}",
                hint="allowed fields: " + ", ".join(ENTRY_FIELDS),
                exit_code=3,
            )
        for key in ENTRY_FIELDS:
            if not isinstance(item.get(key), str):
                raise PrinterError(f"{where}.{key} must be text", hint=CROSSWORD_EXPECTED, exit_code=3)
        entries.append(Entry(item["answer"], item["clue"]))
    return check_crossword(CrosswordFile(tuple(entries), _file_title(data)), name, exit_code=3)


Placement = tuple[int, int, bool]  # row, col, across


def _fits(
    board: dict[tuple[int, int], tuple[str, set[bool]]],
    word: str,
    r: int,
    c: int,
    across: bool,
) -> int | None:
    """How many letters ``word`` at (r, c) shares with the board, or None when it cannot go there: a letter
    clashes, it runs on into another word, it lies alongside one, or it would make the grid too big."""
    dr, dc = (0, 1) if across else (1, 0)
    before, after = (r - dr, c - dc), (r + dr * len(word), c + dc * len(word))
    if before in board or after in board:
        return None
    crossings = 0
    for k, ch in enumerate(word):
        cell = (r + dr * k, c + dc * k)
        if cell in board:
            letter, ways = board[cell]
            if letter != ch or across in ways:
                return None
            crossings += 1
            continue
        side_a, side_b = (cell[0] + dc, cell[1] + dr), (cell[0] - dc, cell[1] - dr)
        if side_a in board or side_b in board:
            return None
    cells = list(board) + [(r + dr * k, c + dc * k) for k in range(len(word))]
    rows = [cell[0] for cell in cells]
    cols = [cell[1] for cell in cells]
    if max(rows) - min(rows) >= MAX_CROSSWORD or max(cols) - min(cols) >= MAX_CROSSWORD:
        return None
    return crossings


def _arrange(words: list[str], order: list[int]) -> dict[int, Placement]:
    """Place ``words`` in ``order``: the first across, each next one where it crosses most letters (then where it
    keeps the grid smallest); one that cannot cross anything yet is tried again after the others."""
    board: dict[tuple[int, int], tuple[str, set[bool]]] = {}
    placed: dict[int, Placement] = {}

    def put(index: int, r: int, c: int, across: bool) -> None:
        dr, dc = (0, 1) if across else (1, 0)
        for k, ch in enumerate(words[index]):
            cell = (r + dr * k, c + dc * k)
            board.setdefault(cell, (ch, set()))[1].add(across)
        placed[index] = (r, c, across)

    first, *rest = order
    put(first, 0, 0, True)
    waiting = rest
    while waiting:
        left = []
        for index in waiting:
            word = words[index]
            best: tuple[tuple[int, int], Placement] | None = None
            for (br, bc), (letter, ways) in sorted(board.items()):
                for k, ch in enumerate(word):
                    if ch != letter:
                        continue
                    for across in (True, False):
                        if across in ways:
                            continue
                        r, c = (br, bc - k) if across else (br - k, bc)
                        crossings = _fits(board, word, r, c, across)
                        if not crossings:
                            continue
                        cells = list(board) + [
                            (r + (0 if across else j), c + (j if across else 0)) for j in range(len(word))
                        ]
                        area = (max(x for x, _ in cells) - min(x for x, _ in cells) + 1) * (
                            max(y for _, y in cells) - min(y for _, y in cells) + 1
                        )
                        score = (crossings, -area)
                        if best is None or score > best[0]:
                            best = (score, (r, c, across))
            if best is None:
                left.append(index)
            else:
                put(index, *best[1])
        if len(left) == len(waiting):
            break
        waiting = left
    return placed


@dataclass(frozen=True)
class CrosswordLayout:
    rows: int
    cols: int
    letters: dict[tuple[int, int], str]  # the answer squares, (row, col) from 0
    numbers: dict[tuple[int, int], int]  # the numbered squares
    clues: dict[str, list[tuple[int, str, str]]]  # "across"/"down": (number, clue, enumeration)


def _enumeration(answer: str) -> str:
    """The letter counts shown after a clue: ``(6)``, or ``(4,6)`` for "Buzz Aldrin", ``(3-4)`` for "Ice-cream"."""
    groups = []
    for word in answer.replace("-", " - ").split():
        if word == "-":
            groups.append("-")
        elif letters_of(word):
            if groups and groups[-1] != "-":
                groups.append(",")
            groups.append(str(len(letters_of(word))))
    return "(" + "".join(groups) + ")"


def crossword(file: CrosswordFile = DEFAULT_CROSSWORD, title: str | None = None) -> Puzzle:
    """The crossword of a checked crossword file: every answer placed so that it crosses another, in a grid of at
    most :data:`MAX_CROSSWORD` squares a side, numbered as printed crosswords are. The layout depends on the entries
    alone. An answer that cannot be made to cross the others is refused (exit code 3), naming it."""
    words = [letters_of(e.answer) for e in file.entries]
    by_length = sorted(range(len(words)), key=lambda k: (-len(words[k]), k))
    best: dict[int, Placement] = {}
    # Start from each answer in turn (longest first) and keep the first layout that places them all.
    for start in by_length:
        order = [start] + [k for k in by_length if k != start]
        placed = _arrange(words, order)
        if len(placed) > len(best):
            best = placed
        if len(best) == len(words):
            break
    missing = [k for k in range(len(words)) if k not in best]
    if missing:
        named = ", ".join(f"entries[{k}] ({file.entries[k].answer!r})" for k in missing[:5])
        raise PrinterError(
            f"the crossword cannot be laid out: {named} cannot cross the other answers",
            hint="add an answer that shares a letter with it, or leave it out",
            exit_code=3,
        )
    min_r = min(r for r, _, _ in best.values())
    min_c = min(c for _, c, _ in best.values())
    letters: dict[tuple[int, int], str] = {}
    starts: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    for index, (r, c, across) in best.items():
        r, c = r - min_r, c - min_c
        dr, dc = (0, 1) if across else (1, 0)
        for k, ch in enumerate(words[index]):
            letters[(r + dr * k, c + dc * k)] = ch
        starts.setdefault((r, c), []).append((index, across))
    rows = max(r for r, _ in letters) + 1
    cols = max(c for _, c in letters) + 1
    numbers: dict[tuple[int, int], int] = {}
    clues: dict[str, list[tuple[int, str, str]]] = {"across": [], "down": []}
    solution: dict[str, Any] = {"rows": rows, "cols": cols, "across": [], "down": []}
    for cell in sorted(starts):
        numbers[cell] = len(numbers) + 1
        for index, across in sorted(starts[cell], key=lambda item: not item[1]):
            entry = file.entries[index]
            way = "across" if across else "down"
            clues[way].append((numbers[cell], entry.clue, _enumeration(entry.answer)))
            solution[way].append(
                {
                    "number": numbers[cell],
                    "answer": words[index],
                    "written": entry.answer,
                    "clue": entry.clue,
                    "row": cell[0] + 1,
                    "col": cell[1] + 1,
                    "length": len(words[index]),
                }
            )
    solution["grid"] = ["".join(letters.get((r, c), ".") for c in range(cols)) for r in range(rows)]
    layout = CrosswordLayout(rows, cols, letters, numbers, clues)
    return Puzzle("crossword", title or file.title or TITLES["crossword"], None, None, solution, layout)


# --- maze ----------------------------------------------------------------------------------------------------------

MAZE_SIZE = {"easy": (10, 13), "medium": (16, 21), "hard": (24, 32)}  # (columns, rows)


@dataclass(frozen=True)
class MazeLayout:
    cols: int
    rows: int
    open: frozenset[tuple[tuple[int, int], tuple[int, int]]]  # pairs of neighbouring cells with no wall between
    path: tuple[tuple[int, int], ...]


def maze(seed: int, difficulty: str = DEFAULT_DIFFICULTY, title: str = TITLES["maze"]) -> Puzzle:
    """A perfect maze (one way between any two squares) dug by a randomised depth-first walk; the way in is at the
    top-left square and the way out at the bottom-right one. The solution is the path between them."""
    cols, rows = MAZE_SIZE[difficulty]
    rng = random.Random(f"maze:{seed}")
    open_: set[frozenset[tuple[int, int]]] = set()
    seen = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        r, c = stack[-1]
        nexts = [
            (r + dr, c + dc)
            for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0))
            if 0 <= r + dr < rows and 0 <= c + dc < cols and (r + dr, c + dc) not in seen
        ]
        if not nexts:
            stack.pop()
            continue
        cell = rng.choice(nexts)
        open_.add(frozenset(((r, c), cell)))
        seen.add(cell)
        stack.append(cell)
    start, end = (0, 0), (rows - 1, cols - 1)
    came: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    queue = deque([start])
    while queue:
        r, c = queue.popleft()
        for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            cell = (r + dr, c + dc)
            if cell not in came and frozenset(((r, c), cell)) in open_:
                came[cell] = (r, c)
                queue.append(cell)
    path = []
    step: tuple[int, int] | None = end
    while step is not None:
        path.append(step)
        step = came[step]
    path.reverse()
    solution = {
        "rows": rows,
        "cols": cols,
        "start": [1, 1],
        "finish": [rows, cols],
        "path": [[r + 1, c + 1] for r, c in path],
    }
    # Each pair holds two cells; sorted() gives them as a list, so the 2-tuple shape is not inferred.
    pairs = frozenset(cast(tuple[tuple[int, int], tuple[int, int]], tuple(sorted(pair))) for pair in open_)
    return Puzzle("maze", title, seed, difficulty, solution, MazeLayout(cols, rows, pairs, tuple(path)))


# --- the page ------------------------------------------------------------------------------------------------------

MARGIN = 90
INK = (28, 28, 36)
LINE = (60, 60, 72)
GREY = (110, 110, 120)
TITLE_TOP = 100
TITLE_SIZE, TITLE_MIN = 110, 64
BODY_TOP = 270  # where the puzzle starts, under the title
FOOT = H - 80  # the label line
FOOTER_SIZE = 36


def _title(d: ImageDraw.ImageDraw, title: str) -> Cuts:
    font, size, lines, cut = _fit_face(title, "hand-bold", TITLE_SIZE, TITLE_MIN, W - 2 * MARGIN, 1)
    d.text((W / 2, TITLE_TOP + TITLE_SIZE / 2), lines[0] if lines else "", font=font, fill=INK, anchor="mm")
    return {"title": cut} if cut else {}


def _foot(d: ImageDraw.ImageDraw, puzzle: Puzzle) -> None:
    d.text((W / 2, FOOT), puzzle.label(), font=fonts.face("hand", FOOTER_SIZE), fill=GREY, anchor="mm")


def _sudoku_page(d: ImageDraw.ImageDraw, layout: SudokuLayout) -> None:
    cell = 110
    size = cell * 9
    x0, y0 = (W - size) // 2, BODY_TOP + 60
    digit = fonts.face("body-bold", 66)
    label = fonts.face("body", 30)
    for k in range(9):
        d.text((x0 - 34, y0 + k * cell + cell / 2), "ABCDEFGHI"[k], font=label, fill=GREY, anchor="mm")
        d.text((x0 + k * cell + cell / 2, y0 - 30), str(k + 1), font=label, fill=GREY, anchor="mm")
    for i, v in enumerate(layout.givens):
        if v:
            r, c = divmod(i, 9)
            d.text((x0 + c * cell + cell / 2, y0 + r * cell + cell / 2 + 2), str(v), font=digit, fill=INK, anchor="mm")
    for k in range(10):
        width = 6 if k % 3 == 0 else 2
        d.line((x0 + k * cell, y0, x0 + k * cell, y0 + size), fill=LINE, width=width)
        d.line((x0, y0 + k * cell, x0 + size, y0 + k * cell), fill=LINE, width=width)
    hint = fonts.face("hand", 38)
    d.text(
        (W / 2, y0 + size + 90),
        "Fill every row, column and 3×3 box with the digits 1 to 9.",
        font=hint,
        fill=GREY,
        anchor="mm",
    )


LIST_INDENT = 20  # an item's indent inside its column of the word list


def _columns(
    d: ImageDraw.ImageDraw, items: list[str], top: int, bottom: int, columns: int, role: str, size: int, min_size: int
) -> Cuts:
    """``items`` in ``columns`` columns between ``top`` and ``bottom``, each on one line inside its column,
    as large as fits: the type shrinks (``size`` down to ``min_size``) for the rows and the widest item to
    hold, then the columns widen (fewer of them) when an item still is too wide. An item too long even for
    a column of its own is cut with "…" and named ``words[i]`` (its place in the list) in the cuts."""
    fitted = None
    for cols in range(columns, 0, -1):
        width = (W - 2 * MARGIN) // cols
        per = -(-len(items) // cols)
        for s in range(size, min_size - 1, -2):
            font, lead = fonts.face(role, s), round(s * 1.35)
            if per * lead <= bottom - top and all(
                font.getlength(_printable_face(item, font)) <= width - LIST_INDENT for item in items
            ):
                fitted = font, lead, width, per
                break
        if fitted:
            break
    if fitted is None:
        # Even a column of its own cannot take an item: keep the asked-for columns and cut it below.
        fitted = (
            fonts.face(role, min_size),
            round(min_size * 1.35),
            (W - 2 * MARGIN) // columns,
            -(-len(items) // columns),
        )
    font, lead, width, per = fitted
    cut: Cuts = {}
    for k, item in enumerate(items):
        col, row = divmod(k, per)
        shown = _printable_face(item, font)
        if font.getlength(shown) > width - LIST_INDENT:
            ellipsized = _ellipsize(shown, font, width - LIST_INDENT)
            cut[f"words[{k}]"] = _in_source(_cut(shown, [ellipsized]), item, lambda c: _printable_face(c, font))
            shown = ellipsized
        d.text((MARGIN + col * width + LIST_INDENT, top + row * lead), shown, font=font, fill=INK)
    return cut


def _search_page(d: ImageDraw.ImageDraw, layout: SearchLayout) -> Cuts:
    size = len(layout.grid)
    cell = min(68, (W - 2 * MARGIN) // size)
    side = cell * size
    x0, y0 = (W - side) // 2, BODY_TOP + 30
    font = fonts.face("body-bold", round(cell * 0.62))
    for r, row in enumerate(layout.grid):
        for c, ch in enumerate(row):
            d.text((x0 + c * cell + cell / 2, y0 + r * cell + cell / 2), ch, font=font, fill=INK, anchor="mm")
    d.rounded_rectangle((x0 - 14, y0 - 14, x0 + side + 14, y0 + side + 14), radius=18, outline=LINE, width=4)
    top = y0 + side + 70
    hint = fonts.face("hand", 38)
    d.text(
        (W / 2, top),
        f"Find the {len(layout.words)} words. Circle each one you find.",
        font=hint,
        fill=GREY,
        anchor="mm",
    )
    return _columns(d, list(layout.words), top + 50, FOOT - 60, 3 if len(layout.words) <= 15 else 4, "body", 40, 22)


CLUE_INDENT = 50
CLUE_GUTTER = 30  # between the two columns of clues
CLUE_SIZE, CLUE_MIN = 32, 18
CELL_MAX, CELL_MIN = 66, 34  # a crossword square, in pixels at 150 dpi (11 mm down to about 6 mm)
GRID_H = 760  # the most height the grid takes, so the clues keep room under it


def _clue_blocks(layout: CrosswordLayout, font, width: int) -> list[tuple[str, list[str]]]:
    """The clues as blocks kept together in a column: a heading ("Across", "Down", with no lines) or a clue's number
    and its wrapped lines (the clue and its letter count)."""
    blocks: list[tuple[str, list[str]]] = []
    for way in ("across", "down"):
        if not layout.clues[way]:
            continue
        blocks.append((way.title(), []))
        for number, clue, enumeration in layout.clues[way]:
            blocks.append(
                (
                    str(number),
                    _wrap(_printable_face(f"{clue} {enumeration}", font), font, width - CLUE_INDENT - CLUE_GUTTER),
                )
            )
    return blocks


def _flow(blocks: list[tuple[str, list[str]]], lead: int, room: int, columns: int) -> list[list[int]] | None:
    """Which blocks go in which column, filling each top to bottom, or None when they need more columns. A heading
    takes 1.6 lines and never ends a column."""
    out: list[list[int]] = [[]]
    used = 0
    for k, (_, lines) in enumerate(blocks):
        height = len(lines) * lead if lines else round(lead * 1.6)
        heading_last = not lines and k + 1 < len(blocks)
        need = height + (len(blocks[k + 1][1]) * lead if heading_last else 0)
        if used + need > room and out[-1]:
            if len(out) == columns:
                return None
            out.append([])
            used = 0
        out[-1].append(k)
        used += height
    return out


def _crossword_fit(layout: CrosswordLayout) -> tuple[int, int, list[tuple[str, list[str]]], list[list[int]]]:
    """The largest squares (then the largest clue type) that let the grid and every clue fit on the page: the cell
    size, the clue size, the clue blocks and how they flow into two columns."""
    col_w = (W - 2 * MARGIN) // 2
    widest = min(CELL_MAX, (W - 2 * MARGIN) // layout.cols)
    fallback = None
    for cell in [*range(widest, CELL_MIN, -2), CELL_MIN]:
        if cell * layout.rows > GRID_H and cell > CELL_MIN:
            continue
        room = FOOT - 70 - (BODY_TOP + 10 + cell * layout.rows + 50)
        for size in range(CLUE_SIZE, CLUE_MIN - 1, -2):
            blocks = _clue_blocks(layout, fonts.face("body", size), col_w)
            flow = _flow(blocks, round(size * 1.3), room, 2)
            if flow is not None:
                return cell, size, blocks, flow
            fallback = (cell, size, blocks)
    cell, size, blocks = fallback  # type: ignore[misc]  # the CELL_MIN pass always sets fallback: never None here
    # Limits on entries and clues keep this from happening; if it did, the second column runs on past the room.
    return cell, size, blocks, [list(range(len(blocks) // 2)), list(range(len(blocks) // 2, len(blocks)))]


def _crossword_page(d: ImageDraw.ImageDraw, layout: CrosswordLayout) -> None:
    cell, size, blocks, flow = _crossword_fit(layout)
    x0, y0 = (W - cell * layout.cols) // 2, BODY_TOP + 10
    number = fonts.face("body", max(14, round(cell * 0.3)))
    for r, c in layout.letters:
        box = (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell)
        d.rectangle(box, outline=LINE, width=3)
        if (r, c) in layout.numbers:
            d.text((box[0] + 5, box[1] + 3), str(layout.numbers[(r, c)]), font=number, fill=INK)
    top = y0 + cell * layout.rows + 50
    col_w = (W - 2 * MARGIN) // 2
    font, bold, head = (fonts.face("body", size), fonts.face("body-bold", size), fonts.face("body-bold", size + 6))
    lead = round(size * 1.3)
    for k, column in enumerate(flow):
        x, y = MARGIN + k * col_w, top
        for index in column:
            label, lines = blocks[index]
            if not lines:
                d.text((x, y), label, font=head, fill=INK)
                y += round(lead * 1.6)
                continue
            d.text((x, y), label, font=bold, fill=INK)
            for line in lines:
                d.text((x + CLUE_INDENT, y), line, font=font, fill=INK)
                y += lead


def _maze_page(d: ImageDraw.ImageDraw, layout: MazeLayout) -> None:
    cell = min((W - 2 * MARGIN) // layout.cols, (FOOT - 140 - BODY_TOP - 60) // layout.rows)
    x0 = (W - cell * layout.cols) // 2
    y0 = BODY_TOP + 60
    width = max(4, cell // 9)
    pairs = layout.open

    def is_open(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return tuple(sorted((a, b))) in pairs

    for r in range(layout.rows):
        for c in range(layout.cols):
            x, y = x0 + c * cell, y0 + r * cell
            if c + 1 < layout.cols and not is_open((r, c), (r, c + 1)):
                d.line((x + cell, y, x + cell, y + cell), fill=INK, width=width)
            if r + 1 < layout.rows and not is_open((r, c), (r + 1, c)):
                d.line((x, y + cell, x + cell, y + cell), fill=INK, width=width)
    right, bottom = x0 + cell * layout.cols, y0 + cell * layout.rows
    d.line((x0 + cell, y0, right, y0), fill=INK, width=width)  # the top, open over the first square
    d.line((x0, bottom, right - cell, bottom), fill=INK, width=width)  # the bottom, open under the last
    d.line((x0, y0, x0, bottom), fill=INK, width=width)
    d.line((right, y0, right, bottom), fill=INK, width=width)
    label = fonts.face("hand-bold", 44)
    d.text((x0 + cell / 2, y0 - 34), "Start", font=label, fill=(46, 160, 67), anchor="mm")
    d.text((right - cell / 2, bottom + 36), "Finish", font=label, fill=(214, 48, 49), anchor="mm")


def compose(puzzle: Puzzle) -> tuple[Image.Image, Cuts]:
    """The A4 page of ``puzzle``: the title in handwriting, the puzzle, and the label at the foot that says which
    puzzle it is; and the texts cut with "…" to fit (``title``, or ``words[i]`` in a word search's list)."""
    page = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(page)
    cut = _title(d, puzzle.title)
    pages: dict[str, Callable[[ImageDraw.ImageDraw, Any], Cuts | None]] = {
        "sudoku": _sudoku_page,
        "wordsearch": _search_page,
        "crossword": _crossword_page,
        "maze": _maze_page,
    }
    more = pages[puzzle.kind](d, puzzle.layout)
    if more:
        cut.update(more)
    _foot(d, puzzle)
    return page, cut


def to_dict(file: CrosswordFile) -> dict[str, Any]:
    """A crossword file as JSON data (the schema's example)."""
    return {"title": file.title, "entries": [{"answer": e.answer, "clue": e.clue} for e in file.entries]}
