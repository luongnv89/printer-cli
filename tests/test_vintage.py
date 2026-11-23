import gc
import inspect
import io
import json
import os
import re
import sys
import time
import weakref
from dataclasses import replace
from functools import lru_cache
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from prt import article, booth, fonts, layout, testpage, vintage
from prt.booth import LIMITS, Story
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.transport import ipp

A4_150DPI = (1240, 1754)
PAPER, INK = (244, 236, 216), (26, 26, 26)  # #f4ecd8 and #1a1a1a, the reference gazette's two tones
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


INSET, WHITE = 45, (255, 255, 255)  # the cream stops short of the edge the printer cannot reach


def _blank_sheet():
    sheet = Image.new("RGB", A4_150DPI, WHITE)
    sheet.paste(PAPER, (INSET, INSET, A4_150DPI[0] - INSET, A4_150DPI[1] - INSET))
    return sheet


def _paper_only(page, band):
    """Nothing but the bare sheet in ``band``: white round the edge, cream inside it, no ink."""
    return ImageChops.difference(page.crop(band), _blank_sheet().crop(band)).getbbox() is None


def _longest_story(prefix=""):
    """Every field at its limit, and every field holding characters some bundled face lacks."""
    return booth.check_story(Story(headline=(prefix + "H" * LIMITS["headline"])[:LIMITS["headline"]],
                                   story=prefix + "A" * 200 + "\n" + "Wide paragraph.\n" * 140,
                                   kicker=(prefix + "W" * LIMITS["kicker"])[:LIMITS["kicker"]],
                                   caption=(prefix + "c " * 100)[:LIMITS["caption"]],
                                   byline=(prefix + "B" * LIMITS["byline"])[:LIMITS["byline"]]), 2)


def _overfull_story():
    """Every field too wide for its place, so each one ends in "…" drawn in its own face."""
    return booth.check_story(Story(headline=(DIVERGENT + "W" * LIMITS["headline"])[:LIMITS["headline"]],
                                   story=DIVERGENT + "A" * 200 + "\n" + "Wide paragraph.\n" * 140,
                                   kicker=("Ǆ " + "㎑" * LIMITS["kicker"])[:LIMITS["kicker"]],  # ㎑ prints as "kHz"
                                   caption=(DIVERGENT + "W" * LIMITS["caption"])[:LIMITS["caption"]],
                                   byline=(DIVERGENT + "W" * LIMITS["byline"])[:LIMITS["byline"]]), 2)


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
    assert all(c in getattr(story, name) for c in DIVERGENT.split() for name in booth.FIELDS)
    photos = [Image.new("RGB", (400 + 50 * k, 300), (20 * k, 80, 160)) for k in range(n)]
    pages, _, _ = vintage.compose_pages(photos, story)
    assert len(pages) == len(vintage.plan_pages(n)) and strict_drawing.fallbacks == []
    assert any("DZ" in text for text in strict_drawing.drawn)  # a divergent letter was replaced, not dropped


def test_text_cut_to_fit_ends_in_a_glyph_every_face_has(strict_drawing):
    _, _, cut = vintage.compose_pages([Image.new("RGB", (400, 300), "grey")] * 3, _overfull_story())
    assert sorted(cut) == sorted(booth.FIELDS) and strict_drawing.fallbacks == []
    assert sum(text.endswith("…") for text in strict_drawing.drawn) >= len(booth.FIELDS)


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


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("shape", [(900, 1200), (900, 1600), (600, 1200), (2400, 800)])  # 3:4, 9:16, 1:2, 3:1
@pytest.mark.parametrize("story", ["default", "longest"])
def test_the_lead_photo_is_printed_whole(n, shape, story):
    lead = vintage._downscale(Image.new("RGB", shape, "grey"), vintage.LEAD_LIMIT)
    photos = [lead] + [Image.new("RGB", (400, 300), "grey")] * (n - 1)
    _, placements, _ = vintage.compose_pages(photos, None if story == "default" else _longest_story())
    k, (x0, y0, x1, y1) = placements[0][0]
    shown, photo = (x1 - x0) / (y1 - y0), lead.width / lead.height
    assert k == 0 and min(shown / photo, photo / shown) >= 0.9, (shape, (x0, y0, x1, y1))


@pytest.mark.parametrize("n", [4, 5, 6, 8, 9])  # page 2 holds 1, 2, 3, 5 and 6 plates
def test_plates_in_a_row_share_a_foot_and_a_lone_last_plate_is_centred(n):
    shapes = [(300, 400), (400, 300), (500, 500)]
    pages, placements, _ = vintage.compose_pages([Image.new("RGB", shapes[k % 3], "grey") for k in range(n)])
    boxes = [box for _, box in placements[1]]
    for left, right in zip(boxes[0::2], boxes[1::2]):
        assert left[3] == right[3]  # one foot, so the two "Fig." labels sit on one line
    if len(boxes) % 2:
        x0, _, x1, _ = boxes[-1]
        assert abs((x0 + x1) / 2 - A4_150DPI[0] / 2) <= 1
        assert all(lo == hi for lo, hi in pages[1].crop(boxes[-1]).getextrema())  # no rule runs through it
    assert all(box[3] - box[1] <= vintage.PLATE_LIMIT for box in boxes)  # a plate is never blown up past its size


@pytest.mark.parametrize("caption", ["A caption.", ""])
def test_the_lead_photo_is_fig_1(strict_drawing, caption):
    story = Story(headline="Headline", story="Story.", caption=caption)
    vintage.compose_pages([Image.new("RGB", (400, 300), "grey")] * 3, story)
    assert any(text.startswith("Fig. 1") for text in strict_drawing.drawn)


# --- the page --------------------------------------------------------------------------------


def test_default_story_passes_the_story_check():
    assert booth.check_story(vintage.DEFAULT_GAZETTE_STORY, 2) == vintage.DEFAULT_GAZETTE_STORY


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("shape", [(800, 1200), (1200, 800), (1000, 1000)])
def test_default_story_is_never_cut(n, shape):
    _, _, cut = vintage.compose_pages([Image.new("RGB", shape, "grey")] * n, vintage.DEFAULT_GAZETTE_STORY)
    assert cut == {}


def test_too_long_text_is_cut_and_named():
    _, _, cut = vintage.compose_pages([Image.new("RGB", (400, 300), "grey")] * 3, _longest_story())
    assert list(cut) == ["headline", "story"]


@pytest.mark.parametrize("landscape", [False, True])
def test_cut_fields_report_how_much_was_printed(landscape):
    photos = [Image.new("RGB", (400, 300), "grey")] * 3
    story = booth.check_story(Story(headline="W" * LIMITS["headline"], story="Wide paragraph.\n" * 140,
                                    kicker="W" * LIMITS["kicker"], caption="Wide caption words " * 10,
                                    byline="W" * LIMITS["byline"]), 2)
    cut = vintage.compose_pages(photos, story, landscape=landscape)[2]
    assert {"headline", "story"} <= set(cut) and len(cut) >= 3
    for name, fit in cut.items():
        assert fit.given == len(getattr(story, name)) and 0 < fit.max_fit < fit.given, name
        shorter = replace(story, **{name: getattr(story, name)[:fit.max_fit]})
        assert name not in vintage.compose_pages(photos, shorter, landscape=landscape)[2], name


def test_the_figure_number_is_not_counted_as_caption():
    caption = ("caption words " * 14).strip()
    font, size, lines, cut = vintage._figure_caption(3, caption, 400)
    printed = " ".join(lines).removesuffix("…").rstrip()
    assert printed.startswith("Fig. 3. ") and cut.max_fit == len(printed) - len("Fig. 3. ")
    assert cut.given == len(caption)
    assert vintage._figure_caption(3, "Short", 400)[3] is None


@pytest.mark.parametrize("landscape", [False, True])
def test_cut_counts_the_characters_given_not_their_capitals_or_stand_ins(landscape):
    # "ß" prints as "SS" in the capitals of the headline; a separate accent mark prints as nothing.
    photos = [Image.new("RGB", (400, 300), "grey")] * 3
    story = booth.check_story(Story(headline="Straßeß " * 15, byline="€ß" * 40,
                                    story=("Die Straße kostet € 5. Cafe\u0301. " * 90)[:2500]), 2)
    cut = vintage.compose_pages(photos, story, landscape=landscape)[2]
    assert {"headline", "story"} <= set(cut)
    for name, fit in cut.items():
        assert fit.given == len(getattr(story, name)) and 0 < fit.max_fit <= fit.given, name
        shorter = replace(story, **{name: getattr(story, name)[:fit.max_fit]})
        assert name not in vintage.compose_pages(photos, shorter, landscape=landscape)[2], name


def test_small_caps_cut_counts_the_letters_printed():
    runs, _, cut = vintage._fit_caps("W" * 120, "body-bold", 24, 16, 1060)
    printed = "".join(text for _, text in runs).removesuffix("…")
    assert cut == vintage.Cut(len(printed), 120)


def test_pages_are_cream_paper_with_dark_ink_rules():
    pages, _, _ = vintage.compose_pages([Image.new("RGB", (400, 300), "white")] * 4)
    w, h = A4_150DPI
    for page in pages:
        assert page.size == A4_150DPI and page.getpixel((5, 5)) == WHITE  # no ink spent where the printer can't print
        for x, y in [(INSET, INSET), (w - INSET - 1, INSET), (INSET, h - INSET - 1), (w - INSET - 1, h - INSET - 1)]:
            assert page.getpixel((x, y)) == PAPER, (x, y)  # cream inside the inset
            for dx, dy in [(-1, 0), (0, -1), (1, 0), (0, 1)]:
                px, py = x + dx, y + dy
                if not (INSET <= px < w - INSET and INSET <= py < h - INSET):
                    assert page.getpixel((px, py)) == WHITE, (px, py)  # white outside it
        assert ImageChops.difference(page.crop((0, 0, w, INSET)), Image.new("RGB", (w, INSET), WHITE)).getbbox() is None
        assert set(page.crop((0, 0, 1240, 1754)).getdata()) >= {PAPER, INK}
    assert pages[0].getpixel((620, 90)) == INK  # the hairline above the masthead
    closing = [pages[1].getpixel((620, y)) for y in range(A4_150DPI[1] - 110, A4_150DPI[1] - 90)]
    assert closing.count(INK) >= 4  # the double rule closing a plate page: two lines of ink


def test_the_longest_story_stays_inside_the_margins():
    pages, _, _ = vintage.compose_pages([Image.new("RGB", (1600, 400), "red")] * 10, _longest_story())
    w, h = A4_150DPI
    for page in pages:
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


def test_small_caps_shrink_then_cut_to_fit():
    assert vintage._fit_caps("W" * 40, "body-bold", 24, 16, 1060)[1:] == (24, None)
    runs, size, cut = vintage._fit_caps("㎑" * 40, "body-bold", 24, 16, 1060)  # 120 letters once spelt out
    assert size == 16 and cut and runs[-1][1].endswith("…") and vintage._caps_width(runs) <= 1060


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

    def compose(photos, story=None, **options):
        sizes.extend(photo.size for photo in photos)
        return real_compose(photos, story, **options)

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
    assert json.loads(out) == {"action": "gazette", "photos": 1, "pages": 1, "fits": True, "cut": {}, "job_ids": [101]}
    assert "1 page queued as job 101" in err


def test_several_photos_print_one_job_per_page(capsys, tmp_path, printer):
    photos = _trip(tmp_path, 4)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, photos), "--yes")
    expected = _expected_pages(photos)
    assert code == 0 and len(expected) == 2 and printer.jobs() == expected
    assert json.loads(out) == {"action": "gazette", "photos": 4, "pages": 2, "fits": True, "cut": {},
                               "job_ids": [101, 102]}
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
    assert json.loads(out) == {"action": "gazette", "photos": 4, "pages": 2, "fits": True, "cut": {},
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


def test_story_file_dash_reads_the_story_from_stdin(capsys, tmp_path, printer, stdin):
    photo = _photo(tmp_path)
    stdin(json.dumps({"headline": "Piped In", "story": "From stdin.", "byline": "By Zoë"}).encode())
    code, _, _ = run(capsys, "-p", "demo", "gazette", str(photo), "--story-file", "-", "--yes")
    assert code == 0 and printer.jobs() == _expected_pages([photo], Story("Piped In", "From stdin.", byline="By Zoë"))


def test_bad_story_on_stdin_prints_nothing(capsys, tmp_path, printer, stdin):
    stdin(b'{"headline": "H"}')
    code, out, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)), "--story-file", "-", "--yes")
    assert code == 3 and out == "" and "story on stdin is missing story" in err
    assert printer.made == [] and printer.jobs() == []


def test_warns_when_text_is_cut_to_fit(capsys, tmp_path, printer):
    code, _, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)), "--headline", "W" * 120, "--yes")
    assert code == 0 and "headline cut to fit the front page" in err


def test_json_reports_each_cut_field(capsys, tmp_path, printer):
    headline = "W" * LIMITS["headline"]
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "gazette", str(_photo(tmp_path)), "--headline", headline,
                         "--yes")
    report = json.loads(out)
    assert code == 0 and report["job_ids"] == [101] and report["fits"] is False and list(report["cut"]) == ["headline"]
    fit = report["cut"]["headline"]
    assert fit["given"] == len(headline) and 0 < fit["max_fit"] < fit["given"]
    code, out, _ = run(capsys, "-f", "json", "gazette", str(_photo(tmp_path)), "--headline", headline[:fit["max_fit"]],
                       "-o", str(tmp_path / "page.png"))
    assert code == 0 and json.loads(out)["fits"] is True and json.loads(out)["cut"] == {}


def test_warning_names_the_cut_fields_without_json(capsys, tmp_path, printer):
    code, out, err = run(capsys, "-p", "demo", "gazette", str(_photo(tmp_path)), "--headline", "W" * 120, "--yes")
    assert code == 0 and "headline cut to fit the front page" in err and "max_fit" not in out + err


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


# --- landscape -------------------------------------------------------------------------------

A4_LANDSCAPE = (1754, 1240)  # A4 at 150 dpi on its side
UPRIGHT = vintage.Sheet(1240, 1754, 90, 1060, 1664, 45, masthead_max=132, headline_lines=3, plate_cols=2)


def _bare(page, band):
    """Like ``_paper_only``, for a page of any size: white round the edge, cream inside it, no ink in ``band``."""
    sheet = Image.new("RGB", page.size, WHITE)
    sheet.paste(PAPER, (INSET, INSET, page.width - INSET, page.height - INSET))
    return ImageChops.difference(page.crop(band), sheet.crop(band)).getbbox() is None


def _margins(size):
    w, h = size
    return [(0, 0, w, 80), (0, h - 80, w, h), (0, 0, 80, h), (w - 80, 0, w, h)]


@pytest.mark.parametrize("n", [1, 4])
def test_without_landscape_the_pages_are_the_upright_ones(monkeypatch, n):
    photos = [Image.new("RGB", (400 + 60 * k, 300), (40 + 20 * k,) * 3) for k in range(n)]
    default = vintage.compose_pages(photos)
    upright = vintage.compose_pages(photos, landscape=False)
    monkeypatch.setattr(vintage, "PORTRAIT", UPRIGHT)  # the upright sheet spelt out: A4 at 150 dpi, as before
    explicit = vintage.compose_pages(photos)
    assert [page.size for page in default[0]] == [A4_150DPI] * len(vintage.plan_pages(n))
    for other in (upright, explicit):
        assert [page.tobytes() for page in other[0]] == [page.tobytes() for page in default[0]]
        assert other[1:] == default[1:]


@pytest.mark.parametrize("n", [1, 3, 4, 10])
def test_landscape_pages_are_a4_on_its_side_in_cream_with_a_white_rim(n):
    pages, _, _ = vintage.compose_pages([Image.new("RGB", (400, 300), "white")] * n, landscape=True)
    w, h = A4_LANDSCAPE
    assert len(pages) == len(vintage.plan_pages(n))
    for page in pages:
        assert page.size == A4_LANDSCAPE
        for x, y in [(INSET, INSET), (w - INSET - 1, INSET), (INSET, h - INSET - 1), (w - INSET - 1, h - INSET - 1)]:
            assert page.getpixel((x, y)) == PAPER, (x, y)
            assert page.getpixel((x - 1 if x == INSET else x + 1, y)) == WHITE, (x, y)
            assert page.getpixel((x, y - 1 if y == INSET else y + 1)) == WHITE, (x, y)
        assert all(_bare(page, band) for band in [(0, 0, w, INSET), (0, h - INSET, w, h)])
        assert {PAPER, INK} <= {colour for _, colour in page.getcolors(maxcolors=1 << 16)}
    assert pages[0].getpixel((w // 2, 90)) == INK  # the hairline above the masthead runs across the long side


@pytest.mark.parametrize("n", [1, 2, 3, 4, 10])
def test_landscape_prints_every_photo_in_its_own_box_on_its_planned_page(n):
    shapes = [(300, 400), (400, 300), (500, 500)]
    greys = [40 + 16 * k for k in range(n)]
    photos = [Image.new("RGB", shapes[k % 3], (g, g, g)) for k, g in enumerate(greys)]
    pages, placements, _ = vintage.compose_pages(photos, landscape=True)
    plan = vintage.plan_pages(n)
    assert len(pages) == len(plan) == len(placements)
    assert sorted(k for page in placements for k, _ in page) == list(range(n))
    assert [[k for k, _ in page] for page in placements] == plan
    for page, placed in zip(pages, placements):
        for k, box in placed:
            expected = vintage._treat(Image.new("RGB", (4, 4), (greys[k],) * 3)).getpixel((0, 0))
            mean = ImageStat.Stat(page.crop(box)).mean
            assert all(abs(m - e) <= 2 for m, e in zip(mean, expected)), (k, mean, expected)


@pytest.mark.parametrize("n", [1, 10])
def test_landscape_words_are_drawn_in_a_bundled_font_that_has_their_glyphs(strict_drawing, n):
    photos = [Image.new("RGB", (400 + 50 * k, 300), (20 * k, 80, 160)) for k in range(n)]
    pages, _, _ = vintage.compose_pages(photos, _longest_story(DIVERGENT), landscape=True)
    assert len(pages) == len(vintage.plan_pages(n)) and strict_drawing.fallbacks == []
    assert any("DZ" in text for text in strict_drawing.drawn)


def test_the_longest_story_stays_inside_the_landscape_margins():
    pages, _, cut = vintage.compose_pages([Image.new("RGB", (1600, 400), "red")] * 10, _longest_story(),
                                          landscape=True)
    assert "story" in cut
    for page in pages:
        assert all(_bare(page, band) for band in _margins(A4_LANDSCAPE))


def test_unbroken_story_renders_quickly_inside_the_landscape_margins():
    story = booth.check_story(Story(headline="H", story="x" * LIMITS["story"]), 2)
    start = time.perf_counter()
    for n in (1, 3):
        pages, _, _ = vintage.compose_pages([Image.new("RGB", (300, 400), "blue")] * n, story, landscape=True)
        assert all(_bare(pages[0], band) for band in _margins(A4_LANDSCAPE)), n
    assert time.perf_counter() - start < 10


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("shape", [(800, 1200), (1200, 800), (1000, 1000)])
def test_default_story_is_never_cut_in_landscape(n, shape):
    _, _, cut = vintage.compose_pages([Image.new("RGB", shape, "grey")] * n, vintage.DEFAULT_GAZETTE_STORY,
                                      landscape=True)
    assert cut == {}


@pytest.mark.parametrize("n", [1, 3])
@pytest.mark.parametrize("shape", [(900, 1200), (900, 1600), (600, 1200), (2400, 800), (1600, 1067)])
@pytest.mark.parametrize("story", ["default", "longest"])
def test_the_landscape_lead_is_printed_whole_and_no_wider_than_its_limit(n, shape, story):
    lead = vintage._downscale(Image.new("RGB", shape, "grey"), vintage.LEAD_LIMIT)
    photos = [lead] + [Image.new("RGB", (400, 300), "grey")] * (n - 1)
    _, placements, _ = vintage.compose_pages(photos, None if story == "default" else _longest_story(),
                                             landscape=True)
    k, (x0, y0, x1, y1) = placements[0][0]
    shown, photo = (x1 - x0) / (y1 - y0), lead.width / lead.height
    assert k == 0 and min(shown / photo, photo / shown) >= 0.9, (shape, (x0, y0, x1, y1))
    assert x1 - x0 <= LEAD_LIMIT and y1 - y0 <= LEAD_LIMIT  # never printed larger than it was kept
    assert x0 < A4_LANDSCAPE[0] // 2  # the lead stands on the left, beside the story


def test_landscape_front_page_sets_the_lead_beside_the_story_and_the_plates_under_the_story():
    photos = [Image.new("RGB", (1200, 800), (60, 60, 60))] + [Image.new("RGB", (400, 300), (120, 120, 120))] * 2
    _, placements, _ = vintage.compose_pages(photos, landscape=True)
    (_, lead), (_, fig2), (_, fig3) = placements[0]
    assert fig2[0] > lead[2] and fig3[0] > lead[2]  # both plates right of the lead
    assert fig2[3] == fig3[3] and fig2[1] > lead[1] + 200  # one foot, below the story's first lines


def test_a_lone_landscape_front_plate_is_centred_under_the_story():
    photos = [Image.new("RGB", (1200, 800), (60, 60, 60)), Image.new("RGB", (400, 300), (120, 120, 120))]
    pages, placements, _ = vintage.compose_pages(photos, landscape=True)
    (_, lead), (_, plate) = placements[0]
    assert plate[0] > lead[2] and abs((plate[0] + plate[2]) / 2 - (lead[2] + A4_LANDSCAPE[0] - 90) / 2) <= 30
    assert all(lo == hi for lo, hi in pages[0].crop(plate).getextrema())  # no rule runs through it


@pytest.mark.parametrize("lead, columns", [((1200, 800), 2), ((800, 1200), 3)])
def test_a_narrow_landscape_lead_gives_the_story_a_third_column(monkeypatch, lead, columns):
    asked, real = [], vintage.flow_columns
    monkeypatch.setattr(vintage, "flow_columns", lambda *args, **kw: asked.append(args[4]) or real(*args, **kw))
    vintage.compose_pages([Image.new("RGB", lead, "grey")] * 3, landscape=True)
    assert set(asked) == {columns}


@pytest.mark.parametrize("n, rows", [(4, [1]), (5, [2]), (6, [3]), (8, [3, 2]), (9, [3, 3])])
def test_landscape_plates_stand_three_abreast_and_a_short_last_row_is_centred(n, rows):
    shapes = [(300, 400), (400, 300), (500, 500)]
    pages, placements, _ = vintage.compose_pages([Image.new("RGB", shapes[k % 3], "grey") for k in range(n)],
                                                 landscape=True)
    boxes = [box for _, box in placements[1]]
    feet = sorted({box[3] for box in boxes})
    assert [sum(box[3] == foot for box in boxes) for foot in feet] == rows  # one foot a row: labels line up
    last = [box for box in boxes if box[3] == feet[-1]]
    if len(last) < 3:  # each plate is centred in its cell, so the row's centres balance about the middle
        assert abs(sum(b[0] + b[2] for b in last) / (2 * len(last)) - A4_LANDSCAPE[0] / 2) <= 1
    for box in boxes:
        assert all(lo == hi for lo, hi in pages[1].crop(box).getextrema())  # no rule runs through a plate
        assert box[3] - box[1] <= vintage.PLATE_LIMIT


def test_a_landscape_page_is_turned_counter_clockwise_for_the_printer():
    page = Image.new("RGB", A4_LANDSCAPE, "white")
    page.paste(INK, (0, 0, A4_LANDSCAPE[0], 10))  # a band along the head of the page
    turned = vintage.for_printer(page)
    assert turned.size == A4_150DPI
    assert turned.getpixel((5, 5)) == INK and turned.getpixel((5, A4_150DPI[1] - 5)) == INK  # up the left edge
    assert turned.getpixel((A4_150DPI[0] - 5, 5)) == WHITE
    upright = Image.new("RGB", A4_150DPI, "white")
    assert vintage.for_printer(upright) is upright


def test_gazette_help_offers_landscape(capsys):
    code, out, _ = run(capsys, "gazette", "--help")
    assert code == 0 and "--landscape" in out and "--no-landscape" not in out


def test_landscape_prints_each_page_turned_upright_and_keeps_saved_pages_landscape(capsys, tmp_path, printer):
    photos, copy = _trip(tmp_path, 4), tmp_path / "copy.png"
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "gazette", *map(str, photos), "--landscape", "--save",
                       str(copy), "--yes")
    pages = vintage.compose_pages(vintage.load_photos(photos), landscape=True)[0]
    assert code == 0 and json.loads(out)["job_ids"] == [101, 102]
    assert printer.jobs() == [booth.to_jpeg(page.transpose(Image.Transpose.ROTATE_90)) for page in pages]
    sheets = [Image.open(io.BytesIO(job)).convert("L") for job in printer.jobs()]
    assert [sheet.size for sheet in sheets] == [A4_150DPI] * 2
    # The hairline over the front page's masthead (y = 90 on the page) lies up the sheet's left edge, at x = 90.
    assert ImageStat.Stat(sheets[0].crop((89, 200, 92, 1500))).mean[0] < 100
    assert ImageStat.Stat(sheets[0].crop((1148, 200, 1151, 1500))).mean[0] > 150
    assert [Image.open(tmp_path / f"copy-{k}.png").size for k in (1, 2)] == [A4_LANDSCAPE] * 2


def test_landscape_preview_keeps_pages_on_their_side(capsys, tmp_path, printer):
    photos = _trip(tmp_path, 4)
    code, _, _ = run(capsys, "-p", "demo", "gazette", *map(str, photos), "--landscape", "-o",
                     str(tmp_path / "page.png"))
    assert code == 0 and printer.made == []
    assert [Image.open(tmp_path / f"page-{k}.png").size for k in (1, 2)] == [A4_LANDSCAPE] * 2
    code, _, _ = run(capsys, "-p", "demo", "gazette", *map(str, photos), "--landscape", "-o",
                     str(tmp_path / "page.pdf"))
    boxes = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", (tmp_path / "page.pdf").read_bytes())
    assert code == 0 and len(boxes) == 2 and all(float(w) > float(h) for w, h in boxes), boxes
