import io
import json
import subprocess

import pytest

from prt import cache, style
from prt.cli import hoist_globals, main, render_status
from prt.drivers.fake import FakeDriver
from prt.models import Status
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
                                  ["booth", "--help"]])
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
    code, out, _ = run(capsys, "printers", "list")
    assert "office\tEPSON721655.local\tepson\t" in out and "home\tdemo\tfake\tdefault" in out
    code, out, _ = run(capsys, "status")  # uses the default (demo)
    assert code == 0 and out.startswith("state\tidle")
    assert run(capsys, "printers", "remove", "nope")[0] == 2
    assert run(capsys, "printers", "remove", "home")[0] == 0
    assert run(capsys, "printers", "default", "office")[0] == 0


def test_unknown_driver_is_usage_error(capsys):
    code, _, err = run(capsys, "printers", "add", "x", "h", "--driver", "canon9000")
    assert code == 2 and "unknown driver" in err


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
    return booth.render_jpeg(booth.load_photo(photo), story or booth.DEFAULT_STORY, style)


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


def test_booth_styles(capsys, tmp_path, booth_printer):
    photo = _booth_photo(tmp_path)
    for job_id, style in enumerate(("metro", "daily"), start=101):
        code, out, _ = run(capsys, "-p", "demo", "-f", "json", "booth", str(photo), "--style", style, "--yes")
        assert code == 0 and json.loads(out) == {"action": "booth", "style": style, "job_id": job_id}
    metro, daily = booth_printer.jobs()
    assert metro == _expected_page(photo, style="metro") and daily == _expected_page(photo, style="daily")
    assert metro != daily
    code, _, err = run(capsys, "-p", "demo", "booth", str(photo), "-s", "tabloid", "--yes")
    assert code == 2 and "tabloid" in err


def test_booth_style_choices_match_renderer():
    from prt import booth
    from prt.cli import BoothStyle
    assert {s.value for s in BoothStyle} == set(booth.STYLES) == {"metro", "daily"}


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
    assert {"--style", "--story-file", "--preview", "--yes"} <= flags
    assert [f for f in sorted(flags) if f not in help_text] == []
    monkeypatch.chdir(tmp_path)
    _booth_photo(tmp_path)
    (tmp_path / "story.json").write_text(re.search(r"```json\n(.*?)```", skill, re.S).group(1))
    commands = [shlex.split(line) for line in re.findall(r"^prt booth .*$", skill, re.M)]
    assert len(commands) == 2
    for argv in commands:
        assert run(capsys, "-p", "demo", *argv[1:])[0] == 0
    assert (tmp_path / "page.jpg").read_bytes()[:2] == b"\xff\xd8" and len(booth_printer.jobs()) == 1


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
