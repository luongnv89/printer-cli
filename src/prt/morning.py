"""The morning-edition JSON file: validation, schema and conversion to a text gazette."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from prt import booth
from prt.drivers.base import PrinterError
from prt.layout import _clean, _glyphs
from prt.textgazette import Section, TextBlock, TextGazette

MORNING_FILE_LIMIT = 32 * 1024
MAX_DETAILS = 4
MAX_ITEMS = 10
MAX_HEADLINES = 6

LIMITS = {
    "title": 80,
    "date": 60,
    "byline": 80,
    "weather.summary": 160,
    "weather.details[]": 140,
    "calendar.items[]": 120,
    "todos.items[]": 120,
    "headlines.items[].title": 120,
    "headlines.items[].summary": 240,
    "headlines.items[].source": 60,
}
REQUIRED = ("weather.summary", "calendar.items", "todos.items", "headlines.items[].title")


@dataclass(frozen=True)
class Headline:
    title: str
    summary: str = ""
    source: str = ""


@dataclass(frozen=True)
class Morning:
    title: str
    date: str
    byline: str
    weather_summary: str
    weather_details: tuple[str, ...]
    calendar_items: tuple[str, ...]
    todo_items: tuple[str, ...]
    headlines: tuple[Headline, ...]


def _bad(message: str) -> PrinterError:
    return PrinterError(message, hint="prt schema morning lists the file's shape and limits", exit_code=3)


def _text(value, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise _bad(f"{field} must be a string")
    text = _clean(value, keep_newlines=False)
    if not text:
        raise _bad(f"{field} must not be empty")
    if len(text) > max_length:
        raise _bad(f"{field} must be at most {max_length} characters")
    unprintable = sorted({c for c in text if _glyphs(c) is None})
    if unprintable:
        shown = ", ".join(f"{c} (U+{ord(c):04X})" for c in unprintable[:10])
        raise _bad(f"{field} has characters the page font cannot print: {shown}")
    return text


def _object(value, field: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise _bad(f"{field} must be an object")
    for key in sorted(set(value) - allowed):
        raise _bad(f"unknown {field}.{key}")
    return value


def _string_list(value, field: str, item_limit: int, count_limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _bad(f"{field} must be a list")
    if len(value) > count_limit:
        raise _bad(f"{field} must have at most {count_limit} items")
    return tuple(_text(item, f"{field}[{index}]", item_limit) for index, item in enumerate(value))


def parse_morning(text: str, name: str = "morning file", kind: str = "morning file") -> Morning:
    """Validate morning-edition JSON and return it as a :class:`Morning` (``PrinterError`` exit 3 when bad)."""
    if len(text.encode("utf-8")) > MORNING_FILE_LIMIT:
        raise _bad(f"{name} is over {MORNING_FILE_LIMIT // 1024} KiB")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _bad(f"{name} is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}") from e
    if not isinstance(data, dict):
        raise _bad(f"{kind} must be a JSON object")
    for key in sorted(set(data) - {"title", "date", "byline", "weather", "calendar", "todos", "headlines"}):
        raise _bad(f"unknown {key}")
    for key in ("weather", "calendar", "todos", "headlines"):
        if key not in data:
            raise _bad(f"{kind} needs a {key} object")

    weather = _object(data["weather"], "weather", {"summary", "details"})
    if "summary" not in weather:
        raise _bad("weather needs a summary")
    summary = _text(weather["summary"], "weather.summary", LIMITS["weather.summary"])
    details: tuple[str, ...] = ()
    if "details" in weather:
        details = _string_list(weather["details"], "weather.details", LIMITS["weather.details[]"], MAX_DETAILS)

    calendar = _object(data["calendar"], "calendar", {"items"})
    todos = _object(data["todos"], "todos", {"items"})
    if "items" not in calendar:
        raise _bad("calendar needs items")
    if "items" not in todos:
        raise _bad("todos needs items")
    calendar_items = _string_list(calendar["items"], "calendar.items", LIMITS["calendar.items[]"], MAX_ITEMS)
    todo_items = _string_list(todos["items"], "todos.items", LIMITS["todos.items[]"], MAX_ITEMS)

    headlines_obj = _object(data["headlines"], "headlines", {"items"})
    if "items" not in headlines_obj:
        raise _bad("headlines needs items")
    if not isinstance(headlines_obj["items"], list):
        raise _bad("headlines.items must be a list")
    if len(headlines_obj["items"]) > MAX_HEADLINES:
        raise _bad(f"headlines.items must have at most {MAX_HEADLINES} items")
    headlines: list[Headline] = []
    for index, item in enumerate(headlines_obj["items"]):
        item = _object(item, f"headlines.items[{index}]", {"title", "summary", "source"})
        if "title" not in item:
            raise _bad(f"headlines.items[{index}] needs a title")
        headlines.append(Headline(
            _text(item["title"], f"headlines.items[{index}].title", LIMITS["headlines.items[].title"]),
            _text(item["summary"], f"headlines.items[{index}].summary", LIMITS["headlines.items[].summary"])
            if "summary" in item else "",
            _text(item["source"], f"headlines.items[{index}].source", LIMITS["headlines.items[].source"])
            if "source" in item else ""))

    return Morning(
        _text(data["title"], "title", LIMITS["title"]) if "title" in data else "The Morning Paper",
        _text(data["date"], "date", LIMITS["date"]) if "date" in data else booth._today(),
        _text(data["byline"], "byline", LIMITS["byline"]) if "byline" in data else "",
        summary, details, calendar_items, todo_items, tuple(headlines))


def load_morning(path: Path) -> Morning:
    """Read a morning JSON file: ``-`` reads stdin. Bad input exits 3 before anything is drawn or sent."""
    if str(path) == "-":
        name = "morning on stdin"
        content = booth.read_stdin(MORNING_FILE_LIMIT)
        too_big = len(content) > MORNING_FILE_LIMIT
    else:
        if not path.is_file():
            raise PrinterError(f"morning file not found: {path}", hint="check the path", exit_code=3)
        name = f"morning file {path.name}"
        too_big = path.stat().st_size > MORNING_FILE_LIMIT
        content = b"" if too_big else path.read_bytes()
    if too_big:
        raise PrinterError(f"{name} is larger than {MORNING_FILE_LIMIT // 1024} KiB", exit_code=3)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8 JSON", exit_code=3) from exc
    return parse_morning(text, name, "morning file")


def _blocks(items: tuple[str, ...], key: str, label: str, empty: str) -> list[TextBlock]:
    if not items:
        return [TextBlock(empty, key)]
    return [TextBlock(item, f"{key}[{index}]", label) for index, item in enumerate(items)]


def document(paper: Morning) -> TextGazette:
    """Convert a validated morning file into the sectioned text gazette it prints as."""
    weather = [TextBlock(paper.weather_summary, "weather.summary")]
    weather += [TextBlock(item, f"weather.details[{index}]", "- ") for index, item in enumerate(paper.weather_details)]
    calendar = _blocks(paper.calendar_items, "calendar.items", "- ", "Nothing is scheduled today.")
    todos = _blocks(paper.todo_items, "todos.items", "[ ] ", "Nothing is on the list today.")
    headlines: list[TextBlock] = []
    if not paper.headlines:
        headlines.append(TextBlock("No headlines this morning.", "headlines.items"))
    for index, item in enumerate(paper.headlines):
        headlines.append(TextBlock(item.title, f"headlines.items[{index}].title", "- "))
        if item.summary:
            headlines.append(TextBlock(item.summary, f"headlines.items[{index}].summary", "  "))
        if item.source:
            headlines.append(TextBlock(item.source, f"headlines.items[{index}].source", "  — "))
    return TextGazette(
        masthead="The Morning Gazette", headline=paper.title, kicker="Morning edition", byline=paper.byline,
        dateline=paper.date, edition="Morning edition", tagline="Weather, calendar, to-dos and headlines",
        sections=(
            Section("Weather", tuple(weather), "weather"),
            Section("Today", tuple(calendar), "calendar"),
            Section("To do", tuple(todos), "todos"),
            Section("Headlines", tuple(headlines), "headlines"),
        ))


def _string(limit: int) -> dict:
    # ``\S`` keeps a whitespace-only string schema-invalid: the parser cleans surrounding whitespace and
    # would otherwise reject a document the schema had accepted.
    return {"type": "string", "minLength": 1, "maxLength": limit, "pattern": r"\S"}


def morning_schema() -> dict:
    """The JSON Schema (draft 2020-12) of a morning-edition file."""
    example = {
        "title": "The Morning Paper",
        "weather": {"summary": "Fair and mild, 18°C, clouding over after lunch",
                    "details": ["Light northwest wind", "Dry until evening"]},
        "calendar": {"items": ["09:00 Team standup", "13:00 Dentist"]},
        "todos": {"items": ["Water the plants", "Post the parcel"]},
        "headlines": {"items": [{"title": "Local library reopens",
                                 "summary": "The branch opens again after six months of repairs.",
                                 "source": "Example News"}]},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://luongnv.com/schemas/prt-morning.json",
            "title": "prt morning edition", "type": "object", "additionalProperties": False,
            "required": ["weather", "calendar", "todos", "headlines"],
            "properties": {
                "title": _string(LIMITS["title"]), "date": _string(LIMITS["date"]), "byline": _string(LIMITS["byline"]),
                "weather": {"type": "object", "additionalProperties": False, "required": ["summary"],
                            "properties": {"summary": _string(LIMITS["weather.summary"]),
                                           "details": {"type": "array", "maxItems": MAX_DETAILS,
                                                       "items": _string(LIMITS["weather.details[]"])}}},
                "calendar": {"type": "object", "additionalProperties": False, "required": ["items"],
                             "properties": {"items": {"type": "array", "maxItems": MAX_ITEMS,
                                                      "items": _string(LIMITS["calendar.items[]"])}}},
                "todos": {"type": "object", "additionalProperties": False, "required": ["items"],
                          "properties": {"items": {"type": "array", "maxItems": MAX_ITEMS,
                                                   "items": _string(LIMITS["todos.items[]"])}}},
                "headlines": {"type": "object", "additionalProperties": False, "required": ["items"],
                              "properties": {"items": {"type": "array", "maxItems": MAX_HEADLINES,
                                                       "items": {"type": "object", "additionalProperties": False,
                                                                 "required": ["title"],
                                                                 "properties": {
                                                                     "title": _string(LIMITS["headlines.items[].title"]),
                                                                     "summary": _string(LIMITS["headlines.items[].summary"]),
                                                                     "source": _string(LIMITS["headlines.items[].source"])}}}}},
            }, "examples": [example]}


def morning_fields() -> tuple:
    """Catalog field rows for the morning file (imported lazily by ``prt.catalog``)."""
    from prt.catalog import Field
    return (
        Field("title", "The newspaper's front-page headline (default “The Morning Paper”)", False, LIMITS["title"]),
        Field("date", "The dateline (default: today)", False, LIMITS["date"]),
        Field("byline", "An italic line under the headline", False, LIMITS["byline"]),
        Field("weather.summary", "The weather summary", True, LIMITS["weather.summary"]),
        Field("weather.details[]", "Up to 4 short weather detail lines", False, LIMITS["weather.details[]"]),
        Field("calendar.items[]", "Up to 10 appointments or events", True, LIMITS["calendar.items[]"]),
        Field("todos.items[]", "Up to 10 tasks for the day", True, LIMITS["todos.items[]"]),
        Field("headlines.items[].title", "Up to 6 headline titles", True, LIMITS["headlines.items[].title"]),
        Field("headlines.items[].summary", "An optional summary under each headline", False, LIMITS["headlines.items[].summary"]),
        Field("headlines.items[].source", "An optional source line under each headline", False, LIMITS["headlines.items[].source"]),
    )
