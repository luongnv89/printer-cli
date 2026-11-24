"""prt bingo: a different bingo card for each player (#37)."""

import json
import math
import re
from itertools import combinations
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import bingo, catalog, guard, render
from prt.bingo import Bingo
from prt.cli import main
from prt.config import Config
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import A4
from prt.transport import ipp

from conftest import ROOT, without_estimate


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


def _words(n, prefix="Word"):
    return [f"{prefix} {i}" for i in range(1, n + 1)]


def _words_file(tmp_path, data, name="words.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``jobs()`` are the JPEGs sent over IPP, ``names()`` their job names."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, requests=requests,
                           jobs=lambda: [data for _, _, _, data in requests()],
                           names=lambda: [next(a.value for a in r[1] if a.name == "job-name") for r in requests()])


def _sets(game, cards):
    return [frozenset(game.words[i] for i in card) for card in cards]


# --- dealing: every card in a run is different ---------------------------------------------------

@pytest.mark.parametrize("count, n", [(25, 25), (25, 1), (26, 30), (27, 30), (36, 30), (200, 30)])
def test_every_card_in_a_run_holds_a_different_set_of_words(count, n):
    cards = bingo.deal(count, n, seed=11)
    assert len(cards) == n
    assert all(len(card) == bingo.PER_CARD == len(set(card)) for card in cards)
    assert all(0 <= i < count for card in cards for i in card)
    assert len({frozenset(card) for card in cards}) == n  # no two cards share their set of words


def test_the_same_seed_deals_the_same_cards_and_another_seed_others():
    assert bingo.deal(36, 8, seed=5) == bingo.deal(36, 8, seed=5)
    assert bingo.deal(36, 8, seed=5) != bingo.deal(36, 8, seed=6)


def test_the_deal_shuffles_where_each_word_goes():
    cards = bingo.deal(36, 8, seed=3)
    assert any(list(card) != sorted(card) for card in cards)


def test_more_cards_than_the_words_can_make_different_is_refused():
    assert math.comb(25, bingo.PER_CARD) == 25
    with pytest.raises(PrinterError) as exc:
        bingo.deal(25, 26, seed=1)
    assert exc.value.exit_code == 3 and "only 25 different bingo cards" in str(exc.value)


def test_every_bundled_theme_can_deal_the_most_cards_and_fits_its_squares():
    width = bingo.CELL - 2 * bingo.CELL_PAD
    for name, words in bingo.THEMES.items():
        assert math.comb(len(words), bingo.PER_CARD) >= bingo.MAX_CARDS, name
        game, theme = bingo.game(name)
        assert theme == name and game.words == words  # checked: printable, short enough, no repeats
        for word in words:
            font, size, lines, cut = bingo._fit_word(word, width)
            assert cut is None and all(font.getlength(part) <= width for part in word.split()), word
    assert bingo.DEFAULT_THEME in bingo.THEMES


def test_the_longest_word_fits_its_square_without_a_cut():
    width = bingo.CELL - 2 * bingo.CELL_PAD
    for word in ["W" * bingo.MAX_WORD, "M" * bingo.MAX_WORD, "WWWWWWWWWWW " + "W" * (bingo.MAX_WORD - 12)]:
        font, size, lines, cut = bingo._fit_word(word, width)
        assert cut is None and len(lines) <= bingo.WORD_LINES
        assert size * 1.05 * len(lines) <= bingo.CELL - 2 * bingo.CELL_PAD + 4  # the lines fit the square's height


# --- the game: theme, words file, title ----------------------------------------------------------

def test_themes_are_matched_without_regard_to_case_or_dashes():
    for given in ("road trip", "Road Trip", "road-trip", "ROAD_TRIP", "  road   trip "):
        game, theme = bingo.game(given)
        assert theme == "road trip" and game.title == "Road Trip Bingo"
    game, theme = bingo.game(None)
    assert theme == bingo.DEFAULT_THEME and game.title == "Road Trip Bingo" and game.free == "FREE"


def test_an_unknown_theme_without_words_is_refused_with_its_own_kind():
    with pytest.raises(PrinterError) as exc:
        bingo.game("dinosaurs")
    err = exc.value
    assert err.exit_code == 2 and err.kind == "unknown_theme" and "dinosaurs" in str(err)
    assert "road trip" in err.hint and "--words-file" in err.hint


def test_a_words_file_gives_the_words_for_any_theme_and_the_title():
    words = Bingo(words=tuple(_words(30)))
    game, theme = bingo.game("Grandma's 80th", words)
    assert theme is None and game.words == words.words and game.title == "Grandma's 80th Bingo"
    assert bingo.game("Space Bingo", words)[0].title == "Space Bingo"  # never "Bingo Bingo"
    assert bingo.game(None, words)[0].title == "Bingo"
    titled = Bingo(words=words.words, title="The Big Game", free="STAR")
    game, _ = bingo.game("beach", titled)
    assert game.title == "The Big Game" and game.free == "STAR" and game.words == words.words


def test_words_file(tmp_path):
    path = _words_file(tmp_path, {"title": "  Party   time ", "words": _words(25)})
    game = bingo.load_bingo(path)
    assert game.title == "Party time" and len(game.words) == 25 and game.free == "FREE"


@pytest.mark.parametrize("data, message", [
    ({"words": _words(24)}, "24 words, fewer than 25"),
    ({"words": _words(24) + ["word 1"]}, "words[24] repeats words[0]"),
    ({"words": _words(24) + ["  "]}, "words[24] is empty"),
    ({"words": _words(24) + ["x" * (bingo.MAX_WORD + 1)]}, "words[24] is too long"),
    ({"words": _words(24) + ["Smile 😀"]}, "words[24] has characters"),
    ({"words": _words(bingo.MAX_WORDS + 1)}, "more than 200"),
    ({"words": "a b c"}, "must be a list of texts"),
    ({"words": _words(24) + [3]}, "must be a list of texts"),
    ({"title": 5, "words": _words(25)}, "fields must be text: title"),
    ({"title": "x" * 61, "words": _words(25)}, "title is too long"),
    ({"free": "x" * 13, "words": _words(25)}, "free is too long"),
    ({"words": _words(25), "theme": "zoo"}, "unknown field(s) in words file: theme"),
    ({"title": "No words"}, "is missing words"),
    ([], "must hold a JSON object"),
])
def test_bad_words_files_are_refused(tmp_path, data, message):
    with pytest.raises(PrinterError) as exc:
        bingo.load_bingo(_words_file(tmp_path, data))
    assert exc.value.exit_code == 3 and message in str(exc.value)


def test_unreadable_words_files_are_refused(tmp_path):
    for content, message in ((b"{nope", "not valid JSON"), (b"\xff\xfe", "not UTF-8")):
        path = tmp_path / "w.json"
        path.write_bytes(content)
        with pytest.raises(PrinterError, match=message):
            bingo.load_bingo(path)
    with pytest.raises(PrinterError, match="words file not found"):
        bingo.load_bingo(tmp_path / "missing.json")


def _accepted(data) -> bool:
    try:
        bingo.parse_bingo(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


@pytest.mark.parametrize("data", [
    {"words": _words(25)},
    {"title": "T", "free": "", "words": _words(bingo.MAX_WORDS)},
    {"title": "", "words": _words(25)},
    {"words": _words(24)},
    {"words": _words(bingo.MAX_WORDS + 1)},
    {"words": _words(24) + ["Word 1"]},
    {"words": _words(24) + [" "]},
    {"words": _words(24) + ["x" * (bingo.MAX_WORD + 1)]},
    {"words": _words(25), "title": "x" * 61},
    {"words": _words(25), "free": None},
    {"words": _words(25), "extra": 1},
    {"title": "T"},
    [],
])
def test_bingo_schema_agrees_with_the_words_file_check(data):
    assert Draft202012Validator(catalog.schema("bingo")).is_valid(data) == _accepted(data)


@pytest.mark.parametrize("repeat", ["word 1", "Word  1", " WORD 1 "])
def test_a_repeat_in_other_case_or_spacing_passes_the_schema_but_is_refused(repeat):
    # The schema's uniqueItems compares exact texts; the check compares them as printed (documented exception).
    data = {"words": _words(24) + [repeat]}
    assert Draft202012Validator(catalog.schema("bingo")).is_valid(data) and not _accepted(data)


def test_bingo_style_and_schema():
    style = next(s for s in catalog.styles() if s["name"] == "bingo")
    assert style["command"] == "bingo" and style["schema"] == "bingo" and style["kind"] == "bingo card"
    assert style["max_pages"] == bingo.MAX_CARDS and style["photos"] == {"min": 0, "max": 0}
    assert style["paper"] == {"size": "a4", "media": "a4", "tray": None}
    assert [f["name"] for f in style["fields"] if f["required"]] == ["words"]
    assert (ROOT / "assets" / "samples" / "bingo.json").is_file()
    sample = json.loads((ROOT / "assets" / "samples" / "bingo.json").read_text())
    assert Draft202012Validator(catalog.schema("bingo")).is_valid(sample) and _accepted(sample)


# --- the card ------------------------------------------------------------------------------------

def test_a_card_is_an_a4_page_with_its_words_and_a_free_centre():
    game, _ = bingo.game("zoo")
    rendered = render.bingo_cards(game, bingo.deal(len(game.words), 2, seed=1))
    assert len(rendered.pages) == 2 and rendered.paper is A4 and rendered.cut == {}
    page = rendered.pages[0]
    assert page.size == A4.size == (1240, 1754)
    centre = (bingo.GRID_X + 2 * bingo.CELL + 6, bingo.grid_top() + 2 * bingo.CELL + 6)
    assert page.getpixel(centre) == bingo.FREE_FILL
    assert rendered.pages[0].tobytes() != rendered.pages[1].tobytes()


def test_a_long_title_goes_on_two_lines_and_a_longer_one_is_cut_and_reported():
    words = tuple(_words(25))
    two = Bingo(words=words, title="Grandma's Eightieth Birthday Garden Party Bingo")
    assert bingo.compose_card(two, tuple(range(24)), 1, 1)[1] == {}
    assert bingo.compose_card(Bingo(words=words, title="W" * bingo.LIMITS["title"]), tuple(range(24)), 1, 1)[1] == {}
    wide = Bingo(words=words, title="W" * 150)  # longer than a words file may give: compose_card cuts it
    cut = bingo.compose_card(wide, tuple(range(24)), 1, 1)[1]
    assert set(cut) == {"title"} and cut["title"].given == 150


def test_job_names_number_the_cards():
    assert render.bingo_job_names("Road Trip Bingo", 2) == ["prt bingo 1/2 Road Trip Bingo",
                                                            "prt bingo 2/2 Road Trip Bingo"]


# --- prt bingo -----------------------------------------------------------------------------------

def test_bingo_preview_saves_n_different_cards_and_prints_nothing(capsys, tmp_path, printer):
    out_file = tmp_path / "cards.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "--theme", "road trip", "-n", "8",
                         "--seed", "42", "--preview", str(out_file))
    assert code == 0, err
    assert "8 bingo cards of 'Road Trip Bingo' rendered" in err and "not printed" in err
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    data = without_estimate(out)
    files = [tmp_path / f"cards-{n}.png" for n in range(1, 9)]
    assert data == {"action": "bingo", "title": "Road Trip Bingo", "theme": "road trip", "pages": 8, "seed": 42,
                    "words": len(bingo.THEMES["road trip"]), "paper": {"size": "a4", "media": "a4", "tray": None},
                    "fits": True, "cut": {},
                    "files": [{"file": str(f), "format": "png", "bytes": f.stat().st_size} for f in files]}
    images = [Image.open(f) for f in files]
    assert all(image.size == (1240, 1754) for image in images)
    assert len({image.tobytes() for image in images}) == 8
    # The cards are the deal of that seed: 8 different sets of words.
    game, _ = bingo.game("road trip")
    assert len(set(_sets(game, bingo.deal(len(game.words), 8, 42)))) == 8


def test_bingo_preview_pdf_holds_every_card(capsys, tmp_path, printer):
    out_file = tmp_path / "cards.pdf"
    code, out, err = run(capsys, "-f", "json", "bingo", "-n", "3", "-o", str(out_file))
    assert code == 0, err
    data = json.loads(out)
    assert data["pages"] == 3 and [f["file"] for f in data["files"]] == [str(out_file)]
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", out_file.read_bytes())) == 3
    assert isinstance(data["seed"], int) and data["theme"] == "road trip"  # a fresh seed is reported
    assert printer.made == []


def test_the_reported_seed_deals_the_same_cards_again(capsys, tmp_path, printer):
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    code, out, _ = run(capsys, "-f", "json", "bingo", "--theme", "space", "-n", "2", "-o", str(first))
    seed = json.loads(out)["seed"]
    code, out, _ = run(capsys, "-f", "json", "bingo", "--theme", "space", "-n", "2", "--seed", str(seed),
                       "-o", str(second))
    assert code == 0
    for n in (1, 2):
        assert (tmp_path / f"a-{n}.png").read_bytes() == (tmp_path / f"b-{n}.png").read_bytes()


def test_bingo_prints_every_card_as_its_own_a4_job(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "--theme", "road trip", "-n", "8",
                         "--seed", "9", "--yes")
    assert code == 0, err
    data = json.loads(out)
    game, _ = bingo.game("road trip")
    expected = render.bingo_cards(game, bingo.deal(len(game.words), 8, 9))
    assert data["pages"] == 8 and len(data["job_ids"]) == 8 and data["estimate"]["pages"] == 8
    assert printer.jobs() == expected.jpegs()
    assert printer.names() == [f"prt bingo {n}/8 Road Trip Bingo" for n in range(1, 9)]
    for request in printer.requests():
        col = next(a.value for a in request[2] if a.name == "media-col")
        assert col["media-size"][1]["x-dimension"][1] == 21000
    assert guard.used_today() == 8


def test_bingo_reads_a_words_file_from_stdin(capsys, tmp_path, printer, stdin):
    stdin(json.dumps({"title": "Office Bingo", "free": "COFFEE", "words": _words(30, "Meeting")}).encode())
    out_file = tmp_path / "office.jpg"
    code, out, err = run(capsys, "-f", "json", "bingo", "--words-file", "-", "-n", "4", "-o", str(out_file))
    assert code == 0, err
    data = json.loads(out)
    assert data["title"] == "Office Bingo" and data["theme"] is None and data["words"] == 30
    assert all((tmp_path / f"office-{n}.jpg").is_file() for n in range(1, 5))


def test_a_words_file_with_any_theme(capsys, tmp_path, printer):
    path = _words_file(tmp_path, {"words": _words(25)})
    code, out, err = run(capsys, "-f", "json", "bingo", "--theme", "dinosaurs", "--words-file", str(path), "-n", "25",
                         "-o", str(tmp_path / "d.pdf"))
    assert code == 0, err
    data = json.loads(out)
    assert data["title"] == "Dinosaurs Bingo" and data["pages"] == 25


def test_bad_input_is_refused_before_anything_is_written_or_sent(capsys, tmp_path, printer):
    code, out, err = run(capsys, "-f", "json", "bingo", "--theme", "dinosaurs", "-n", "2", "--yes")
    assert code == 2 and _error(err)["kind"] == "unknown_theme" and out == ""
    path = _words_file(tmp_path, {"words": _words(25)})
    code, _, err = run(capsys, "-f", "json", "bingo", "--words-file", str(path), "-n", "26", "-o",
                       str(tmp_path / "x.png"))
    assert code == 3 and _error(err)["kind"] == "invalid_input" and "only 25 different" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "bingo", "--words-file", str(_words_file(tmp_path, {"words": []}, "e.json")),
                       "--yes")
    assert code == 3 and "fewer than 25" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "bingo", "-n", str(bingo.MAX_CARDS + 1), "-o", str(tmp_path / "y.png"))
    assert code == 2 and _error(err)["kind"] == "usage"
    code, _, err = run(capsys, "-f", "json", "bingo", "-n", "0", "-o", str(tmp_path / "y.png"))
    assert code == 2
    assert list(tmp_path.glob("*.png")) == [] and printer.made == [] and guard.used_today() == 0


def test_output_over_the_words_file_is_refused(capsys, tmp_path, printer):
    path = _words_file(tmp_path, {"words": _words(25)}, name="words.png")
    original = path.read_bytes()
    code, _, err = run(capsys, "-f", "json", "bingo", "--words-file", str(path), "-o", str(path))
    assert code == 2 and "overwrite" in _error(err)["message"] and path.read_bytes() == original


def test_bingo_needs_yes_in_agent_mode(capsys, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "--theme", "road trip", "-n", "8")
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []


def test_more_cards_than_the_agent_page_cap_are_refused_whole(capsys, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "-n", "12", "--yes")
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and printer.jobs() == []
    assert guard.used_today() == 0


def test_a_configured_daily_cap_counts_every_card(capsys, printer):
    cfg = Config.load()
    cfg.max_pages_per_day = 5
    cfg.save()
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "-n", "6", "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded" and printer.jobs() == []


def test_save_keeps_a_copy_and_prints(capsys, tmp_path, printer):
    copy = tmp_path / "copy.pdf"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "bingo", "-n", "2", "--save", str(copy), "--yes")
    assert code == 0, err
    data = json.loads(out)
    assert copy.is_file() and data["saved"][0]["file"] == str(copy) and len(printer.jobs()) == 2


def test_a_small_pool_deals_from_every_possible_card():
    # 26 words make 325 different cards (each leaves 2 words off); deal() itself has no cap, so a deal of all 325
    # lists each of them exactly once.
    cards = bingo.deal(26, 325, seed=0)
    assert {frozenset(c) for c in cards} == {frozenset(set(range(26)) - set(out))
                                             for out in combinations(range(26), 2)}
