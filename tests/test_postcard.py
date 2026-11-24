"""prt postcard: a photo front and a message, address and stamp-box back, printed two-sided on 4×6 (#32)."""

import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import catalog, guard, postcard, printing, render
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.models import PrintOptions
from prt.paper import A4, PHOTO_4X6
from prt.postcard import Postcard
from prt.transport import ipp

from conftest import without_estimate

RED = (220, 30, 30)


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


def _photo(tmp_path, name="photo.jpg", colour=RED, size=(600, 400)):
    path = tmp_path / name
    Image.new("RGB", size, colour).save(path, quality=95)
    return path


def _postcard_file(tmp_path, data, name="postcard.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _media_col(request):
    col = next(a.value for a in request[2] if a.name == "media-col")
    return {k: v[1] for k, v in col.items() if k != "media-size"}


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI, with no CUPS queue unless a test gives it one. ``jobs()`` are the
    JPEGs sent over IPP, ``cups`` the two-sided jobs handed to CUPS."""
    fake = FakeDriver()
    made, cups = [], []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    monkeypatch.setattr("prt.printing.find_cups_queue", lambda uuid, host: None)

    def cups_print(queue, path, options):
        cups.append(SimpleNamespace(queue=queue, pdf=path.read_bytes(), options=options))
        return 77
    monkeypatch.setattr("prt.printing.cups_print", cups_print)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, cups=cups, requests=requests,
                           jobs=lambda: [data for _, _, _, data in requests()])


@pytest.fixture
def terminal(monkeypatch):
    """A terminal on stdin; ``answers`` are what each prompt gets, in order, and ``asked`` the prompts."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    state = SimpleNamespace(asked=[], answers=[])

    def confirm(question, **kw):
        state.asked.append(question)
        return state.answers.pop(0) if state.answers else True
    monkeypatch.setattr("typer.confirm", confirm)
    return state


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)


def _sides(photo_path, card=None):
    from prt import booth
    return postcard.compose(booth.load_photo(photo_path), card)[0]


# --- the layout ----------------------------------------------------------------------------------

def test_both_sides_are_landscape_4x6_at_300dpi():
    pages, cut = postcard.compose(Image.new("RGB", (600, 400), RED))
    assert [page.size for page in pages] == [(1800, 1200), (1800, 1200)] == [PHOTO_4X6.landscape_size] * 2
    assert cut == {}


def test_front_is_the_photo_inside_a_white_edge():
    page = postcard.front(Image.new("RGB", (300, 600), RED), Postcard(message="x"), {})
    assert page.getpixel((10, 10)) == postcard.CARD and page.getpixel((1790, 1190)) == postcard.CARD
    assert page.getpixel((900, 600)) == RED and page.getpixel((postcard.EDGE + 2, 1200 - postcard.EDGE - 3)) == RED


def test_greeting_goes_on_one_line_when_it_can_and_never_needs_cutting():
    width = postcard.PHOTO_BOX[2] - postcard.PHOTO_BOX[0] - 2 * postcard.GREETING_PAD
    assert len(postcard.fit_greeting("Greetings from Lisbon", width)[2]) == 1
    _, size, lines, cut = postcard.fit_greeting("W" * postcard.LIMITS["greeting"], width)
    assert len(lines) == 2 and size <= 72 and cut is None


def test_front_greeting_is_written_on_a_band_across_the_foot_of_the_photo():
    photo = Image.new("RGB", (600, 400), (240, 240, 240))
    plain = postcard.front(photo, Postcard(message="x"), {})
    greeted = postcard.front(photo, Postcard(greeting="Greetings from Lisbon", message="x"), {})
    foot, top = (postcard.EDGE + 5, 1200 - postcard.EDGE - 5), (900, 200)
    assert plain.getpixel(foot) == (240, 240, 240) and sum(greeted.getpixel(foot)) < sum(plain.getpixel(foot))
    assert greeted.getpixel(top) == plain.getpixel(top)
    band = greeted.crop((postcard.EDGE, 900, 1800 - postcard.EDGE, 1200 - postcard.EDGE))
    assert (255, 255, 255) in {colour for _, colour in band.getcolors(1 << 20)}  # the white handwriting


def test_back_has_the_divider_stamp_box_and_four_address_lines():
    page = postcard.back(Postcard(message="Hello"), {})
    x0, y0, x1, y1 = postcard.STAMP_BOX
    assert page.getpixel((x0, (y0 + y1) // 2)) == postcard.PRINTED == page.getpixel(((x0 + x1) // 2, y1 - 1))
    assert page.getpixel((postcard.DIVIDER_X, 600)) == postcard.PRINTED
    for baseline in postcard.ADDRESS_BASELINES:
        assert page.getpixel(((postcard.ADDRESS_LEFT + postcard.ADDRESS_RIGHT) // 2, baseline)) == postcard.PRINTED
    assert len(postcard.ADDRESS_BASELINES) == postcard.ADDRESS_LINES


def _inked(page, box):
    return any(colour != postcard.CARD for _, colour in page.crop(box).getcolors(1 << 20))


def test_message_goes_left_of_the_divider_and_the_address_on_the_lines_right_of_it():
    blank = postcard.back(Postcard(message="Hello"), {})
    full = postcard.back(Postcard(message="Hello", address="Jo Smith\n12 High Street"), {})
    message_area = (postcard.MARGIN, 180, postcard.DIVIDER_X - 10, 400)
    assert _inked(blank, message_area)
    first_line = (postcard.ADDRESS_LEFT, postcard.ADDRESS_BASELINES[0] - 90, postcard.ADDRESS_RIGHT,
                  postcard.ADDRESS_BASELINES[0] - 4)
    third_line = (postcard.ADDRESS_LEFT, postcard.ADDRESS_BASELINES[2] - 90, postcard.ADDRESS_RIGHT,
                  postcard.ADDRESS_BASELINES[2] - 4)
    assert not _inked(blank, first_line) and _inked(full, first_line) and not _inked(full, third_line)


def test_message_paragraphs_keep_their_break():
    font, size, lines, cut = postcard.fit_message("First paragraph.\nSecond one.", 780, 800)
    assert cut is None and lines == ["First paragraph.", "", "Second one."]


def test_a_message_too_long_for_its_box_is_cut_and_the_cut_part_fits():
    text = " ".join(["Words that go on and on"] * 21)[:500]
    _, _, _, cut = postcard.fit_message(text, 780, 300)
    assert cut is not None and cut.given == len(text) and 0 < cut.max_fit < len(text)
    assert postcard.fit_message(text[:cut.max_fit], 780, 300)[3] is None


def test_cut_fields_are_reported_in_reading_order_and_counted_in_the_address_given():
    card = Postcard(greeting="W" * 60, message=" ".join(["Words that go on"] * 100), signature="S" * 60,
                    address="Jo Smith\n" + "W" * 40)
    _, cut = postcard.compose(Image.new("RGB", (60, 40), RED), card)
    assert list(cut) == ["message", "signature", "address"]  # a greeting of 60 characters always fits
    address = cut["address"]
    assert address.given == len(card.address) and len("Jo Smith\n") < address.max_fit < len(card.address)


def test_flipped_back_is_a_half_turn():
    page = Image.new("RGB", (4, 2), "white")
    page.putpixel((0, 0), RED)
    assert postcard.flipped(page).getpixel((3, 1)) == RED


def test_side_paths_are_one_pdf_or_a_front_and_a_back_image(tmp_path):
    assert postcard.side_paths(tmp_path / "card.pdf", "pdf") == [tmp_path / "card.pdf"]
    assert postcard.side_paths(tmp_path / "card.png", "png") == [tmp_path / "card-front.png",
                                                                 tmp_path / "card-back.png"]


# --- the postcard text ---------------------------------------------------------------------------

@pytest.mark.parametrize("changes, message", [
    ({"message": ""}, "empty message"),
    ({"message": "x" * 501}, "message is too long"),
    ({"greeting": "x" * 61}, "greeting is too long"),
    ({"address": "a\nb\nc\nd\ne"}, "the address has 5 lines, max 4"),
    ({"signature": "Love 🎉"}, "cannot print"),
])
def test_overrides_are_checked_as_usage_errors(changes, message):
    with pytest.raises(PrinterError) as exc:
        postcard.with_overrides(postcard.DEFAULT_POSTCARD, **changes)
    assert exc.value.exit_code == 2 and message in str(exc.value)


def test_text_is_cleaned_and_blank_lines_dropped():
    card = postcard.with_overrides(Postcard(message="  Hello \n\n\n  there  "), address="Jo\n\n Smith ",
                                   greeting=None)
    assert card.message == "Hello\nthere" and card.address == "Jo\nSmith"


@pytest.mark.parametrize("content, message", [
    ("{", "not valid JSON"),
    ("[]", "must hold a JSON object"),
    ('{"greeting": "Hi"}', "missing message"),
    ('{"message": "Hi", "to": "Jo"}', "unknown field(s) in postcard file: to"),
    ('{"message": "Hi", "address": ["Jo"]}', "fields must be text: address"),
])
def test_bad_postcard_file_is_refused_with_exit_code_3(tmp_path, content, message):
    path = tmp_path / "postcard.json"
    path.write_text(content)
    with pytest.raises(PrinterError) as exc:
        postcard.load_postcard(path)
    assert exc.value.exit_code == 3 and message in str(exc.value)


def test_missing_and_oversized_postcard_files(tmp_path):
    with pytest.raises(PrinterError, match="postcard file not found"):
        postcard.load_postcard(tmp_path / "nope.json")
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"message": "x" * (postcard.MAX_POSTCARD_FILE + 1)}))
    with pytest.raises(PrinterError, match="larger than 64 KiB"):
        postcard.load_postcard(big)


# --- prt styles and prt schema postcard --------------------------------------------------------------

def test_postcard_style_is_two_sides_on_4x6():
    style = next(s for s in catalog.styles() if s["name"] == "postcard")
    assert style["command"] == "postcard" and style["args"] == [] and style["schema"] == "postcard"
    assert style["photos"] == {"min": 1, "max": 1} and style["max_pages"] == 2
    assert style["paper"] == {"size": "4x6", "media": "4x6", "tray": "photo"}
    assert [f["name"] for f in style["fields"]] == list(postcard.FIELDS)
    assert {f["name"]: f["max_length"] for f in style["fields"]} == postcard.LIMITS
    assert [f["name"] for f in style["fields"] if f["required"]] == ["message"]


def _schema_valid(data) -> bool:
    return Draft202012Validator(catalog.schema("postcard")).is_valid(data)


def _accepted(data) -> bool:
    try:
        postcard.parse_postcard(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


@pytest.mark.parametrize("data", [
    {"message": "Hi"},
    {"message": "Hi", "greeting": "", "signature": "", "address": ""},
    {"message": "m" * 500, "greeting": "g" * 60, "signature": "s" * 60, "address": "a" * 160},
    {"message": "Hi", "address": "Jo\n1 Road\nTown\nLand"},
    {"message": "Hi", "address": "Jo\n1 Road\nTown\nLand\nEarth"},
    {"message": "m" * 501},
    {"message": "Hi", "address": "a" * 161},
    {"message": " "},
    {"greeting": "Hi"},
    {"message": "Hi", "stamp": "x"},
    {"message": 1},
])
def test_postcard_schema_agrees_with_the_postcard_file_check(data):
    assert _schema_valid(data) == _accepted(data)


def test_postcard_schema_examples_are_accepted():
    schema = catalog.schema("postcard")
    Draft202012Validator.check_schema(schema)
    assert schema["examples"][0] == {name: getattr(postcard.DEFAULT_POSTCARD, name) for name in postcard.FIELDS}
    for example in schema["examples"]:
        assert _schema_valid(example) and _accepted(example)


# --- how the sides are printed ---------------------------------------------------------------------

def _never():
    raise AssertionError("asked the printer when it did not need to")


def test_one_side_needs_nothing_from_the_printer():
    assert postcard.print_method("front", False, PHOTO_4X6, _never, _never, False) == ("front", None)
    assert postcard.print_method("back", False, PHOTO_4X6, _never, _never, False) == ("back", None)


def test_photo_paper_is_turned_over_by_hand_without_asking_the_printer():
    assert postcard.print_method("both", False, PHOTO_4X6, _never, _never, True) == ("flip", None)
    with pytest.raises(PrinterError) as exc:
        postcard.print_method("both", False, PHOTO_4X6, _never, _never, False)
    assert exc.value.exit_code == 2 and exc.value.kind == "manual_flip_required"
    assert "--side front" in exc.value.hint


def test_a_duplex_capable_paper_goes_two_sided_when_the_printer_can():
    assert postcard.print_method("both", False, A4, lambda: True, lambda: "q", False) == ("duplex", "q")
    assert postcard.print_method("both", False, A4, lambda: False, _never, True) == ("flip", None)
    assert postcard.print_method("both", False, A4, lambda: True, lambda: None, True) == ("flip", None)


def test_duplex_asked_for_needs_two_sided_printing_and_a_cups_queue():
    assert postcard.print_method("both", True, PHOTO_4X6, lambda: True, lambda: "q", False) == ("duplex", "q")
    for two_sided, queue, says in ((lambda: False, _never, "does not report two-sided"),
                                   (lambda: True, lambda: None, "CUPS queue")):
        with pytest.raises(PrinterError) as exc:
            postcard.print_method("both", True, PHOTO_4X6, two_sided, queue, True)
        assert exc.value.kind == "duplex_unavailable" and exc.value.exit_code == 1 and says in str(exc.value)
    with pytest.raises(PrinterError) as exc:
        postcard.print_method("front", True, PHOTO_4X6, _never, _never, True)
    assert exc.value.exit_code == 2


def test_sides_option_is_sent_over_ipp_and_cups(monkeypatch, tmp_path):
    assert PrintOptions().sides_keyword is None
    assert PrintOptions(duplex=True).sides_keyword == "two-sided-long-edge"
    assert PrintOptions(duplex=True, sides="two-sided-short-edge").sides_keyword == "two-sided-short-edge"
    fake = FakeDriver()
    fake.submit(b"jpeg", "image/jpeg", PrintOptions(sides="two-sided-short-edge"))
    sent = {a.name: a.value for a in fake.client.requests[-1][2]}
    assert sent["sides"] == "two-sided-short-edge"
    commands = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(
        returncode=0, stdout="request id is q-12 (1 file(s))", stderr=""))
    job = printing.cups_print("q", tmp_path / "a.pdf", PrintOptions(sides="two-sided-short-edge",
                                                                    media_type="photographic", tray="photo"))
    assert job == 12
    assert {"sides=two-sided-short-edge", "media-type=photographic", "media-source=photo"} <= set(commands[0])


def test_send_two_sided_hands_cups_one_upright_two_page_pdf(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("prt.printing.cups_print",
                        lambda queue, path, options: calls.append((queue, path.read_bytes(), options)) or 5)
    pages, _ = postcard.compose(Image.new("RGB", (60, 40), RED))
    ticket = guard.Ticket(2)
    assert render.send_two_sided("q", pages, "prt postcard x", ticket, PHOTO_4X6) == 5
    queue, pdf, options = calls[0]
    assert queue == "q" and guard.count_pdf_pages(pdf) == 2 and ticket.attempted == 2
    assert options.sides == "two-sided-short-edge" and options.media == "na_index-4x6_4x6in"
    assert options.tray == "photo" and options.media_type == "photographic" and options.job_name == "prt postcard x"
    assert pdf.count(b"/MediaBox [ 0 0 288.0 432.0 ]") == 2  # both sides upright 4x6 in, in points


# --- prt postcard ----------------------------------------------------------------------------------

def test_preview_saves_both_sides_and_prints_nothing(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    out_file = tmp_path / "card.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(photo), "--preview", str(out_file))
    assert code == 0, err
    assert "not printed" in err and printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    front, back = tmp_path / "card-front.png", tmp_path / "card-back.png"
    data = json.loads(out)
    assert data["estimate"]["summary"].startswith("~2 pages (4×6)")
    assert without_estimate(out) == {
        "action": "postcard", "pages": 2, "paper": {"size": "4x6", "media": "4x6", "tray": "photo"},
        "fits": True, "cut": {},
        "files": [{"file": str(front), "format": "png", "bytes": front.stat().st_size},
                  {"file": str(back), "format": "png", "bytes": back.stat().st_size}]}
    expected = _sides(photo)
    assert [Image.open(f).convert("RGB").tobytes() for f in (front, back)] == [p.tobytes() for p in expected]
    assert not out_file.exists()


def test_pdf_preview_is_one_file_front_then_back(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")  # a preview needs no --yes
    out_file = tmp_path / "card.pdf"
    code, out, err = run(capsys, "-f", "json", "postcard", str(_photo(tmp_path)), "-o", str(out_file))
    assert code == 0, err
    assert guard.count_pdf_pages(out_file.read_bytes()) == 2
    assert json.loads(out)["files"] == [{"file": str(out_file), "format": "pdf", "bytes": out_file.stat().st_size}]


def test_side_front_then_side_back_print_one_side_each(capsys, tmp_path, printer, no_terminal):
    photo = _photo(tmp_path)
    front, back = _sides(photo)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(photo), "--side", "front", "--yes")
    assert code == 0, err
    data = without_estimate(out)
    assert data["pages"] == 1 and data["method"] == "front" and data["flip"] == postcard.FLIP
    assert data["jobs"] == {"front": data["jobs"]["front"]} and "--side back" in err
    assert printer.jobs() == [PHOTO_4X6.to_jpeg(front)] and guard.used_today() == 1
    assert _media_col(printer.requests()[0]) == {"media-source": "photo", "media-type": "photographic"}
    assert [a.value for a in printer.requests()[0][1] if a.name == "job-name"] == ["prt postcard photo.jpg (front)"]
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(photo), "--side", "back", "--yes")
    assert code == 0, err
    assert json.loads(out)["method"] == "back" and list(json.loads(out)["jobs"]) == ["back"]
    assert printer.jobs()[1] == PHOTO_4X6.to_jpeg(postcard.flipped(back)) and guard.used_today() == 2


def test_both_sides_without_a_terminal_is_refused_before_anything_is_sent(capsys, tmp_path, printer, no_terminal):
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)), "--yes")
    assert code == 2 and _error(err)["kind"] == "manual_flip_required" and "--side front" in _error(err)["hint"]
    assert out == "" and printer.jobs() == [] and guard.used_today() == 0


def test_agent_mode_needs_yes_then_one_side_at_a_time(capsys, tmp_path, printer, terminal, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photo = str(_photo(tmp_path))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", photo)
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required"
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", photo, "--yes")  # a terminal, but an agent
    assert code == 2 and _error(err)["kind"] == "manual_flip_required"
    assert terminal.asked == [] and printer.jobs() == [] and guard.used_today() == 0
    assert run(capsys, "-p", "demo", "postcard", photo, "--side", "front", "--yes")[0] == 0
    assert len(printer.jobs()) == 1 and terminal.asked == []


def test_at_a_terminal_the_front_prints_then_the_back_once_the_card_is_turned(capsys, tmp_path, printer, terminal):
    photo = _photo(tmp_path)
    front, back = _sides(photo)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(photo))
    assert code == 0, err
    assert terminal.asked[0] == ("Print photo.jpg as a postcard, front then back on demo? (load 4×6 paper in the "
                                 "photo tray)")
    assert postcard.FLIP in terminal.asked[1] and terminal.asked[1].endswith("Print the back now?")
    assert printer.jobs() == [PHOTO_4X6.to_jpeg(front), PHOTO_4X6.to_jpeg(postcard.flipped(back))]
    data = without_estimate(out)
    assert data["method"] == "flip" and list(data["jobs"]) == ["front", "back"] and guard.used_today() == 2


def test_declining_the_back_keeps_the_front_and_gives_the_page_back(capsys, tmp_path, printer, terminal):
    terminal.answers = [True, False]
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)))
    assert code == 0, err
    assert "the back was not printed" in err and json.loads(out)["jobs"]["back"] is None
    assert len(printer.jobs()) == 1 and guard.used_today() == 1


def test_duplex_prints_both_sides_in_one_two_sided_job(capsys, tmp_path, printer, no_terminal, monkeypatch):
    monkeypatch.setattr("prt.printing.find_cups_queue", lambda uuid, host: "demo_queue")
    photo = _photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(photo), "--duplex", "--yes")
    assert code == 0, err
    data = without_estimate(out)
    assert data["method"] == "duplex" and data["jobs"] == {"front": 77, "back": 77} and "flip" not in data
    assert printer.jobs() == [] and len(printer.cups) == 1 and guard.used_today() == 2
    job = printer.cups[0]
    assert job.queue == "demo_queue" and guard.count_pdf_pages(job.pdf) == 2
    assert job.options.sides == "two-sided-short-edge" and job.options.tray == "photo"


def test_duplex_is_refused_without_a_cups_queue_or_two_sided_printing(capsys, tmp_path, printer, no_terminal):
    photo = str(_photo(tmp_path))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", photo, "--duplex", "--yes")
    assert code == 1 and _error(err)["kind"] == "duplex_unavailable" and "CUPS" in _error(err)["message"]
    printer.driver.fixture["printer"]["sides-supported"] = ["one-sided"]
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", photo, "--duplex", "--yes")
    assert code == 1 and _error(err)["kind"] == "duplex_unavailable" and "two-sided" in _error(err)["message"]
    assert printer.jobs() == [] and printer.cups == [] and guard.used_today() == 0


def test_duplex_with_one_side_is_a_usage_error_before_anything_else(capsys, tmp_path, printer):
    out_file = tmp_path / "card.png"
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)), "--duplex", "--side",
                       "back", "-o", str(out_file))
    assert code == 2 and "--duplex" in _error(err)["message"]
    assert printer.made == [] and not (tmp_path / "card-front.png").exists()


def test_a_page_cap_counts_both_sides(capsys, tmp_path, printer, terminal):
    from prt.config import Config
    cfg = Config.load()
    cfg.max_pages_per_run = 1
    cfg.save()
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)))
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and terminal.asked == []
    assert printer.jobs() == []
    assert run(capsys, "-p", "demo", "postcard", str(_photo(tmp_path)), "--side", "front")[0] == 0


def test_postcard_file_and_flags_override_it(capsys, tmp_path, printer):
    from prt import booth
    photo = _photo(tmp_path)
    path = _postcard_file(tmp_path, {"message": "From the file", "greeting": "Hello", "address": "Old\nAddress"})
    out_file = tmp_path / "card.pdf"
    code, out, err = run(capsys, "-f", "json", "postcard", str(photo), "--postcard-file", str(path), "--greeting",
                         "", "--address", "Jo Smith", "--address", "12 High Street", "--signature", "Love, Sam",
                         "-o", str(out_file))
    assert code == 0, err
    expected = Postcard(greeting="", message="From the file", signature="Love, Sam",
                        address="Jo Smith\n12 High Street")
    pages, _ = postcard.compose(booth.load_photo(photo), expected)
    from prt.article import page_files
    assert out_file.read_bytes() == page_files(pages, out_file, "pdf", PHOTO_4X6)[0][1]


def test_postcard_file_dash_reads_stdin_and_bad_input_sends_nothing(capsys, tmp_path, printer, stdin, no_terminal):
    stdin(json.dumps({"message": "Hello from stdin"}).encode())
    code, _, err = run(capsys, "postcard", str(_photo(tmp_path)), "--postcard-file", "-", "-o",
                       str(tmp_path / "c.jpg"))
    assert code == 0, err and (tmp_path / "c-front.jpg").is_file() and (tmp_path / "c-back.jpg").is_file()
    stdin(b'{"greeting": "Hi"}')
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)), "--postcard-file",
                         "-", "--side", "front", "--yes")
    assert code == 3 and _error(err)["kind"] == "invalid_input" and "missing message" in _error(err)["message"]
    assert out == "" and printer.made == [] and guard.used_today() == 0


def test_cut_text_is_warned_and_reported(capsys, tmp_path, printer):
    code, out, err = run(capsys, "-f", "json", "postcard", str(_photo(tmp_path)), "--signature", "S" * 60, "-o",
                         str(tmp_path / "c.png"))
    assert code == 0 and "signature cut to fit the postcard" in err
    data = json.loads(out)
    assert data["fits"] is False and set(data["cut"]) == {"signature"} and data["cut"]["signature"]["given"] == 60
    assert 0 < data["cut"]["signature"]["max_fit"] < 60


def test_outputs_over_the_photo_or_postcard_file_and_bad_input_are_refused(capsys, tmp_path, printer):
    photo = _photo(tmp_path, "card-front.png")
    code, _, err = run(capsys, "-f", "json", "postcard", str(photo), "-o", str(tmp_path / "card.png"))
    assert code == 2 and "overwrite" in _error(err)["message"] and not (tmp_path / "card-back.png").exists()
    path = _postcard_file(tmp_path, {"message": "Hi"}, name="text.pdf")
    code, _, err = run(capsys, "-f", "json", "postcard", str(photo), "--postcard-file", str(path), "--save",
                       str(path))
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "postcard", str(photo), "--message", "🎉", "-o", str(tmp_path / "x.png"))
    assert code == 2 and "cannot print" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "postcard", str(photo), "-o", str(tmp_path / "x.gif"))
    assert code == 2 and "unknown file type" in _error(err)["message"]
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    code, _, err = run(capsys, "-f", "json", "postcard", str(bad), "-o", str(tmp_path / "x.png"))
    assert code == 3 and printer.made == []


def test_save_keeps_both_sides_and_prints(capsys, tmp_path, printer, no_terminal):
    copy = tmp_path / "copy.jpg"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)), "--save", str(copy),
                         "--side", "front", "--yes")
    assert code == 0, err
    saved = json.loads(out)["saved"]
    assert [entry["file"] for entry in saved] == [str(tmp_path / "copy-front.jpg"), str(tmp_path / "copy-back.jpg")]
    assert Image.open(io.BytesIO((tmp_path / "copy-back.jpg").read_bytes())).size == (1800, 1200)
    assert len(printer.jobs()) == 1


def test_estimate_counts_only_the_sides_printed(capsys, tmp_path, printer, no_terminal):
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "postcard", str(_photo(tmp_path)), "--side", "back",
                       "--yes")
    assert code == 0 and json.loads(out)["estimate"]["pages"] == 1


def test_a4_is_the_only_paper_left_to_a_duplexer():
    assert A4.duplex is True and PHOTO_4X6.duplex is False and replace(PHOTO_4X6, duplex=True).duplex
