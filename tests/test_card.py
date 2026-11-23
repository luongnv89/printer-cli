"""prt card: a photo and a verse on one A4 sheet that folds into a greeting card (#29)."""

import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw

from prt import card, catalog, guard
from prt.card import Card
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.layout import _font
from prt.testpage import H, W
from prt.transport import ipp

from conftest import without_estimate

PW, PH = card.PANEL_W, card.PANEL_H


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``made`` counts how often a driver was created at all."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    jobs = lambda: [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=jobs)


def _photo(tmp_path, name="photo.jpg", size=(300, 400), colour=(0, 0, 255)):
    path = tmp_path / name
    Image.new("RGB", size, colour).save(path)
    return path


def _card_file(tmp_path, content, name="card.json"):
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def _photo_image(size=(300, 400), colour=(0, 0, 255)):
    return Image.new("RGB", size, colour)


# --- folding: every panel lands in its place, the right way up -------------------------------------------
#
# Sheet coordinates: x to the right, y down, the printed side facing the viewer. The printed fold steps are
# card.FOLD: (1) fold the sheet in half from top to bottom with the printed side out, i.e. the top half turns
# over backwards about the line y = H/2; (2) fold in half again with the photo on the front: the photo panel
# keeps facing the viewer and the other half turns over backwards about the line x = W/2.

def _fold(x, y, z=1):
    """Where a point (x, y) of the sheet, on the face pointing ``z`` (+1 printed side), ends up after both folds:
    (x, y, z) in the same frame. The photo is printed on the bottom half, on the right (``card.PANELS``)."""
    if y < H / 2:  # fold 1: the top half turns over backwards about the horizontal centre line
        x, y, z = x, H - y, -z
    if x < W / 2:  # fold 2: the left half turns over backwards about the vertical centre line
        x, y, z = W - x, y, -z
    return x, y, z


def _views(x, y):
    """Where a printed point of the sheet is seen on the finished card: (view, u, v), the view being "front",
    "back" or "inside", and (u, v) the point's place in that view (u to the right, v down)."""
    fx, fy, fz = _fold(x, y)
    if y >= H / 2:  # a point of an outside face of the card
        if fz > 0:
            return "front", fx - W / 2, fy - H / 2
        return "back", W - fx, fy - H / 2  # the card turned over about its vertical axis
    # The inside spread is the sheet after fold 1 only, opened flat and seen from its other side: turned
    # over about the vertical axis, so the face that pointed away now points at the viewer.
    hx, hy, hz = x, H - y, -1
    assert hz < 0
    return "inside", W - hx, hy - H / 2


def _marked_panels():
    """Panels in four colours, each with a red mark at the top left and a green one at the top right."""
    out = {}
    for name, colour in zip(card.PANELS, [(200, 200, 255), (200, 255, 200), (255, 255, 180), (230, 230, 230)]):
        panel = Image.new("RGB", (PW, PH), colour)
        d = ImageDraw.Draw(panel)
        d.rectangle([60, 60, 100, 100], fill=(255, 0, 0))
        d.rectangle([PW - 100, 60, PW - 60, 100], fill=(0, 160, 0))
        out[name] = panel
    return out


def _centre_of(sheet, box, colour):
    """Centre of the pixels of ``colour`` inside ``box`` of the sheet."""
    region = sheet.crop(box)
    pixels = region.load()
    xs, ys = [], []
    for y in range(region.height):
        for x in range(region.width):
            if pixels[x, y] == colour:
                xs.append(x)
                ys.append(y)
    assert xs, f"no {colour} in {box}"
    return box[0] + sum(xs) / len(xs), box[1] + sum(ys) / len(ys)


def test_folding_the_sheet_as_printed_puts_every_panel_in_place_the_right_way_up():
    panels = _marked_panels()
    sheet = card.impose(panels)
    assert sheet.size == (W, H)
    background = {panel.getpixel((PW // 2, PH // 2)): name for name, panel in panels.items()}
    seen = {}
    for col in range(2):
        for row in range(2):
            box = (col * PW, row * PH, (col + 1) * PW, (row + 1) * PH)
            name = background[sheet.getpixel((box[0] + PW // 2, box[1] + PH // 2))]
            red = _views(*_centre_of(sheet, box, (255, 0, 0)))
            green = _views(*_centre_of(sheet, box, (0, 160, 0)))
            assert red[0] == green[0]
            assert abs(red[2] - green[2]) < 2 and red[2] < PH / 4  # the marks are along the top: upright
            assert red[1] < green[1]  # left before right: not mirrored
            view = red[0] if red[0] != "inside" else ("inside_left" if red[1] < PW else "inside_right")
            seen[name] = view
    assert seen == {"front": "front", "back": "back", "inside_left": "inside_left", "inside_right": "inside_right"}


def test_top_row_is_printed_upside_down_and_bottom_row_upright():
    panels = _marked_panels()
    sheet = card.impose(panels)
    for name, (col, row, flipped) in card.PANELS.items():
        quarter = sheet.crop((col * PW, row * PH, (col + 1) * PW, (row + 1) * PH))
        if flipped:
            quarter = quarter.transpose(Image.Transpose.ROTATE_180)
        # The fold marks are drawn over the sheet's edges; compare away from them.
        assert quarter.crop(INNER).tobytes() == panels[name].crop(INNER).tobytes(), name
    assert [name for name, (_, row, flipped) in card.PANELS.items() if flipped] == ["inside_right", "inside_left"]
    assert all(row == 0 for _, (_, row, flipped) in card.PANELS.items() if flipped)


def test_fold_marks_sit_on_the_fold_lines_at_the_sheet_edges():
    sheet = card.impose(_marked_panels())
    for x, y in ((5, PH), (W - 5, PH), (PW, 5), (PW, H - 5)):
        assert sheet.getpixel((x, y)) == card.GUIDE


def test_impose_refuses_a_panel_of_the_wrong_size():
    panels = _marked_panels()
    panels["back"] = Image.new("RGB", (10, 10))
    with pytest.raises(ValueError):
        card.impose(panels)


INNER = (card.TICK, card.TICK, PW - card.TICK, PH - card.TICK)  # a panel away from the fold marks


def test_compose_puts_the_photo_on_the_front_and_the_verse_inside_right():
    red = (220, 0, 0)
    sheet, cut = card.compose(_photo_image(colour=red))
    assert sheet.size == (W, H) and cut == {}
    drawn, _ = card.panels(_photo_image(colour=red))
    front = sheet.crop((PW, PH, W, H))
    assert front.crop(INNER).tobytes() == drawn["front"].crop(INNER).tobytes()
    x0, y0, x1, y1 = card.PHOTO_BOX
    assert front.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == red
    inside_right = sheet.crop((0, 0, PW, PH)).transpose(Image.Transpose.ROTATE_180)
    assert inside_right.crop(INNER).tobytes() == drawn["inside_right"].crop(INNER).tobytes()
    for name in ("inside_left", "inside_right", "back"):  # the photo is only on the front
        assert red not in {c for _, c in drawn[name].getcolors(1 << 20)}


def test_blank_inside_left_without_a_message_and_text_with_one():
    blank, _ = card.panels(_photo_image())
    written, _ = card.panels(_photo_image(), replace(card.DEFAULT_CARD, message="To Grandma"))
    assert written["inside_left"].tobytes() != blank["inside_left"].tobytes()
    assert written["front"].tobytes() == blank["front"].tobytes()
    assert written["inside_right"].tobytes() == blank["inside_right"].tobytes()
    assert len(blank["inside_left"].getcolors(1 << 20)) < 10  # only the paper and the border


def test_page_is_an_a4_jpeg_at_150dpi():
    sheet, _ = card.compose(_photo_image())
    from prt import booth
    page = Image.open(io.BytesIO(booth.to_jpeg(sheet)))
    assert page.size == (1240, 1754) and page.info["dpi"] == (150, 150)


# --- the verse ---------------------------------------------------------------------------------------------

def test_a_blank_line_in_the_verse_is_kept_as_one_stanza_break():
    clean = card.check_card(Card("Hi", "  One  \n\n\n\ttwo\r\nthree\n\n"), exit_code=3)
    assert clean.verse == "One\n\ntwo\nthree"


def test_stanza_gap_prints_half_a_row():
    font, size, rows, cut = card.fit_verse("One\n\nTwo", 400, 600)
    assert cut is None and [t for _, t in rows] == ["One", None, "Two"]
    with_gap = card._rows_height(rows, size)
    assert with_gap == 2 * round(size * 1.3) + round(size * 1.3) // 2


def test_verse_keeps_each_line_whole_when_smaller_type_allows():
    line = "The quick brown fox jumps over the lazy dog again"
    width = round(_font(card.VERSE_SIZE).getlength(line)) - 10  # wraps at full size only
    font, size, rows, cut = card.fit_verse(line, width, 600)
    assert cut is None and len(rows) == 1 and size < card.VERSE_SIZE


def _long_verse():
    """A verse within the length limit that is still too long for its box: many short lines in stanzas."""
    stanza = "\n".join(f"Line {i} of the song" for i in range(4))
    return card.check_card(Card("Hi", "\n\n".join([stanza] * 12)[:600]), exit_code=3).verse


def test_a_verse_too_long_for_its_box_is_cut_and_its_cut_counts_the_verse_given():
    verse = _long_verse()
    x0, y0, x1, y1 = card.VERSE_BOX
    font, size, rows, cut = card.fit_verse(verse, x1 - x0, y1 - y0)
    assert size == card.VERSE_MIN and cut is not None
    assert cut.given == len(verse) and 0 < cut.max_fit < cut.given
    assert rows[-1][1].endswith("…")
    assert verse[:cut.max_fit].endswith(rows[-1][1].removesuffix("…").rstrip())
    # Cut to max_fit characters (stanza breaks included in the count), the verse fits in the same box.
    assert card.fit_verse(verse[:cut.max_fit], x1 - x0, y1 - y0)[3] is None


def test_verse_cut_counts_characters_as_given_not_as_printed():
    verse = "€ " * 20 + "\n\n" + "words " * 300
    verse = card.check_card(Card("Hi", verse[:600]), exit_code=3).verse
    font, size, rows, cut = card.fit_verse(verse, 300, 120)
    assert cut is not None and cut.given == len(verse)
    assert card.fit_verse(verse[:cut.max_fit], 300, 120)[3] is None


def test_fields_cut_to_fit_are_reported_in_reading_order():
    # Within the length limits only text in very many lines, or in very long words, can overflow its box.
    long_text = card.with_overrides(card.DEFAULT_CARD, greeting="W" * 60, message="A note\n" * 42,
                                    verse=_long_verse(), signature="W" * 80, back="W" * 80)
    _, cut = card.compose(_photo_image(), long_text)
    assert list(cut) == ["greeting", "message", "verse", "signature", "back"]
    for name, c in cut.items():
        assert c.given == len(getattr(long_text, name)) and c.max_fit < c.given
    shorter = Card(**{name: getattr(long_text, name)[:cut[name].max_fit] for name in card.FIELDS})
    assert card.compose(_photo_image(), card.check_card(shorter, exit_code=3))[1] == {}


# --- the card file -----------------------------------------------------------------------------------------

def test_load_card_reads_every_field(tmp_path):
    data = {"greeting": "Happy Birthday!", "verse": "Roses\nare red", "message": "To Jo",
            "signature": "Love, Sam", "back": "Made by Sam"}
    assert card.load_card(_card_file(tmp_path, data)) == Card(**data)


@pytest.mark.parametrize("content, message", [
    ("not json", "not valid JSON"),
    ([1, 2], "must hold a JSON object"),
    ({"greeting": "Hi"}, "missing verse"),
    ({"verse": "x"}, "missing greeting"),
    ({"greeting": "Hi", "verse": "x", "headline": "y"}, "unknown field(s) in card file: headline"),
    ({"greeting": "Hi", "verse": 3}, "card file fields must be text: verse"),
    ({"greeting": "   ", "verse": "x"}, "empty greeting"),
    ({"greeting": "Hi", "verse": "x" * 601}, "verse is too long (601 characters, max 600)"),
    ({"greeting": "Hi 🎂", "verse": "x"}, "cannot print"),
    (b"\xff\xfe", "not UTF-8"),
])
def test_bad_card_file_is_refused_with_exit_code_3(tmp_path, content, message):
    with pytest.raises(PrinterError) as exc:
        card.load_card(_card_file(tmp_path, content))
    assert exc.value.exit_code == 3 and message in str(exc.value)


def test_missing_and_oversized_card_files(tmp_path):
    with pytest.raises(PrinterError, match="card file not found"):
        card.load_card(tmp_path / "nope.json")
    big = _card_file(tmp_path, b" " * (card.MAX_CARD_FILE + 1))
    with pytest.raises(PrinterError, match="larger than 64 KiB"):
        card.load_card(big)


def test_overrides_are_checked_as_usage_errors():
    assert card.with_overrides(card.DEFAULT_CARD, verse="A\nB", greeting=None).verse == "A\nB"
    with pytest.raises(PrinterError) as exc:
        card.with_overrides(card.DEFAULT_CARD, signature="x" * 81)
    assert exc.value.exit_code == 2


# --- prt styles and prt schema card ------------------------------------------------------------------------

def test_card_style_follows_the_card_fields_and_limits():
    style = next(s for s in catalog.styles() if s["name"] == "card")
    assert style["command"] == "card" and style["args"] == [] and style["schema"] == "card"
    assert style["kind"] == "greeting card" and style["photos"] == {"min": 1, "max": 1} and style["max_pages"] == 1
    assert [f["name"] for f in style["fields"]] == list(card.FIELDS)
    assert {f["name"]: f["max_length"] for f in style["fields"]} == card.LIMITS
    assert [f["name"] for f in style["fields"] if f["required"]] == list(card.REQUIRED)


def _schema_valid(data) -> bool:
    return Draft202012Validator(catalog.schema("card")).is_valid(data)


def _card_accepted(data) -> bool:
    try:
        card.parse_card(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


@pytest.mark.parametrize("data", [
    {"greeting": "Hi", "verse": "x"},
    {"greeting": "Hi", "verse": "x", "message": "", "signature": "", "back": ""},
    {"greeting": "x" * 60, "verse": "y" * 600, "message": "m" * 300, "signature": "s" * 80, "back": "b" * 80},
    {"greeting": "x" * 61, "verse": "y"},
    {"greeting": "Hi", "verse": "y" * 601},
    {"greeting": " ", "verse": "y"},
    {"greeting": "Hi"},
    {"greeting": "Hi", "verse": "y", "extra": "z"},
    {"greeting": "Hi", "verse": 1},
])
def test_card_schema_agrees_with_the_card_file_check(data):
    assert _schema_valid(data) == _card_accepted(data)


def test_card_schema_example_is_the_default_card():
    schema = catalog.schema("card")
    assert schema["examples"] == [{name: getattr(card.DEFAULT_CARD, name) for name in card.FIELDS}]
    assert _schema_valid(schema["examples"][0])


# --- prt card ------------------------------------------------------------------------------------------------

def test_card_preview_saves_the_sheet_and_prints_nothing(capsys, tmp_path, printer):
    photo, out_file = _photo(tmp_path), tmp_path / "card.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "card", str(photo), "--preview", str(out_file))
    assert code == 0 and "not printed" in err and "Fold the sheet" in err
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    assert without_estimate(out) == {"action": "card", "file": str(out_file), "bytes": out_file.stat().st_size,
                                     "fits": True, "cut": {}, "fold": card.FOLD}
    expected, _ = card.compose(card_photo := Image.open(photo).convert("RGB"))
    assert card_photo.size == (300, 400)
    assert Image.open(out_file).tobytes() == expected.tobytes()


def test_card_preview_needs_no_yes_in_agent_mode(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    out_file = tmp_path / "card.pdf"
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "card", str(_photo(tmp_path)), "-o", str(out_file))
    assert code == 0 and out_file.read_bytes().startswith(b"%PDF")
    assert printer.made == [] and guard.used_today() == 0


def test_card_prints_the_verse_from_the_command_line(capsys, tmp_path, printer):
    from prt import booth
    photo = _photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "card", str(photo), "--greeting", "Happy Birthday!",
                         "--verse", "Roses are red\n\nThe sky is blue", "--signature", "Love, Sam", "--yes")
    assert code == 0, err
    data = without_estimate(out)
    assert data == {"action": "card", "job_id": data["job_id"], "fits": True, "cut": {}, "fold": card.FOLD}
    expected = Card("Happy Birthday!", "Roses are red\n\nThe sky is blue", "", "Love, Sam",
                    card.DEFAULT_CARD.back)
    sheet, _ = card.compose(booth.load_photo(photo), expected)
    assert printer.jobs() == [booth.to_jpeg(sheet)]
    assert guard.used_today() == 1


def test_card_file_and_flags_override_it(capsys, tmp_path, printer):
    from prt import booth
    photo = _photo(tmp_path)
    path = _card_file(tmp_path, {"greeting": "Congratulations", "verse": "Well done", "back": "From the team"})
    out_file = tmp_path / "card.jpg"
    code, _, err = run(capsys, "card", str(photo), "--card-file", str(path), "--greeting", "Bravo", "-o",
                       str(out_file))
    assert code == 0, err
    expected = Card("Bravo", "Well done", back="From the team")
    assert out_file.read_bytes() == booth.to_jpeg(card.compose(booth.load_photo(photo), expected)[0])


def test_card_file_dash_reads_stdin(capsys, tmp_path, printer, stdin):
    stdin(json.dumps({"greeting": "Get well soon", "verse": "Rest up"}).encode())
    out_file = tmp_path / "card.png"
    code, out, err = run(capsys, "-f", "json", "card", str(_photo(tmp_path)), "--card-file", "-", "-o",
                         str(out_file))
    assert code == 0, err and out_file.is_file()


def test_bad_card_on_stdin_sends_nothing(capsys, tmp_path, printer, stdin):
    stdin(b'{"greeting": "Hi"}')
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "card", str(_photo(tmp_path)), "--card-file", "-",
                         "--yes")
    assert code == 3 and _error(err)["kind"] == "invalid_input" and "missing verse" in _error(err)["message"]
    assert out == "" and printer.made == [] and guard.used_today() == 0


def test_card_cut_is_reported_and_warned(capsys, tmp_path, printer):
    out_file = tmp_path / "card.png"
    code, out, err = run(capsys, "-f", "json", "card", str(_photo(tmp_path)), "--greeting", "W" * 60,
                         "-o", str(out_file))
    assert code == 0 and "greeting cut to fit the card" in err
    data = json.loads(out)
    assert data["fits"] is False and set(data["cut"]) == {"greeting"}
    assert data["cut"]["greeting"]["given"] == 60 and 0 < data["cut"]["greeting"]["max_fit"] < 60


@pytest.mark.parametrize("args, code, message", [
    (["--greeting", ""], 2, "empty greeting"),
    (["--verse", "x" * 601], 2, "verse is too long"),
    (["-o", "card.txt"], 2, "unknown file type"),
])
def test_bad_flags_are_usage_errors_and_print_nothing(capsys, tmp_path, printer, args, code, message):
    result, _, err = run(capsys, "-p", "demo", "-f", "json", "card", str(_photo(tmp_path)), *args, "--yes")
    assert result == code and message in _error(err)["message"]
    assert printer.made == [] and printer.jobs() == []


def test_unreadable_photo_is_bad_input(capsys, tmp_path, printer):
    bad = tmp_path / "photo.jpg"
    bad.write_bytes(b"not an image")
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "card", str(bad), "--yes")
    assert code == 3 and printer.made == []


def test_output_over_the_photo_or_card_file_is_refused(capsys, tmp_path, printer):
    photo = _photo(tmp_path, "photo.png")
    code, _, err = run(capsys, "-f", "json", "card", str(photo), "-o", str(photo))
    assert code == 2 and "overwrite" in _error(err)["message"] and photo.stat().st_size > 0
    path = _card_file(tmp_path, {"greeting": "Hi", "verse": "x"}, name="card.png")
    code, _, err = run(capsys, "-f", "json", "card", str(photo), "--card-file", str(path), "--save", str(path))
    assert code == 2 and "overwrite" in _error(err)["message"]


def test_card_needs_yes_without_a_terminal(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "card", str(_photo(tmp_path)))
    assert code == 2 and _error(err)["kind"] == "confirmation_required" and printer.jobs() == []
    assert guard.used_today() == 0


def test_card_asks_before_printing(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, _, err = run(capsys, "-p", "demo", "card", str(_photo(tmp_path)))
    assert code == 0 and asked == ["Print photo.jpg as a greeting card on demo?"]
    assert "card queued as job" in err and "Fold the sheet" in err and len(printer.jobs()) == 1


def test_save_keeps_a_copy_and_prints(capsys, tmp_path, printer):
    copy = tmp_path / "card.pdf"
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "card", str(_photo(tmp_path)), "--save", str(copy),
                       "--yes")
    assert code == 0 and copy.read_bytes().startswith(b"%PDF") and len(printer.jobs()) == 1
    assert json.loads(out)["saved"] == [{"file": str(copy), "format": "pdf", "bytes": copy.stat().st_size}]
