import io
import json
import subprocess
import threading

import pytest

from prt import cache, style
from prt.cli import hoist_globals, main, render_status
from prt.config import Config, config_path
from prt.discovery import Discovered
from prt.drivers.base import Unreachable
from prt.drivers.fake import FakeDriver
from prt.models import Status, Target
from prt.transport import ipp

from conftest import PRT, clean_env

ESC = "\x1b"


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


# --- help / version / usage -------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["--help"], ["printers", "--help"], ["maintain", "--help"],
                                  ["jobs", "--help"], ["status", "--help"], ["scan", "--help"],
                                  ["print", "--help"], ["maintain", "clean", "--help"], ["tui", "--help"],
                                  ["booth", "--help"], ["article", "--help"], ["list", "--help"]])
def test_help_everywhere(capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code in (0, 2) if not argv else code == 0
    assert "Usage" in out + err


def test_version(capsys):
    assert run(capsys, "--version") == (0, "prt 0.1.0\n", "")


def test_usage_error_exit_2(capsys):
    code, out, err = run(capsys, "status", "--bogus")
    assert code == 2 and "bogus" in err


def test_hoist_globals():
    assert hoist_globals(["ink", "-f", "json", "--no-color"]) == ["-f", "json", "--no-color", "ink"]
    assert hoist_globals(["scan", "-o", "x.pdf", "--format=json"]) == ["--format=json", "scan", "-o", "x.pdf"]
    assert hoist_globals(["print", "--", "-f"]) == ["print", "--", "-f"]


# --- read commands ------------------------------------------------------------------------

def test_status_plain_records(capsys):
    code, out, err = run(capsys, "-p", "demo", "status")
    assert code == 0 and ESC not in out
    lines = out.splitlines()
    assert lines[0] == "state\tidle\tlive"
    assert "ink\tCyan ink\t1\treplace" in lines
    assert all("╭" not in ln for ln in lines)


def test_status_json(capsys):
    code, out, _ = run(capsys, "-p", "demo", "status", "-f", "json")
    data = json.loads(out)
    assert data["state"] == "idle" and data["waste"] == "almost-full"
    assert [i["level"] for i in data["inks"]] == [92, 1, 1, 12, 89]


def test_ink_and_overview(capsys):
    code, out, _ = run(capsys, "-p", "demo", "ink")
    assert code == 0 and out.splitlines()[0] == "Photo Black ink\t92\tok"
    code, out, _ = run(capsys, "-p", "demo", "overview", "-f", "json")
    data = json.loads(out)
    assert data["firmware"] == "20.50.NU30Q6" and data["scanner"]["resolutions"][-1] == 1200
    code, out, _ = run(capsys, "-p", "demo", "info")
    assert "software\tfirmware\t20.50.NU30Q6" in out


def test_jobs(capsys):
    code, out, _ = run(capsys, "-p", "demo", "jobs", "--all")
    assert code == 0 and out.splitlines()[0].startswith("41\tboarding-pass.pdf\tcompleted")


def test_printers_crud(capsys):
    assert run(capsys, "printers", "add", "office", "EPSON721655.local", "--driver", "epson")[0] == 0
    assert run(capsys, "printers", "add", "home", "demo", "--driver", "fake", "--default")[0] == 0
    code, out, _ = run(capsys, "printers", "list", "--no-check")
    assert "office\tEPSON721655.local\tepson\t" in out and "home\tdemo\tfake\tdefault" in out
    code, out, _ = run(capsys, "status")  # uses the default (demo)
    assert code == 0 and out.startswith("state\tidle")
    assert run(capsys, "printers", "remove", "nope")[0] == 2
    assert run(capsys, "printers", "remove", "home")[0] == 0
    assert run(capsys, "printers", "default", "office")[0] == 0


def test_unknown_driver_is_usage_error(capsys):
    code, _, err = run(capsys, "printers", "add", "x", "h", "--driver", "canon9000")
    assert code == 2 and "unknown driver" in err


# --- printers list: connection status ------------------------------------------------------

@pytest.fixture
def checked(monkeypatch):
    """make_driver for the list check: online.local answers, asleep.local and broken.local fail.

    Returns the (host, timeout) of every driver asked for."""
    calls = []
    failures = {"asleep.local": lambda target: Unreachable(target, "timed out"),
                "broken.local": lambda target: RuntimeError("garbled IPP reply")}

    def make_driver(target, timeout=5.0):
        calls.append((target.host, timeout))
        if target.host in failures:
            raise failures[target.host](target)
        return FakeDriver(target, timeout)

    monkeypatch.setattr("prt.cli.make_driver", make_driver)
    return calls


def _save(capsys, *hosts):
    for host in hosts:
        assert run(capsys, "printers", "add", host.split(".")[0], host)[0] == 0


def test_printers_list_shows_connection_status_by_default(capsys, checked):
    _save(capsys, "online.local", "asleep.local", "broken.local")
    code, out, _ = run(capsys, "printers", "list")
    assert code == 0 and out.splitlines() == ["online\tonline.local\tauto\tdefault\tonline",
                                              "asleep\tasleep.local\tauto\t\toffline",
                                              "broken\tbroken.local\tauto\t\toffline"]
    code, out, _ = run(capsys, "-f", "json", "printers", "list")
    assert code == 0 and json.loads(out) == [
        {"name": "online", "host": "online.local", "driver": "auto", "default": True, "reachable": "online"},
        {"name": "asleep", "host": "asleep.local", "driver": "auto", "default": False, "reachable": "offline"},
        {"name": "broken", "host": "broken.local", "driver": "auto", "default": False, "reachable": "offline"}]


def test_printers_list_no_check_contacts_no_printer(capsys, checked):
    _save(capsys, "online.local", "asleep.local")
    code, out, _ = run(capsys, "printers", "list", "--no-check")
    assert code == 0 and out.splitlines() == ["online\tonline.local\tauto\tdefault\t",
                                              "asleep\tasleep.local\tauto\t\t"]
    code, out, _ = run(capsys, "-f", "json", "printers", "list", "--no-check")
    assert [r["reachable"] for r in json.loads(out)] == [None, None]
    assert checked == []


def test_printers_list_with_no_saved_printers(capsys, checked):
    assert run(capsys, "printers", "list") == (0, "", "")
    assert run(capsys, "-f", "json", "printers", "list") == (0, "[]\n", "")
    assert checked == []


def test_printers_list_check_uses_the_timeout(capsys, monkeypatch, checked):
    _save(capsys, "online.local", "asleep.local")
    run(capsys, "printers", "list")
    run(capsys, "--timeout", "1", "printers", "list")
    monkeypatch.setenv("PRT_TIMEOUT", "2")
    run(capsys, "printers", "list")
    assert [t for _, t in checked] == [3, 3, 1.0, 1.0, 2.0, 2.0]


def test_printers_list_checks_in_parallel_and_keeps_the_order(capsys, monkeypatch):
    """The first printer only answers once the second one has been asked: one at a time, it times out.
    The second one is offline, so swapped rows would show."""
    second_asked = threading.Event()

    def make_driver(target, timeout=5.0):
        if target.host == "first.local":
            if not second_asked.wait(timeout=2):
                raise Unreachable(target, "checked one at a time")
            return FakeDriver(target, timeout)
        second_asked.set()
        raise Unreachable(target, "timed out")

    monkeypatch.setattr("prt.cli.make_driver", make_driver)
    _save(capsys, "first.local", "second.local")
    code, out, _ = run(capsys, "printers", "list")
    assert code == 0 and out.splitlines() == ["first\tfirst.local\tauto\tdefault\tonline",
                                              "second\tsecond.local\tauto\t\toffline"]


def test_list_is_printers_list(capsys, checked):
    _save(capsys, "online.local", "asleep.local")
    for args in ([], ["-f", "json"], ["--no-check"]):
        assert run(capsys, "list", *args) == run(capsys, "printers", "list", *args)
    code, out, _ = run(capsys, "list")
    assert code == 0 and out.splitlines()[1] == "asleep\tasleep.local\tauto\t\toffline"


# --- discover (auto-save) ------------------------------------------------------------------

def _dev(host, addresses=(), model="EPSON XP-6000 Series", ipp=True, **kw):
    return Discovered(name=model, host=host, addresses=list(addresses), make_model=model, ipp=ipp, **kw)


XP = _dev("EPSON721655.local", ["192.168.1.70"])
CANON = _dev("Canon-TS8300.local", ["192.168.1.81"], model="Canon TS8300 series")


def _discovers(monkeypatch, *devices):
    monkeypatch.setattr("prt.discovery.discover", lambda timeout=3.0: list(devices))


def test_discover_saves_new_printers(capsys, monkeypatch):
    _discovers(monkeypatch, XP, CANON)
    code, out, err = run(capsys, "discover")
    assert code == 0
    assert out.splitlines() == ["EPSON XP-6000 Series\tEPSON721655.local\t192.168.1.70\tipp\txp-6000\tadded",
                                "Canon TS8300 series\tCanon-TS8300.local\t192.168.1.81\tipp\tts8300\tadded"]
    cfg = Config.load()
    assert cfg.printers["xp-6000"] == Target(name="xp-6000", host="EPSON721655.local")
    assert cfg.printers["ts8300"].host == "Canon-TS8300.local" and cfg.default is None
    assert "added 2 printers: xp-6000, ts8300" in err and "prt printers default xp-6000" in err


def test_discover_json_names_added_printers(capsys, monkeypatch):
    run(capsys, "printers", "add", "office", "Canon-TS8300.local")
    _discovers(monkeypatch, XP, CANON)
    code, out, _ = run(capsys, "-f", "json", "discover")
    data = json.loads(out)
    assert [(d["host"], d["saved_as"], d["added"]) for d in data] == [
        ("EPSON721655.local", "xp-6000", True), ("Canon-TS8300.local", "office", False)]
    assert data[0]["services"] == ["ipp"] and data[0]["addresses"] == ["192.168.1.70"]


def test_discover_twice_adds_nothing_the_second_time(capsys, monkeypatch):
    _discovers(monkeypatch, XP)
    assert run(capsys, "discover")[0] == 0
    before = config_path().read_bytes()
    code, out, err = run(capsys, "discover")
    assert code == 0 and out == "EPSON XP-6000 Series\tEPSON721655.local\t192.168.1.70\tipp\txp-6000\t\n"
    assert "added" not in err
    assert config_path().read_bytes() == before and list(Config.load().printers) == ["xp-6000"]


def test_discover_leaves_config_alone_when_printers_are_already_saved(capsys, monkeypatch):
    """Matched by host (any case) and by one of the advertised addresses: nothing to add, no rewrite."""
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text('# my printers\ndefault = "office"\ncolour = "blue"\n\n'
                    '[printers.office]\nhost = "192.168.1.70"\ndriver = "epson"\n\n'
                    '[printers.study]\nhost = "canon-ts8300.local"\n')
    before = path.read_bytes()
    _discovers(monkeypatch, XP, CANON)
    code, out, err = run(capsys, "discover")
    assert code == 0 and [ln.split("\t")[4:] for ln in out.splitlines()] == [["office", ""], ["study", ""]]
    assert path.read_bytes() == before and "added" not in err


def test_discover_keeps_default_and_existing_printers(capsys, monkeypatch):
    run(capsys, "printers", "add", "office", "192.168.1.50", "--driver", "epson", "--default")
    _discovers(monkeypatch, XP)
    code, out, err = run(capsys, "discover")
    assert code == 0 and out.endswith("\txp-6000\tadded\n")
    cfg = Config.load()
    assert cfg.default == "office" and list(cfg.printers) == ["office", "xp-6000"]
    assert cfg.printers["office"] == Target(name="office", host="192.168.1.50", driver="epson")
    assert "printers default" not in err


def test_discover_suffixes_a_taken_name(capsys, monkeypatch):
    run(capsys, "printers", "add", "xp-6000", "other.local")
    twin = _dev("EPSON999999.local", ["192.168.1.71"])
    again = _dev("epson721655-2.local", ["192.168.1.70"])  # same device answering under a second name
    _discovers(monkeypatch, XP, twin, again)
    code, out, _ = run(capsys, "discover")
    assert [ln.split("\t")[4:] for ln in out.splitlines()] == [["xp-6000-2", "added"], ["xp-6000-3", "added"],
                                                              ["xp-6000-2", ""]]
    cfg = Config.load()
    assert cfg.printers["xp-6000"].host == "other.local"
    assert {n: p.host for n, p in cfg.printers.items() if n != "xp-6000"} == {
        "xp-6000-2": "EPSON721655.local", "xp-6000-3": "EPSON999999.local"}


def test_discover_does_not_save_scanner_only_devices(capsys, monkeypatch):
    _discovers(monkeypatch, _dev("scanner.local", ["192.168.1.90"], model="ScanSnap iX1600", ipp=False,
                                 scanner=True, scan_port=80))
    code, out, err = run(capsys, "discover")
    assert code == 0 and out == "ScanSnap iX1600\tscanner.local\t192.168.1.90\tescl\t\t\n"
    assert not config_path().exists()


def test_discover_with_nothing_found_writes_no_config(capsys, monkeypatch):
    _discovers(monkeypatch)
    code, out, err = run(capsys, "discover")
    assert code == 0 and out == "" and "no printers answered" in err
    assert not config_path().exists()


def test_discover_names_from_untrusted_text_keep_the_config_loadable(capsys, monkeypatch):
    _discovers(monkeypatch, _dev("a.local", model='Café "Imprimante" <b>'), _dev("b.local", model="日本"),
               _dev("c.local", model="Demo"), _dev("d.local", model="Bad", ipp_path="/ipp\nprint"))
    code, out, _ = run(capsys, "discover")
    assert [ln.split("\t")[4:] for ln in out.splitlines()] == [
        ["caf-imprimante-b", "added"], ["printer", "added"], ["demo-2", "added"], ["", ""]]
    code, out, _ = run(capsys, "printers", "list", "--no-check")
    assert code == 0 and [ln.split("\t")[0] for ln in out.splitlines()] == ["caf-imprimante-b", "printer", "demo-2"]


def test_discover_styled_box_names_added_printers(capsys, monkeypatch):
    for var, value in (("FORCE_COLOR", "1"), ("NO_COLOR", "1"), ("COLUMNS", "100")):
        monkeypatch.setenv(var, value)
    _discovers(monkeypatch, XP, CANON)
    code, out, _ = run(capsys, "discover")
    assert code == 0 and "saved as" in out
    assert "added xp-6000, ts8300" in out and "pick a default: prt printers default xp-6000" in out
    assert "2 found" in out and "2 added" in out


# --- offline / cache -----------------------------------------------------------------------

def test_offline_with_cache_shows_stale_and_exits_1(capsys):
    """Default config (driver = auto): the detection probe fails, the cache must still be used."""
    cache.save("ghost", FakeDriver().status())
    assert run(capsys, "printers", "add", "ghost", "127.0.0.1")[0] == 0
    code, out, err = run(capsys, "--timeout", "0.5", "-p", "ghost", "status")
    assert code == 1
    assert out.startswith("state\toffline\tstale")
    assert "last known status" in err
    code, out, _ = run(capsys, "--timeout", "0.5", "-p", "ghost", "ink")
    assert code == 1 and out.startswith("Photo Black ink\t92")


def test_offline_without_cache_errors(capsys):
    code, out, err = run(capsys, "--timeout", "0.5", "-p", "127.0.0.1", "--no-color", "status")
    assert code == 1 and out == ""
    assert "error:" in err and "hint:" in err


# --- maintenance ---------------------------------------------------------------------------

def test_clean_refused_on_low_ink(capsys):
    code, out, err = run(capsys, "-p", "demo", "maintain", "clean", "--yes")
    assert code == 1 and "refused" in err and "Cyan" in err


def test_clean_needs_yes_without_tty(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "maintain", "nozzle-check")
    assert code == 2 and "--yes" in err


def test_nozzle_check_and_forced_clean(capsys):
    assert run(capsys, "-p", "demo", "maintain", "nozzle-check", "--yes")[0] == 0
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "maintain", "clean", "--yes", "--force")
    assert code == 0 and json.loads(out)["forced"] is True


def test_identify_and_test_page(capsys):
    assert run(capsys, "-p", "demo", "maintain", "identify")[0] == 0
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "maintain", "test-page", "--yes")
    assert code == 0 and json.loads(out)["job_id"] > 0


def test_unsupported_capability(capsys, monkeypatch):
    from prt.drivers.ipp import IPPDriver
    monkeypatch.setattr(IPPDriver, "attributes", lambda self, refresh=True: FakeDriver().fixture["printer"])
    code, _, err = run(capsys, "-p", "pantum.local", "printers", "--help")
    from prt.config import Config
    from prt.models import Target
    cfg = Config.load()
    cfg.printers["pantum"] = Target("pantum", "pantum.local", driver="ipp")
    cfg.save()
    code, _, err = run(capsys, "-p", "pantum", "maintain", "clean", "--yes")
    assert code == 1 and "does not support head clean" in err


# --- print / scan --------------------------------------------------------------------------

def test_print_jpeg_png_and_missing(capsys, tmp_path):
    from PIL import Image
    jpg, png = tmp_path / "a.jpg", tmp_path / "b.png"
    Image.new("RGB", (10, 10), "red").save(jpg)
    Image.new("RGB", (10, 10), "blue").save(png)
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "print", str(jpg), str(png), "-n", "2")
    assert code == 0 and [r["job_id"] for r in json.loads(out)] == [101, 102]
    assert run(capsys, "-p", "demo", "print", str(tmp_path / "nope.pdf"))[0] == 3


def test_print_pdf_direct_refused(capsys, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    code, _, err = run(capsys, "-p", "demo", "print", str(pdf), "--direct")
    assert code == 3 and "JPEG" in err


def test_scan_format_option_matches_design(capsys, tmp_path):
    out_file = tmp_path / "s.jpg"
    code, _, _ = run(capsys, "-p", "demo", "scan", "--format", "jpeg", "-o", str(out_file))
    assert code == 0 and out_file.read_bytes()[:2] == b"\xff\xd8"
    assert hoist_globals(["scan", "--format", "jpeg"]) == ["scan", "--format", "jpeg"]


# --- photo booth -----------------------------------------------------------------------------


@pytest.fixture
def booth_printer(monkeypatch):
    """One FakeDriver handed to the CLI; `made` counts how often a driver was created at all."""
    from types import SimpleNamespace
    from prt import booth
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    monkeypatch.setattr(booth, "_today", lambda: "Sunday, November 22, 2026")
    jobs = lambda: [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=jobs)


def _booth_photo(tmp_path, name="photo.jpg"):
    from PIL import Image
    path = tmp_path / name
    Image.new("RGB", (300, 400), (0, 0, 255)).save(path)
    return path


def _expected_page(photo, story=None, style="metro"):
    from prt import booth
    return booth.to_jpeg(booth.compose(booth.load_photo(photo), story or booth.default_story(style), style)[0])


def test_booth_bare_invocation_prints_default_story(capsys, tmp_path, monkeypatch, booth_printer):
    from PIL import Image
    photo = _booth_photo(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, out, err = run(capsys, "-p", "demo", "booth", str(photo))
    assert code == 0 and "queued as job 101" in err
    assert asked == ["Print photo.jpg as a metro newspaper page on demo?"]
    [page] = booth_printer.jobs()
    assert page == _expected_page(photo)
    assert Image.open(io.BytesIO(page)).size == (1240, 1754)


def test_booth_needs_yes_without_tty(capsys, tmp_path, monkeypatch, booth_printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "booth", str(_booth_photo(tmp_path)))
    assert code == 2 and "--yes" in err and booth_printer.jobs() == []


BOOTH_STYLES = ("metro", "daily", "wizard", "wanted")


def test_booth_styles(capsys, tmp_path, booth_printer):
    photo = _booth_photo(tmp_path)
    for job_id, style in enumerate(BOOTH_STYLES, start=101):
        code, out, _ = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "--style", style, "--yes")
        assert code == 0 and json.loads(out) == {"action": "booth", "style": style, "job_id": job_id}
    pages = booth_printer.jobs()
    assert pages == [_expected_page(photo, style=style) for style in BOOTH_STYLES]
    assert len(set(pages)) == len(BOOTH_STYLES)  # every style prints a different page
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "-s", "tabloid", "--yes")
    assert code == 2 and "tabloid" in err


def test_booth_style_choices_match_renderer():
    from prt import booth
    from prt.cli import BoothStyle
    assert {s.value for s in BoothStyle} == set(booth.STYLES) == set(BOOTH_STYLES)


@pytest.mark.parametrize("style, kind", [("wizard", "wizard card"), ("wanted", "wanted poster")])
def test_booth_themed_style_prints_its_own_default(capsys, tmp_path, monkeypatch, booth_printer, style, kind):
    from prt import booth
    photo = _booth_photo(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "-s", style)
    assert code == 0 and "queued as job 101" in err and "cut to fit" not in err
    assert asked == [f"Print photo.jpg as a {kind} on demo?"]
    assert booth_printer.jobs() == [_expected_page(photo, booth.default_story(style), style)]
    assert booth.default_story(style) != booth.DEFAULT_STORY


def test_booth_themed_style_flags_override_its_default(capsys, tmp_path, booth_printer):
    from dataclasses import replace
    from prt import booth
    photo = _booth_photo(tmp_path)
    code, _, _ = run(capsys, "-p", "demo", "booth", str(photo), "-s", "wanted", "--headline", "Captain Jo",
                     "--caption", "Reward 5 gold", "--yes")
    assert code == 0
    expected = replace(booth.default_story("wanted"), headline="Captain Jo", caption="Reward 5 gold")
    assert booth_printer.jobs() == [_expected_page(photo, expected, "wanted")]


def test_booth_themed_style_story_file_replaces_the_default(capsys, tmp_path, booth_printer):
    from prt.booth import Story
    photo, story = _booth_photo(tmp_path), tmp_path / "card.json"
    story.write_text(json.dumps({"headline": "Mira the Bright", "story": "Reads by candlelight.", "kicker": "Rare"}))
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "-s", "wizard", "--story-file", str(story),
                       "--byline", "Card 7", "--yes")
    assert code == 0 and "cut to fit" not in err
    expected = Story(headline="Mira the Bright", story="Reads by candlelight.", kicker="Rare", byline="Card 7")
    assert booth_printer.jobs() == [_expected_page(photo, expected, "wizard")]


@pytest.mark.parametrize("style", ["wizard", "wanted"])
def test_booth_themed_style_preview_never_creates_a_driver(capsys, tmp_path, booth_printer, style):
    from PIL import Image
    photo, out_file = _booth_photo(tmp_path), tmp_path / f"{style}.jpg"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "-s", style, "-o", str(out_file))
    assert code == 0 and "not printed" in err and booth_printer.made == []
    assert json.loads(out) == {"action": "booth", "style": style, "file": str(out_file),
                               "bytes": out_file.stat().st_size}
    assert out_file.read_bytes() == _expected_page(photo, style=style)
    assert Image.open(out_file).size == (1240, 1754)


@pytest.mark.parametrize("style", ["wizard", "wanted"])
def test_booth_themed_style_bad_input_sends_nothing(capsys, tmp_path, booth_printer, style):
    bad = tmp_path / "photo.jpg"
    bad.write_bytes(b"not a picture")
    code, _, err = run(capsys, "-p", "demo", "booth", str(bad), "-s", style, "--yes")
    assert code == 3 and "cannot read photo" in err
    code, _, err = run(capsys, "-p", "demo", "booth", str(_booth_photo(tmp_path, "ok.jpg")), "-s", style,
                       "--headline", "日本", "--yes")
    assert code == 2 and "cannot print" in err
    assert booth_printer.made == [] and booth_printer.jobs() == []


def test_booth_themed_style_warns_when_text_is_cut(capsys, tmp_path, booth_printer):
    code, _, err = run(capsys, "--no-color", "booth", str(_booth_photo(tmp_path)), "-s", "wanted",
                       "-o", str(tmp_path / "page.jpg"), "--headline", "Wide headline words " * 6)
    assert code == 0 and "headline cut to fit the wanted page" in err


def test_booth_story_file_with_flag_override(capsys, tmp_path, booth_printer):
    from prt.booth import Story
    photo = _booth_photo(tmp_path)
    story = tmp_path / "story.json"
    story.write_text(json.dumps({"headline": "From the file", "story": "File body.\nSecond.", "kicker": "Sport"}))
    code, _, _ = run(capsys, "-p", "demo", "booth", str(photo), "--story-file", str(story),
                     "--headline", "Flag wins", "--byline", "By Zoë", "--yes")
    assert code == 0
    expected = Story(headline="Flag wins", story="File body.\nSecond.", kicker="Sport", byline="By Zoë")
    assert booth_printer.jobs() == [_expected_page(photo, expected)]


def test_booth_multiline_flags_keep_words_apart(capsys, tmp_path, booth_printer):
    from dataclasses import replace
    from prt import booth
    photo = _booth_photo(tmp_path)
    code, _, _ = run(capsys, "-p", "demo", "booth", str(photo), "--headline", "Hello\nWorld",
                     "--caption", "Seen at\r\nthe booth", "--yes")
    assert code == 0
    expected = replace(booth.DEFAULT_STORY, headline="Hello World", caption="Seen at the booth")
    assert booth_printer.jobs() == [_expected_page(photo, expected)]


def test_booth_preview_writes_jpeg_and_never_creates_a_driver(capsys, tmp_path, booth_printer):
    from PIL import Image
    photo, out_file = _booth_photo(tmp_path), tmp_path / "page.jpg"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "-s", "daily",
                         "--preview", str(out_file))
    assert booth_printer.made == []
    assert code == 0 and "not printed" in err
    assert json.loads(out) == {"action": "booth", "style": "daily", "file": str(out_file),
                               "bytes": out_file.stat().st_size}
    assert out_file.read_bytes() == _expected_page(photo, style="daily")
    assert Image.open(out_file).size == (1240, 1754)


def test_booth_preview_to_missing_folder(capsys, tmp_path, booth_printer):
    code, _, err = run(capsys, "booth", str(_booth_photo(tmp_path)), "-o", str(tmp_path / "no" / "page.jpg"))
    assert code == 1 and "cannot write" in err and booth_printer.made == []


def _page_of(path):
    from prt import booth
    return booth.compose(booth.load_photo(path), None, "metro")[0]


def _assert_same_page(file, page):
    from PIL import Image, ImageChops
    with Image.open(file) as image:
        assert image.size == (1240, 1754) and ImageChops.difference(image.convert("RGB"), page).getbbox() is None


def _print_jobs(fake):
    """(document-format, data) of every Print-Job the CLI sent."""
    return [(next(a.value for a in attrs if a.name == "document-format"), data)
            for op, attrs, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]


@pytest.mark.parametrize("name, magic", [("page.pdf", b"%PDF-"), ("PAGE.PNG", b"\x89PNG\r\n\x1a\n"),
                                         ("page.Jpeg", b"\xff\xd8\xff")])
def test_booth_preview_picks_the_file_type_from_the_extension(capsys, tmp_path, booth_printer, name, magic):
    photo, out_file = _booth_photo(tmp_path), tmp_path / name
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "--preview", str(out_file))
    assert code == 0 and "not printed" in err and booth_printer.made == []
    assert json.loads(out) == {"action": "booth", "style": "metro", "file": str(out_file),
                               "bytes": out_file.stat().st_size}
    assert out_file.read_bytes().startswith(magic)


def test_booth_save_prints_and_keeps_digital_copies(capsys, tmp_path, booth_printer):
    photo, pdf, png = _booth_photo(tmp_path), tmp_path / "page.pdf", tmp_path / "page.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "--save", str(pdf),
                         "--save", str(png), "--yes")
    assert code == 0 and "queued as job 101" in err and f"saved {pdf}" in err and f"saved {png}" in err
    assert json.loads(out) == {"action": "booth", "style": "metro", "job_id": 101, "saved": [
        {"file": str(pdf), "format": "pdf", "bytes": pdf.stat().st_size},
        {"file": str(png), "format": "png", "bytes": png.stat().st_size}]}
    assert _print_jobs(booth_printer.driver) == [("image/jpeg", _expected_page(photo))]
    assert pdf.read_bytes().startswith(b"%PDF-")
    _assert_same_page(png, _page_of(photo))


def test_booth_preview_with_save_writes_every_file_and_prints_nothing(capsys, tmp_path, booth_printer):
    photo, jpg, pdf = _booth_photo(tmp_path), tmp_path / "page.jpg", tmp_path / "copy.PDF"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "-o", str(jpg),
                         "--save", str(pdf))
    assert code == 0 and "not printed" in err
    assert booth_printer.made == [] and booth_printer.jobs() == []
    assert json.loads(out) == {"action": "booth", "style": "metro", "file": str(jpg),
                               "bytes": jpg.stat().st_size,
                               "saved": [{"file": str(pdf), "format": "pdf", "bytes": pdf.stat().st_size}]}
    assert jpg.read_bytes() == _expected_page(photo) and pdf.read_bytes().startswith(b"%PDF-")


def test_booth_save_keeps_the_copy_when_printing_is_declined(capsys, tmp_path, monkeypatch, booth_printer):
    photo, png = _booth_photo(tmp_path), tmp_path / "page.png"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda q, **kw: False)
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "--save", str(png))
    assert code == 1 and "cancelled" in err and booth_printer.jobs() == []
    _assert_same_page(png, _page_of(photo))


def test_booth_save_works_without_a_reachable_printer(capsys, tmp_path, monkeypatch, booth_printer):
    from prt.drivers.base import Unreachable

    def unreachable(target, timeout=5.0):
        raise Unreachable(target, "timed out")

    monkeypatch.setattr("prt.cli.make_driver", unreachable)
    photo, png = _booth_photo(tmp_path), tmp_path / "page.png"
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "--save", str(png), "--yes")
    assert code == 1 and "cannot reach printer" in err and f"saved {png}" in err
    _assert_same_page(png, _page_of(photo))


@pytest.mark.parametrize("args, bad", [
    (["-o", "page.gif"], "page.gif"),
    (["--preview", "page"], "page"),
    (["--save", "page.pdf", "--save", "page.txt", "--yes"], "page.txt"),
    (["-o", "page.png", "--save", "page.bmp"], "page.bmp"),
])
def test_booth_unsupported_file_type_writes_and_sends_nothing(capsys, tmp_path, monkeypatch, booth_printer,
                                                              args, bad):
    monkeypatch.chdir(tmp_path)
    _booth_photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "--no-color", "booth", "photo.jpg", *args)
    assert code == 2 and out == ""
    assert f"error: cannot save {bad}:" in err and "hint: end the file name in one of .pdf, .png, .jpg, .jpeg" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photo.jpg"]
    assert booth_printer.made == [] and booth_printer.jobs() == []


def test_booth_save_to_missing_folder_prints_nothing(capsys, tmp_path, booth_printer):
    code, _, err = run(capsys, "-p", "demo", "booth", str(_booth_photo(tmp_path)),
                       "--save", str(tmp_path / "no" / "page.pdf"), "--yes")
    assert code == 1 and "cannot write" in err
    assert booth_printer.made == [] and booth_printer.jobs() == []


def test_booth_warns_when_text_is_cut_to_fit(capsys, tmp_path, booth_printer):
    photo, out_file = _booth_photo(tmp_path), tmp_path / "page.jpg"
    code, _, err = run(capsys, "-p", "demo", "--no-color", "booth", str(photo), "--yes",
                       "--caption", "A caption that runs on and on " * 6)
    assert code == 0 and "queued as job" in err
    assert "caption cut to fit the metro page" in err and "--preview" in err
    code, _, err = run(capsys, "--no-color", "booth", str(photo), "-o", str(out_file),
                       "--headline", "Wide headline words " * 6, "--story", "Wide paragraph.\n" * 140)
    assert code == 0 and "headline, story cut to fit the metro page" in err
    assert len(booth_printer.made) == 1  # the preview created no driver
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "-o", str(out_file))
    assert code == 0 and "cut to fit" not in err


@pytest.mark.parametrize("content", [b"not a picture", b"", b"\xff\xd8\xff\xe0 truncated jpeg"])
def test_booth_unreadable_photo_sends_nothing(capsys, tmp_path, booth_printer, content):
    bad = tmp_path / "photo.jpg"
    bad.write_bytes(content)
    code, out, err = run(capsys, "-p", "demo", "--no-color", "booth", str(bad), "--yes")
    assert code == 3 and out == ""
    assert "error: cannot read photo photo.jpg" in err and "hint:" in err
    assert booth_printer.made == [] and booth_printer.jobs() == []


def test_booth_missing_photo_sends_nothing(capsys, tmp_path, booth_printer):
    code, _, err = run(capsys, "-p", "demo", "booth", str(tmp_path / "nope.jpg"), "--yes")
    assert code == 3 and "photo not found" in err and booth_printer.made == []


def test_booth_bad_story_sends_nothing(capsys, tmp_path, booth_printer):
    photo, story = _booth_photo(tmp_path), tmp_path / "story.json"
    story.write_text("{oops")
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "--story-file", str(story), "--yes")
    assert code == 3 and "not valid JSON" in err
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "--headline", "", "--yes")
    assert code == 2 and "empty headline" in err
    assert booth_printer.made == []


def test_photo_booth_skill_matches_the_cli(capsys, tmp_path, monkeypatch, booth_printer):
    """Every flag the agent skill names exists, and its example story and commands work as written."""
    import re
    import shlex
    from conftest import ROOT
    skill = (ROOT / "skills" / "photo-booth" / "SKILL.md").read_text()
    code, help_text, _ = run(capsys, "booth", "--help")
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", skill))
    assert {"--style", "--story-file", "--preview", "--save", "--yes"} <= flags
    assert [f for f in sorted(flags) if f not in help_text] == []
    monkeypatch.chdir(tmp_path)
    _booth_photo(tmp_path)
    (tmp_path / "story.json").write_text(re.search(r"```json\n(.*?)```", skill, re.S).group(1))
    commands = [shlex.split(line) for line in re.findall(r"^prt booth .*$", skill, re.M)]
    assert len(commands) == 4
    assert {"metro", "wanted"} <= {argv[argv.index("--style") + 1] for argv in commands}
    for argv in commands:
        assert run(capsys, "-p", "demo", *argv[1:])[0] == 0
    assert (tmp_path / "page.jpg").read_bytes()[:2] == b"\xff\xd8" and len(booth_printer.jobs()) == 2
    assert (tmp_path / "page.pdf").read_bytes().startswith(b"%PDF-")


def test_article_print_skill_matches_the_cli(capsys, tmp_path, monkeypatch, booth_printer):
    """Every flag the article skill names exists, and its example article and file commands work as written."""
    import re
    import shlex
    from conftest import ROOT
    skill = (ROOT / "skills" / "article-print" / "SKILL.md").read_text()
    help_text = run(capsys, "article", "--help")[1] + run(capsys, "--help")[1]  # it also names --timeout
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", skill))
    assert {"--style", "--preview", "--save", "--yes"} <= flags
    assert [f for f in sorted(flags) if f not in help_text] == []
    monkeypatch.chdir(tmp_path)
    (tmp_path / "article.json").write_text(re.search(r"```json\n(.*?)```", skill, re.S).group(1))
    commands = [shlex.split(line) for line in re.findall(r"^prt article article\.json .*$", skill, re.M)]
    assert len(commands) == 3
    assert {"magazine", "classic-magazine"} == {argv[argv.index("--style") + 1] for argv in commands}
    for argv in commands:
        assert run(capsys, "-p", "demo", *argv[1:])[0] == 0
    assert (tmp_path / "page.pdf").read_bytes().startswith(b"%PDF-") and len(booth_printer.jobs()) == 2


def test_status_header_has_page_count(capsys):
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "status")
    assert json.loads(out)["page_count"] == 1538


def test_scan_to_file(capsys, tmp_path):
    out_file = tmp_path / "s.pdf"
    code, out, err = run(capsys, "-p", "demo", "scan", "-o", str(out_file))
    assert code == 0 and out_file.read_bytes().startswith(b"%PDF")


# --- btop style verification (references/btop-style.md §7) ---------------------------------

def _sub(*args, **env):
    return subprocess.run([*PRT, *args], capture_output=True, env=clean_env(**env))


def test_v1_captured_stdout_is_plain():
    r = _sub("-p", "demo", "status")
    assert r.returncode == 0 and b"\x1b" not in r.stdout and "╭".encode() not in r.stdout


def test_v2_json_with_force_color_is_clean():
    r = _sub("-p", "demo", "status", "-f", "json", FORCE_COLOR="1")
    assert b"\x1b" not in r.stdout
    json.loads(r.stdout)


def test_v3_no_color_beats_force_color():
    r = _sub("-p", "demo", "status", FORCE_COLOR="1", NO_COLOR="1")
    assert b"\x1b" not in r.stdout and "╭─┐".encode() in r.stdout


def test_v4_force_color_styles():
    r = _sub("-p", "demo", "status", FORCE_COLOR="1")
    assert b"\x1b" in r.stdout and "╭─┐".encode() in r.stdout


def test_v5_renderer_16_colors_unicode():
    mode = style.Mode(styled=True, tier="16", unicode=True, width=80)
    out = render_status(FakeDriver().status(), "demo", "EPSON", mode)
    assert "\x1b[" in out and "╭─┐" in out


def test_v6_ascii_glyphs_are_ascii():
    mode = style.Mode(styled=True, tier="none", unicode=False, width=80)
    out = render_status(FakeDriver().status(), "demo", "EPSON XP-6000", mode)
    assert all(ord(c) < 0x80 for c in out), [c for c in out if ord(c) >= 0x80]


def test_v7_meter_no_color():
    out = style.render(style.Mode(styled=True, tier="none", unicode=True), style.meter(50, 10, style.Mode(
        styled=True, tier="none", unicode=True)))
    assert out == "■" * 5 + "·" * 5 + "  50%"


def test_v8_control_chars_escaped():
    rec = style.plain_record("ink", "Evil\x1b[31m\tname", 5)
    assert "\x1b" not in rec and rec.count("\t") == 2
    styled = style.box("t", [style.t("x\x1by\tz")], style.Mode(styled=True, tier="none", unicode=True))
    assert "\x1b" not in styled and "\\x1b" in styled


def test_stale_box_footer():
    st = FakeDriver().status()
    st.stale = True
    out = render_status(st, "demo", "EPSON", style.Mode(styled=True, tier="none", unicode=True, width=90))
    assert "cached" in out


def test_verbose_traces_protocol(capsys):
    code, out, err = run(capsys, "-v", "--timeout", "0.5", "-p", "127.0.0.1", "status")
    assert code == 1 and "IPP → Get-Printer-Attributes" in err
    from prt.transport import ipp
    ipp.TRACE = False
