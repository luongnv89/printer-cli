"""prt colour-in: a photo turned into black line art on white, as an A4 colouring page (#36)."""

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from prt import catalog, colourin, guard, photocard
from prt.cli import main
from prt.drivers.fake import FakeDriver
from prt.paper import A4
from prt.transport import ipp

from conftest import without_estimate

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
INSET = colourin.FRAME + colourin.PAD + 10  # well inside the frame, clear of its line


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
    return SimpleNamespace(driver=fake, made=made, jobs=lambda: [data for _, _, _, data in requests()])


def _circle(size=(400, 400)):
    """A dark filled circle on a light background: one clear shape to outline."""
    img = Image.new("RGB", size, (235, 230, 220))
    w, h = size
    ImageDraw.Draw(img).ellipse((w // 4, h // 4, 3 * w // 4, 3 * h // 4), fill=(30, 60, 120))
    return img


def _photo(tmp_path, img=None, name="photo.jpg"):
    path = tmp_path / name
    (img or _circle()).save(path, quality=95)
    return path


def _art_box(page):
    """The box inside the frame, found from the frame's black line, shrunk by ``INSET`` - frame - pad."""
    grey = page.convert("L")
    x0, y0, x1, y1 = Image.eval(grey, lambda v: 255 - v).getbbox()  # the frame is the outermost black
    return x0 + INSET, y0 + INSET, x1 - INSET, y1 - INSET


def _black_inside(page):
    return page.crop(_art_box(page)).convert("L").histogram()[0]


# --- the page ------------------------------------------------------------------------------------

def test_a_shape_becomes_a_black_outline_on_white():
    page, cut = colourin.compose(_circle())
    assert cut == {} and page.mode == "RGB" and page.size == A4.size
    assert {colour for _, colour in page.getcolors()} == {BLACK, WHITE}
    assert _black_inside(page) > 1000  # the circle's outline, not just the frame
    # The outline is a line round the circle, not a filled shape: its middle stays white to colour in.
    x0, y0, x1, y1 = _art_box(page)
    assert page.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == WHITE


def test_a_plain_photo_has_nothing_to_outline():
    page, _ = colourin.compose(Image.new("RGB", (400, 300), (0, 0, 255)))
    assert _black_inside(page) == 0  # so the circle's black pixels above are line art, not the frame
    assert page.convert("L").histogram()[0] > 0  # the frame is still drawn


def test_line_art_is_pure_black_and_white_and_repeatable():
    photo = _circle((300, 500))
    art = colourin.line_art(photo, (300, 500))
    assert art.mode == "L" and art.size == (300, 500) and {v for _, v in art.getcolors()} == {0, 255}
    assert art.tobytes() == colourin.line_art(photo, (300, 500)).tobytes()


def test_the_page_follows_the_photo_and_keeps_it_whole():
    assert colourin.compose(_circle((300, 500)))[0].size == A4.size
    assert colourin.compose(_circle((300, 300)))[0].size == A4.size
    landscape, _ = colourin.compose(_circle((600, 300)))
    assert landscape.size == A4.landscape_size
    # The art keeps the photo's shape: the frame round a 2:1 photo is about twice as wide as it is tall.
    x0, y0, x1, y1 = Image.eval(landscape.convert("L"), lambda v: 255 - v).getbbox()
    assert (x1 - x0) / (y1 - y0) == pytest.approx(2, rel=0.05)


@pytest.mark.parametrize("size", [(1200, 1), (1, 1200), (1, 1), (3, 2)])
def test_very_thin_or_tiny_photos_still_make_a_page(size):
    page, _ = colourin.compose(Image.new("RGB", size, (200, 30, 30)))
    assert {colour for _, colour in page.getcolors()} == {BLACK, WHITE}


def test_the_page_is_estimated_black_and_white():
    est = guard.estimate([colourin.compose(_circle())[0]])
    assert est["colour"] is False and est["ink"]["cyan"] == est["ink"]["magenta"] == est["ink"]["yellow"] == 0


def test_styles_list_colour_in_on_a4_with_no_text():
    style = next(s for s in catalog.styles() if s["name"] == "colour-in")
    assert style["command"] == "colour-in" and style["args"] == [] and style["kind"] == colourin.KIND
    assert style["schema"] is None and style["fields"] == [] and style["photos"] == {"min": 1, "max": 1}
    assert style["max_pages"] == 1 and style["paper"] == {"size": "a4", "media": "a4", "tray": None}
    assert style["sample"] == catalog.SAMPLES + "colour-in.jpg"


# --- prt colour-in ---------------------------------------------------------------------------------

def test_preview_saves_the_page_and_prints_nothing(capsys, tmp_path, printer):
    photo, out_file = _photo(tmp_path), tmp_path / "page.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "colour-in", str(photo), "--preview", str(out_file))
    assert code == 0, err
    assert "not printed" in err
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0
    data = json.loads(out)
    assert data["estimate"]["colour"] is False and data["estimate"]["summary"].startswith("~1 page, black and white")
    assert without_estimate(out) == {"action": "colour-in", "file": str(out_file), "bytes": out_file.stat().st_size,
                                     "paper": {"size": "a4", "media": "a4", "tray": None}, "fits": True, "cut": {}}
    saved = Image.open(out_file).convert("RGB")
    expected, _ = colourin.compose(photocard.load_photos([photo])[0])
    assert saved.size == A4.size and saved.tobytes() == expected.tobytes()
    assert {colour for _, colour in saved.getcolors()} == {BLACK, WHITE}


def test_preview_as_pdf_and_save_a_copy(capsys, tmp_path, printer):
    photo, out_file, copy = _photo(tmp_path), tmp_path / "page.pdf", tmp_path / "copy.jpg"
    code, out, err = run(capsys, "-f", "json", "colour-in", str(photo), "-o", str(out_file), "--save", str(copy))
    assert code == 0, err
    assert out_file.read_bytes().startswith(b"%PDF") and Image.open(copy).size == A4.size
    assert without_estimate(out)["saved"] == [{"file": str(copy), "format": "jpeg", "bytes": copy.stat().st_size}]
    assert printer.made == [] and guard.used_today() == 0


def test_print_sends_the_page_and_counts_one(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "colour-in", str(photo), "--yes")
    assert code == 0, err
    data = without_estimate(out)
    assert data["action"] == "colour-in" and data["job_id"] and data["paper"]["size"] == "a4"
    expected, _ = colourin.compose(photocard.load_photos([photo])[0])
    assert printer.jobs() == [A4.to_jpeg(expected)]
    assert guard.used_today() == 1


def test_a_landscape_page_is_sent_upright(capsys, tmp_path, printer):
    photo = _photo(tmp_path, _circle((600, 300)))
    code, _, err = run(capsys, "-p", "demo", "colour-in", str(photo), "--yes")
    assert code == 0, err
    sent = Image.open(io.BytesIO(printer.jobs()[0]))
    assert sent.size == A4.size  # turned a quarter for the printer, read once the sheet is turned


def test_agent_mode_needs_yes_but_preview_does_not(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photo = str(_photo(tmp_path))
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "colour-in", photo)
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []
    made = len(printer.made)
    code, _, _ = run(capsys, "-p", "demo", "colour-in", photo, "-o", str(tmp_path / "p.png"))
    assert code == 0 and len(printer.made) == made and printer.jobs() == [] and guard.used_today() == 0


def test_bad_photo_and_output_over_the_photo_are_refused(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    code, _, err = run(capsys, "-f", "json", "colour-in", str(photo), "-o", str(photo))
    assert code == 2 and "overwrite" in _error(err)["message"]
    code, _, err = run(capsys, "-f", "json", "colour-in", str(tmp_path / "missing.jpg"), "-o", str(tmp_path / "x.png"))
    assert code == 3 and _error(err)["kind"] == "invalid_input"
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    code, _, err = run(capsys, "-f", "json", "colour-in", str(bad), "-o", str(tmp_path / "x.png"))
    assert code == 3
    code, _, err = run(capsys, "-f", "json", "colour-in", str(photo), "-o", str(tmp_path / "x.gif"))
    assert code == 2
    assert printer.made == [] and not (tmp_path / "x.png").exists()
