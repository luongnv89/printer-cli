import io
import json

import pytest
from PIL import Image

from prt import booth
from prt.booth import Story
from prt.drivers.base import PrinterError

A4_150DPI = (1240, 1754)  # 210 × 297 mm at 150 dpi
# Documented limits (skills/photo-booth/SKILL.md, "Write story.json").
LIMITS = {"headline": 120, "story": 2500, "kicker": 40, "caption": 200, "byline": 80}


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

@pytest.mark.parametrize("style", ["metro", "daily"])
def test_page_is_a4_jpeg_at_150dpi(tmp_path, style):
    page = Image.open(io.BytesIO(booth.render_jpeg(booth.load_photo(_photo(tmp_path / "p.png")), style=style)))
    assert page.format == "JPEG" and page.size == A4_150DPI
    assert page.info["dpi"] == (150, 150)


def test_metro_keeps_colour_and_daily_is_black_and_white():
    blue = Image.new("RGB", (300, 400), (0, 0, 255))
    metro, daily = booth.render(blue, style="metro"), booth.render(blue, style="daily")
    blue_px = sum(n for n, (r, g, b) in metro.getcolors(A4_150DPI[0] * A4_150DPI[1]) if b > 200 and r < 60 and g < 60)
    assert blue_px > 100_000  # the photo is printed in colour
    assert daily.convert("HSV").getchannel("S").getextrema() == (0, 0)  # no colour anywhere on the page


@pytest.mark.parametrize("style", ["metro", "daily"])
def test_longest_story_stays_inside_the_page_margins(style):
    story = Story(headline="H" * LIMITS["headline"], story=("A" * 200 + " ") + "word " * 450,
                  kicker="K" * LIMITS["kicker"], caption="c " * 100, byline="B" * LIMITS["byline"])
    page = booth.render(Image.new("RGB", (1600, 400), "red"), booth.check_story(story, 2), style)
    w, h = A4_150DPI
    for band in [(0, 0, w, 80), (0, h - 80, w, h), (0, 0, 80, h), (w - 80, 0, w, h)]:
        assert page.crop(band).getextrema() == ((255, 255),) * 3, band


def test_empty_optional_fields_render(tmp_path):
    page = booth.render(Image.new("RGB", (40, 40), "white"), Story(headline="Hi", story="Short."), "daily")
    assert page.size == A4_150DPI


# --- photo -------------------------------------------------------------------------------------

def test_load_photo_decodes_to_rgb_and_applies_exif_rotation(tmp_path):
    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    Image.new("L", (40, 20), 128).save(path, exif=exif)
    img = booth.load_photo(path)
    assert img.mode == "RGB" and img.size == (20, 40)
    assert booth.load_photo(_photo(tmp_path / "plain.png", (40, 20))).size == (40, 20)


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
    assert s.headline == "Big news[1m today" and s.story == "One\n\nTwo"


def test_check_story_refuses_text_the_font_cannot_print():
    with pytest.raises(PrinterError) as exc:
        booth.check_story(Story(headline="日本 news", story="S"), 2)
    assert "headline has characters the page font cannot print: 日 本" in str(exc.value)
    assert exc.value.exit_code == 2
    assert booth.check_story(Story(headline="Zoë – Straße", story="S"), 2).headline == "Zoë – Straße"


def test_with_overrides_replaces_only_given_fields():
    base = Story(headline="Old", story="Body", kicker="K")
    assert booth.with_overrides(base, headline="New", story=None, kicker=None, caption="Cap", byline=None) == \
        Story(headline="New", story="Body", kicker="K", caption="Cap")
    with pytest.raises(PrinterError) as exc:
        booth.with_overrides(base, story="")
    assert exc.value.exit_code == 2
