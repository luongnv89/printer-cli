"""What prt can print, as data: every page style with its fields, limits and a sample image, and the JSON Schemas of
the input files the print commands read.

``styles()`` lists the styles (``prt styles -f json``) and ``schema(name)`` returns one JSON Schema (``prt schema
NAME``). Both are built from the constants the commands check their input against (``booth.FIELDS``,
``booth.LIMITS``, ``card.LIMITS``, ``article.FIELDS``, ``vintage.MAX_PHOTOS``, …), so they say what the commands accept; the page
schema is built by :mod:`prt.compose`, which checks page files against it. Other front
ends (an MCP server, page validation) import this module instead of listing the styles again; a new input schema
is added with ``@register_schema``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from prt import article, booth, card, compose, vintage
from prt.drivers.base import PrinterError

# The sample pages shown in the README; assets/ is not in the package, so they are linked, not bundled.
SAMPLES = "https://raw.githubusercontent.com/luongnv89/printer-cli/main/assets/samples/"
JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
NON_BLANK = r"\S"  # a required text must have more than spaces in it


@dataclass(frozen=True)
class Field:
    """One text field of an input file, as a style prints it."""
    name: str
    description: str
    required: bool = False
    max_length: int | None = None  # in characters, runs of spaces counting as one; None: no limit of its own


@dataclass(frozen=True)
class Style:
    """One look a print command can give its page(s)."""
    name: str  # unique across commands
    command: str  # the prt command that prints it
    args: tuple[str, ...]  # what to add to the command to pick this style; () for its default
    kind: str  # what it prints, e.g. "wizard card"
    description: str
    schema: str  # the input file's JSON Schema: `prt schema <schema>`
    fields: tuple[Field, ...]
    photos: tuple[int, int]  # how many photos the command takes: (fewest, most)
    max_pages: int
    sample: str  # file name under SAMPLES

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": self.command, "args": list(self.args), "default": not self.args,
                "kind": self.kind, "description": self.description, "schema": self.schema,
                "fields": [asdict(f) for f in self.fields],
                "photos": {"min": self.photos[0], "max": self.photos[1]}, "max_pages": self.max_pages,
                "sample": SAMPLES + self.sample}


# --- what each field is on each style -------------------------------------------------------------

# Booth stories and the gazette share the fields of booth.Story; the themed styles read them differently.
NEWSPAPER_ROLES = {
    "headline": "Headline",
    "story": "Story text; a newline starts a new paragraph",
    "kicker": "Small label above the headline",
    "caption": "Line under the photo",
    "byline": "Author line, e.g. 'By Jo Smith'",
}
WIZARD_ROLES = {
    "headline": "The name on the card",
    "story": "The lore, in the box under the portrait; a newline starts a new paragraph",
    "kicker": "Card type at the top, e.g. 'Legendary Wizard'",
    "caption": "Subtitle under the name",
    "byline": "Footer line",
}
WANTED_ROLES = {
    "headline": "The name on the poster",
    "story": "The description under the photo; a newline starts a new paragraph",
    "kicker": "The 'Dead or alive' line at the top",
    "caption": "Reward line",
    "byline": "Footer line",
}
CERTIFICATE_ROLES = {
    "headline": "The name of the person the certificate is presented to",
    "story": "The citation: what it is awarded for, under the award line; a newline starts a new paragraph",
    "kicker": "The certificate's title at the top, e.g. 'Certificate of Achievement'",
    "caption": "The award, under the name, e.g. 'For the Brightest Smile of the Day'",
    "byline": "Who signed it, above the signature line, e.g. 'The Awards Committee'",
}
SPORTS_ROLES = {
    "headline": "The player's name, on the slanted bar across the photo",
    "story": "Stats and scouting report, in the box under the name; a newline starts a new paragraph",
    "kicker": "Team name at the top of the card",
    "caption": "Position and number, under the name, e.g. 'Forward · No. 23'",
    "byline": "Footer line, e.g. the series and card number",
}
MISSING_ROLES = {
    "headline": "The pet's name, under the photo",
    "story": "Description: what the pet looks like, where it was last seen; a newline starts a new paragraph",
    "kicker": "The question under MISSING, e.g. 'Have you seen this cat?'",
    "caption": "Reward line",
    "byline": "Contact, e.g. a phone number, printed on each tear-off tab",
}
MOVIE_ROLES = {
    "headline": "The film's title",
    "story": "The credits block at the foot of the poster; a newline starts a new paragraph",
    "kicker": "Tagline at the top",
    "caption": "Release line under the title, e.g. 'Coming soon'",
    "byline": "Rating box at the foot, e.g. 'Rated PG for scenes of extreme smiling'",
}
GAZETTE_ROLES = {**NEWSPAPER_ROLES, "caption": "Line under the lead photo"}
STORY_ROLES = {  # every booth style at once, for the style-independent story schema
    "headline": "Headline; the name on wizard, wanted, certificate, sports and missing, the film title on movie",
    "story": "Story text, or the lore / description on wizard and wanted, the citation on certificate, the stats on "
             "sports, the description on missing, the credits on movie; a newline starts a new paragraph",
    "kicker": "Small label above the headline; the card type on wizard, the 'Dead or alive' line on wanted, the "
              "title on certificate, the team on sports, the question on missing, the tagline on movie",
    "caption": "Line under the photo; the subtitle on wizard, the reward line on wanted and missing, the award on "
               "certificate, the position on sports, the release line on movie",
    "byline": "Author line, e.g. 'By Jo Smith'; the footer on wizard, wanted and sports, the signature on "
              "certificate, the tear-off tabs on missing, the rating on movie",
}
CARD_ROLES = {
    "greeting": "Greeting on the front, under the photo, e.g. 'Happy Birthday!'",
    "verse": "Verse on the inside right; a newline starts a new line, a blank line a new stanza",
    "message": "Short note on the inside left; when empty that panel is left blank to write on. A newline starts a "
               "new paragraph",
    "signature": "Line under the verse, e.g. 'With love, Jo'",
    "back": "Small line on the back of the card",
}
ARTICLE_ROLES = {
    "title": "Headline; when empty the source, else 'Untitled', is printed",
    "body": "The article text: one string with a newline between paragraphs, or a list of paragraphs",
    "author": "Author, printed as the byline ('By ...')",
    "source": "Site or publication name, printed above the headline",
    "url": "Link to the original, printed in the page footer on magazine",
}
ARTICLE_REQUIRED = ("title", "body")  # as article.parse refuses a file without them


def story_fields(roles: dict[str, str]) -> tuple[Field, ...]:
    """The booth.Story fields with what ``roles`` says each one is, required flags and length limits."""
    return tuple(Field(name, roles[name], name in booth.REQUIRED, booth.LIMITS[name]) for name in booth.FIELDS)


def card_fields() -> tuple[Field, ...]:
    return tuple(Field(name, CARD_ROLES[name], name in card.REQUIRED, card.LIMITS[name]) for name in card.FIELDS)


def article_fields() -> tuple[Field, ...]:
    return tuple(Field(name, ARTICLE_ROLES[name], name in ARTICLE_REQUIRED) for name in article.FIELDS)


# --- the styles ---------------------------------------------------------------------------------

BOOTH = {  # style: (description, roles, sample)
    "metro": ("Colour tabloid front page, two columns", NEWSPAPER_ROLES, "booth-metro.jpg"),
    "daily": ("Black-and-white broadsheet front page, three columns", NEWSPAPER_ROLES, "booth-daily.jpg"),
    "wizard": ("Ornate trading card: arched gold portrait frame, name ribbon and lore box", WIZARD_ROLES,
               "booth-wizard.jpg"),
    "wanted": ("Weathered parchment pirate poster with the name and a reward line", WANTED_ROLES,
               "booth-wanted.jpg"),
    "certificate": ("Award certificate: ivory paper in a green-and-gold border, oval portrait, name, award, "
                    "citation, date and signature lines and a gold seal", CERTIFICATE_ROLES,
                    "booth-certificate.jpg"),
    "sports": ("Sports trading card: royal-blue card with stripes, full-width photo, star badge, the name on a "
               "slanted red bar and a stats box", SPORTS_ROLES, "booth-sports.jpg"),
    "missing": ("Missing-pet poster: huge red MISSING, the question, the photo, name, reward, description and "
                "tear-off tabs with the contact", MISSING_ROLES, "booth-missing.jpg"),
    "movie": ("Movie poster: dark poster with a tagline, the photo fading into the title, stars, a release line, "
              "credits and a rating box", MOVIE_ROLES, "booth-movie.jpg"),
}
CARD_DESCRIPTION = ("Quarter-fold greeting card on one A4 sheet: the photo in a gold frame and the greeting on the "
                    "front, a message inside left, the verse and signature inside right, a line on the back; folds "
                    "into an A6 card with every panel the right way up")
ARTICLE = {  # style: (description, sample)
    "magazine": ("Bold headline, three columns, colour accents", "article-magazine.jpg"),
    "classic-magazine": ("Ivory page, centred single column, drop cap, ornaments", "article-classic.jpg"),
}
GAZETTE_KIND = "vintage newspaper"
GAZETTE_DESCRIPTION = "Cream paper and one ink, a blackletter masthead and ruled columns; every photo is printed"


def _build() -> tuple[Style, ...]:
    # Iterating the commands' own tables means a style added there and not here fails at import, in the tests.
    out = [Style(name, "booth", () if name == "metro" else ("--style", name), booth.KINDS[name], BOOTH[name][0],
                 "story", story_fields(BOOTH[name][1]), (1, 1), 1, BOOTH[name][2]) for name in booth.STYLES]
    out.append(Style("card", "card", (), card.KIND, CARD_DESCRIPTION, "card", card_fields(), (1, 1), 1, "card.jpg"))
    out += [Style(name, "article", () if name == "magazine" else ("--style", name),
                  article.ARTICLE_STYLES[name].kind, ARTICLE[name][0], "article", article_fields(), (0, 0),
                  article.MAX_PAGES, ARTICLE[name][1]) for name in article.ARTICLE_STYLES]
    gazette: dict[str, Any] = {"command": "gazette", "kind": GAZETTE_KIND, "schema": "gazette",
                               "fields": story_fields(GAZETTE_ROLES), "photos": (1, vintage.MAX_PHOTOS),
                               "max_pages": vintage.MAX_PAGES}
    out += [Style("gazette", args=(), description=f"{GAZETTE_DESCRIPTION}. Upright A4: the lead photo above the "
                  "story, plates two abreast", sample="gazette.jpg", **gazette),
            Style("gazette-landscape", args=("--landscape",), description=f"{GAZETTE_DESCRIPTION}. A4 on its side: "
                  "the lead photo beside the story, plates three abreast", sample="gazette-landscape.jpg",
                  **gazette)]
    return tuple(out)


STYLES = _build()


def styles() -> list[dict[str, Any]]:
    """Every style, as ``prt styles -f json`` prints it."""
    return [s.to_dict() for s in STYLES]


# --- JSON Schemas of the input files --------------------------------------------------------------

SchemaBuilder = Callable[[], dict[str, Any]]
_SCHEMAS: dict[str, tuple[str, SchemaBuilder]] = {}  # name: (what it describes, builder), in registration order


def register_schema(name: str, summary: str) -> Callable[[SchemaBuilder], SchemaBuilder]:
    """Add a schema to ``prt schema``: decorate a function that returns it; ``summary`` says what it describes."""
    def add(build: SchemaBuilder) -> SchemaBuilder:
        if name in _SCHEMAS:
            raise ValueError(f"schema {name!r} is already registered")
        _SCHEMAS[name] = (summary, build)
        return build
    return add


def schema_names() -> list[str]:
    return list(_SCHEMAS)


def schema_summaries() -> dict[str, str]:
    return {name: summary for name, (summary, _) in _SCHEMAS.items()}


def schema(name: str) -> dict[str, Any]:
    """The JSON Schema called ``name``, built fresh (the caller may change it); exit code 2 for an unknown name."""
    if name not in _SCHEMAS:
        raise PrinterError(f"unknown schema: {name}", hint="choose one of: " + ", ".join(_SCHEMAS), exit_code=2)
    return _SCHEMAS[name][1]()


TEXT_NOTE = ("Lengths count characters, with each run of spaces as one. Text is printed in Latin letters: accents "
             "are printed without their marks, and characters the page font cannot print, such as emoji or "
             "Chinese, are refused.")


def _text(field: Field, non_blank: bool | None = None) -> dict[str, Any]:
    """A text property; ``non_blank`` (default: when the field is required) refuses an empty or all-space text."""
    prop: dict[str, Any] = {"type": "string", "description": field.description}
    if field.max_length is not None:
        prop["maxLength"] = field.max_length
    if field.required if non_blank is None else non_blank:
        prop.update(minLength=1, pattern=NON_BLANK)
    return prop


def _story_schema(title: str, description: str, roles: dict[str, str], example: booth.Story) -> dict[str, Any]:
    fields = story_fields(roles)
    return {"$schema": JSON_SCHEMA, "title": title, "description": f"{description} {TEXT_NOTE}", "type": "object",
            "properties": {f.name: _text(f) for f in fields}, "required": [f.name for f in fields if f.required],
            "additionalProperties": False, "examples": [asdict(example)]}


@register_schema("story", "photo booth story file: prt booth PHOTO --story-file story.json")
def story_schema() -> dict[str, Any]:
    return _story_schema("prt booth story", "The story file of `prt booth --story-file` (a file, or - for stdin), "
                         f"for every style ({', '.join(booth.STYLES)}); at most {booth.MAX_STORY_FILE // 1024} KiB.",
                         STORY_ROLES, booth.DEFAULT_STORY)


@register_schema("article", "article JSON file: prt article article.json (or JSON piped to prt article -)")
def article_schema() -> dict[str, Any]:
    props = {f.name: _text(f, non_blank=False) for f in article_fields()}  # an empty title is allowed
    paragraph = {"type": "string", "pattern": r"[^\s?-]"}  # a paragraph of only dashes or "?" is dropped
    props["body"] = {"description": ARTICLE_ROLES["body"], "anyOf": [
        paragraph, {"type": "array", "items": {"type": "string"}, "contains": paragraph}]}
    return {"$schema": JSON_SCHEMA, "title": "prt article",
            "description": "An article for `prt article`, saved as a .json file or piped to `prt article -`; at "
                           f"most {article.MAX_BYTES // (1024 * 1024)} MiB, and at most {article.MAX_PAGES} pages "
                           "once laid out. Text is printed in Latin letters: an article mostly in another script "
                           "is refused, and emoji are left out.",
            "type": "object", "properties": props, "required": list(ARTICLE_REQUIRED), "additionalProperties": False,
            "examples": [{"title": "A Short Walk", "author": "A. Writer", "source": "Example Blog",
                          "url": "https://example.com/walk",
                          "body": ["The first paragraph.", "The second paragraph."]}]}


@register_schema("gazette", "vintage gazette story file: prt gazette PHOTO... --story-file story.json")
def gazette_schema() -> dict[str, Any]:
    return _story_schema("prt gazette story", "The story file of `prt gazette --story-file` (a file, or - for "
                         f"stdin); at most {booth.MAX_STORY_FILE // 1024} KiB. The photos, 1 to "
                         f"{vintage.MAX_PHOTOS}, are given on the command line.", GAZETTE_ROLES,
                         vintage.DEFAULT_GAZETTE_STORY)


@register_schema("page", "compose page file: prt compose page.json (or JSON piped to prt compose -)")
def page_schema() -> dict[str, Any]:
    # Built by prt.compose, which checks page files against this same schema.
    return compose.page_schema()


@register_schema("card", "greeting card file: prt card PHOTO --card-file card.json")
def card_schema() -> dict[str, Any]:
    fields = card_fields()
    return {"$schema": JSON_SCHEMA, "title": "prt card",
            "description": "The card file of `prt card --card-file` (a file, or - for stdin); at most "
                           f"{card.MAX_CARD_FILE // 1024} KiB. The photo is given on the command line. {TEXT_NOTE} "
                           "In the verse each newline counts as a character, and a run of blank lines counts as one "
                           "blank line.",
            "type": "object", "properties": {f.name: _text(f) for f in fields},
            "required": [f.name for f in fields if f.required], "additionalProperties": False,
            "examples": [asdict(card.DEFAULT_CARD)]}
