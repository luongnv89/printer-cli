"""prt comic: 3 to 6 photos as comic panels with speech bubbles from a JSON comic file (#30)."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import booth, catalog, comic, guard, render
from prt.cli import main
from prt.comic import Bubble, Comic, Panel
from prt.config import Config
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import A4
from prt.transport import ipp

from conftest import ROOT, without_estimate

DARK = (20, 30, 60)


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


def _photos(tmp_path, n=3, colour=DARK, size=(400, 300)):
    paths = []
    for i in range(n):
        path = tmp_path / f"photo{i}.jpg"
        Image.new("RGB", size, colour).save(path, quality=95)
        paths.append(path)
    return paths


def _images(n=3, colour=DARK):
    return [Image.new("RGB", (400, 300), colour) for _ in range(n)]


def _comic_file(tmp_path, data, name="comic.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _panels(n, **panel):
    return [dict(panel) for _ in range(n)]


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``jobs()`` are the JPEGs sent over IPP."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=lambda: [data for _, _, _, data in requests()])


# --- the page ----------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [3, 4, 5, 6])
def test_panels_fill_the_page_in_rows_without_overlapping(n):
    boxes = comic.panel_boxes(n)
    assert len(boxes) == n == sum(comic.ROWS[n])
    for x0, y0, x1, y1 in boxes:
        assert comic.MARGIN <= x0 < x1 <= comic.W - comic.MARGIN and comic.MARGIN <= y0 < y1 <= comic.H - comic.MARGIN
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    rows = sorted({y0 for _, y0, _, _ in boxes})
    assert len(rows) == len(comic.ROWS[n])
    assert [b for b in boxes] == sorted(boxes, key=lambda b: (b[1], b[0]))  # reading order


@pytest.mark.parametrize("n", [3, 4, 5, 6])
def test_the_built_in_comic_fits_on_an_a4_page_for_every_number_of_photos(n):
    page, cut = comic.compose(_images(n))
    assert page.size == A4.size and page.mode == "RGB"
    assert cut == {}
    built_in = comic.default_comic(n)
    assert len(built_in.panels) == n and built_in.panels[-1] == comic.check_comic(comic.DEFAULT_COMIC, 2).panels[-1]


@pytest.mark.parametrize("n", [0, 1, 2, 7, 12])
def test_a_comic_takes_3_to_6_photos(n):
    with pytest.raises(PrinterError) as exc:
        comic.compose(_images(n))
    assert exc.value.exit_code == 2 and "3 to 6 photos" in str(exc.value)


def test_a_comic_needs_one_panel_per_photo():
    with pytest.raises(PrinterError) as exc:
        comic.compose(_images(4), comic.default_comic(3))
    assert exc.value.exit_code == 2 and "3 panels but 4 photos" in str(exc.value)


def _band_is_lighter(page, box, top=True):
    """Whether the top (or bottom) third of a panel ``box`` holds white bubble pixels over the dark photo."""
    x0, y0, x1, y1 = box
    third = (y1 - y0) // 3
    band = page.crop((x0 + 10, y0 + 10, x1 - 10, y0 + third) if top else (x0 + 10, y1 - third, x1 - 10, y1 - 10))
    return band.convert("L").getextrema()[1] == 255


def test_bubbles_are_drawn_in_their_band_over_the_photo():
    data = Comic(
        panels=(Panel((Bubble("Hello there!", "top-left"),)), Panel((Bubble("Down here", "bottom-right"),)), Panel())
    )
    page, cut = comic.compose(_images(3), comic.check_comic(data, 3))
    assert cut == {}
    boxes = comic.panel_boxes(3, comic.MARGIN)  # no title or byline: the panels start at the margin
    assert _band_is_lighter(page, boxes[0], top=True) and not _band_is_lighter(page, boxes[0], top=False)
    assert _band_is_lighter(page, boxes[1], top=False) and not _band_is_lighter(page, boxes[1], top=True)
    assert not _band_is_lighter(page, boxes[2]) and not _band_is_lighter(page, boxes[2], top=False)


@pytest.mark.parametrize(
    "kind, tail", [("speech", "left"), ("speech", "none"), ("thought", "right"), ("thought", "none")]
)
def test_every_kind_and_tail_draws(kind, tail):
    panels = tuple(
        Panel((Bubble("Words in a bubble", position, tail, kind),)) for position in ("top", "bottom-left", "top-right")
    )
    page, cut = comic.compose(_images(3), comic.check_comic(Comic(panels), 3))
    assert cut == {} and _band_is_lighter(page, comic.panel_boxes(3)[0])


def test_three_bubbles_in_one_band_share_it_without_overlapping():
    bubbles = tuple(Bubble(text, "top-left") for text in ("One", "Two words", "And three"))
    panels = (Panel(bubbles),) + (Panel(),) * 2
    checked = comic.check_comic(Comic(panels), 3)
    assert comic.compose(_images(3), checked)[1] == {}


def test_overlong_text_is_cut_and_reported_per_bubble_in_reading_order():
    long = "word " * 28
    data = Comic(
        title="W" * 60,
        panels=(
            Panel((Bubble("Short."), Bubble(long), Bubble(long)), caption="WWWW " * 20),
            Panel((Bubble("Short.", "bottom"),)),
            Panel(),
            Panel(),
            Panel(),
            Panel(),
        ),
    )
    checked = comic.check_comic(data, 3)
    _, cut = comic.compose(_images(6), checked)
    assert list(cut) == ["title", "panels[0].caption", "panels[0].bubbles[1]", "panels[0].bubbles[2]"]
    for c in cut.values():
        assert 0 <= c.max_fit < c.given
    assert cut["panels[0].bubbles[1]"].given == len(checked.panels[0].bubbles[1].text)


def test_a_bubble_cut_to_its_max_fit_then_fits():
    """The cut's contract: the text cut to its first ``max_fit`` characters fits in the same bubble."""
    text = "This is a rather long line of dialogue that keeps on going well past the space a bubble has"
    panels = (Panel((Bubble("One"), Bubble("Two"), Bubble(text, "top-right"))),) + (Panel(),) * 5
    _, cut = comic.compose(_images(6), comic.check_comic(Comic(panels), 3))
    c = cut["panels[0].bubbles[2]"]
    shorter = (Panel((Bubble("One"), Bubble("Two"), Bubble(text[: c.max_fit], "top-right"))),) + (Panel(),) * 5
    assert comic.compose(_images(6), comic.check_comic(Comic(shorter), 3))[1] == {}


def test_fit_bubble_keeps_words_whole_when_it_can():
    fitted = comic.fit_bubble("Too easy. Something is up.", "thought", 250, 300)
    assert fitted.cut is None and " ".join(fitted.lines) == "Too easy. Something is up."
    assert fitted.width <= 250 and fitted.height <= 300


def test_fit_bubble_leaves_a_bubble_with_no_room_empty_and_cut():
    fitted = comic.fit_bubble("Hello", "speech", 300, 30)
    assert fitted.lines == [] and fitted.cut.max_fit == 0 and fitted.cut.given == 5


def test_fit_bubble_of_only_combining_marks_is_empty_and_not_cut():
    fitted = comic.fit_bubble("́", "thought", 300, 300)
    assert fitted.lines == [] and fitted.cut is None and (fitted.width, fitted.height, fitted.puff) == (0, 0, 0)


@pytest.mark.parametrize("field", ["bubble", "caption", "title", "byline"])
def test_text_of_only_combining_marks_is_drawn_without_crashing(capsys, tmp_path, field):
    """A text that passes the schema but has nothing to print (a lone combining accent) is left out, not a crash."""
    mark = "́"
    first = {"bubbles": [{"text": mark}]} if field == "bubble" else {"caption": mark} if field == "caption" else {}
    data = {"panels": [first, {"bubbles": [{"text": "Hi!"}]}, {}]}
    if field in ("title", "byline"):
        data[field] = mark
    assert _accepted(data)
    page, cut = comic.compose(_images(3), comic.parse_comic(json.dumps(data)))
    assert page.size == (comic.W, comic.H) and cut == {}
    out_file = tmp_path / "comic.png"
    code, _, err = run(
        capsys,
        "comic",
        *map(str, _photos(tmp_path, 3)),
        "--comic-file",
        str(_comic_file(tmp_path, data)),
        "-o",
        str(out_file),
    )
    assert code == 0, err
    assert out_file.read_bytes().startswith(b"\x89PNG")


# --- the comic file ----------------------------------------------------------------------------


def test_comic_file_fills_in_default_positions_and_cleans_text():
    parsed = comic.parse_comic(
        json.dumps(
            {
                "title": "  A   day ",
                "panels": [
                    {
                        "caption": "Morning",
                        "bubbles": [{"text": "Hi!"}, {"text": "Hello\nyou"}, {"text": "Bye", "kind": "thought"}],
                    },
                    {},
                    {"bubbles": []},
                ],
            }
        )
    )
    assert parsed.title == "A day" and len(parsed.panels) == 3
    assert [b.position for b in parsed.panels[0].bubbles] == ["top-left", "top-right", "bottom-right"]
    assert parsed.panels[0].bubbles[1].text == "Hello you" and parsed.panels[0].bubbles[2].kind == "thought"
    assert parsed.panels[0].bubbles[0].tail == "center" and parsed.panels[1] == Panel()


@pytest.mark.parametrize(
    "data, where",
    [
        ({"panels": _panels(2)}, "2 panels"),
        ({"panels": _panels(7)}, "7 panels"),
        ({}, "missing panels"),
        ({"panels": {}}, "panels must be a list"),
        ({"panels": [{"bubbles": [{"text": "a"}] * 4}, {}, {}]}, "panels[0] has 4 bubbles"),
        ({"panels": [{}, {"bubbles": [{"text": "a", "position": "middle"}]}, {}]}, "panels[1].bubbles[0].position"),
        ({"panels": [{}, {}, {"bubbles": [{"text": "a", "tail": "up"}]}]}, "panels[2].bubbles[0].tail"),
        ({"panels": [{"bubbles": [{"text": "a", "kind": "shout"}]}, {}, {}]}, "panels[0].bubbles[0].kind"),
        ({"panels": [{"bubbles": [{"position": "top"}]}, {}, {}]}, "panels[0].bubbles[0] is missing text"),
        ({"panels": [{"bubbles": [{"text": "  "}]}, {}, {}]}, "panels[0].bubbles[0].text is empty"),
        ({"panels": [{"bubbles": [{"text": "x" * 141}]}, {}, {}]}, "panels[0].bubbles[0].text is too long"),
        ({"panels": [{"bubbles": [{"text": 5}]}, {}, {}]}, "must be text"),
        ({"panels": [{"bubbles": ["Hi"]}, {}, {}]}, "panels[0].bubbles[0] must be a JSON object"),
        ({"panels": [{"caption": "c" * 101}, {}, {}]}, "panels[0].caption is too long"),
        ({"panels": [{"speech": "Hi"}, {}, {}]}, "unknown field(s) in panels[0]: speech"),
        ({"panels": _panels(3), "colour": "red"}, "unknown field(s)"),
        ({"panels": _panels(3), "title": "t" * 61}, "title is too long"),
        ({"panels": [{"bubbles": [{"text": "Party \U0001f389"}]}, {}, {}]}, "cannot print"),
        ([], "must hold a JSON object"),
    ],
)
def test_bad_comic_files_are_refused_with_exit_code_3_saying_where(data, where):
    with pytest.raises(PrinterError) as exc:
        comic.parse_comic(json.dumps(data))
    assert exc.value.exit_code == 3 and where in str(exc.value)


def test_comic_file_that_is_missing_not_json_or_too_big_is_refused(tmp_path):
    for content, message in (
        (b"{nope", "not valid JSON"),
        (b"\xff\xfe", "not UTF-8"),
        (b" " * (comic.MAX_COMIC_FILE + 1), "larger than 64 KiB"),
    ):
        path = tmp_path / "c.json"
        path.write_bytes(content)
        with pytest.raises(PrinterError) as exc:
            comic.load_comic(path)
        assert exc.value.exit_code == 3 and message in str(exc.value)
    with pytest.raises(PrinterError) as exc:
        comic.load_comic(tmp_path / "missing.json")
    assert exc.value.exit_code == 3 and "not found" in str(exc.value)


def test_flags_override_the_file_and_are_checked_as_usage():
    base = comic.default_comic(3)
    assert comic.with_overrides(base, title="New", byline="").title == "New"
    assert comic.with_overrides(base, byline="").byline == ""
    assert comic.with_overrides(base).title == base.title
    with pytest.raises(PrinterError) as exc:
        comic.with_overrides(base, title="t" * 61)
    assert exc.value.exit_code == 2


def test_an_override_changes_only_its_own_field():
    base = replace(comic.default_comic(3), title="Base title", byline="by the base")
    kept = comic.with_overrides(base)
    assert (kept.title, kept.byline, kept.panels) == ("Base title", "by the base", base.panels)
    titled = comic.with_overrides(base, title="New")
    assert (titled.title, titled.byline) == ("New", "by the base")
    signed = comic.with_overrides(base, byline="by us")
    assert (signed.title, signed.byline) == ("Base title", "by us")


def test_to_dict_round_trips_through_the_parser():
    checked = comic.check_comic(comic.DEFAULT_COMIC, 3)
    assert comic.parse_comic(json.dumps(comic.to_dict(checked))) == checked
    assert comic.parse_comic(json.dumps(comic.to_dict(comic.DEFAULT_COMIC))) == checked


# --- styles and schema -------------------------------------------------------------------------


def _accepted(data) -> bool:
    try:
        comic.parse_comic(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


@pytest.mark.parametrize(
    "data",
    [
        {"panels": _panels(3)},
        {"panels": _panels(6), "title": "T", "byline": "B"},
        {
            "panels": [
                {"caption": "c", "bubbles": [{"text": "a", "position": "bottom", "tail": "none", "kind": "thought"}]},
                {"bubbles": [{"text": "a"}, {"text": "b"}, {"text": "c"}]},
                {},
            ]
        },
        {"panels": [{"bubbles": [{"text": "Ça va? Привет!"}]}, {}, {}]},
        {"panels": _panels(2)},
        {"panels": _panels(7)},
        {"panels": [{"bubbles": [{"text": "a"}] * 4}, {}, {}]},
        {"panels": [{"bubbles": [{"text": "a", "position": "middle"}]}, {}, {}]},
        {"panels": [{"bubbles": [{"text": "a", "tail": "up"}]}, {}, {}]},
        {"panels": [{"bubbles": [{"text": "a", "kind": "shout"}]}, {}, {}]},
        {"panels": [{"bubbles": [{"text": ""}]}, {}, {}]},
        {"panels": [{"bubbles": [{"text": "   "}]}, {}, {}]},
        {"panels": [{"bubbles": [{"text": "x" * 141}]}, {}, {}]},
        {"panels": [{"bubbles": [{"tail": "left"}]}, {}, {}]},
        {"panels": [{"bubbles": ["Hi"]}, {}, {}]},
        {"panels": [{"caption": 1}, {}, {}]},
        {"panels": [{"caption": "c" * 101}, {}, {}]},
        {"panels": [{"extra": 1}, {}, {}]},
        {"panels": _panels(3), "title": None},
        {"panels": _panels(3), "title": "t" * 61},
        {"panels": "three"},
        {"title": "T"},
        [],
    ],
)
def test_schema_agrees_with_the_comic_parser(data):
    assert Draft202012Validator(catalog.schema("comic")).is_valid(data) == _accepted(data)


def test_schema_example_and_sample_input_pass_the_schema_and_the_parser():
    schema = catalog.schema("comic")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    sample = json.loads((ROOT / "assets" / "samples" / "comic.json").read_text())
    for data in schema["examples"] + [sample]:
        assert validator.is_valid(data) and _accepted(data)


def test_styles_list_the_comic_strip(capsys):
    style = next(s for s in catalog.styles() if s["name"] == "comic")
    assert style["command"] == "comic" and style["schema"] == "comic" and style["max_pages"] == 1
    assert style["photos"] == {"min": 3, "max": 6} and style["paper"]["size"] == "a4"
    assert [(f["name"], f["max_length"]) for f in style["fields"]] == [
        ("title", 60),
        ("byline", 60),
        ("panels[].caption", 100),
        ("panels[].bubbles[].text", 140),
    ]
    code, out, _ = run(capsys, "schema", "comic")
    assert code == 0 and json.loads(out)["title"] == "prt comic"


# --- prt comic ---------------------------------------------------------------------------------


@pytest.mark.parametrize("suffix, magic", [(".png", b"\x89PNG"), (".pdf", b"%PDF"), (".jpg", b"\xff\xd8")])
def test_preview_saves_the_page_and_prints_nothing(capsys, tmp_path, printer, suffix, magic):
    out_file = tmp_path / f"comic{suffix}"
    photos = [str(p) for p in _photos(tmp_path, 4)]
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "comic", *photos, "-o", str(out_file))
    assert code == 0, err
    assert "not printed" in err and out_file.read_bytes().startswith(magic)
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    assert without_estimate(out) == {
        "action": "comic",
        "file": str(out_file),
        "bytes": out_file.stat().st_size,
        "photos": 4,
        "title": "The Big Day",
        "paper": {"size": "a4", "media": "a4", "tray": None},
        "fits": True,
        "cut": {},
    }


def test_preview_png_is_the_composed_page(capsys, tmp_path, printer):
    photos = _photos(tmp_path, 3)
    data = {"title": "Ours", "panels": [{"bubbles": [{"text": "Hi!"}]}, {"caption": "Later"}, {}]}
    out_file = tmp_path / "comic.png"
    code, _, err = run(
        capsys,
        "comic",
        *map(str, photos),
        "--comic-file",
        str(_comic_file(tmp_path, data)),
        "--byline",
        "by us",
        "-o",
        str(out_file),
    )
    assert code == 0, err
    expected = render.comic_page(
        [booth.load_photo(p) for p in photos], comic.with_overrides(comic.parse_comic(json.dumps(data)), byline="by us")
    ).pages[0]
    assert Image.open(out_file).convert("RGB").tobytes() == expected.tobytes()


def test_yes_prints_one_a4_page(capsys, tmp_path, printer):
    photos = _photos(tmp_path, 3)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "comic", *map(str, photos), "--yes")
    assert code == 0, err
    data = without_estimate(out)
    assert data["action"] == "comic" and data["job_id"] and data["fits"] is True
    images = [booth.load_photo(p) for p in photos]
    assert printer.jobs() == render.comic_page(images, comic.default_comic(3)).jpegs()
    assert guard.used_today() == 1


def test_the_prompt_asks_before_printing(capsys, tmp_path, printer, monkeypatch):
    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda question, **kw: asked.append(question) or False)
    code, _, _ = run(capsys, "-p", "demo", "comic", *map(str, _photos(tmp_path, 3)))
    assert code == 1 and printer.jobs() == [] and guard.used_today() == 0
    assert len(asked) == 1 and "3 photos as a comic strip" in asked[0]


def test_agent_mode_needs_yes_but_preview_does_not(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photos = [str(p) for p in _photos(tmp_path, 3)]
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "comic", *photos)
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []
    assert run(capsys, "-p", "demo", "comic", *photos, "-o", str(tmp_path / "c.png"))[0] == 0
    assert guard.used_today() == 0
    assert run(capsys, "-p", "demo", "comic", *photos, "--yes")[0] == 0 and len(printer.jobs()) == 1


def test_a_daily_page_cap_refuses_the_print_before_anything_is_sent(capsys, tmp_path, printer):
    cfg = Config.load()
    cfg.max_pages_per_day = 1
    cfg.save()
    photos = [str(p) for p in _photos(tmp_path, 3)]
    assert run(capsys, "-p", "demo", "comic", *photos, "--yes")[0] == 0
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "comic", *photos, "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded"
    assert len(printer.jobs()) == 1 and guard.used_today() == 1


def test_comic_file_from_stdin(capsys, tmp_path, printer, stdin):
    photos = [str(p) for p in _photos(tmp_path, 3)]
    stdin(json.dumps({"title": "Piped", "panels": _panels(3, bubbles=[{"text": "Hi"}])}).encode())
    code, out, err = run(capsys, "-f", "json", "comic", *photos, "--comic-file", "-", "-o", str(tmp_path / "a.png"))
    assert code == 0, err
    assert json.loads(out)["title"] == "Piped" and printer.made == []


@pytest.mark.parametrize(
    "content",
    [
        b"{nope",
        b'{"panels": 3}',
        b'{"panels": [{}, {}, {"bubbles": [{"text": 1}]}]}',
        b'{"panels": [{}, {}, {"bubbles": [{"text": "\xf0\x9f\x8e\x89"}]}]}',
    ],
)
def test_bad_comic_on_stdin_exits_3_and_prints_nothing(capsys, tmp_path, printer, stdin, content):
    stdin(content)
    code, out, err = run(
        capsys, "-p", "demo", "-f", "json", "comic", *map(str, _photos(tmp_path, 3)), "--comic-file", "-", "--yes"
    )
    assert code == 3 and _error(err)["kind"] == "invalid_input" and out == ""
    assert printer.made == [] and guard.used_today() == 0


def test_wrong_counts_bad_photos_and_outputs_are_refused_before_anything_is_written(capsys, tmp_path, printer):
    photos = [str(p) for p in _photos(tmp_path, 4)]
    out_file = tmp_path / "x.png"
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:2], "-o", str(out_file))
    assert code == 2 and "3 to 6 photos" in _error(err)["message"]
    path = _comic_file(tmp_path, {"panels": _panels(3)}, name="comic.png")
    code, _, err = run(capsys, "-f", "json", "comic", *photos, "--comic-file", str(path), "-o", str(out_file))
    assert code == 2 and "3 panels but 4 photos" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:3], "--title", "\U0001f389", "-o", str(out_file))
    assert code == 2 and "cannot print" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:3], "-o", photos[0])
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:3], "--comic-file", str(path), "-o", str(path))
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:2], str(tmp_path / "missing.jpg"), "-o", str(out_file))
    assert code == 3 and _error(err)["kind"] == "invalid_input"
    code, _, err = run(capsys, "-f", "json", "comic", *photos[:3], "-o", str(tmp_path / "x.gif"))
    assert code == 2
    assert printer.made == [] and not out_file.exists() and guard.used_today() == 0


def test_a_cut_bubble_is_warned_and_reported(capsys, tmp_path, printer):
    long = ("This bubble has far more words than a small comic panel could ever hold, " * 2)[:140].strip()
    data = {"panels": _panels(5, bubbles=[{"text": "One"}, {"text": "Two"}, {"text": long}])}
    code, out, err = run(
        capsys,
        "-f",
        "json",
        "comic",
        *map(str, _photos(tmp_path, 5)),
        "--comic-file",
        str(_comic_file(tmp_path, data)),
        "-o",
        str(tmp_path / "c.jpg"),
    )
    assert code == 0, err
    assert "panels[0].bubbles[2]" in err and "cut to fit the comic strip" in err
    report = json.loads(out)
    assert report["fits"] is False and "panels[0].bubbles[2]" in report["cut"]
    c = report["cut"]["panels[0].bubbles[2]"]
    assert c["given"] == len(long) and 0 < c["max_fit"] < len(long)


def test_the_readme_example_is_a_valid_comic_file():
    section = (ROOT / "README.md").read_text(encoding="utf-8").split("## Comic strips")[1]
    example = json.loads(section.split("```json")[1].split("```")[0])
    assert Draft202012Validator(catalog.schema("comic")).is_valid(example) and _accepted(example)
