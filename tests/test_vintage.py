import gc
import inspect
import json
import os
import sys
import time
import weakref
from functools import lru_cache
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageStat

from prt import article, booth, fonts, layout, testpage, vintage
from prt.booth import LIMITS, Story
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.transport import ipp

A4_150DPI = (1240, 1754)
FAMILIES = {"Old Standard TT", "Playfair Display", "UnifrakturMaguntia"}
DRAWN_ROLES = ["body", "body-bold", "body-italic", "display-bold", "display-black"]  # the masthead is fixed ASCII
DIVERGENT = "Ǆ Ǫ Ĳ ſ Ǎ ǆ Ǧ "  # each one is missing from at least one bundled face
LEAD_LIMIT, PLATE_LIMIT, MAX_PHOTOS, MAX_PAGES = 1060, 540, 57, 10  # from the issue's agreed plan


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; `made` counts how often a driver was created at all."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    monkeypatch.setattr(booth, "_today", lambda: "Sunday, November 22, 2026")
    jobs = lambda: [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=jobs)


def _photo(tmp_path, name="photo.jpg", size=(300, 400), colour=(0, 0, 255)):
    path = tmp_path / name
    Image.new("RGB", size, colour).save(path)
    return path


def _trip(tmp_path, n, prefix="trip"):
    return [_photo(tmp_path, f"{prefix}-{k}.jpg", colour=(30 * k, 90, 120)) for k in range(1, n + 1)]


def _expected_pages(paths, story=vintage.DEFAULT_GAZETTE_STORY):
    return [booth.to_jpeg(page) for page in vintage.compose_pages(vintage.load_photos(paths), story)[0]]


def _paper_only(page, band):
    return page.crop(band).getextrema() == tuple((c, c) for c in vintage.PAPER)


def _longest_story(prefix=""):
    """Every field at its limit, and every field holding characters some bundled face lacks."""
    return booth.check_story(Story(headline=(prefix + "H" * LIMITS["headline"])[:LIMITS["headline"]],
                                   story=prefix + "A" * 200 + "\n" + "Wide paragraph.\n" * 140,
                                   kicker=(prefix + "W" * LIMITS["kicker"])[:LIMITS["kicker"]],
                                   caption=(prefix + "c " * 100)[:LIMITS["caption"]],
                                   byline=(prefix + "B" * LIMITS["byline"])[:LIMITS["byline"]]), 2)


# --- bundled fonts ---------------------------------------------------------------------------


@pytest.fixture
def fresh_fonts():
    fonts.face.cache_clear()
    fonts._data.cache_clear()
    yield
    fonts.face.cache_clear()
    fonts._data.cache_clear()


@pytest.mark.parametrize("role", list(fonts.ROLES))
def test_every_face_is_bundled_and_uses_basic_layout(monkeypatch, fresh_fonts, role):
    monkeypatch.setattr(ImageFont.core, "HAVE_RAQM", True)  # as on a Pillow built with raqm
    face = fonts.face(role, 40)
    assert face.layout_engine == ImageFont.Layout.BASIC
    assert face.getname()[0] in FAMILIES


def test_the_bundled_families_are_the_reference_ones():
    assert {fonts.face(role, 40).getname()[0] for role in fonts.ROLES} == FAMILIES


def test_each_weight_has_its_own_font_object():
    black, bold = fonts.face("display-black", 60), fonts.face("display-bold", 60)
    assert black is not bold and black.getlength("GAZETTE") > bold.getlength("GAZETTE")
    assert fonts.face("display-bold", 60) is bold  # cached: asking again does not reset the weight


@pytest.mark.parametrize("file", ["Missing.ttf", "OFL-OldStandardTT.txt"])  # absent, and not a font
def test_a_missing_or_broken_font_is_an_error_never_a_fallback(monkeypatch, fresh_fonts, file):
    monkeypatch.setitem(fonts.ROLES, "body", (file, None))
    with pytest.raises(PrinterError, match=f"bundled font missing: {file}") as err:
        fonts.face("body", 30)
    assert err.value.exit_code == 1


# --- no text in any other font ---------------------------------------------------------------


def _from_glyph_gate() -> bool:
    frame = sys._getframe(2)
    while frame:
        if frame.f_code.co_name in ("_has_glyph", "_missing_glyph") and frame.f_globals.get("__name__") == "prt.layout":
            return True
        frame = frame.f_back
    return False


@pytest.fixture
def strict_drawing(monkeypatch):
    """Fail on any font fallback, and on any text drawn in a font that lacks one of its characters."""
    fallbacks, drawn = [], []

    def guard(original, name):
        def recorder(*args, **kwargs):
            if _from_glyph_gate():
                return original(*args, **kwargs)
            fallbacks.append(name)
            raise AssertionError(f"{name} called while rendering the gazette")
        return recorder

    for module in (testpage, layout, booth):
        monkeypatch.setattr(module, "_font", guard(module._font, f"{module.__name__}._font"))
    monkeypatch.setattr(ImageFont, "load_default", guard(ImageFont.load_default, "ImageFont.load_default"))

    real_text = ImageDraw.ImageDraw.text
    signature = inspect.signature(real_text)

    def text(self, xy, text, *args, **kwargs):
        bound = signature.bind(self, xy, text, *args, **kwargs).arguments
        font = bound.get("font")
        assert isinstance(font, ImageFont.FreeTypeFont) and font.getname()[0] in FAMILIES, font
        assert not {"features", "direction", "language"} & {k for k, v in bound.items() if v is not None}
        notdef = bytes(font.getmask("\U0010fffd"))
        missing = [c for c in text if not c.isspace() and bytes(font.getmask(c)) == notdef]
        assert missing == [], (font.getname(), text)
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    return SimpleNamespace(fallbacks=fallbacks, drawn=drawn)


def test_vintage_module_has_no_page_font():
    assert not hasattr(vintage, "_font")


@pytest.mark.parametrize("n", [1, 10])
def test_every_word_is_drawn_in_a_bundled_font_that_has_its_glyphs(strict_drawing, n):
    story = _longest_story(DIVERGENT)
    assert all(DIVERGENT.split()[0] in getattr(story, name) for name in booth.FIELDS)
    photos = [Image.new("RGB", (400 + 50 * k, 300), (20 * k, 80, 160)) for k in range(n)]
    pages, _, _ = vintage.compose_pages(photos, story)
    assert len(pages) == len(vintage.plan_pages(n)) and strict_drawing.fallbacks == []
    assert any("DZ" in text for text in strict_drawing.drawn)  # a divergent letter was replaced, not dropped


# --- glyph coverage --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admitted():
    """Every character the story check lets through (U+0020..U+FFFF, surrogates are not characters).

    Measuring needs the page font once per character; memoising it only saves time, the glyphs are the same.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(layout, "_font", lru_cache(maxsize=None)(testpage._font))
        return [chr(c) for c in range(0x20, 0x10000)
                if not 0xD800 <= c <= 0xDFFF and layout._glyphs(chr(c)) is not None]


def _latin(c: str) -> bool:
    return 0x00A0 <= ord(c) <= 0x024F or 0x1E00 <= ord(c) <= 0x1EFF


@pytest.mark.parametrize("role", DRAWN_ROLES)
def test_every_admitted_character_prints_in_every_face(admitted, role):
    face = fonts.face(role, 40)
    notdef = bytes(face.getmask("\U0010fffd"))
    tofu, lost, replaced, unknown = [], [], 0, 0
    for c in admitted:
        out = vintage._printable_face(c, face)
        if c.isspace():
            assert out == " ", repr(c)
            continue
        tofu += [c for g in out if not g.isspace() and bytes(face.getmask(g)) == notdef]
        unknown += out.count("?") - (c == "?")
        if "?" in out and c != "?" and (_latin(c) or (c in layout._STAND_INS and 0x2000 <= ord(c) <= 0x206F)):
            lost.append(c)
        replaced += out not in (c, "")
    assert tofu == [], f"{role}: drawn as empty boxes: {''.join(tofu)}"
    assert lost == [], f"{role}: printed as '?': {''.join(lost)} ({unknown} '?' in all)"
    assert replaced > 0, f"{role}: no character needed a stand-in, so the sweep checks nothing ({unknown} '?')"


# --- every photo printed ---------------------------------------------------------------------


@pytest.mark.parametrize("n, pages", [(1, 1), (2, 1), (3, 1), (4, 2), (9, 2), (10, 3), (MAX_PHOTOS, MAX_PAGES)])
def test_plan_pages_puts_every_photo_on_exactly_one_page(n, pages):
    plan = vintage.plan_pages(n)
    assert [k for page in plan for k in page] == list(range(n))
    assert len(plan) == pages and len(plan[0]) == min(n, 3) and all(1 <= len(page) <= 6 for page in plan[1:])


def test_plan_pages_needs_a_photo():
    with pytest.raises(ValueError):
        vintage.plan_pages(0)


def test_the_photo_cap_fills_exactly_the_page_cap():
    vintage.check_count(MAX_PHOTOS)
    with pytest.raises(PrinterError, match=f"at most {MAX_PHOTOS} photos, got {MAX_PHOTOS + 1}") as err:
        vintage.check_count(MAX_PHOTOS + 1)
    assert err.value.exit_code == 3


@pytest.mark.parametrize("n", [1, 3, 4, 10])  # alone, a full front page, one more, and page 2 full plus one
def test_every_photo_is_printed_in_its_own_box_on_its_planned_page(n):
    shapes = [(300, 400), (400, 300), (500, 500)]
    greys = [40 + 16 * k for k in range(n)]  # distinct, and well away from the ink and the paper
    photos = [Image.new("RGB", shapes[k % 3], (g, g, g)) for k, g in enumerate(greys)]
    pages, placements, _ = vintage.compose_pages(photos)
    plan = vintage.plan_pages(n)
    assert len(pages) == len(plan) == len(placements)
    assert sorted(k for page in placements for k, _ in page) == list(range(n))
    assert [[k for k, _ in page] for page in placements] == plan
    for page, placed in zip(pages, placements):
        for k, box in placed:
            expected = vintage._treat(Image.new("RGB", (4, 4), (greys[k],) * 3)).getpixel((0, 0))
            mean = ImageStat.Stat(page.crop(box)).mean
            assert all(abs(m - e) <= 2 for m, e in zip(mean, expected)), (k, mean, expected)


# --- the page --------------------------------------------------------------------------------


def test_default_story_passes_the_story_check():
    assert booth.check_story(vintage.DEFAULT_GAZETTE_STORY, 2) == vintage.DEFAULT_GAZETTE_STORY


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("shape", [(800, 1200), (1200, 800), (1000, 1000)])
def test_default_story_is_never_cut(n, shape):
    _, _, cut = vintage.compose_pages([Image.new("RGB", shape, "grey")] * n, vintage.DEFAULT_GAZETTE_STORY)
    assert cut == []


def test_too_long_text_is_cut_and_named():
    _, _, cut = vintage.compose_pages([Image.new("RGB", (400, 300), "grey")] * 3, _longest_story())
    assert cut == ["headline", "story"]


def test_pages_are_a4_cream_and_the_longest_story_stays_inside_the_margins():
    pages, _, _ = vintage.compose_pages([Image.new("RGB", (1600, 400), "red")] * 10, _longest_story())
    w, h = A4_150DPI
    for page in pages:
        assert page.size == A4_150DPI and page.getpixel((5, 5)) == vintage.PAPER
        for band in [(0, 0, w, 80), (0, h - 80, w, h), (0, 0, 80, h), (w - 80, 0, w, h)]:
            assert _paper_only(page, band), band


def test_unbroken_story_renders_quickly_inside_the_margins():
    # A pasted URL or base64 blob is one 2500-character "word": it must wrap in good time.
    story = booth.check_story(Story(headline="H", story="x" * LIMITS["story"]), 2)
    start = time.perf_counter()
    for n in (1, 3):
        pages, _, _ = vintage.compose_pages([Image.new("RGB", (300, 400), "blue")] * n, story)
        w, h = A4_150DPI
        for band in [(0, 0, 80, h), (w - 80, 0, w, h), (0, h - 80, w, h)]:
            assert _paper_only(pages[0], band), (n, band)
    assert time.perf_counter() - start < 10


def test_small_caps_set_lower_case_letters_as_smaller_capitals():
    runs = vintage._caps("Price", "body-bold", 20)
    assert [(font.size, text) for font, text in runs] == [(20, "P"), (15, "RICE")]


# --- photos are shrunk, only for the gazette -------------------------------------------------


def test_downscale_keeps_photos_to_the_size_they_print_at():
    big = Image.new("RGB", (6000, 4000), "grey")
    assert vintage.LEAD_LIMIT == LEAD_LIMIT and vintage.PLATE_LIMIT == PLATE_LIMIT
    assert vintage._downscale(big, vintage.LEAD_LIMIT).size == (1060, 707)
    assert vintage._downscale(big, vintage.PLATE_LIMIT).size == (540, 360)
    small = Image.new("RGB", (300, 200), "grey")
    copy = vintage._downscale(small, vintage.PLATE_LIMIT)
    assert copy.size == (300, 200) and copy is not small  # never enlarged, always a copy
    assert big.size == (6000, 4000)


def test_downscaling_leaves_the_other_styles_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(booth, "_today", lambda: "Sunday, November 22, 2026")
    path = _photo(tmp_path, size=(2000, 1500), colour=(200, 40, 40))
    photo = booth.load_photo(path)
    before = photo.tobytes()
    vintage.compose_pages([vintage._downscale(photo, vintage.LEAD_LIMIT)])
    assert photo.size == (2000, 1500) and photo.tobytes() == before
    for style in booth.STYLES:
        assert booth.compose(photo, None, style)[0].tobytes() == \
            booth.compose(booth.load_photo(path), None, style)[0].tobytes(), style


def test_large_photos_are_shrunk_one_at_a_time(capsys, tmp_path, monkeypatch, printer):
    paths = [_photo(tmp_path, f"big-{k}.jpg", size=(4000, 3000), colour=(50 * k, 90, 120)) for k in range(3)]
    full_size, leaks, sizes = [], [], []
    real_load, real_compose = booth.load_photo, vintage.compose_pages

    def load(path):
        gc.collect()
        leaks.extend(ref for ref in full_size if ref() is not None)
        image = real_load(path)
        full_size.append(weakref.ref(image))
        return image

    def compose(photos, story=None):
        sizes.extend(photo.size for photo in photos)
        return real_compose(photos, story)

    monkeypatch.setattr(booth, "load_photo", load)
    monkeypatch.setattr(vintage, "compose_pages", compose)
    code, _, _ = run(capsys, "-p", "demo", "gazette", *map(str, paths), "-o", str(tmp_path / "page.pdf"))
    assert code == 0 and leaks == [] and len(full_size) == 3
    assert sizes == [(1060, 795), (540, 405), (540, 405)]


# --- the command -----------------------------------------------------------------------------


def test_gazette_has_help(capsys):
    code, out, _ = run(capsys, "gazette", "--help")
    assert code == 0 and "Usage" in out and "--story-file" in out
    code, out, _ = run(capsys, "--help")
    assert code == 0 and "gazette" in out


def test_one_photo_prints_one_page(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", str(photo), "--yes")
    assert code == 0 and printer.jobs() == _expected_pages([photo])
    assert json.loads(out) == {"action": "gazette", "photos": 1, "pages": 1, "job_ids": [101]}
    assert "1 page queued as job 101" in err


def test_several_photos_print_one_job_per_page(capsys, tmp_path, printer):
    photos = _trip(tmp_path, 4)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, photos), "--yes")
    expected = _expected_pages(photos)
    assert code == 0 and len(expected) == 2 and printer.jobs() == expected
    assert json.loads(out) == {"action": "gazette", "photos": 4, "pages": 2, "job_ids": [101, 102]}
    assert "2 pages queued as jobs 101, 102" in err


def test_asks_once_before_printing(capsys, tmp_path, monkeypatch, printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, _, _ = run(capsys, "-p", "demo", "gazette", *map(str, _trip(tmp_path, 4)))
    assert code == 0 and len(printer.jobs()) == 2
    assert asked == ["Print 4 photos as 2 pages of a vintage newspaper on demo?"]


def test_declined_prompt_prints_nothing_and_keeps_the_copies(capsys, tmp_path, monkeypatch, printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda q, **kw: False)
    copy = tmp_path / "copy.pdf"
    code, _, err = run(capsys, "-p", "demo", "gazette", *map(str, _trip(tmp_path, 4)), "--save", str(copy))
    assert code == 1 and "nothing was sent" in err and printer.jobs() == []
    assert b"/Count 2" in copy.read_bytes()


def test_needs_yes_without_a_terminal(capsys, tmp_path, monkeypatch, printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)))
    assert code == 2 and "--yes" in err and printer.jobs() == []


def test_preview_writes_numbered_images_and_never_creates_a_driver(capsys, tmp_path, printer):
    out_file = tmp_path / "page.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, _trip(tmp_path, 4)), "-o",
                         str(out_file))
    files = [tmp_path / "page-1.png", tmp_path / "page-2.png"]
    assert code == 0 and "not printed" in err and printer.made == []
    assert json.loads(out) == {"action": "gazette", "photos": 4, "pages": 2,
                               "files": [{"file": str(f), "format": "png", "bytes": f.stat().st_size}
                                         for f in files]}
    assert all(Image.open(f).size == A4_150DPI for f in files) and not out_file.exists()


def test_save_keeps_a_pdf_and_prints(capsys, tmp_path, printer):
    copy = tmp_path / "copy.pdf"
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, _trip(tmp_path, 4)), "--save",
                       str(copy), "--yes")
    assert code == 0 and len(printer.jobs()) == 2 and b"/Count 2" in copy.read_bytes()
    assert json.loads(out)["saved"] == [{"file": str(copy), "format": "pdf", "bytes": copy.stat().st_size}]


def test_story_flags_override_the_default(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    headline = 'Café «Noir» & "Friends" <b>'
    code, _, _ = run(capsys, "-p", "demo", "gazette", str(photo), "--headline", headline, "--byline", "By Zoë",
                     "--yes")
    story = booth.with_overrides(vintage.DEFAULT_GAZETTE_STORY, headline=headline, byline="By Zoë")
    assert story.headline == headline
    assert code == 0 and printer.jobs() == _expected_pages([photo], story)


def test_warns_when_text_is_cut_to_fit(capsys, tmp_path, printer):
    code, _, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)), "--headline", "W" * 120, "--yes")
    assert code == 0 and "headline cut to fit the front page" in err


# --- nothing is sent for bad input -----------------------------------------------------------


def _bad_photo(tmp_path, kind):
    if kind == "unreadable":
        path = tmp_path / "broken.jpg"
        path.write_bytes(b"not really a jpeg")
    elif kind == "unsupported":
        path = tmp_path / "notes.txt"
        path.write_text("hello")
    else:
        path = tmp_path / "missing.jpg"
    return path


@pytest.mark.parametrize("kind, message", [("unreadable", "cannot read photo broken.jpg"),
                                           ("unsupported", "cannot read photo notes.txt"),
                                           ("missing", "photo not found")])
@pytest.mark.parametrize("mode", [[], ["-o", "out.pdf"], ["--save", "copy.png"]])
def test_a_bad_photo_anywhere_sends_and_writes_nothing(capsys, tmp_path, monkeypatch, printer, kind, message, mode):
    monkeypatch.chdir(tmp_path)
    good, bad = _photo(tmp_path), _bad_photo(tmp_path, kind)
    code, out, err = run(capsys, "-p", "demo", "gazette", str(good), str(bad), *mode, "--yes")
    assert code == 3 and message in err and bad.name in err and out == ""
    assert printer.made == [] and printer.jobs() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted({good.name, bad.name} - {"missing.jpg"})


def test_a_bad_file_type_is_refused_before_any_photo_is_read(capsys, tmp_path, monkeypatch, printer):
    loaded = []
    monkeypatch.setattr(booth, "load_photo", lambda path: loaded.append(path) or Image.new("RGB", (30, 40)))
    code, _, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)), "-o", str(tmp_path / "page.gif"))
    assert code == 2 and "unknown file type .gif" in err and loaded == [] and printer.made == []


def test_too_many_photos_are_refused_before_any_photo_is_read(capsys, tmp_path, monkeypatch, printer):
    loaded = []
    monkeypatch.setattr(booth, "load_photo", lambda path: loaded.append(path) or Image.new("RGB", (30, 40)))
    photo = _photo(tmp_path)
    code, _, err = run(capsys, "-p", "demo", "gazette", *[str(photo)] * (MAX_PHOTOS + 1), "-o",
                       str(tmp_path / "out.pdf"), "--save", str(tmp_path / "copy.png"))
    assert code == 3 and f"gazette takes at most {MAX_PHOTOS} photos, got {MAX_PHOTOS + 1}" in err
    assert loaded == [] and printer.made == [] and [p.name for p in tmp_path.iterdir()] == ["photo.jpg"]


# --- a page never overwrites a photo ---------------------------------------------------------


def _refused(capsys, printer, photos, *args):
    before = {p: p.read_bytes() for p in photos if p.exists()}
    code, _, err = run(capsys, "-p", "demo", "gazette", *map(str, photos), *args, "--yes")
    assert code == 2 and "would overwrite input photo" in err
    assert {p: p.read_bytes() for p in before} == before and printer.made == []
    return err


def _ignores_case(tmp_path) -> bool:
    probe = _photo(tmp_path, "case-probe.jpg")
    ignores = (tmp_path / "CASE-PROBE.JPG").exists()
    probe.unlink()
    return ignores


def test_output_named_like_the_photo_is_refused(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    err = _refused(capsys, printer, [photo], "-o", str(photo))
    assert f"output {photo} would overwrite input photo {photo}" in err


def test_numbered_copies_that_would_overwrite_photos_are_refused(capsys, tmp_path, printer):
    photos = _trip(tmp_path, 4)  # two pages: --save trip.jpg writes trip-1.jpg and trip-2.jpg
    _refused(capsys, printer, photos, "--save", str(tmp_path / "trip.jpg"))


def test_output_spelled_in_other_case_is_refused_where_case_is_ignored(capsys, tmp_path, printer):
    if not _ignores_case(tmp_path):
        pytest.skip("this file system tells PHOTO.JPG and photo.jpg apart, so they are different files")
    _refused(capsys, printer, [_photo(tmp_path)], "-o", str(tmp_path / "PHOTO.JPG"))
    _refused(capsys, printer, _trip(tmp_path, 4), "--save", str(tmp_path / "Trip.jpg"))


def test_output_through_a_hard_link_is_refused(capsys, tmp_path, printer):
    photo = _photo(tmp_path)
    link = tmp_path / "link.jpg"
    try:
        os.link(photo, link)
    except OSError as exc:
        pytest.skip(f"this file system has no hard links: {exc}")
    _refused(capsys, printer, [photo], "-o", str(link))


def test_output_named_like_a_photo_not_yet_there_is_refused(capsys, tmp_path, printer):
    gone = tmp_path / "gone.jpg"
    _refused(capsys, printer, [gone], "-o", str(gone))
    if _ignores_case(tmp_path):
        _refused(capsys, printer, [gone], "-o", str(tmp_path / "GONE.JPG"))


def test_page_paths_match_the_files_written(tmp_path):
    for fmt, count, name in [("pdf", 1, "out.pdf"), ("pdf", 3, "out.pdf"), ("png", 1, "out.png"),
                             ("png", 3, "out.png"), ("jpeg", 2, "out.jpg")]:
        path = tmp_path / name
        pages = [Image.new("RGB", (8, 8), "white")] * count
        assert vintage.page_paths(path, fmt, count) == [file for file, _ in article.page_files(pages, path, fmt)]


# --- a page that fails after others were queued ----------------------------------------------


def _failing_submit(monkeypatch, fake, on_call, error):
    real, calls = fake.submit, []

    def submit(*args, **kwargs):
        calls.append(1)
        if len(calls) == on_call:
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(fake, "submit", submit)


@pytest.mark.parametrize("error, code", [(PrinterError("printer went to sleep"), 1), (KeyboardInterrupt(), 130)])
def test_a_failed_page_names_the_jobs_already_queued(capsys, tmp_path, monkeypatch, printer, error, code):
    _failing_submit(monkeypatch, printer.driver, 2, error)
    got, out, err = run(capsys, "-p", "demo", "-q", "-f", "json", "gazette", *map(str, _trip(tmp_path, 4)), "--yes")
    assert got == code and len(printer.jobs()) == 1
    assert "page 2 of 2 may not have been sent" in err and "job(s) 101" in err and "prt jobs cancel" in err
    assert json.loads(out) == {"action": "gazette", "pages": 2, "job_ids": [101], "failed_page": 2}


def test_a_failed_first_page_has_nothing_to_report(capsys, tmp_path, monkeypatch, printer):
    _failing_submit(monkeypatch, printer.driver, 1, PrinterError("printer went to sleep"))
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, _trip(tmp_path, 4)), "--yes")
    assert code == 1 and "printer went to sleep" in err and "may not have been sent" not in err and out == ""
