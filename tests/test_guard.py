"""The print guard: agent mode, page caps, the daily ledger, page counting and the cost estimate (#21)."""

import json
import os
import zlib
from types import SimpleNamespace

import pytest
from PIL import Image

from prt import guard
from prt.cli import main
from prt.config import Config
from prt.drivers.base import PrinterError, Unreachable
from prt.drivers.fake import FakeDriver
from prt.transport import ipp


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI. ``sent()`` counts what reached the printer: Print-Jobs, raw ESC/P2
    payloads (nozzle check, cleaning) and CUPS jobs."""
    fake = FakeDriver()
    cups = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: fake)
    monkeypatch.setattr("prt.printing.find_cups_queue", lambda uuid, host: "demo_queue")
    monkeypatch.setattr("prt.printing.cups_print", lambda queue, path, options: cups.append(path) or 7)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)  # a terminal: agent mode still refuses

    def sent():
        jobs = [op for op, *_ in fake.client.requests if op == ipp.OP_PRINT_JOB]
        return len(jobs) + len(fake.raw_sent) + len(cups)
    return SimpleNamespace(driver=fake, cups=cups, sent=sent)


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")


def _photo(tmp_path, name="photo.jpg", colour=(0, 0, 255)):
    path = tmp_path / name
    Image.new("RGB", (300, 400), colour).save(path)
    return path


def _article(tmp_path, paragraphs=3):
    path = tmp_path / "article.json"
    path.write_text(json.dumps({"title": "Paper", "body": ["Words on paper. " * 40] * paragraphs}))
    return path


def _page_file(tmp_path):
    path = tmp_path / "page.json"
    path.write_text(json.dumps({"blocks": [{"type": "heading", "text": "Sale"}, {"type": "qr", "data": "x"}]}))
    return path


def _recipe_file(tmp_path):
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps({"title": "Toast", "ingredients": ["Bread"], "steps": ["Toast the bread."]}))
    return path


def _config(**keys):
    cfg = Config.load()
    for k, v in keys.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


def _commands(tmp_path):
    photo = _photo(tmp_path)
    morning_file = tmp_path / "morning.json"
    morning_file.write_text(json.dumps({"weather": {"summary": "Fair"}, "calendar": {"items": []},
                                        "todos": {"items": []}, "headlines": {"items": []}}))
    return {
        "print": ["print", str(photo)],
        "booth": ["booth", str(photo)],
        "card": ["card", str(photo)],
        "article": ["article", str(_article(tmp_path))],
        "gazette": ["gazette", str(photo)],
        "gazette-morning": ["gazette", "--edition", "morning", "--edition-file", str(morning_file)],
        "receipt": ["receipt", os.getcwd(), "--period", "day"],
        "compose": ["compose", str(_page_file(tmp_path))],
        "strip": ["strip", str(photo), str(photo), str(photo), str(photo)],
        "instant": ["instant", str(photo)],
        "colour-in": ["colour-in", str(photo)],
        "recipe": ["recipe", str(_recipe_file(tmp_path))],
        "postcard": ["postcard", str(photo), "--side", "front"],
        "disc": ["disc", str(photo)],
        "bingo": ["bingo", "--theme", "road trip", "-n", "2"],
        "puzzle": ["puzzle", "sudoku", "--seed", "1"],
        "test-page": ["maintain", "test-page"],
        "nozzle-check": ["maintain", "nozzle-check"],
        "clean": ["maintain", "clean", "--force"],
    }


COMMANDS = ["print", "booth", "card", "article", "gazette", "gazette-morning", "receipt", "compose", "strip", "instant", "colour-in", "recipe",
            "postcard", "disc", "bingo", "puzzle", "test-page", "nozzle-check", "clean"]


# --- agent mode ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_agent_mode_refuses_every_printing_command_without_yes(capsys, tmp_path, printer, agent, command):
    code, out, err = run(capsys, "-p", "demo", "-f", "json", *_commands(tmp_path)[command])
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and "--yes" in _error(err)["message"]
    assert printer.sent() == 0 and printer.driver.raw_sent == [] and out == ""


@pytest.mark.parametrize("command", COMMANDS)
def test_agent_mode_prints_with_yes(capsys, tmp_path, printer, agent, command):
    code, _, err = run(capsys, "-p", "demo", *_commands(tmp_path)[command], "--yes")
    assert code == 0, err
    assert printer.sent() >= 1


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False),
                                       ("", False), ("false", False), ("no", False)])
def test_agent_mode_switch(monkeypatch, value, on):
    monkeypatch.setenv("PRT_AGENT", value)
    assert guard.agent_mode() is on


def test_without_agent_mode_print_needs_no_yes(capsys, tmp_path, printer):
    assert run(capsys, "-p", "demo", "print", str(_photo(tmp_path)))[0] == 0
    assert printer.sent() == 1


def test_agent_mode_preview_needs_no_yes_and_books_nothing(capsys, tmp_path, printer, agent):
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "booth", str(_photo(tmp_path)),
                       "--preview", str(tmp_path / "page.png"))
    assert code == 0 and json.loads(out)["estimate"]["pages"] == 1
    assert printer.sent() == 0 and guard.used_today() == 0


# --- per-run cap -----------------------------------------------------------------------------------------


def test_run_cap_refuses_a_print_over_it_before_anything_is_sent(capsys, tmp_path, printer):
    _config(max_pages_per_run=2)
    a, b = _photo(tmp_path, "a.jpg"), _photo(tmp_path, "b.png")
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(a), str(b), "-n", "2")
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and "4 pages" in _error(err)["message"]
    assert printer.sent() == 0 and guard.used_today() == 0
    assert run(capsys, "-p", "demo", "print", str(a), str(b))[0] == 0 and printer.sent() == 2


def test_run_cap_holds_for_multi_page_commands(capsys, tmp_path, printer):
    _config(max_pages_per_run=1)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "article", str(_article(tmp_path, 12)), "--yes")
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and printer.sent() == 0


def test_agent_mode_has_a_default_run_cap(capsys, tmp_path, printer, agent):
    photo = _photo(tmp_path)
    over = guard.DEFAULT_AGENT_MAX_PAGES_PER_RUN + 1
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(photo), "-n", str(over), "--yes")
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and printer.sent() == 0
    _config(max_pages_per_run=50)  # the owner's own cap replaces the default
    assert run(capsys, "-p", "demo", "print", str(photo), "-n", str(over), "--yes")[0] == 0


def test_no_cap_outside_agent_mode_by_default():
    assert guard.caps(Config.load()) == guard.Caps(None, None)


# --- daily cap and ledger --------------------------------------------------------------------------------


def test_daily_cap_counts_across_runs_and_refuses_before_sending(capsys, tmp_path, printer):
    _config(max_pages_per_day=3)
    photo = _photo(tmp_path)
    assert run(capsys, "-p", "demo", "print", str(photo))[0] == 0
    assert run(capsys, "-p", "demo", "maintain", "test-page", "--yes")[0] == 0
    assert guard.used_today() == 2 and printer.sent() == 2
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(photo), "-n", "2")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded" and "1 left today" in _error(err)["hint"]
    assert printer.sent() == 2 and guard.used_today() == 2
    assert run(capsys, "-p", "demo", "booth", str(photo), "--yes")[0] == 0 and guard.used_today() == 3
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "maintain", "nozzle-check", "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded"
    assert printer.driver.raw_sent == [] and printer.sent() == 3


def test_daily_cap_refusal_comes_before_the_prompt(capsys, tmp_path, printer, monkeypatch):
    _config(max_pages_per_day=1)
    guard._update(guard.ledger_path(), lambda used: 1)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda *a, **k: asked.append(a) or True)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(_photo(tmp_path)))
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded" and asked == []


def test_ledger_from_another_day_counts_as_zero(tmp_path):
    path = tmp_path / "pages.json"
    path.write_text(json.dumps({"date": "2000-01-01", "pages": 99}))
    assert guard.used_today(path) == 0
    for junk in ("not json", "[]", '{"date": "x"}', json.dumps({"date": guard._today(), "pages": True})):
        path.write_text(junk)
        assert guard.used_today(path) == 0
    path.write_bytes(b"\xff\xfe garbage")  # not even UTF-8: still 0, and the next booking rewrites it
    assert guard.used_today(path) == 0
    assert guard._update(path, lambda used: used + 2) == 2 and guard.used_today(path) == 2


def test_reserve_gives_back_only_pages_never_attempted():
    cfg = _config(max_pages_per_day=10)
    with pytest.raises(PrinterError):
        with guard.reserve(cfg, 4) as ticket:
            ticket.attempt(1)
            assert guard.used_today() == 4  # booked while the job runs, so a parallel run sees it
            ticket.attempt(1)
            raise Unreachable(Config.load().resolve("demo"))  # the second submit failed: it still counts
    assert guard.used_today() == 2


def test_a_failed_submit_still_counts_the_page(capsys, tmp_path, printer, monkeypatch):
    _config(max_pages_per_day=5)

    def fail(*a, **k):
        raise Unreachable(printer.driver.target, "timed out")
    monkeypatch.setattr(printer.driver, "submit", fail)
    assert run(capsys, "-p", "demo", "booth", str(_photo(tmp_path)), "--yes")[0] == 1
    assert guard.used_today() == 1


def test_a_failed_page_gives_back_the_pages_after_it(capsys, tmp_path, printer, monkeypatch):
    _config(max_pages_per_day=20)
    photos = [str(_photo(tmp_path, f"p{i}.jpg")) for i in range(10)]  # two gazette pages
    real, calls = printer.driver.submit, []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise Unreachable(printer.driver.target, "timed out")
        return real(*a, **k)
    monkeypatch.setattr(printer.driver, "submit", second_fails)
    assert run(capsys, "-p", "demo", "gazette", *photos, "--yes")[0] == 1
    assert guard.used_today() == 2  # both attempted


def test_declined_prompt_books_nothing(capsys, tmp_path, printer, monkeypatch):
    _config(max_pages_per_day=5)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    assert run(capsys, "-p", "demo", "maintain", "test-page")[0] == 1
    assert guard.used_today() == 0 and printer.sent() == 0


def test_unwritable_ledger_refuses_under_a_daily_cap(capsys, tmp_path, printer, monkeypatch):
    _config(max_pages_per_day=5)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(_photo(tmp_path)))
    assert code == 1 and _error(err)["kind"] == "page_ledger_unavailable" and printer.sent() == 0


def test_unwritable_ledger_is_ignored_without_a_daily_cap(capsys, tmp_path, printer, monkeypatch):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    assert run(capsys, "-p", "demo", "print", str(_photo(tmp_path)))[0] == 0 and printer.sent() == 1


def test_uncountable_file_is_refused_while_a_cap_is_in_force(capsys, tmp_path, printer):
    text = tmp_path / "notes.txt"
    text.write_text("hello\n")
    assert run(capsys, "-p", "demo", "print", str(text))[0] == 0 and printer.cups == [text]
    _config(max_pages_per_day=5)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(text))
    assert code == 1 and _error(err)["kind"] == "page_count_unknown" and printer.cups == [text]


def test_pdf_pages_count_toward_the_cap(capsys, tmp_path, printer):
    _config(max_pages_per_run=2)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_pdf(3))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "print", str(pdf))
    assert code == 1 and _error(err)["kind"] == "page_cap_exceeded" and printer.cups == []


# --- config ----------------------------------------------------------------------------------------------


def test_caps_survive_a_config_save(capsys):
    _config(max_pages_per_run=5, max_pages_per_day=40)
    assert run(capsys, "printers", "add", "office", "office.local")[0] == 0  # rewrites the file
    back = Config.load()
    assert (back.max_pages_per_run, back.max_pages_per_day) == (5, 40) and "office" in back.printers


@pytest.mark.parametrize("value", ["0", "-3", "true", '"20"', "2.5"])
def test_bad_cap_in_config_is_refused(tmp_path, value):
    path = tmp_path / "c.toml"
    path.write_text(f"max_pages_per_day = {value}\n")
    with pytest.raises(PrinterError) as exc:
        Config.load(path)
    assert exc.value.exit_code == 3 and "max_pages_per_day" in str(exc.value)


# --- counting pages --------------------------------------------------------------------------------------


def _pdf(pages, *, extra=b"", page=b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]>>"):
    objs = [b"<</Type /Catalog /Pages 2 0 R>>",
            b"<</Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (3 + i) for i in range(pages)) + b"] /Count %d>>"
            % pages] + [page] * pages
    body = b"".join(b"%d 0 obj\n" % (n + 1) + o + b"\nendobj\n" for n, o in enumerate(objs))
    return b"%PDF-1.4\n" + body + extra + b"trailer<</Root 1 0 R>>\n%%EOF\n"


def _objstm_pdf(pages):
    """A PDF 1.5 file whose page tree is packed in a compressed object stream, as pdflatex writes it."""
    packed = [b"<</Type/Pages/Kids[" + b" ".join(b"%d 0 R" % (3 + i) for i in range(pages)) + b"]/Count %d>>"
              % pages] + [b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>"] * pages
    offsets, pos = [], 0
    for i, o in enumerate(packed):
        offsets += [b"%d" % (2 + i), b"%d" % pos]
        pos += len(o) + 1
    header = b" ".join(offsets) + b" "
    data = zlib.compress(header + b" ".join(packed))
    stream = (b"9 0 obj\n<</Type/ObjStm/N %d/First %d/Filter/FlateDecode/Length %d>>stream\n"
              % (len(packed), len(header), len(data)) + data + b"\nendstream\nendobj\n")
    return b"%PDF-1.5\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n" + stream + b"%%EOF\n"


def test_pdf_page_count_plain_and_compact():
    assert guard.count_pdf_pages(_pdf(1)) == 1
    assert guard.count_pdf_pages(_pdf(3)) == 3
    assert guard.count_pdf_pages(_pdf(2, page=b"<</Type/Page/Parent 2 0 R>>")) == 2  # no spaces
    from prt.drivers.fake import _PDF
    assert guard.count_pdf_pages(_PDF) == 1


def test_pdf_page_count_reads_compressed_object_streams():
    assert guard.count_pdf_pages(_objstm_pdf(7)) == 7


def test_pdf_page_count_never_trusts_a_low_count():
    decoy = _pdf(3).replace(b"/Count 3", b"/Count 1")
    assert guard.count_pdf_pages(decoy) == 3


def test_pdf_page_count_takes_a_higher_page_tree_count():
    assert guard.count_pdf_pages(_pdf(2).replace(b"/Count 2", b"/Count 9")) == 9


def test_pdf_page_count_decodes_escaped_names_and_finds_untyped_pages():
    escaped = _pdf(1, extra=b"7 0 obj\n<</Typ#65 /P#61ge /Parent 2 0 R>>\nendobj\n"
                            b"8 0 obj\n<</Typ#65 /P#61ge /Parent 2 0 R>>\nendobj\n")
    assert guard.count_pdf_pages(escaped) >= 3
    untyped = _pdf(1, extra=b"7 0 obj\n<</Parent 2 0 R /Contents 9 0 R>>\nendobj\n")
    assert guard.count_pdf_pages(untyped) >= 2


def test_pdf_page_count_ignores_annotations_outlines_and_pages_nodes():
    pdf = _pdf(1, extra=b"7 0 obj\n<</Type /Annot /Subtype /Popup /Parent 8 0 R /Contents (hi)>>\nendobj\n"
                        b"8 0 obj\n<</Type /Outlines /Count 50 /First 9 0 R>>\nendobj\n"
                        b"9 0 obj\n<</Title (One) /Parent 8 0 R>>\nendobj\n")
    assert guard.count_pdf_pages(pdf) == 1


def test_pdf_page_count_unknown_when_it_cannot_be_read():
    assert guard.count_pdf_pages(b"%PDF-1.4\nnothing here\n%%EOF") is None
    broken = _objstm_pdf(2).replace(b"stream\n", b"stream\nGARBAGE", 1)
    assert guard.count_pdf_pages(broken) is None


def test_file_pages(tmp_path):
    photo = _photo(tmp_path, "a.PNG")
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(_pdf(2))
    text = tmp_path / "t.txt"
    text.write_text("x")
    assert guard.count_file_pages(photo) == 1 and guard.count_file_pages(pdf) == 2
    assert guard.count_file_pages(text) is None and guard.count_file_pages(tmp_path / "x.pwg") is None
    assert guard.job_pages([photo, pdf], copies=3) == 9
    assert guard.job_pages([photo, tmp_path / "missing.jpg"]) == 1
    assert guard.job_pages([photo, text]) is None


# --- cost estimate ---------------------------------------------------------------------------------------


def _page(colour):
    return Image.new("RGB", (1240, 1754), colour)


def test_estimate_of_a_blank_page():
    assert guard.estimate([_page("white")]) == {
        "pages": 1, "colour": False, "ink": {"cyan": 0.0, "magenta": 0.0, "yellow": 0.0, "black": 0.0},
        "ink_percent": 0.0, "summary": "~1 page, black and white, est. ink <1%"}


def test_estimate_of_solid_black_and_red_pages():
    black = guard.estimate([_page("black")])
    full = 1 / guard.RATED_COVERAGE / guard.CARTRIDGE_PAGES * 100  # one fully inked page, % of a cartridge
    assert black["ink"]["black"] == round(full, 1) and black["colour"] is False and black["ink"]["cyan"] == 0.0
    red = guard.estimate([_page((255, 0, 0)), _page((255, 0, 0)), _page("white")])
    assert red["colour"] is True and red["pages"] == 3
    assert red["ink"]["magenta"] == red["ink"]["yellow"] == round(2 * full, 1) and red["ink"]["cyan"] == 0.0
    assert red["summary"] == f"~3 pages, colour, est. ink {2 * full:.0f}%"


def test_estimate_of_grey_text_is_black_and_white():
    page = _page("white")
    page.paste((90, 90, 90), (100, 100, 1100, 1600))
    est = guard.estimate([page])
    assert est["colour"] is False and est["ink"]["black"] > 0 and est["ink"]["cyan"] == 0.0


def test_preview_json_carries_the_estimate(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "-o", str(tmp_path / "b.png"))
    est = json.loads(out)["estimate"]
    assert code == 0 and est["pages"] == 1 and est["colour"] is True and est["summary"].startswith("~1 page, colour")
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "--style", "daily",
                       "-o", str(tmp_path / "d.png"))
    assert code == 0 and json.loads(out)["estimate"]["colour"] is False  # the black-and-white broadsheet
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "article", str(_article(tmp_path, 12)),
                       "-o", str(tmp_path / "a.pdf"))
    data = json.loads(out)
    assert code == 0 and data["estimate"]["pages"] == data["pages"] > 1
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "gazette", str(photo), "-o", str(tmp_path / "g.pdf"))
    assert code == 0 and json.loads(out)["estimate"]["pages"] == 1
    assert printer.sent() == 0


# --- TUI -------------------------------------------------------------------------------------------------


async def test_tui_test_page_obeys_the_daily_cap():
    from prt.models import Target
    from prt.tui.app import PrtApp
    cfg = _config(max_pages_per_day=1)
    guard._update(guard.ledger_path(), lambda used: 1)
    app = PrtApp([Target(name="demo", host="demo", driver="fake")], cfg=cfg)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app._run_action("test")
        await app.workers.wait_for_complete()
        await pilot.pause()
        jobs = [op for op, *_ in app.drivers["demo"].client.requests if op == ipp.OP_PRINT_JOB]
        assert jobs == [] and guard.used_today() == 1
