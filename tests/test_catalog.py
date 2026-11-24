"""prt.catalog, `prt styles` and `prt schema`: the styles and input schemas say what the commands accept."""

import json

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import article, booth, card, catalog, photocard, recipe, vintage
from prt.cli import ArticleStyle, BoothStyle, main
from prt.drivers.base import PrinterError

from conftest import ROOT


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _photo(tmp_path, name="photo.jpg"):
    path = tmp_path / name
    Image.new("RGB", (300, 400), (0, 0, 255)).save(path)
    return path


def _valid(name: str, data) -> bool:
    return Draft202012Validator(catalog.schema(name)).is_valid(data)


def _story_accepted(data) -> bool:
    try:
        booth.parse_story(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


def _article_accepted(data) -> bool:
    try:
        article.normalise(article.parse(json.dumps(data).encode(), "json"))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


# --- prt styles ---------------------------------------------------------------------------------

def test_styles_json_lists_every_style_with_fields_limits_and_sample(capsys):
    code, out, err = run(capsys, "styles", "-f", "json")
    assert code == 0 and err == ""
    styles = {s["name"]: s for s in json.loads(out)}
    assert list(styles) == ["metro", "daily", "wizard", "wanted", "certificate", "sports", "missing", "movie",
                            "card", "strip", "instant", "magazine", "classic-magazine", "recipe", "gazette",
                            "gazette-landscape"]
    metro = styles["metro"]
    assert metro["command"] == "booth" and metro["args"] == [] and metro["default"] is True
    assert metro["kind"] == "metro newspaper page" and metro["schema"] == "story"
    assert metro["photos"] == {"min": 1, "max": 1} and metro["max_pages"] == 1
    assert metro["fields"][0] == {"name": "headline", "description": "Headline", "required": True,
                                  "max_length": 120}
    assert {f["name"]: f["max_length"] for f in metro["fields"]} == booth.LIMITS
    assert metro["sample"] == ("https://raw.githubusercontent.com/luongnv89/printer-cli/main/assets/samples/"
                               "booth-metro.jpg")
    assert styles["wanted"]["args"] == ["--style", "wanted"] and styles["wanted"]["default"] is False
    assert next(f for f in styles["wanted"]["fields"] if f["name"] == "caption")["description"] == "Reward line"
    for name, kind in (("certificate", "certificate"), ("sports", "sports card"), ("missing", "missing-pet poster"),
                       ("movie", "movie poster")):
        assert styles[name]["command"] == "booth" and styles[name]["args"] == ["--style", name]
        assert styles[name]["kind"] == kind and styles[name]["schema"] == "story"
        assert styles[name]["photos"] == {"min": 1, "max": 1} and styles[name]["max_pages"] == 1
        assert styles[name]["sample"] == catalog.SAMPLES + f"booth-{name}.jpg"
        assert {f["name"]: f["max_length"] for f in styles[name]["fields"]} == booth.LIMITS
    assert "tear-off" in next(f for f in styles["missing"]["fields"] if f["name"] == "byline")["description"]
    assert next(f for f in styles["movie"]["fields"] if f["name"] == "headline")["description"] == "The film's title"
    assert styles["classic-magazine"]["args"] == ["--style", "classic-magazine"]
    assert [f["name"] for f in styles["magazine"]["fields"] if f["required"]] == ["title", "body"]
    assert styles["magazine"]["photos"] == {"min": 0, "max": 0} and styles["magazine"]["max_pages"] == 30
    assert styles["gazette-landscape"]["args"] == ["--landscape"] and styles["gazette"]["args"] == []
    assert styles["gazette"]["photos"] == {"min": 1, "max": vintage.MAX_PHOTOS}
    for s in styles.values():
        assert s["description"] and s["fields"] and all(f["description"] for f in s["fields"])


def test_styles_needs_no_printer(capsys):
    # No saved printer and no -p: listing styles never resolves or contacts one.
    code, out, _ = run(capsys, "styles")
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == len(catalog.STYLES)
    assert lines[1].split("\t") == ["daily", "prt booth --style daily", "daily newspaper page", "story",
                                    catalog.SAMPLES + "booth-daily.jpg"]


def test_styles_styled_table(capsys, monkeypatch):
    for var, value in (("FORCE_COLOR", "1"), ("NO_COLOR", "1"), ("COLUMNS", "100")):
        monkeypatch.setenv(var, value)
    code, out, _ = run(capsys, "styles")
    assert code == 0 and "╭" in out and "prt gazette --landscape" in out and "prt styles -f json" in out


def test_sample_images_exist():
    for s in catalog.STYLES:
        assert (ROOT / "assets" / "samples" / s.sample).is_file(), s.sample


def test_catalog_follows_the_commands():
    """A style added to a command (or its CLI choice) without a catalog entry fails here."""
    by_command = {}
    for s in catalog.STYLES:
        by_command.setdefault(s.command, []).append(s.name)
    assert by_command["booth"] == list(booth.STYLES) == [s.value for s in BoothStyle]
    assert by_command["article"] == list(article.ARTICLE_STYLES) == [s.value for s in ArticleStyle]
    assert by_command["card"] == ["card"]
    assert by_command["strip"] == ["strip"] and by_command["instant"] == ["instant"]
    assert by_command["recipe"] == ["recipe"]
    assert len({s.name for s in catalog.STYLES}) == len(catalog.STYLES)
    fields = {"article": article.FIELDS, "card": card.FIELDS, "recipe": recipe.FIELDS, "strip": ("caption",),
              "instant": ("caption",)}
    for s in catalog.STYLES:
        names = [f.name for f in s.fields]
        assert names == list(fields.get(s.command, booth.FIELDS))
        # strip and instant take their only text from --caption, so they have no input file
        assert s.schema in catalog.schema_names() if s.command not in ("strip", "instant") else s.schema is None


# --- prt schema ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["story", "article", "gazette", "page", "card"])
def test_schema_prints_a_valid_json_schema(capsys, name):
    code, out, err = run(capsys, "schema", name)
    assert code == 0 and err == ""
    schema = json.loads(out)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    for example in schema["examples"]:
        assert Draft202012Validator(schema).is_valid(example)


def test_schema_is_json_under_any_format(capsys):
    assert json.loads(run(capsys, "-f", "json", "schema", "story")[1]) == catalog.schema("story")


def test_schema_story_matches_the_limits():
    schema = catalog.schema("story")
    assert schema["required"] == ["headline", "story"]
    assert {name: p["maxLength"] for name, p in schema["properties"].items()} == booth.LIMITS
    assert catalog.schema("gazette")["properties"]["caption"]["description"] == "Line under the lead photo"


def test_schema_unknown_name_is_a_usage_error(capsys):
    code, out, err = run(capsys, "schema", "flyer")
    assert code == 2 and out == "" and "unknown schema: flyer" in err and "story, article, gazette, page" in err
    code, _, err = run(capsys, "-f", "json", "schema", "flyer")
    assert code == 2 and json.loads(err)["error"]["kind"] == "usage"


def test_schema_registry_takes_new_schemas(monkeypatch):
    monkeypatch.setattr(catalog, "_SCHEMAS", dict(catalog._SCHEMAS))
    catalog.register_schema("poster", "a poster")(lambda: {"type": "object"})
    assert catalog.schema_names()[-1] == "poster" and catalog.schema("poster") == {"type": "object"}
    assert catalog.schema_summaries()["poster"] == "a poster"
    with pytest.raises(ValueError, match="already registered"):
        catalog.register_schema("story", "again")(dict)


def test_schema_is_built_fresh_each_time():
    catalog.schema("story")["properties"].clear()
    assert catalog.schema("story")["properties"]


# --- a file that passes the schema is accepted by the command, and the other way round ------------

def _stories():
    limits = booth.LIMITS
    full = {name: "x" * limit for name, limit in limits.items()}
    return [
        {"headline": "Hi", "story": "Text."},
        full,  # every field at its limit
        {**full, "headline": "x" * (limits["headline"] + 1)},
        {**full, "story": "x" * (limits["story"] + 1)},
        {**full, "kicker": "x" * (limits["kicker"] + 1)},
        {"headline": "Hi", "story": "Text.", "kicker": "", "caption": "", "byline": ""},
        {"headline": "Hi"},
        {"story": "Text."},
        {"headline": "", "story": "Text."},
        {"headline": "   ", "story": "Text."},
        {"headline": "Hi", "story": "\n\n"},
        {"headline": "Hi", "story": "Text.", "subtitle": "no such field"},
        {"headline": "Hi", "story": 3},
        {"headline": "Hi", "story": "Text.", "byline": None},
        {"headline": "Café crème", "story": "Line one.\nLine two."},
        [],
        "a story",
    ]


@pytest.mark.parametrize("name", ["story", "gazette"])
@pytest.mark.parametrize("data", _stories())
def test_story_schema_agrees_with_the_story_parser(name, data):
    assert _valid(name, data) == _story_accepted(data)


@pytest.mark.parametrize("data", [
    {"title": "T", "body": "One.\nTwo."},
    {"title": "", "body": ["One.", "Two."]},
    {"title": "T", "body": ["", "One."], "author": "A", "source": "S", "url": "https://example.com/"},
    {"title": "T", "body": ""},
    {"title": "T", "body": "   "},
    {"title": "T", "body": []},
    {"title": "T", "body": ["", " "]},
    {"title": "T", "body": "- ? -"},
    {"title": "T", "body": ["One.", 2]},
    {"title": "T", "body": 7},
    {"title": "T"},
    {"body": "One."},
    {"title": 1, "body": "One."},
    {"title": "T", "body": "One.", "author": None},
    {"title": "T", "body": "One.", "tags": ["x"]},
    [],
])
def test_article_schema_agrees_with_the_article_parser(data):
    assert _valid("article", data) == _article_accepted(data)


@pytest.mark.parametrize("style", list(booth.STYLES))
def test_schema_valid_story_file_prints_on_every_booth_style(capsys, tmp_path, style):
    story = tmp_path / "story.json"
    story.write_text(json.dumps(catalog.schema("story")["examples"][0]))
    out_file = tmp_path / "page.png"
    code, _, err = run(capsys, "booth", str(_photo(tmp_path)), "-s", style, "--story-file", str(story),
                       "-o", str(out_file))
    assert code == 0, err
    assert out_file.is_file()


def test_schema_valid_gazette_story_file_is_accepted(capsys, tmp_path):
    story = tmp_path / "story.json"
    story.write_text(json.dumps(catalog.schema("gazette")["examples"][0]))
    out_file = tmp_path / "gazette.png"
    code, _, err = run(capsys, "gazette", str(_photo(tmp_path)), "--story-file", str(story), "-o", str(out_file))
    assert code == 0, err
    assert out_file.is_file()


@pytest.mark.parametrize("style", list(article.ARTICLE_STYLES))
def test_schema_valid_article_file_is_accepted(capsys, tmp_path, style):
    source = tmp_path / "article.json"
    source.write_text(json.dumps(catalog.schema("article")["examples"][0]))
    out_file = tmp_path / "article.pdf"
    code, _, err = run(capsys, "article", str(source), "-s", style, "-o", str(out_file))
    assert code == 0, err
    assert out_file.is_file()


def test_sample_inputs_pass_their_schemas():
    """The README's sample story and article are valid against the schemas."""
    story = json.loads((ROOT / "assets" / "samples" / "story.json").read_text())
    assert _valid("story", story) and _valid("gazette", story)
    assert _valid("article", json.loads((ROOT / "assets" / "samples" / "article.json").read_text()))
