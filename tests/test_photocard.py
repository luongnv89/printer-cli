"""prt strip and prt instant: 4×6 photo-tray prints, a four-frame booth strip and an instant photo (#27)."""

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from prt import catalog, guard, photocard
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import PHOTO_4X6
from prt.transport import ipp

from conftest import without_estimate

COLOURS = [(220, 30, 30), (30, 200, 40), (30, 40, 220), (240, 200, 20)]


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture
def printer(monkeypatch):
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, requests=requests,
                           jobs=lambda: [data for _, _, _, data in requests()])


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    monkeypatch.setattr(photocard, "today", lambda: "24 November 2026")


def _photos(tmp_path, n=4):
    paths = []
    for i in range(n):
        path = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (400, 300), COLOURS[i % 4]).save(path, quality=95)
        paths.append(path)
    return paths


def _images(n=4):
    return [Image.new("RGB", (400, 300), COLOURS[i % 4]) for i in range(n)]


def _centre(box):
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


def _media_col(request):
    col = next(a.value for a in request[2] if a.name == "media-col")
    return {k: v[1] for k, v in col.items() if k != "media-size"}


# --- the strip -----------------------------------------------------------------------------------

def test_strip_has_the_four_photos_in_order_from_the_top():
    sheet, cut = photocard.strip(_images(), "Hello")
    assert sheet.size == PHOTO_4X6.size and cut == {}
    for colour, box in zip(COLOURS, photocard.frame_boxes()):
        assert sheet.getpixel(_centre(box)) == colour


def test_strip_is_printed_twice_side_by_side_with_a_cut_line_between():
    sheet, _ = photocard.strip(_images(), "Hello")
    half = photocard.STRIP_W
    left, right = sheet.crop((10, 0, half - 10, sheet.height)), sheet.crop((half + 10, 0, 2 * half - 10, sheet.height))
    assert left.tobytes() == right.tobytes()
    column = {sheet.getpixel((half, y)) for y in range(0, sheet.height, 4)}
    assert photocard.CUT_LINE in column and (255, 255, 255) in column  # dashed


def test_strip_caption_sits_under_the_frames():
    blank, _ = photocard.strip(_images(), "")
    captioned, _ = photocard.strip(_images(), "Anna & Ben")
    foot = (0, photocard.frame_boxes()[-1][3] + 10, photocard.W, photocard.H)
    assert {colour for _, colour in blank.crop(foot).getcolors(1 << 16)} == {(255, 255, 255), photocard.CUT_LINE}
    assert photocard.STRIP_INK in {colour for _, colour in captioned.crop(foot).getcolors(1 << 16)}


def test_strip_mono_prints_the_photos_in_black_and_white():
    sheet, _ = photocard.strip(_images(), "", mono=True)
    r, g, b = sheet.getpixel(_centre(photocard.frame_boxes()[0]))
    assert r == g == b


def test_strip_takes_exactly_four_photos():
    for n in (1, 3, 5):
        with pytest.raises(PrinterError, match=f"takes 4 photos, got {n}") as err:
            photocard.strip(_images(n), "")
        assert err.value.exit_code == 2


def test_a_long_caption_is_cut_and_reported():
    sheet, cut = photocard.strip(_images(), "W" * 60)
    assert set(cut) == {"caption"} and cut["caption"].given == 60 and 0 < cut["caption"].max_fit < 60


# --- the instant print ---------------------------------------------------------------------------

def test_instant_crops_the_photo_square_in_a_white_frame():
    sheet, cut = photocard.instant(Image.new("RGB", (400, 300), COLOURS[2]), "Lake")
    x0, y0, x1, y1 = photocard.PHOTO_BOX
    assert sheet.size == PHOTO_4X6.size and cut == {} and x1 - x0 == y1 - y0
    assert sheet.getpixel(_centre(photocard.PHOTO_BOX)) == COLOURS[2]
    assert sheet.getpixel((x0 + 2, y0 + 2)) == COLOURS[2] and sheet.getpixel((20, 20)) == photocard.FRAME


def test_instant_caption_is_written_in_the_space_under_the_photo():
    blank, _ = photocard.instant(Image.new("RGB", (400, 300)), "")
    written, _ = photocard.instant(Image.new("RGB", (400, 300)), "Summer at the lake")
    box = photocard.CAPTION_BOX
    assert blank.crop(box).getcolors() == [((box[2] - box[0]) * (box[3] - box[1]), photocard.FRAME)]
    assert any(abs(r - photocard.PEN[0]) < 20 and abs(b - photocard.PEN[2]) < 20
               for _, (r, g, b) in written.crop(box).getcolors(1 << 20))


def test_captions_are_checked_for_length_and_script():
    assert photocard.check_caption("  Été   à Paris ", 80) == "Été à Paris"
    with pytest.raises(PrinterError, match="too long"):
        photocard.check_caption("x" * 81, 80)
    with pytest.raises(PrinterError, match="cannot print") as err:
        photocard.check_caption("party 🎉", 80)
    assert err.value.exit_code == 2


# --- the catalog ---------------------------------------------------------------------------------

def test_styles_list_strip_and_instant_on_4x6():
    styles = {s["name"]: s for s in catalog.styles()}
    assert styles["strip"]["photos"] == {"min": 4, "max": 4} and styles["instant"]["photos"] == {"min": 1, "max": 1}
    for name, limit in (("strip", 60), ("instant", 80)):
        assert styles[name]["command"] == name and styles[name]["schema"] is None
        assert styles[name]["paper"] == {"size": "4x6", "media": "4x6", "tray": "photo"}
        assert styles[name]["fields"] == [{"name": "caption", "description": styles[name]["fields"][0]["description"],
                                           "required": False, "max_length": limit}]
    assert styles["metro"]["paper"]["size"] == "a4"


# --- prt strip / prt instant -----------------------------------------------------------------------

def test_strip_preview_saves_the_sheet_and_prints_nothing(capsys, tmp_path, printer):
    out_file = tmp_path / "strip.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "strip", *map(str, _photos(tmp_path)), "-o",
                         str(out_file))
    assert code == 0, err
    assert "not printed" in err and "dashed line" in err
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    data = json.loads(out)
    assert data["estimate"]["summary"].startswith("~1 page (4×6)")
    assert without_estimate(out) == {"action": "strip", "file": str(out_file), "bytes": out_file.stat().st_size,
                                     "photos": 4, "paper": {"size": "4x6", "media": "4x6", "tray": "photo"},
                                     "fits": True, "cut": {}}
    saved = Image.open(out_file)
    assert saved.size == (1200, 1800)
    expected, _ = photocard.strip(photocard.load_photos(_photos(tmp_path)), "24 November 2026")
    assert saved.convert("RGB").tobytes() == expected.tobytes()


def test_strip_prints_from_the_photo_tray(capsys, tmp_path, printer):
    paths = _photos(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "strip", *map(str, paths), "--caption", "Booth",
                         "--mono", "--yes")
    assert code == 0, err
    assert "photo tray" in err
    data = without_estimate(out)
    assert data["action"] == "strip" and data["job_id"] and data["paper"]["tray"] == "photo"
    expected, _ = photocard.strip(photocard.load_photos(paths), "Booth", mono=True)
    assert printer.jobs() == [PHOTO_4X6.to_jpeg(expected)]
    assert _media_col(printer.requests()[0]) == {"media-source": "photo", "media-type": "photographic"}
    assert guard.used_today() == 1


@pytest.mark.parametrize("count", [1, 3, 5])
def test_strip_with_the_wrong_number_of_photos_is_a_usage_error(capsys, tmp_path, printer, count):
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "strip", *map(str, _photos(tmp_path, count)), "--yes")
    assert code == 2 and _error(err)["kind"] == "usage" and out == ""
    assert printer.made == [] and guard.used_today() == 0


def test_strip_caption_cut_is_warned(capsys, tmp_path, printer):
    code, out, err = run(capsys, "-f", "json", "strip", *map(str, _photos(tmp_path)), "--caption", "W" * 60,
                         "-o", str(tmp_path / "s.jpg"))
    assert code == 0 and "caption cut to fit" in err
    assert json.loads(out)["fits"] is False


def test_instant_preview_and_save(capsys, tmp_path, printer):
    photo = _photos(tmp_path, 1)[0]
    out_file, copy = tmp_path / "instant.pdf", tmp_path / "copy.jpg"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "instant", str(photo), "--caption", "Lake", "-o",
                         str(out_file), "--save", str(copy))
    assert code == 0, err
    assert out_file.read_bytes().startswith(b"%PDF") and Image.open(copy).size == (1200, 1800)
    data = without_estimate(out)
    assert data["action"] == "instant" and data["saved"] == [{"file": str(copy), "format": "jpeg",
                                                             "bytes": copy.stat().st_size}]
    assert printer.made == [] and guard.used_today() == 0


def test_instant_default_caption_is_today_and_empty_leaves_it_blank(capsys, tmp_path, printer):
    photo = _photos(tmp_path, 1)[0]
    image = photocard.load_photos([photo])[0]
    for args, caption in (([], "24 November 2026"), (["--caption", ""], "")):
        code, _, err = run(capsys, "instant", str(photo), *args, "-o", str(tmp_path / "i.png"))
        assert code == 0, err
        assert Image.open(tmp_path / "i.png").convert("RGB").tobytes() == photocard.instant(image, caption)[0].tobytes()


def test_instant_prints_from_the_photo_tray_and_counts_one_page(capsys, tmp_path, printer):
    code, _, err = run(capsys, "-p", "demo", "instant", str(_photos(tmp_path, 1)[0]), "--yes")
    assert code == 0, err
    assert len(printer.jobs()) == 1 and _media_col(printer.requests()[0])["media-source"] == "photo"
    assert guard.used_today() == 1


def test_agent_mode_needs_yes_but_preview_does_not(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photo = str(_photos(tmp_path, 1)[0])
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "instant", photo)
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []
    code, _, _ = run(capsys, "-p", "demo", "instant", photo, "-o", str(tmp_path / "i.png"))
    assert code == 0 and guard.used_today() == 0


def test_a_page_cap_refuses_the_print(capsys, tmp_path, printer, monkeypatch):
    from prt.config import Config
    cfg = Config.load()
    cfg.max_pages_per_day = 1
    cfg.save()
    photo = str(_photos(tmp_path, 1)[0])
    assert run(capsys, "-p", "demo", "instant", photo, "--yes")[0] == 0
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "instant", photo, "--yes")
    assert code == 1 and _error(err)["kind"] == "daily_page_cap_exceeded" and len(printer.jobs()) == 1


def test_bad_caption_and_output_over_a_photo_are_refused(capsys, tmp_path, printer):
    paths = _photos(tmp_path)
    code, _, err = run(capsys, "-f", "json", "instant", str(paths[0]), "--caption", "🎉", "-o", str(tmp_path / "x.png"))
    assert code == 2 and "cannot print" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "strip", *map(str, paths), "-o", str(paths[1]))
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "instant", str(tmp_path / "missing.jpg"), "-o", str(tmp_path / "x.png"))
    assert code == 3 and _error(err)["kind"] == "invalid_input"
    assert printer.made == [] and not (tmp_path / "x.png").exists()
