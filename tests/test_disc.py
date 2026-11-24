"""prt disc: a photo and a track list on a printable CD/DVD, printed through the disc tray (#26)."""

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import catalog, disc, guard, render
from prt.cli import main
from prt.config import Config
from prt.disc import Disc
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import DISC
from prt.transport import ipp

from conftest import ROOT, without_estimate

DARK = (20, 40, 90)
WHITE = (255, 255, 255)


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


def _photo(tmp_path, name="photo.jpg", colour=DARK, size=(600, 400)):
    path = tmp_path / name
    Image.new("RGB", size, colour).save(path, quality=95)
    return path


def _disc_file(tmp_path, data, name="disc.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _media_col(request):
    """The media-col a Print-Job asked for, as {name: value}, with the media size as {dimension: value}."""
    col = next(a.value for a in request[2] if a.name == "media-col")
    return {k: (v[1] if k != "media-size" else {d: x[1] for d, x in v[1].items()}) for k, v in col.items()}


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI (the demo XP-6000, which has a disc tray). ``jobs()`` are the JPEGs sent
    over IPP; ``no_disc_tray()`` makes it report its trays without a disc tray."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731

    def no_disc_tray():
        info = replace(fake.info(), media_sources=["auto", "main", "photo"])
        monkeypatch.setattr(fake, "info", lambda: info)
    return SimpleNamespace(driver=fake, made=made, requests=requests, no_disc_tray=no_disc_tray,
                           jobs=lambda: [data for _, _, _, data in requests()])


def _radius(x, y):
    return math.hypot(x - disc.C, y - disc.C)


def _ring_points(r):
    """Eight pixels at distance ``r`` from the centre of the disc."""
    return [(round(disc.C + r * math.cos(a)), round(disc.C + r * math.sin(a))) for a in
            [k * math.pi / 4 for k in range(8)]]


# --- the paper and the media size ---------------------------------------------------------------

def test_the_label_is_a_120mm_square_at_300dpi_from_the_disc_tray():
    page, cut = disc.compose(Image.new("RGB", (600, 400), DARK))
    assert page.size == DISC.size == (1417, 1417) and cut == {}
    assert (disc.R_OUT, disc.R_IN) == (DISC.mm(disc.OUTER_D_MM) // 2, DISC.mm(disc.INNER_D_MM) // 2) == (685, 254)


# --- the ring: no ink outside it, nor over the hub ----------------------------------------------

def test_everything_outside_the_ring_and_over_the_hub_is_white():
    page, _ = disc.compose(Image.new("RGB", (600, 400), DARK))
    for point in [(0, 0), (disc.W - 1, 0), (0, disc.H - 1), (disc.W - 1, disc.H - 1), (disc.C, disc.C)]:
        assert page.getpixel(point) == WHITE, point
    for r in (disc.R_IN - 2, disc.R_IN / 2, disc.R_OUT + 2, disc.R_OUT + 20):
        for point in _ring_points(r):
            assert page.getpixel(point) == WHITE, (r, point)
    # Every pixel outside the outer circle or inside the hole, not only a sample of them.
    assert all(page.getpixel((x, y)) == WHITE for y in range(0, disc.H, 7) for x in range(0, disc.W, 7)
               if not disc.R_IN - 1 <= _radius(x, y) <= disc.R_OUT + 1)


def test_the_photo_fills_the_ring_up_to_its_edges():
    page, _ = disc.compose(Image.new("RGB", (600, 400), DARK), Disc(title="T"))
    # Just inside the hub's edge and just inside the outer edge, clear of the text.
    for point in [(disc.C + disc.R_IN + 3, disc.C), (disc.C - disc.R_IN - 3, disc.C), (disc.C, disc.C + disc.R_IN + 3),
                  (disc.C + disc.R_OUT - 3, disc.C), (disc.C, disc.C + disc.R_OUT - 3)]:
        assert page.getpixel(point) != WHITE and sum(page.getpixel(point)) < 300, point


def test_the_estimate_counts_only_the_printed_ring():
    solid = Image.new("RGB", DISC.size, DARK)
    ring = (math.pi * ((disc.OUTER_D_MM / 2) ** 2 - (disc.INNER_D_MM / 2) ** 2)) / (DISC.width_mm * DISC.height_mm)
    full, _ = guard._coverage(solid)
    clipped, _ = guard._coverage(disc.clip(solid))
    for ink in ("cyan", "magenta", "black"):
        assert clipped[ink] / full[ink] == pytest.approx(ring, rel=0.03)
    estimate = guard.estimate([disc.clip(solid)], DISC)
    assert estimate["ink_percent"] > 0 and estimate["colour"] is True
    assert estimate["summary"].startswith("~1 page (CD/DVD), colour")
    assert guard.estimate([disc.clip(solid)], DISC)["ink"]["black"] < guard.estimate([solid], DISC)["ink"]["black"]


# --- the text -------------------------------------------------------------------------------------

def _inked(page, box):
    """Whether ``box`` holds the white handwriting (on a dark photo)."""
    return any(colour == disc.TEXT for _, colour in page.crop(box).getcolors(1 << 20))


def test_title_goes_above_the_hub_and_tracks_either_side_of_it():
    photo = Image.new("RGB", (600, 400), DARK)
    plain, _ = disc.compose(photo, Disc(title="T"))
    page, cut = disc.compose(photo, Disc(title="Road Trip Mix", tracks=("One", "Two", "Three", "Four")))
    assert cut == {}
    above = (disc.C - 300, disc.C - disc.R_OUT, disc.C + 300, disc.C - disc.R_IN)
    left = (disc.C - disc.R_OUT, disc.C - disc.R_IN, disc.C - disc.R_IN, disc.C + disc.R_IN)
    right = (disc.C + disc.R_IN, disc.C - disc.R_IN, disc.C + disc.R_OUT, disc.C + disc.R_IN)
    assert _inked(page, above) and _inked(page, left) and _inked(page, right)
    assert not _inked(plain, left) and not _inked(plain, right)


def test_text_never_reaches_the_hub_or_the_edge():
    long = Disc(title="W" * 60, subtitle="W" * 80, tracks=tuple("W" * 80 for _ in range(24)), footer="W" * 60)
    page, cut = disc.compose(Image.new("RGB", (600, 400), DARK), long)
    assert set(cut) == {"title", "subtitle", "tracks", "footer"}
    assert list(cut) == ["title", "subtitle", "tracks", "footer"]  # the order the label is read
    for y in range(0, disc.H, 3):
        for x in range(0, disc.W, 3):
            if page.getpixel((x, y)) == disc.TEXT:
                assert disc.R_IN + disc.HUB_PAD - 4 <= _radius(x, y) <= disc.R_OUT - disc.EDGE + 4, (x, y)


def test_overlong_lines_are_cut_and_reported():
    _, cut = disc.compose(Image.new("RGB", (60, 40)), Disc(title="T", tracks=("Short", "W" * 80)))
    assert set(cut) == {"tracks"}
    given = len("Short\n" + "W" * 80)
    assert cut["tracks"].given == given and len("Short\n") < cut["tracks"].max_fit < given


def test_tracks_with_no_line_left_are_left_out_and_reported():
    """More tracks than lines (more than a checked label may hold): the first that has no line is where it is cut."""
    tracks = tuple(f"Track {n}" for n in range(1, 81))
    _, cut = disc.compose(Image.new("RGB", (60, 40)), Disc(title="T", tracks=tracks))
    joined = "\n".join(tracks)
    assert set(cut) == {"tracks"} and cut["tracks"].given == len(joined)
    assert 0 < cut["tracks"].max_fit < len(joined) and joined[cut["tracks"].max_fit] == "\n"


def test_a_short_label_fits():
    _, cut = disc.compose(Image.new("RGB", (60, 40)), disc.DEFAULT_DISC)
    assert cut == {}


# --- the label file ------------------------------------------------------------------------------

def test_disc_file_takes_tracks_as_a_list_or_one_text(tmp_path):
    as_list = disc.load_disc(_disc_file(tmp_path, {"title": "Mix", "tracks": ["One", "  Two  ", ""]}))
    as_text = disc.parse_disc(json.dumps({"title": "Mix", "tracks": "One\n  Two  \n\n"}))
    assert as_list == as_text == Disc(title="Mix", tracks=("One", "Two"))


@pytest.mark.parametrize("data, message", [
    ({"subtitle": "x"}, "missing title"),
    ({"title": "   "}, "empty title"),
    ({"title": "x", "songs": []}, "unknown field"),
    ({"title": "x", "tracks": [1, 2]}, "must be text"),
    ({"title": "x", "footer": None}, "must be text"),
    ({"title": "x" * 61}, "title is too long"),
    ({"title": "x", "tracks": ["t"] * 25}, "25 tracks, more than 24"),
    ({"title": "x", "tracks": ["t" * 81]}, "track 1 is too long"),
    ({"title": "x", "tracks": ["Party 🎉"]}, "cannot print"),
    (["x"], "JSON object"),
])
def test_bad_disc_files_are_refused_with_exit_code_3(tmp_path, data, message):
    with pytest.raises(PrinterError, match=message) as err:
        disc.load_disc(_disc_file(tmp_path, data))
    assert err.value.exit_code == 3 and err.value.kind == "invalid_input"


def test_disc_file_that_is_not_json_or_too_big_is_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"title": "x" * (disc.MAX_DISC_FILE + 1)}))
    for path, message in ((bad, "not valid JSON"), (big, "larger than 64 KiB"), (tmp_path / "gone.json", "not found")):
        with pytest.raises(PrinterError, match=message) as err:
            disc.load_disc(path)
        assert err.value.exit_code == 3


def test_flags_override_the_file_and_track_replaces_the_list():
    base = Disc(title="Mix", subtitle="Sub", tracks=("A", "B"), footer="Foot")
    assert disc.with_overrides(base) == base
    assert disc.with_overrides(base, title="New", tracks=["C"]) == Disc("New", "Sub", ("C",), "Foot")
    assert disc.with_overrides(base, subtitle="", footer="") == Disc("Mix", "", ("A", "B"), "")
    with pytest.raises(PrinterError, match="cannot print") as err:
        disc.with_overrides(base, tracks=["ok", "🎶"])
    assert err.value.exit_code == 2


# --- the schema ----------------------------------------------------------------------------------

def _accepted(data) -> bool:
    try:
        disc.parse_disc(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


@pytest.mark.parametrize("data", [
    {"title": "Mix"},
    {"title": "Mix", "subtitle": "", "tracks": [], "footer": ""},
    {"title": "Mix", "tracks": ["One", "Two"]},
    {"title": "Mix", "tracks": "One\nTwo"},
    {"title": "x" * 60, "subtitle": "x" * 80, "footer": "x" * 60, "tracks": ["x" * 80] * 24},
    {"title": "Mix", "tracks": "\n".join(["x"] * 24)},
    {"title": "Mix", "tracks": "\n".join(["x"] * 25)},
    {"title": "Mix", "tracks": ["x"] * 25},
    {"title": "Mix", "tracks": ["x" * 81]},
    {"title": "Mix", "tracks": "x" * 81},
    {"title": "x" * 61},
    {"title": ""},
    {"title": "  "},
    {"subtitle": "x"},
    {"title": "Mix", "tracks": [1]},
    {"title": "Mix", "tracks": None},
    {"title": "Mix", "colour": "red"},
    {"title": "Café", "tracks": ["Ça va", "Москва", "Straße łódź"]},
    [],
])
def test_schema_agrees_with_the_disc_parser(data):
    assert Draft202012Validator(catalog.schema("disc")).is_valid(data) == _accepted(data)


def test_default_disc_and_sample_input_pass_the_schema():
    validator = Draft202012Validator(catalog.schema("disc"))
    assert validator.is_valid({**disc.DEFAULT_DISC.__dict__, "tracks": list(disc.DEFAULT_DISC.tracks)})
    sample = json.loads((ROOT / "assets" / "samples" / "disc.json").read_text())
    assert validator.is_valid(sample) and _accepted(sample)


def test_styles_list_the_disc_label_on_the_disc_tray():
    style = next(s for s in catalog.styles() if s["name"] == "disc")
    assert style["command"] == "disc" and style["schema"] == "disc" and style["max_pages"] == 1
    assert style["paper"] == {"size": "disc", "media": "disc", "tray": "disc"}
    assert style["photos"] == {"min": 1, "max": 1}
    assert [(f["name"], f["required"], f["max_length"]) for f in style["fields"]] == [
        ("title", True, 60), ("subtitle", False, 80), ("tracks", False, None), ("footer", False, 60)]


# --- prt disc ----------------------------------------------------------------------------------

@pytest.mark.parametrize("suffix, magic", [(".png", b"\x89PNG"), (".pdf", b"%PDF"), (".jpg", b"\xff\xd8")])
def test_preview_saves_the_label_and_prints_nothing(capsys, tmp_path, printer, suffix, magic):
    out_file = tmp_path / f"label{suffix}"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "disc", str(_photo(tmp_path)), "-o", str(out_file))
    assert code == 0, err
    assert "not printed" in err and out_file.read_bytes().startswith(magic)
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    data = json.loads(out)
    assert data["estimate"]["summary"].startswith("~1 page (CD/DVD)") and data["estimate"]["ink_percent"] > 0
    assert without_estimate(out) == {"action": "disc", "file": str(out_file), "bytes": out_file.stat().st_size,
                                     "title": "Road Trip Mix", "tracks": 8,
                                     "paper": {"size": "disc", "media": "disc", "tray": "disc"},
                                     "fits": True, "cut": {}}


def test_preview_png_is_the_composed_label(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    out_file = tmp_path / "label.png"
    code, _, err = run(capsys, "disc", str(photo), "--title", "Mix", "--track", "One", "--track", "Two", "-o",
                       str(out_file))
    assert code == 0, err
    from prt import booth
    expected, _ = disc.compose(booth.load_photo(photo), Disc(title="Mix", subtitle="Summer 2026",
                                                             tracks=("One", "Two"), footer="For Sam, from Jo"))
    assert Image.open(out_file).convert("RGB").tobytes() == expected.tobytes()


def test_yes_prints_one_job_through_the_disc_tray(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "disc", str(photo), "--yes")
    assert code == 0, err
    assert "disc tray" in err
    data = without_estimate(out)
    assert data["action"] == "disc" and data["job_id"] and data["paper"]["tray"] == "disc"
    assert len(printer.requests()) == 1
    assert _media_col(printer.requests()[0]) == {"media-size": {"x-dimension": 12000, "y-dimension": 12000},
                                                 "media-source": "disc", "media-type": "disc"}
    from prt import booth
    assert printer.jobs() == render.disc_label(booth.load_photo(photo), disc.DEFAULT_DISC).jpegs()
    assert guard.used_today() == 1


def test_the_prompt_says_to_load_a_disc_in_the_disc_tray(capsys, tmp_path, printer, monkeypatch):
    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda question, **kw: asked.append(question) or False)
    code, _, _ = run(capsys, "-p", "demo", "disc", str(_photo(tmp_path)))
    assert code == 1 and printer.jobs() == [] and guard.used_today() == 0
    assert len(asked) == 1 and "load a printable CD/DVD in the disc tray" in asked[0]


def test_a_printer_without_a_disc_tray_is_refused_before_anything_is_sent(capsys, tmp_path, printer):
    printer.no_disc_tray()
    copy = tmp_path / "copy.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "disc", str(_photo(tmp_path)), "--save", str(copy),
                         "--yes")
    assert code == 1 and out == ""
    error = _error(err)
    assert error["kind"] == "disc_tray_unavailable" and "no disc tray" in error["message"] and error["hint"]
    assert copy.is_file()  # copies are written before the printer is asked
    assert printer.jobs() == [] and guard.used_today() == 0


def test_agent_mode_without_yes_is_refused_first_even_without_a_disc_tray(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    printer.no_disc_tray()
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "disc", str(_photo(tmp_path)))
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required"
    assert printer.jobs() == [] and guard.used_today() == 0


def test_agent_mode_needs_yes_but_preview_does_not(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photo = str(_photo(tmp_path))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "disc", photo)
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []
    assert run(capsys, "-p", "demo", "disc", photo, "-o", str(tmp_path / "d.png"))[0] == 0
    assert guard.used_today() == 0
    assert run(capsys, "-p", "demo", "disc", photo, "--yes")[0] == 0 and len(printer.jobs()) == 1


def test_a_daily_page_cap_refuses_the_print_before_anything_is_sent(capsys, tmp_path, printer):
    cfg = Config.load()
    cfg.max_pages_per_day = 1
    cfg.save()
    photo = str(_photo(tmp_path))
    assert run(capsys, "-p", "demo", "disc", photo, "--yes")[0] == 0
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "disc", photo, "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded"
    assert len(printer.jobs()) == 1 and guard.used_today() == 1


def test_disc_file_stdin_and_track_flags(capsys, tmp_path, printer, stdin):
    photo = str(_photo(tmp_path))
    path = _disc_file(tmp_path, {"title": "Wedding", "tracks": ["First dance", "Cake"]})
    code, out, err = run(capsys, "-f", "json", "disc", photo, "--disc-file", str(path), "-o", str(tmp_path / "a.png"))
    assert code == 0, err
    assert json.loads(out)["title"] == "Wedding" and json.loads(out)["tracks"] == 2
    stdin(json.dumps({"title": "Game Night", "tracks": "Catan\nCodenames\nPoker"}).encode())
    code, out, err = run(capsys, "-f", "json", "disc", photo, "--disc-file", "-", "--track", "Chess",
                         "-o", str(tmp_path / "b.png"))
    assert code == 0, err
    assert json.loads(out)["title"] == "Game Night" and json.loads(out)["tracks"] == 1
    assert printer.made == []


@pytest.mark.parametrize("content", [b"{nope", b'{"subtitle": "x"}', b'{"title": "x", "tracks": [1]}',
                                     b'{"title": "x", "tracks": ["\xf0\x9f\x8e\x89"]}'])
def test_bad_disc_on_stdin_exits_3_and_prints_nothing(capsys, tmp_path, printer, stdin, content):
    stdin(content)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "disc", str(_photo(tmp_path)), "--disc-file", "-",
                         "--yes")
    assert code == 3 and _error(err)["kind"] == "invalid_input" and out == ""
    assert printer.made == [] and guard.used_today() == 0


def test_bad_flags_photo_and_outputs_are_refused_before_anything_is_written(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    out_file = tmp_path / "x.png"
    code, _, err = run(capsys, "-f", "json", "disc", str(photo), "--track", "🎉", "-o", str(out_file))
    assert code == 2 and "cannot print" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "disc", str(photo), "-o", str(photo))
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "disc", str(tmp_path / "missing.jpg"), "-o", str(out_file))
    assert code == 3 and _error(err)["kind"] == "invalid_input"
    code, _, err = run(capsys, "-f", "json", "disc", str(photo), "-o", str(tmp_path / "x.gif"))
    assert code == 2
    assert printer.made == [] and not out_file.exists()


def test_a_cut_is_warned_and_reported(capsys, tmp_path, printer):
    code, out, err = run(capsys, "-f", "json", "disc", str(_photo(tmp_path)), "--title", "W" * 60, "-o",
                         str(tmp_path / "d.jpg"))
    assert code == 0 and "title cut to fit the disc label" in err
    data = json.loads(out)
    assert data["fits"] is False and data["cut"]["title"]["given"] == 60
    assert 0 < data["cut"]["title"]["max_fit"] < 60
