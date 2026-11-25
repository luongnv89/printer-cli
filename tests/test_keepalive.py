"""prt keepalive: a small patch of every ink colour, printed every few days to stop nozzle clogs (#41)."""

import dataclasses
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from prt import guard, keepalive
from prt.cli import main
from prt.config import Config
from prt.drivers.fake import FakeDriver
from prt.paper import A4
from prt.transport import ipp

from conftest import without_estimate

# The issue asks for every ink colour; a solid patch of each primary ink fires its own cartridge on any printer.
CYAN, MAGENTA, YELLOW, BLACK = (0, 255, 255), (255, 0, 255), (255, 255, 0), (0, 0, 0)


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``made`` records every driver the CLI asked for."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    sent = lambda: len([op for op, *_ in fake.client.requests if op == ipp.OP_PRINT_JOB])  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, sent=sent)


def _colours(page):
    return {colour for _, colour in page.getcolors(maxcolors=1 << 16)}


def _solid(page, minimum=10_000):
    """The colours that cover at least ``minimum`` pixels: a patch square, not the edge of a letter."""
    return {colour for count, colour in page.getcolors(maxcolors=1 << 16) if count >= minimum}


# --- the page ------------------------------------------------------------------------------------

def test_the_patch_holds_every_ink_colour_on_a4():
    page = keepalive.compose()
    assert page.mode == "RGB" and page.size == A4.size
    solid = _solid(page)
    assert {CYAN, MAGENTA, YELLOW, BLACK} <= solid
    # Light tints and greys too, for printers that keep light, grey or photo inks.
    assert {(178, 255, 255), (255, 178, 255), (192, 192, 192), (128, 128, 128)} <= solid


def test_each_square_is_solid_and_the_rest_of_the_sheet_is_blank():
    page = keepalive.compose()
    names = [name for row in keepalive.PATCHES for name, _ in row]
    assert names[:4] == ["cyan", "magenta", "yellow", "black"]
    for r, row in enumerate(keepalive.PATCHES):
        for c, (name, rgb) in enumerate(row):
            x0, y0, x1, y1 = keepalive.square(r, c)
            assert _colours(page.crop((x0, y0, x1 + 1, y1 + 1))) == {rgb}, name
    # A small patch: the lower half of the sheet takes no ink at all.
    assert _colours(page.crop((0, A4.size[1] // 2, *A4.size))) == {(255, 255, 255)}


def test_the_patch_is_estimated_as_colour_and_light_on_ink():
    est = guard.estimate([keepalive.compose()])
    assert est["pages"] == 1 and est["colour"] is True
    assert all(est["ink"][ink] > 0 for ink in ("cyan", "magenta", "yellow", "black"))
    assert est["ink_percent"] < 1


# --- the command ---------------------------------------------------------------------------------

def test_yes_prints_one_page_and_books_one(capsys, printer):
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "keepalive", "--yes")
    assert code == 0, err
    assert printer.sent() == 1 and guard.used_today() == 1
    data = without_estimate(out)
    assert data["action"] == "keepalive" and data["job_id"] > 0
    assert data["inks"] == ["cyan", "magenta", "yellow", "black"]


def test_a_scheduler_with_no_terminal_prints_with_yes(capsys, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)  # cron, a systemd timer, launchd
    code, _, err = run(capsys, "-p", "demo", "keepalive", "--yes")
    assert code == 0, err
    assert printer.sent() == 1 and guard.used_today() == 1


def test_without_yes_it_asks_and_a_no_sends_nothing(capsys, printer, monkeypatch):
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or False)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "keepalive")
    assert code == 1 and out == "" and _error(err)["kind"] == "declined"
    assert len(asked) == 1 and "keep-alive colour patch" in asked[0]
    assert printer.sent() == 0 and guard.used_today() == 0


def test_without_yes_and_no_terminal_it_is_refused(capsys, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "keepalive")
    assert code == 2 and _error(err)["kind"] == "confirmation_required" and "--yes" in _error(err)["hint"]
    assert printer.sent() == 0 and guard.used_today() == 0


def test_a_busy_printer_is_refused_before_anything_is_sent(capsys, printer, monkeypatch):
    busy = dataclasses.replace(printer.driver.status(), state="processing")
    monkeypatch.setattr(printer.driver, "status", lambda: busy)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "keepalive", "--yes")
    assert code == 1 and _error(err)["kind"] == "refused" and "busy" in err
    assert printer.sent() == 0 and guard.used_today() == 0


def test_a_reached_daily_cap_refuses_it_before_sending(capsys, printer):
    cfg = Config.load()
    cfg.max_pages_per_day = 1
    cfg.save()
    assert run(capsys, "-p", "demo", "keepalive", "--yes")[0] == 0 and printer.sent() == 1
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "keepalive", "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded"
    assert printer.sent() == 1 and guard.used_today() == 1


def test_preview_in_agent_mode_needs_no_yes_and_makes_no_driver(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    out_file = tmp_path / "keepalive.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "keepalive", "--preview", str(out_file))
    assert code == 0, err
    assert json.loads(out)["file"] == str(out_file)
    with Image.open(out_file) as saved:  # PNG is lossless: the squares come back exactly
        assert {CYAN, MAGENTA, YELLOW, BLACK} <= _solid(saved.convert("RGB"))
    assert printer.made == [] and printer.sent() == 0 and guard.used_today() == 0


def test_save_writes_a_copy_and_still_prints(capsys, tmp_path, printer):
    copy = tmp_path / "keepalive.pdf"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "keepalive", "--save", str(copy), "--yes")
    assert code == 0, err
    assert printer.sent() == 1 and guard.used_today() == 1
    assert copy.read_bytes().startswith(b"%PDF") and json.loads(out)["saved"][0]["file"] == str(copy)


def test_a_bad_output_name_is_refused_before_a_driver_exists(capsys, tmp_path, printer):
    code, _, err = run(capsys, "-p", "demo", "keepalive", "--save", str(tmp_path / "keepalive.txt"), "--yes")
    assert code == 2 and "unknown file type .txt" in err
    assert printer.made == [] and printer.sent() == 0
