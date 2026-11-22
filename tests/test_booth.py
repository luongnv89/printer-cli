import io
import json
import time

import pytest
from PIL import Image, ImageChops

from prt import booth
from prt.booth import Story
from prt.drivers.base import PrinterError

A4_150DPI = (1240, 1754)  # 210 × 297 mm at 150 dpi
# Documented limits (skills/photo-booth/SKILL.md, "Write story.json").
LIMITS = {"headline": 120, "story": 2500, "kicker": 40, "caption": 200, "byline": 80}


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    """Pages rendered twice in one test must not differ because midnight passed in between."""
    monkeypatch.setattr(booth, "_today", lambda: "Sunday, November 22, 2026")


STYLES = list(booth.STYLES)


def _render(photo, story=None, style="metro"):
    return booth.compose(photo, story, style)[0]


def _render_jpeg(photo, story=None, style="metro"):
    return booth.to_jpeg(_render(photo, story, style))


def _photo(path, size=(300, 400), colour=(0, 0, 255)):
    Image.new("RGB", size, colour).save(path)
    return path


def _story_file(tmp_path, content):
    path = tmp_path / "story.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


# --- rendering -------------------------------------------------------------------------------

@pytest.mark.parametrize("style", STYLES)
def test_page_is_a4_jpeg_at_150dpi(tmp_path, style):
    page = Image.open(io.BytesIO(_render_jpeg(booth.load_photo(_photo(tmp_path / "p.png")), style=style)))
    assert page.format == "JPEG" and page.size == A4_150DPI
    assert page.info["dpi"] == (150, 150)


def test_metro_keeps_colour_and_daily_is_black_and_white():
    blue = Image.new("RGB", (300, 400), (0, 0, 255))
    metro, daily = _render(blue, style="metro"), _render(blue, style="daily")
    blue_px = sum(n for n, (r, g, b) in metro.getcolors(A4_150DPI[0] * A4_150DPI[1]) if b > 200 and r < 60 and g < 60)
    assert blue_px > 100_000  # the photo is printed in colour
    assert daily.convert("HSV").getchannel("S").getextrema() == (0, 0)  # no colour anywhere on the page


@pytest.mark.parametrize("style", STYLES)
def test_longest_story_stays_inside_the_page_margins(style):
    # 141 paragraphs cannot fit even at the smallest type, so the end of the story is cut.
    # W is the widest capital, so the kicker only fits by shrinking.
    story = Story(headline="H" * LIMITS["headline"], story="A" * 200 + "\n" + "Wide paragraph.\n" * 140,
                  kicker="W" * LIMITS["kicker"], caption="c " * 100, byline="B" * LIMITS["byline"])
    page = _render(Image.new("RGB", (1600, 400), "red"), booth.check_story(story, 2), style)
    w, h = A4_150DPI
    for band in [(0, 0, w, 80), (0, h - 80, w, h), (0, 0, 80, h), (w - 80, 0, w, h)]:
        assert page.crop(band).getextrema() == ((255, 255),) * 3, band


@pytest.mark.parametrize("style", ["metro", "daily", "wanted"])  # wizard's arch frames the top: see below
def test_full_length_portrait_keeps_the_top_of_the_photo(monkeypatch, style):
    # A 9:16 photo whose top 8% (where the head is) is black and the rest white.
    photo = Image.new("RGB", (360, 640), "white")
    photo.paste((0, 0, 0), (0, 0, 360, 51))
    boxes = []
    real = booth._photo
    monkeypatch.setattr(booth, "_photo", lambda page, img, box, mono: boxes.append(box) or real(page, img, box, mono))
    page = _render(photo, style=style).convert("L")
    [(x0, y0, x1, _)] = boxes
    top = page.crop((x0 + 20, y0 + 10, x1 - 20, y0 + 20))  # just inside metro's thick frame
    assert top.getextrema()[1] < 60


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("shape", [(300, 400), (360, 640), (1600, 900)])
def test_default_story_is_never_cut(style, shape):
    page, cut = booth.compose(Image.new("RGB", shape, "blue"), booth.default_story(style), style)
    assert cut == [] and page == _render(Image.new("RGB", shape, "blue"), style=style)


def test_unbroken_story_renders_quickly():
    # A pasted URL or base64 blob is one 2500-character "word": it must wrap in good time, not minutes.
    story = booth.check_story(Story(headline="H", story="x" * LIMITS["story"]), 2)
    start = time.perf_counter()
    for style in STYLES:
        page, _ = booth.compose(Image.new("RGB", (300, 400), "blue"), story, style)
        assert page.size == A4_150DPI
        w, h = A4_150DPI
        for band in [(0, 0, 80, h), (w - 80, 0, w, h), (0, h - 80, w, h)]:
            assert page.crop(band).getextrema() == ((255, 255),) * 3, (style, band)
    assert time.perf_counter() - start < 10


def test_wrap_splits_a_long_word_at_the_widest_prefix_that_fits():
    font = booth._font(24)
    lines = booth._wrap("go " + "x" * 300 + " end", font, 200)
    assert lines[0] == "go" and lines[-1].endswith(" end") and "".join(lines[1:])[:-4] == "x" * 300
    for line in lines[1:-1]:  # every full piece fits, and one more character would not
        assert font.getlength(line) <= 200 < font.getlength(line + "x")


def test_ellipsis_cuts_at_a_whole_word():
    font = booth._font(48)
    width = int(font.getlength("REMARKABLE HEADLINE"))
    assert booth._ellipsize("REMARKABLE HEADLINE", font, width) == "REMARKABLE…"
    lone = booth._ellipsize("W" * 40, font, width)  # a lone word wider than the line is cut mid-word
    assert lone == "W" * (len(lone) - 1) + "…" and len(lone) > 2 and font.getlength(lone) <= width


def test_metro_headline_is_cut_at_a_word_boundary(monkeypatch):
    words = ("Remarkable scenes as local resident smiles broadly for the cameras outside the old town hall "
             "while onlookers cheer")
    blocks = []
    real = booth._text_block
    monkeypatch.setattr(booth, "_text_block", lambda d, x, y, block, *a, **kw: blocks.append(block) or
                        real(d, x, y, block, *a, **kw))
    story = booth.check_story(Story(headline=words, story="S"), 2)
    cut = booth.compose(Image.new("RGB", (300, 400), "blue"), story, "metro")[1]
    headline = blocks[0]
    assert len(headline) == 3 and cut == ["headline"]  # three lines at the smallest size, then cut
    assert headline[-1].endswith("…")
    printed = " ".join(headline)[:-1].split()
    assert printed == words.upper().split()[:len(printed)]  # only whole words, in order


def test_blank_lines_between_paragraphs_add_no_extra_gap():
    def page(text):
        return _render(Image.new("RGB", (300, 400), "blue"), booth.check_story(Story(headline="H", story=text), 2))
    assert page("A\n\nB") == page("A\nB") == page("A\n \n\n\nB")
    assert page("A\nB") != page("A B")


def test_metro_frame_lines_up_with_the_masthead():
    # A very wide photo spans the full content width; its thick frame must not stick out past the margins.
    page = _render(Image.new("RGB", (1600, 400), "red"), style="metro")
    x0, x1 = booth.MARGIN, booth.W - booth.MARGIN
    for x in (x0 - 4, x0 - 1, x1 + 1, x1 + 4):
        assert page.crop((x, 0, x + 1, A4_150DPI[1])).getextrema() == ((255, 255),) * 3, x
    assert page.getpixel((x0, 1000)) == (0, 0, 0)  # the frame reaches the edge on both sides
    assert page.getpixel((x1, 1000)) == (0, 0, 0)


def test_daily_short_story_fills_the_page():
    # The photo (and, once it is at its largest, the type) grows, so little paper is left blank.
    for shape in [(300, 400), (360, 640), (1600, 900)]:
        page = _render(Image.new("RGB", shape, "blue"), style="daily").convert("L")
        story_area = page.crop((booth.MARGIN, 0, booth.W - booth.MARGIN, booth.BOTTOM - 1))
        rows = [y for y in range(story_area.height) if story_area.crop((0, y, story_area.width, y + 1)).getextrema()[0] < 128]
        assert booth.BOTTOM - rows[-1] < 160, shape  # last line of text sits close to the bottom rule


def test_compose_names_the_fields_cut_to_fit():
    portrait = Image.new("RGB", (300, 400), "blue")
    long = Story(headline="Wide headline words " * 6, story="Wide paragraph.\n" * 140, kicker="W" * 40,
                 caption="caption words " * 14, byline="B" * 80)
    assert booth.compose(portrait, booth.check_story(long, 2), "metro")[1] == ["headline", "caption", "story"]
    assert booth.compose(portrait, booth.check_story(long, 2), "daily")[1] == ["story"]
    every = ["kicker", "headline", "caption", "story", "byline"]
    assert booth.compose(portrait, booth.check_story(long, 2), "wizard")[1] == every
    assert booth.compose(portrait, booth.check_story(long, 2), "wanted")[1] == every
    # Text that already ends in "…" is not mistaken for a cut.
    assert booth.compose(portrait, Story(headline="Wait for it…", story="Line one…"), "metro")[1] == []


@pytest.mark.parametrize("style", STYLES)
def test_kicker_fits_the_page_even_when_stand_ins_widen_it(style):
    # Each "€" prints as "EUR", so this 40-character kicker is 120 capitals wide: cut, not overflowing.
    story = booth.check_story(Story(headline="H", story="S", kicker="€" * LIMITS["kicker"]), 2)
    page, cut = booth.compose(Image.new("RGB", (300, 400), "blue"), story, style)
    w, h = A4_150DPI
    for band in [(0, 0, 80, h), (w - 80, 0, w, h)]:
        assert page.crop(band).getextrema() == ((255, 255),) * 3, band
    assert cut == ["kicker"]


@pytest.mark.parametrize("style", STYLES)
def test_kicker_of_only_an_accent_mark_renders(style):
    story = booth.check_story(Story(headline="H", story="S", kicker="¨"), 2)  # prints as a blank
    assert booth.compose(Image.new("RGB", (40, 40), "white"), story, style)[1] == []


@pytest.mark.parametrize("style", STYLES)
def test_empty_optional_fields_render(style):
    page, cut = booth.compose(Image.new("RGB", (40, 40), "white"), Story(headline="Hi", story="Short."), style)
    assert page.size == A4_150DPI and cut == []


# --- themed styles -------------------------------------------------------------------------------

def _share(page, test):
    small = page.reduce(4)  # a quarter-size copy is plenty for colour shares
    return sum(n for n, p in small.getcolors(small.width * small.height) if test(*p)) / (small.width * small.height)


def test_every_style_looks_different():
    grey = Image.new("RGB", (300, 400), (128, 128, 128))  # a neutral photo, so the colours are the style's own
    pages = {style: _render(grey, style=style) for style in STYLES}
    assert len({page.tobytes() for page in pages.values()}) == len(STYLES) == 4

    def navy(r, g, b):
        return b > r + 20 and b > g + 15 and max(r, g, b) < 130

    def gold(r, g, b):
        return r > 170 and g > 130 and b < 120 and r - b > 80

    def parchment(r, g, b):
        return r > 150 and g > 110 and b < 180 and r - b > 40

    def red(r, g, b):
        return r > 180 and g < 60 and b < 80
    signature = {style: {test.__name__: _share(page, test) for test in (navy, gold, parchment, red)}
                 for style, page in pages.items()}
    assert signature["wizard"]["navy"] > 0.25 and signature["wizard"]["gold"] > 0.03  # dark card, gold trim
    assert signature["wanted"]["parchment"] > 0.45  # aged paper
    assert signature["metro"]["red"] > 0.005  # red accents
    assert pages["daily"].convert("HSV").getchannel("S").getextrema() == (0, 0)  # black and white
    for style in ("metro", "daily", "wanted"):
        assert signature[style]["navy"] < 0.001, style  # only the wizard card is dark blue
    for style in ("metro", "daily", "wizard"):
        assert signature[style]["parchment"] < signature["wanted"]["parchment"] / 4, style


@pytest.mark.parametrize("style, frame", [
    ("wizard", ((booth.W - booth.CARD_W) // 2, (booth.H - booth.CARD_H) // 2,
                (booth.W + booth.CARD_W) // 2, (booth.H + booth.CARD_H) // 2)),
    ("wanted", (booth.MARGIN, booth.MARGIN, booth.W - booth.MARGIN, booth.BOTTOM)),
])
@pytest.mark.parametrize("shape", [(300, 400), (1600, 900)])
def test_photo_is_composited_into_the_frame(style, frame, shape):
    magenta = Image.new("RGB", shape, (255, 0, 255))  # a colour no style uses on its own
    page = _render(magenta, style=style)
    r, g, b = page.split()
    hits = ImageChops.multiply(ImageChops.multiply(r.point(lambda v: 255 if v > 180 else 0),
                                                   b.point(lambda v: 255 if v > 160 else 0)),
                               g.point(lambda v: 255 if v < 80 else 0))
    x0, y0, x1, y1 = hits.getbbox()
    assert hits.histogram()[255] > 150_000  # the photo is there, in its colours
    assert frame[0] < x0 and frame[1] < y0 and x1 < frame[2] and y1 < frame[3]  # inside the card or poster
    assert y0 > frame[1] + 100  # below the top label or the WANTED lettering


def test_each_style_has_its_own_default_story():
    assert booth.default_story("metro") is booth.default_story("daily") is booth.DEFAULT_STORY
    themed = [booth.default_story("wizard"), booth.default_story("wanted")]
    for story in themed:
        assert all([story.headline, story.story, story.kicker, story.caption, story.byline])
        assert booth.check_story(story, 2) == story  # valid as written: no blank lines, printable, in limits
        assert story != booth.DEFAULT_STORY
    assert themed[0] != themed[1]
    assert booth.default_story("wanted").kicker.upper() == "DEAD OR ALIVE"
    assert booth.default_story("wanted").caption.upper().startswith("REWARD")
    assert set(booth.DEFAULT_STORIES) == set(booth.STYLES) == set(booth.KINDS)


@pytest.mark.parametrize("style", STYLES)
def test_compose_without_a_story_uses_the_styles_default(style):
    photo = Image.new("RGB", (300, 400), "blue")
    assert booth.compose(photo, None, style) == booth.compose(photo, booth.default_story(style), style)


def test_wizard_arch_keeps_the_top_of_a_full_length_portrait(monkeypatch):
    # As for the other styles, the head (top 8%, black) stays in the picture under the arch.
    photo = Image.new("RGB", (360, 640), "white")
    photo.paste((0, 0, 0), (0, 0, 360, 51))
    boxes = []
    real = booth._photo
    monkeypatch.setattr(booth, "_photo", lambda page, img, box, mono: boxes.append(box) or real(page, img, box, mono))
    page = _render(photo, style="wizard").convert("L")
    [(x0, y0, x1, _)] = boxes
    mid = (x0 + x1) // 2
    assert page.crop((mid - 40, y0 + 10, mid + 40, y0 + 20)).getextrema()[1] < 60


# --- photo -------------------------------------------------------------------------------------

def test_load_photo_decodes_to_rgb_and_applies_exif_rotation(tmp_path):
    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    Image.new("L", (40, 20), 128).save(path, exif=exif)
    img = booth.load_photo(path)
    assert img.mode == "RGB" and img.size == (20, 40)
    assert booth.load_photo(_photo(tmp_path / "plain.png", (40, 20))).size == (40, 20)


def test_load_photo_prints_transparent_parts_as_white_paper(tmp_path):
    cutout = tmp_path / "cutout.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(cutout)
    assert booth.load_photo(cutout).getcolors() == [(400, (255, 255, 255))]
    palette = tmp_path / "palette.png"
    img = Image.new("P", (20, 20), 0)
    img.putpalette([0, 0, 0, 255, 0, 0])
    img.paste(1, (0, 0, 10, 20))
    img.save(palette, transparency=0)  # the black half is see-through, the red half is not
    assert sorted(booth.load_photo(palette).getcolors()) == [(200, (255, 0, 0)), (200, (255, 255, 255))]


def _bad_photo(tmp_path, kind):
    path = tmp_path / "photo.jpg"
    if kind == "text":
        path.write_text("not a picture")
    elif kind == "empty":
        path.write_bytes(b"")
    elif kind == "truncated":
        Image.new("RGB", (200, 200), "red").save(path, quality=95)
        path.write_bytes(path.read_bytes()[:400])
    elif kind == "directory":
        path.mkdir()
    return path


@pytest.mark.parametrize("kind, message", [
    ("text", "cannot read photo photo.jpg: not a supported image format"),
    ("empty", "cannot read photo photo.jpg: not a supported image format"),
    ("truncated", "cannot read photo photo.jpg: "),
    ("missing", "photo not found"),
    ("directory", "photo not found"),
])
def test_load_photo_rejects_unreadable_input(tmp_path, kind, message):
    with pytest.raises(PrinterError) as exc:
        booth.load_photo(_bad_photo(tmp_path, kind))
    assert str(exc.value).startswith(message) and exc.value.exit_code == 3 and exc.value.hint


def test_load_photo_rejects_decompression_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(PrinterError) as exc:
        booth.load_photo(_photo(tmp_path / "big.png", (300, 300)))
    assert "decompression bomb" in str(exc.value) and exc.value.exit_code == 3


# --- story -------------------------------------------------------------------------------------

def test_default_story_is_a_complete_valid_story():
    s = booth.DEFAULT_STORY
    assert all([s.headline, s.story, s.kicker, s.caption, s.byline])
    assert booth.check_story(s, 2) == s


def test_story_file_round_trips_unusual_text(tmp_path):
    data = {"headline": "Café « l'été » <b>“quoted”</b> & co", "story": "Line one…\nLine \"two\" — ok",
            "kicker": "Naïve", "caption": "<script>x</script>", "byline": "By Zoë"}
    assert booth.load_story(_story_file(tmp_path, data)) == Story(**data)


def test_story_file_optional_fields_default_to_empty(tmp_path):
    assert booth.load_story(_story_file(tmp_path, {"headline": "H", "story": "S"})) == Story("H", "S", "", "", "")


@pytest.mark.parametrize("content, message", [
    ("{not json", "is not valid JSON"),
    ("[1, 2]", "must hold a JSON object"),
    ({"headline": "H"}, "is missing story"),
    ({"story": "S"}, "is missing headline"),
    ({"headline": "H", "story": "S", "colour": "red"}, "unknown field(s) in story file: colour"),
    ({"headline": 5, "story": "S"}, "fields must be text: headline"),
    ({"headline": "   ", "story": "S"}, "the story has an empty headline"),
    ({"headline": "H", "story": "S" * 2501}, "story is too long (2501 characters, max 2500)"),
    (b"\xff\xfe\x00bad", "is not UTF-8 text"),
    ("x" * (64 * 1024 + 1), "larger than 64 KiB"),
])
def test_story_file_errors(tmp_path, content, message):
    with pytest.raises(PrinterError) as exc:
        booth.load_story(_story_file(tmp_path, content))
    assert message in str(exc.value) and exc.value.exit_code == 3 and exc.value.hint


def test_story_file_missing(tmp_path):
    with pytest.raises(PrinterError) as exc:
        booth.load_story(tmp_path / "nope.json")
    assert "story file not found" in str(exc.value) and exc.value.exit_code == 3


@pytest.mark.parametrize("field", list(LIMITS))
def test_length_limits(field):
    base = {"headline": "H", "story": "S"}
    at_limit = Story(**{**base, field: "x" * LIMITS[field]})
    assert getattr(booth.check_story(at_limit, 2), field) == "x" * LIMITS[field]
    with pytest.raises(PrinterError) as exc:
        booth.check_story(Story(**{**base, field: "x" * (LIMITS[field] + 1)}), 2)
    assert f"{field} is too long ({LIMITS[field] + 1} characters, max {LIMITS[field]})" in str(exc.value)
    assert exc.value.exit_code == 2


def test_check_story_strips_control_characters_but_keeps_paragraphs():
    s = booth.check_story(Story(headline="Big\x00 news\x1b[1m\ttoday", story="  One\r\n\r\nTwo  "), 2)
    assert s.headline == "Big news[1m today" and s.story == "One\nTwo"  # blank lines collapse


def test_check_story_refuses_text_the_font_cannot_print():
    with pytest.raises(PrinterError) as exc:
        booth.check_story(Story(headline="日本 news", story="S"), 2)
    assert "headline has characters the page font cannot print: 日 (U+65E5), 本 (U+672C)" in str(exc.value)
    assert exc.value.exit_code == 2
    assert booth.check_story(Story(headline="Zoë – Straße", story="S"), 2).headline == "Zoë – Straße"
    assert booth.check_story(Story(headline="Café", story="S"), 2).headline == "Café"  # decomposed é


def test_accents_print_without_their_marks():
    def page(headline):
        return _render(Image.new("RGB", (40, 40), "white"), Story(headline=headline, story="S"), "daily")
    assert page("Café") == page("Café") == page("Cafe")
    assert page("Café") != page("Cafe?")


def test_with_overrides_replaces_only_given_fields():
    base = Story(headline="Old", story="Body", kicker="K")
    assert booth.with_overrides(base, headline="New", story=None, kicker=None, caption="Cap", byline=None) == \
        Story(headline="New", story="Body", kicker="K", caption="Cap")
    with pytest.raises(PrinterError) as exc:
        booth.with_overrides(base, story="")
    assert exc.value.exit_code == 2
