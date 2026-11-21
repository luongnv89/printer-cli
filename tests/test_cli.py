import json
import subprocess

import pytest

from prt import cache, style
from prt.cli import hoist_globals, main, render_status
from prt.drivers.fake import FakeDriver
from prt.models import Status

from conftest import PRT, clean_env

ESC = "\x1b"


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


# --- help / version / usage -------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["--help"], ["printers", "--help"], ["maintain", "--help"],
                                  ["jobs", "--help"], ["status", "--help"], ["scan", "--help"],
                                  ["print", "--help"], ["maintain", "clean", "--help"], ["tui", "--help"]])
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
    cache.save("ghost", FakeDriver().status())
    code, out, err = run(capsys, "--timeout", "0.5", "printers", "add", "ghost", "127.0.0.1:9", "--driver", "ipp")
    # host with :port isn't valid for IPP here; use an explicit closed port via config instead
    from prt.config import Config
    from prt.models import Target
    cfg = Config.load()
    cfg.printers["ghost"] = Target("ghost", "127.0.0.1", driver="ipp", ipp_port=9)
    cfg.save()
    code, out, err = run(capsys, "--timeout", "0.5", "-p", "ghost", "status")
    assert code == 1
    assert out.startswith("state\toffline\tstale")
    assert "last known status" in err


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
