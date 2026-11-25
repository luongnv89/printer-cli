"""Booth, card, comic, article, compose, strip, instant, colouring, recipe, postcard, disc, bingo and puzzle pages as callable steps, shared by the CLI commands and the MCP server.

Each front end reads its own input (files and flags, or MCP tool arguments) and decides how to show the result;
what happens in between is here: lay the pages out, say what did not fit and what printing them would cost, and
send them to the printer one page at a time under a :mod:`prt.guard` ticket, on the paper they were laid out
for (see :mod:`prt.paper`).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prt import guard
from prt.paper import A4, DISC, PHOTO_4X6, Paper

if TYPE_CHECKING:
    from PIL import Image

    from prt.article import Article
    from prt.bingo import Bingo
    from prt.booth import Story
    from prt.card import Card
    from prt.comic import Comic
    from prt.compose import Page
    from prt.disc import Disc
    from prt.drivers.base import Driver
    from prt.layout import Cut
    from prt.morning import Morning
    from prt.postcard import Postcard
    from prt.receipt import Receipt
    from prt.textgazette import TextGazette
    from prt.puzzle import Puzzle
    from prt.recipe import Recipe


@dataclass
class Rendered:
    """Laid-out pages: ``cut`` maps each field cut with "…" to fit (in page order) to how much of it was printed;
    ``paper`` is the sheet the pages were laid out for."""
    pages: list[Image.Image]
    cut: dict[str, Cut] = field(default_factory=dict)
    paper: Paper = A4

    def fit(self) -> dict[str, Any]:
        return fit_report(self.cut)

    def estimate(self) -> dict[str, Any]:
        return {"estimate": guard.estimate(self.pages, self.paper)}

    def jpegs(self) -> list[bytes]:
        """The pages as the printer takes them (see ``Paper.to_jpeg``)."""
        return [self.paper.to_jpeg(page) for page in self.pages]


def fit_report(cut: dict[str, Any]) -> dict[str, Any]:
    """What a booth or gazette JSON line says about the text that did not fit: ``fits`` is false when any
    field was cut with "…", and ``cut`` maps each cut field (in page order) to how many of its characters
    were printed (``max_fit``) and how many were given, so the text can be shortened and tried again."""
    return {"fits": not cut, "cut": {name: c.to_dict() for name, c in cut.items()}}


def booth_story(style: str, base: Story | None = None, **overrides: str | None) -> Story:
    """The story a booth page prints: ``base`` (the style's own story when None) with ``overrides`` applied
    over it, checked for required fields, length limits and printable characters."""
    from prt import booth
    return booth.with_overrides(booth.default_story(style) if base is None else base, **overrides)


def booth_page(photo: Image.Image, story: Story, style: str) -> Rendered:
    """One booth page of ``photo`` in ``style`` (see ``booth.STYLES``)."""
    from prt import booth
    page, cut = booth.compose(photo, story, style)
    return Rendered([page], cut)


def card_page(photo: Image.Image, card: Card) -> Rendered:
    """The greeting card sheet of ``photo`` and ``card`` (see ``card.compose``): four panels on one A4 sheet,
    laid out to fold into a card."""
    from prt import card as cards
    page, cut = cards.compose(photo, card)
    return Rendered([page], cut)


def comic_page(photos: Sequence[Image.Image], comic: Comic | None = None) -> Rendered:
    """The comic page of 3 to 6 ``photos`` as its panels, with the title, captions and bubbles of ``comic`` (see
    ``comic.compose``); bubbles and captions cut with "…" to fit are named ``panels[i].bubbles[j]`` and
    ``panels[i].caption`` in ``cut``."""
    from prt import comic as comics
    page, cut = comics.compose(photos, comic)
    return Rendered([page], cut)


def text_gazette_page(document: TextGazette) -> Rendered:
    """Lay a sectioned text gazette out as one A4 page."""
    from prt.textgazette import compose_page
    page, cut = compose_page(document)
    return Rendered([page], cut)


def morning_page(paper: Morning) -> Rendered:
    """Lay a validated morning-edition file out as one A4 page."""
    from prt.morning import document
    return text_gazette_page(document(paper))


def receipt_page(receipt: Receipt) -> Rendered:
    """Lay collected commits out as one A4 commit receipt."""
    from prt.receipt import document
    return text_gazette_page(document(receipt))


def article_pages(text: Article, style: str) -> Rendered:
    """The pages of a normalised article in ``style`` (see ``article.ARTICLE_STYLES``); every word is printed,
    so nothing is ever cut."""
    from prt import article
    return Rendered(article.compose_pages(text, style))


def article_job_names(title: str, count: int) -> list[str]:
    return [f"prt article {number}/{count} {title[:60]}" for number in range(1, count + 1)]


def composed_pages(page: Page) -> Rendered:
    """The pages of a checked page file (see ``compose.load``); blocks run on to the next page, so nothing is
    ever cut."""
    from prt import compose
    return Rendered(compose.compose_pages(page))


def compose_job_names(title: str, count: int) -> list[str]:
    return [f"prt compose {number}/{count} {title[:60]}" for number in range(1, count + 1)]


def strip_page(photos: Sequence[Image.Image], caption: str, mono: bool = False) -> Rendered:
    """The photo-booth strip sheet of four ``photos`` (see ``photocard.strip``) on 4×6 photo paper."""
    from prt import photocard
    page, cut = photocard.strip(photos, caption, mono)
    return Rendered([page], cut, PHOTO_4X6)


def instant_page(photo: Image.Image, caption: str) -> Rendered:
    """The instant-photo print of ``photo`` with a handwritten ``caption`` (see ``photocard.instant``) on 4×6."""
    from prt import photocard
    page, cut = photocard.instant(photo, caption)
    return Rendered([page], cut, PHOTO_4X6)


def colour_in_page(photo: Image.Image) -> Rendered:
    """The colouring page of ``photo`` (see ``colourin.compose``): its line art, black on white, on A4 (on its
    side for a landscape photo). Nothing is ever cut."""
    from prt import colourin
    page, cut = colourin.compose(photo)
    return Rendered([page], cut)


def recipe_cards(recipe: Recipe) -> Rendered:
    """The 4×6 cards of a normalised recipe (see ``recipe.compose_cards``); every ingredient and step is printed,
    over more cards when one is not enough. Only a title too long for two lines is cut, and named in ``cut``."""
    from prt import recipe as recipes
    pages, cut = recipes.compose_cards(recipe)
    return Rendered(pages, cut, PHOTO_4X6)


def recipe_job_names(title: str, count: int) -> list[str]:
    return [f"prt recipe {number}/{count} {title[:60]}" for number in range(1, count + 1)]


def bingo_cards(game: Bingo, cards: Sequence[tuple[int, ...]]) -> Rendered:
    """One A4 page per bingo card (see ``bingo.compose_card``), ``cards`` being the word indices ``bingo.deal``
    gave; a title or word cut with "…" to fit is named in ``cut`` (``title``, ``words[i]``)."""
    from prt import bingo
    pages, cut = bingo.compose_cards(game, list(cards))
    return Rendered(pages, cut)


def bingo_job_names(title: str, count: int) -> list[str]:
    return [f"prt bingo {number}/{count} {title[:60]}" for number in range(1, count + 1)]


def puzzle_page(puzzle: Puzzle) -> Rendered:
    """The A4 page of a sudoku, word search, crossword or maze (see ``puzzle.compose``); a title too long for
    its line or a word-search word too wide for its column is cut with "…" and named in ``cut`` (``title``,
    ``words[i]``). The solution stays with ``puzzle`` (``puzzle.answers()``)."""
    from prt import puzzle as puzzles
    page, cut = puzzles.compose(puzzle)
    return Rendered([page], cut)


def postcard_sides(photo: Image.Image, card: Postcard) -> Rendered:
    """The two sides of a postcard, front then back (see ``postcard.compose``), each upright as it is read
    (landscape) on 4×6 photo paper."""
    from prt import postcard
    pages, cut = postcard.compose(photo, card)
    return Rendered(pages, cut, PHOTO_4X6)


def disc_label(photo: Image.Image, disc: Disc) -> Rendered:
    """The disc label of ``photo`` and ``disc`` (see ``disc.compose``) on a printable CD/DVD, white outside the
    printable ring."""
    from prt import disc as discs
    page, cut = discs.compose(photo, disc)
    return Rendered([page], cut, DISC)


def send_two_sided(queue: str, pages: Sequence[Image.Image], name: str, ticket: guard.Ticket,
                   paper: Paper = A4) -> int:
    """Submit ``pages`` (front, back) as one two-sided job on ``paper`` through the CUPS queue ``queue``,
    returning the CUPS job id.

    The printer takes one image per IPP job, so the sides go to CUPS as one PDF of upright pages, flipped on the
    short edge: a landscape side turned upright for the printer is read turned over like a book page. Both pages
    are booked on ``ticket`` right before the submit.
    """
    import tempfile
    from dataclasses import replace
    from pathlib import Path

    from prt import media, printing
    upright = [paper.for_printer(page) for page in pages]
    options = replace(paper.options(name), media=media.resolve_alias(paper.media), sides="two-sided-short-edge")
    with tempfile.TemporaryDirectory(prefix="prt-") as folder:
        path = Path(folder) / "postcard.pdf"
        upright[0].save(path, "PDF", resolution=paper.dpi, quality=92, save_all=True, append_images=upright[1:])
        ticket.attempt(len(pages))
        return printing.cups_print(queue, path, options)


def send_pages(driver: Driver, pages: Sequence[bytes], names: Sequence[str], ticket: guard.Ticket,
               paper: Paper = A4) -> Iterator[int]:
    """Submit each JPEG page as its own job on ``paper`` (its media size, tray and media type; A4 by default),
    yielding the job ids in order.

    Each page is booked on ``ticket`` right before its submit, so a failure part-way gives back only the pages
    never attempted; the caller collects the ids it was given and so knows what is already on the printer.
    """
    for data, name in zip(pages, names, strict=True):
        ticket.attempt(1)
        yield driver.submit(data, "image/jpeg", paper.options(name))
