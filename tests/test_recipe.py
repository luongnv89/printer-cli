"""prt recipe: a recipe from the web, a saved page or a recipe file, printed as 4×6 index cards (#28)."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import article, catalog, guard, recipe
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import PHOTO_4X6
from prt.recipe import Recipe
from prt.transport import ipp

from conftest import without_estimate

REAL_FETCH = article.fetch
URL = "https://kitchen.example/lemon-cake"

LD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebPage", "name": "Lemon cake page"},
        {"@type": ["Recipe"], "name": "Lemon Drizzle Cake", "recipeYield": ["8", "8 slices"],
         "prepTime": "PT15M", "cookTime": "PT45M",
         "recipeIngredient": ["225 g butter", "225 g caster sugar", "4 eggs", "Juice of 1&frac12; lemons"],
         "recipeInstructions": [
             {"@type": "HowToSection", "name": "Cake", "itemListElement": [
                 {"@type": "HowToStep", "text": "Heat the oven to 180&deg;C."},
                 {"@type": "HowToStep", "text": "Beat the <b>butter</b> and sugar."}]},
             {"@type": "HowToStep", "text": "Bake for 45 minutes."}]},
    ],
}


def _page(ld=LD, body="<p>A long story about cake.</p>", head=""):
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld is not None else ""
    return (f'<html><head><title>Lemon Drizzle Cake | Kitchen</title><meta property="og:site_name" '
            f'content="Kitchen">{script}{head}</head><body>{body}</body></html>')


HEADINGS_PAGE = """
<html><head><title>Pancakes</title></head><body><main>
<h1>Pancakes</h1><p>My grandmother made these every Sunday.</p>
<h2>Ingredients</h2>
<h3>For the batter</h3>
<ul><li>100 g plain flour</li><li>2 eggs</li><li>300 ml milk</li></ul>
<h2>Method</h2>
<ol><li>Whisk everything together.</li><li>Fry in a hot pan.</li></ol>
<h2>Notes</h2><p>Serve with lemon.</p>
</main></body></html>"""

SHORT = Recipe("Toast", ("Bread", "Butter"), ("Toast the bread.", "Spread the butter."))


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture(autouse=True)
def web(monkeypatch):
    """The only "internet" a test sees: the responses it puts in ``routes``."""
    routes = {}

    def handler(request):
        reply = routes.get(str(request.url))
        if reply is None:
            raise AssertionError(f"unexpected request to {request.url}")
        return reply

    monkeypatch.setattr(article, "fetch", lambda url, timeout, transport=None:
                        REAL_FETCH(url, timeout, transport=httpx.MockTransport(handler)))
    return SimpleNamespace(routes=routes)


@pytest.fixture
def printer(monkeypatch):
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, requests=requests,
                           jobs=lambda: [data for _, _, _, data in requests()])


def _html_response(text):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=text.encode())


def _long(steps=12):
    return Recipe("Long stew", tuple(f"Ingredient number {n}" for n in range(20)),
                  tuple(f"Step {n}: " + "stir the pot slowly and keep tasting it as it cooks. " * 3
                        for n in range(steps)))


# --- reading a recipe ----------------------------------------------------------------------------

def test_schema_org_recipe_data_is_read_first():
    found = recipe.normalise(recipe.from_html(_page(), URL, "kitchen.example"))
    assert found == Recipe("Lemon Drizzle Cake", ("225 g butter", "225 g caster sugar", "4 eggs",
                                                  "Juice of 1 1/2 lemons"),
                           ("Heat the oven to 180°C.", "Beat the butter and sugar.", "Bake for 45 minutes."),
                           servings="8 slices", time="Prep 15 min · Cook 45 min", source="Kitchen", url=URL)


def test_instructions_as_text_and_a_bare_yield():
    ld = {"@type": "Recipe", "name": "Tea", "recipeYield": 2, "totalTime": "PT1H5M",
          "recipeIngredient": "Tea leaves\nWater", "recipeInstructions": "Boil the water.\nSteep the tea."}
    found = recipe.from_html(_page(ld))
    assert found.ingredients == ("Tea leaves", "Water") and found.steps == ("Boil the water.", "Steep the tea.")
    assert found.servings == "Serves 2" and found.time == "Total 1 h 5 min"


def test_a_broken_json_ld_block_does_not_hide_a_good_one():
    page = _page(head='<script type="application/ld+json">{not json</script>')
    assert recipe.from_html(page).title == "Lemon Drizzle Cake"


def test_a_deeply_nested_json_ld_block_falls_back_to_the_headings():
    """Untrusted JSON nested thousands deep must not crash the command (RecursionError)."""
    deep = '<script type="application/ld+json">' + "[" * 100000 + "</script>"
    page = HEADINGS_PAGE.replace("<head>", "<head>" + deep)
    assert recipe.from_html(page).steps == ("Whisk everything together.", "Fry in a hot pan.")


def test_a_long_title_goes_on_two_lines_and_a_longer_one_is_cut_and_reported():
    medium = "Grandma's Famous Slow-Cooked Sunday Lemon Drizzle Cake With Extra Icing | The Kitchen"
    _, size, lines, cut = recipe.fit_title(medium)
    assert len(lines) == 2 and cut is None and size <= 60
    assert recipe.compose_cards(recipe.normalise(replace(SHORT, title=medium)))[1] == {}
    _, cut = recipe.compose_cards(recipe.normalise(replace(SHORT, title="WWWWWWWWW " * 20)))
    assert set(cut) == {"title"} and 0 < cut["title"].max_fit < cut["title"].given


def test_a_recipe_nested_in_another_object_is_found():
    page = _page({"@type": "WebPage", "mainEntity": {**LD["@graph"][1], "@type": "http://schema.org/Recipe"}})
    assert recipe.from_html(page).steps[-1] == "Bake for 45 minutes."


@pytest.mark.parametrize("value, shown", [("PT15M", "15 min"), ("PT1H30M", "1 h 30 min"), ("PT90M", "1 h 30 min"),
                                          ("P0DT2H", "2 h"), ("P1D", "24 h"), ("PT0M", ""), ("soon", ""),
                                          (None, "")])
def test_durations_read_as_people_write_them(value, shown):
    assert recipe.duration(value) == shown


def test_without_recipe_data_the_headings_are_used():
    found = recipe.from_html(HEADINGS_PAGE)
    assert found.title == "Pancakes"
    assert found.ingredients == ("100 g plain flour", "2 eggs", "300 ml milk")
    assert found.steps == ("Whisk everything together.", "Fry in a hot pan.")


def test_a_page_without_a_recipe_is_refused_with_its_own_kind():
    with pytest.raises(PrinterError, match="no recipe found") as err:
        recipe.from_html(_page(ld=None), URL)
    assert err.value.exit_code == 3 and err.value.kind == "no_recipe"


def test_plain_text_recipe():
    text = "Toast\n\nIngredients\n- Bread\n- Butter\n\nMethod\n1. Toast the bread.\n2) Spread the butter.\n"
    assert recipe.from_text(text) == SHORT


def test_recipe_json_file(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"title": "Toast", "ingredients": "Bread\nButter",
                                "steps": ["Toast the bread.", "Spread the butter."], "servings": "Serves 1"}))
    assert recipe.load(str(path), 5) == Recipe("Toast", ("Bread", "Butter"), ("Toast the bread.", "Spread the butter."),
                                               servings="Serves 1")


@pytest.mark.parametrize("data, message", [
    ({"title": "T", "ingredients": ["a"]}, "missing steps"),
    ({"title": "T", "ingredients": ["a"], "steps": ["b"], "photo": "x"}, "unknown field"),
    ({"title": "T", "ingredients": [1], "steps": ["b"]}, "must be text"),
    ({"title": "T", "ingredients": ["  "], "steps": ["b"]}, "no ingredients"),
    ([1], "JSON object"),
])
def test_bad_recipe_files_are_refused(data, message):
    with pytest.raises(PrinterError, match=message) as err:
        recipe.normalise(recipe.parse_json(json.dumps(data)))
    assert err.value.exit_code == 3


def test_too_many_items_are_refused():
    with pytest.raises(PrinterError, match="more than 100"):
        recipe.normalise(Recipe("T", tuple("x" for _ in range(101)), ("s",)))


# --- the cards -----------------------------------------------------------------------------------

def test_a_short_recipe_fits_one_card_at_the_largest_type():
    size, cards = recipe.plan(recipe.normalise(SHORT))
    assert size == recipe.SIZES[0] and len(cards) == 1
    pages, cut = recipe.compose_cards(recipe.normalise(SHORT))
    assert len(pages) == 1 and cut == {} and pages[0].size == (1800, 1200) == PHOTO_4X6.landscape_size


def test_a_long_recipe_runs_on_to_more_cards_and_nothing_is_cut():
    long = recipe.normalise(_long())
    size, cards = recipe.plan(long)
    assert size == recipe.RUN_ON_SIZE and 1 < len(cards) <= recipe.MAX_CARDS
    printed = [row for card in cards for column in card for row in column]
    assert [row.mark for row in printed if row.kind == "item"][-1] == f"{len(long.steps)}."
    assert len(recipe.compose_cards(long)[0]) == len(cards)


def test_a_recipe_too_long_for_six_cards_is_refused():
    with pytest.raises(PrinterError, match="more than 6") as err:
        recipe.plan(recipe.normalise(_long(steps=99)))
    assert err.value.exit_code == 3


def test_a_heading_never_ends_a_column_and_a_gap_never_starts_one():
    rows = [recipe.Row("item", "a", "•"), recipe.Row("gap"), recipe.Row("head", "Method"), recipe.Row("item", "b", "1.")]
    cards = recipe.flow(rows, 3)
    assert cards == [[[rows[0], rows[1]], [rows[2], rows[3]]]]
    assert recipe.flow(rows, 1) == [[[rows[0]], [rows[2]]], [[rows[3]]]]


# --- the catalog and schema -----------------------------------------------------------------------

def test_recipe_style_and_schema():
    style = next(s for s in catalog.styles() if s["name"] == "recipe")
    assert style["command"] == "recipe" and style["schema"] == "recipe" and style["max_pages"] == recipe.MAX_CARDS
    assert style["paper"] == {"size": "4x6", "media": "4x6", "tray": "photo"}
    assert [f["name"] for f in style["fields"] if f["required"]] == ["title", "ingredients", "steps"]
    Draft202012Validator.check_schema(catalog.schema("recipe"))


@pytest.mark.parametrize("data", [
    {"title": "T", "ingredients": ["a"], "steps": ["b"]},
    {"title": "T", "ingredients": "a\nb", "steps": "c"},
    {"title": "T", "ingredients": ["a"]},
    {"title": "T", "ingredients": ["a"], "steps": ["b"], "extra": "x"},
    {"title": "T", "ingredients": [], "steps": ["b"]},
    {"title": "T", "ingredients": ["a"], "steps": [2]},
    {"title": "T" * 201, "ingredients": ["a"], "steps": ["b"]},
])
def test_recipe_schema_agrees_with_the_recipe_file_check(data):
    try:
        recipe.normalise(recipe.parse_json(json.dumps(data)))
        accepted = True
    except PrinterError as exc:
        assert exc.exit_code == 3
        accepted = False
    assert Draft202012Validator(catalog.schema("recipe")).is_valid(data) == accepted
    assert Draft202012Validator(catalog.schema("recipe")).is_valid(catalog.schema("recipe")["examples"][0])


# --- prt recipe ------------------------------------------------------------------------------------

def test_recipe_link_preview_saves_the_card_and_prints_nothing(capsys, tmp_path, printer, web):
    web.routes[URL] = _html_response(_page())
    out_file = tmp_path / "card.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "recipe", URL, "--preview", str(out_file))
    assert code == 0, err
    assert "not printed" in err and printer.made == [] and guard.used_today() == 0
    data = without_estimate(out)
    assert data == {"action": "recipe", "title": "Lemon Drizzle Cake", "pages": 1,
                    "paper": {"size": "4x6", "media": "4x6", "tray": "photo"}, "fits": True, "cut": {},
                    "files": [{"file": str(out_file), "format": "png", "bytes": out_file.stat().st_size}]}
    assert Image.open(out_file).size == (1800, 1200)


def test_recipe_prints_every_card_upright_from_the_photo_tray(capsys, tmp_path, printer):
    path = tmp_path / "stew.json"
    long = _long()
    path.write_text(json.dumps({"title": long.title, "ingredients": list(long.ingredients),
                                "steps": list(long.steps)}))
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "recipe", str(path), "--yes")
    assert code == 0, err
    data = json.loads(out)
    cards, _ = recipe.compose_cards(recipe.normalise(long))
    assert data["pages"] == len(cards) > 1 and len(data["job_ids"]) == len(cards)
    assert data["estimate"]["pages"] == len(cards) and "(4×6)" in data["estimate"]["summary"]
    assert printer.jobs() == [PHOTO_4X6.to_jpeg(card) for card in cards] and data["fits"] is True
    for request in printer.requests():
        col = next(a.value for a in request[2] if a.name == "media-col")
        assert col["media-source"][1] == "photo" and col["media-size"][1]["x-dimension"][1] == 10160
    names = [next(a.value for a in r[1] if a.name == "job-name") for r in printer.requests()]
    assert names[0] == f"prt recipe 1/{len(cards)} Long stew"
    assert guard.used_today() == len(cards)


def test_recipe_reads_stdin(capsys, tmp_path, printer, stdin):
    stdin(json.dumps({"title": "Toast", "ingredients": ["Bread"], "steps": ["Toast it."]}).encode())
    out_file = tmp_path / "toast.jpg"
    code, out, err = run(capsys, "-f", "json", "recipe", "-", "-o", str(out_file))
    assert code == 0, err and out_file.is_file()


def test_a_page_without_a_recipe_sends_nothing(capsys, tmp_path, printer, web):
    web.routes[URL] = _html_response(_page(ld=None))
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "recipe", URL, "--yes")
    assert code == 3 and _error(err)["kind"] == "no_recipe" and out == ""
    assert printer.made == [] and guard.used_today() == 0


def test_a_failed_fetch_and_a_bad_link_are_bad_input(capsys, printer, web):
    web.routes[URL] = httpx.Response(404)
    code, _, err = run(capsys, "-f", "json", "recipe", URL, "--yes")
    assert code == 3 and _error(err)["kind"] == "fetch_failed"
    code, _, err = run(capsys, "-f", "json", "recipe", "ftp://kitchen.example/cake", "--yes")
    assert code == 3 and "only http" in _error(err)["message"]
    assert printer.made == []


def test_recipe_needs_yes_in_agent_mode(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    path = tmp_path / "r.html"
    path.write_text(HEADINGS_PAGE)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "recipe", str(path))
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []


def test_output_over_the_recipe_file_is_refused(capsys, tmp_path, printer):
    path = tmp_path / "toast.png"  # a plain-text recipe, whatever its name says
    path.write_text("Toast\nIngredients\nBread\nMethod\nToast it.\n")
    original = path.read_bytes()
    code, _, err = run(capsys, "-f", "json", "recipe", str(path), "-o", str(path))
    assert code == 2 and "overwrite" in _error(err)["message"] and path.read_bytes() == original


def test_a_cut_title_is_warned_and_reported(capsys, tmp_path, printer):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"title": "WWWWWWWWW " * 20, "ingredients": ["a"], "steps": ["b"]}))
    code, out, err = run(capsys, "-f", "json", "recipe", str(path), "-o", str(tmp_path / "r.png"))
    assert code == 0 and "title cut to fit" in err
    data = json.loads(out)
    assert data["fits"] is False and set(data["cut"]) == {"title"}
