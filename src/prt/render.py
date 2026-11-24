"""Booth, card, article, compose, strip, instant and recipe pages as callable steps, shared by the CLI commands and the MCP server.

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
from prt.paper import A4, PHOTO_4X6, Paper

if TYPE_CHECKING:
    from PIL import Image

    from prt.article import Article
    from prt.booth import Story
    from prt.card import Card
    from prt.compose import Page
    from prt.drivers.base import Driver
    from prt.layout import Cut
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


def recipe_cards(recipe: Recipe) -> Rendered:
    """The 4×6 cards of a normalised recipe (see ``recipe.compose_cards``); every ingredient and step is printed,
    over more cards when one is not enough. Only a title too long for two lines is cut, and named in ``cut``."""
    from prt import recipe as recipes
    pages, cut = recipes.compose_cards(recipe)
    return Rendered(pages, cut, PHOTO_4X6)


def recipe_job_names(title: str, count: int) -> list[str]:
    return [f"prt recipe {number}/{count} {title[:60]}" for number in range(1, count + 1)]


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
