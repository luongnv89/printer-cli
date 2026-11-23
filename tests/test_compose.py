"""`prt compose`: a page file of blocks (heading, text, image, QR code, columns, rule), its schema and its checks."""

import json
import sys
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import catalog, compose
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.transport import ipp

from conftest import ROOT, without_estimate


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``made`` counts how often a driver was created at all."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    jobs = lambda: [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=jobs)


def _photo(folder, name="photo.jpg", size=(300, 400), colour=(0, 0, 255)):
    path = folder / name
    Image.new("RGB", size, colour).save(path)
    return path


def _page_file(folder, page, name="page.json"):
    path = folder / name
    path.write_text(json.dumps(page))
    return path


EVERY_BLOCK = {"title": "Everything", "blocks": [
    {"type": "heading", "text": "Title", "level": 1, "align": "center"},
    {"type": "text", "text": "One.\nTwo.", "size": "small", "align": "right"},
    {"type": "image", "path": "photo.jpg", "height": 50, "caption": "A photo", "align": "left"},
    {"type": "qr", "data": "https://example.com/", "size": 30, "caption": "Scan me", "align": "right"},
    {"type": "rule", "weight": "thick"},
    {"type": "columns", "columns": [
        [{"type": "heading", "text": "Left", "level": 3}, {"type": "text", "text": "Left text."}],
        [{"type": "image", "path": "photo.jpg"}, {"type": "rule"}],
        [{"type": "qr", "data": "WIFI:S:home;T:WPA;P:secret;;"}]]},
]}


def _parse(page, folder=None):
    return compose.parse(json.dumps(page).encode(), "page.json", folder)


# --- the schema --------------------------------------------------------------------------------------


def test_schema_page_is_a_valid_json_schema(capsys):
    code, out, err = run(capsys, "schema", "page")
    assert code == 0 and err == ""
    schema = json.loads(out)
    Draft202012Validator.check_schema(schema)
    assert schema == catalog.schema("page") == compose.page_schema()
    assert schema["required"] == ["blocks"] and schema["additionalProperties"] is False
    kinds = [b["properties"]["type"]["const"] for b in schema["properties"]["blocks"]["items"]["oneOf"]]
    assert kinds == ["heading", "text", "image", "qr", "columns", "rule"]
    for example in schema["examples"]:
        assert Draft202012Validator(schema).is_valid(example) and compose.problem(example) is None
    assert "page" in catalog.schema_names()


def test_every_keyword_of_the_schema_is_checked():
    def keywords(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties":
                    for sub in value.values():
                        yield from keywords(sub)
                    continue
                yield key
                if key in ("items",):
                    yield from keywords(value)
                elif key == "oneOf":
                    for sub in value:
                        yield from keywords(sub)
    assert set(keywords(compose.page_schema())) <= compose.KEYWORDS


def test_a_keyword_the_checker_does_not_know_fails_loudly(monkeypatch):
    schema = compose.page_schema()
    schema["properties"]["title"]["format"] = "email"
    monkeypatch.setattr(compose, "page_schema", lambda: schema)
    with pytest.raises(ValueError, match="format"):
        compose.problem({"title": "x", "blocks": [{"type": "rule"}]})


def _pages():
    text = {"type": "text", "text": "Hi."}
    return [
        {"blocks": [text]},
        EVERY_BLOCK,
        {"title": "", "blocks": [{"type": "rule"}]},
        {"blocks": [{"type": "heading", "text": "x" * compose.MAX_HEADING}]},
        {"blocks": [{"type": "heading", "text": "x" * (compose.MAX_HEADING + 1)}]},
        {"blocks": [{"type": "heading", "text": "   "}]},
        {"blocks": [{"type": "heading", "text": ""}]},
        {"blocks": [{"type": "heading", "text": "H", "level": 4}]},
        {"blocks": [{"type": "heading", "text": "H", "level": 2.0}]},
        {"blocks": [{"type": "heading", "text": "H", "level": True}]},
        {"blocks": [{"type": "heading", "text": "H", "level": "1"}]},
        {"blocks": [{"type": "text", "text": "T", "size": "huge"}]},
        {"blocks": [{"type": "text", "text": "T", "align": "justify"}]},
        {"blocks": [{"type": "text", "text": "\n\n"}]},
        {"blocks": [{"type": "text", "text": 5}]},
        {"blocks": [{"type": "text"}]},
        {"blocks": [{"type": "text", "text": "T", "colour": "red"}]},
        {"blocks": [{"type": "video", "src": "x"}]},
        {"blocks": [{"text": "no type"}]},
        {"blocks": [{"type": None}]},
        {"blocks": ["just text"]},
        {"blocks": []},
        {"blocks": [text] * compose.MAX_BLOCKS},
        {"blocks": [text] * (compose.MAX_BLOCKS + 1)},
        {"blocks": {"type": "text", "text": "T"}},
        {"title": 3, "blocks": [text]},
        {"title": "x" * (compose.MAX_TITLE + 1), "blocks": [text]},
        {"blocks": [text], "margin": 10},
        {},
        {"blocks": [{"type": "image", "path": "a.jpg", "height": 10}]},
        {"blocks": [{"type": "image", "path": "a.jpg", "height": 9.5}]},
        {"blocks": [{"type": "image", "path": "a.jpg", "height": 261}]},
        {"blocks": [{"type": "image", "path": "a.jpg", "height": "50"}]},
        {"blocks": [{"type": "image", "path": ""}]},
        {"blocks": [{"type": "image", "path": "https://example.com/a.jpg"}]},
        {"blocks": [{"type": "image", "path": "file:///etc/passwd"}]},
        {"blocks": [{"type": "image", "path": "C:\\photos\\a.jpg"}]},
        {"blocks": [{"type": "image", "path": "a.jpg", "caption": ""}]},
        {"blocks": [{"type": "image"}]},
        {"blocks": [{"type": "qr", "data": "x" * compose.MAX_QR_DATA, "size": 150}]},
        {"blocks": [{"type": "qr", "data": "x" * (compose.MAX_QR_DATA + 1)}]},
        {"blocks": [{"type": "qr", "data": ""}]},
        {"blocks": [{"type": "qr", "data": " "}]},
        {"blocks": [{"type": "qr", "data": "d", "size": 14}]},
        {"blocks": [{"type": "qr", "data": "d", "size": False}]},
        {"blocks": [{"type": "rule", "weight": "medium"}]},
        {"blocks": [{"type": "columns", "columns": [[text], []]}]},
        {"blocks": [{"type": "columns", "columns": [[text]]}]},
        {"blocks": [{"type": "columns", "columns": [[text]] * 5}]},
        {"blocks": [{"type": "columns", "columns": [[text], [{"type": "columns", "columns": [[], []]}]]}]},
        {"blocks": [{"type": "columns", "columns": [[text], text]}]},
        {"blocks": [{"type": "columns", "columns": [[text] * (compose.MAX_CELL_BLOCKS + 1), []]}]},
        {"blocks": [{"type": "columns"}]},
        [],
        "a page",
        None,
    ]


@pytest.mark.parametrize("page", _pages())
def test_checker_agrees_with_the_schema(page):
    valid = Draft202012Validator(compose.page_schema()).is_valid(page)
    assert (compose.problem(page) is None) == valid


@pytest.mark.parametrize("page, message", [
    ({"blocks": [{"type": "text"}]}, "blocks[0].text is missing"),
    ({"blocks": [{"type": "video"}]}, 'blocks[0] has an unknown type "video" (use one of heading, text, image'),
    ({"blocks": [{"type": "heading", "text": "H", "level": 4}]}, "blocks[0].level must be one of 1, 2, 3, not 4"),
    ({"blocks": [{"type": "text", "text": "T", "font": "x"}]}, 'blocks[0] has unknown field "font"'),
    ({"blocks": [{"type": "image", "path": "http://x/a.jpg"}]}, "blocks[0].path is a web link"),
    ({"blocks": [{"type": "columns", "columns": [[{"type": "text", "text": " "}], []]}]},
     "blocks[0].columns[0][0].text has only spaces in it"),
    ({"blocks": [{"type": "qr", "data": "d", "size": 200}]}, "blocks[0].size is 200, more than 150"),
    ({"blocks": []}, "blocks needs at least 1 item"),
    ({"pages": []}, "blocks is missing"),
    ([], "the page must be a JSON object"),
])
def test_checker_says_where_the_page_file_is_wrong(page, message):
    with pytest.raises(PrinterError) as exc:
        _parse(page)
    assert exc.value.exit_code == 3 and exc.value.kind == "invalid_input"
    assert message in str(exc.value) and str(exc.value).startswith("page.json: ")
    assert "prt schema page" in exc.value.hint


# --- reading a page file -------------------------------------------------------------------------------


def test_parse_reads_images_from_the_page_folder_and_cleans_text(tmp_path):
    _photo(tmp_path)
    page = _parse({"blocks": [{"type": "heading", "text": "  Café\tcrème  "},
                              {"type": "image", "path": "photo.jpg"}, {"type": "image", "path": "photo.jpg"}]},
                  tmp_path)
    assert page.title == "Café crème" and page.blocks[0]["text"] == "Café crème"
    assert page.inputs == [tmp_path / "photo.jpg"]  # read once, however often it is used
    assert page.blocks[1]["image"] is page.blocks[2]["image"] and page.blocks[1]["image"].mode == "RGB"
    assert _parse({"title": " My  page ", "blocks": [{"type": "rule"}]}).title == "My page"
    assert _parse({"blocks": [{"type": "rule"}]}).title == "page"


def test_absolute_image_paths_work(tmp_path):
    photo = _photo(tmp_path)
    page = _parse({"blocks": [{"type": "image", "path": str(photo)}]}, tmp_path / "elsewhere")
    assert page.inputs == [photo]


@pytest.mark.parametrize("text", ["Hi 🎉", "你好"])
@pytest.mark.parametrize("field", ["heading", "text", "caption"])
def test_text_the_font_cannot_print_is_refused(tmp_path, field, text):
    block = {"type": "qr", "data": "d", "caption": text} if field == "caption" else {"type": field, "text": text}
    with pytest.raises(PrinterError, match="characters the page font cannot print") as exc:
        _parse({"blocks": [block]})
    assert exc.value.exit_code == 3


def test_qr_data_may_be_any_text():
    assert _parse({"blocks": [{"type": "qr", "data": "你好 🎉"}]}).blocks[0]["data"] == "你好 🎉"


def _refused(tmp_path, block, match):
    with pytest.raises(PrinterError, match=match) as exc:
        _parse({"blocks": [block]}, tmp_path)
    assert exc.value.exit_code == 3
    return exc.value


def test_missing_image_is_refused(tmp_path):
    err = _refused(tmp_path, {"type": "image", "path": "gone.jpg"}, r"blocks\[0\]: image not found: .*gone.jpg")
    assert "page file's folder" in err.hint


def test_folder_is_not_an_image(tmp_path):
    (tmp_path / "dir.jpg").mkdir()
    _refused(tmp_path, {"type": "image", "path": "dir.jpg"}, "image not found")


def test_a_file_that_is_not_an_image_is_refused(tmp_path):
    (tmp_path / "notes.jpg").write_text("not an image")
    _refused(tmp_path, {"type": "image", "path": "notes.jpg"}, "not a supported image format")


def test_image_formats_outside_the_photo_formats_are_refused(tmp_path):
    Image.new("RGB", (20, 20)).save(tmp_path / "pic.ppm")  # Pillow reads it, but compose does not let it
    _refused(tmp_path, {"type": "image", "path": "pic.ppm"}, "not a supported image format")


def test_truncated_image_is_refused(tmp_path):
    data = _photo(tmp_path, "big.jpg", size=(800, 800)).read_bytes()
    (tmp_path / "big.jpg").write_bytes(data[: len(data) // 2])
    _refused(tmp_path, {"type": "image", "path": "big.jpg"}, "cannot read image big.jpg")


def test_image_file_size_is_capped(tmp_path, monkeypatch):
    _photo(tmp_path)
    monkeypatch.setattr(compose, "MAX_IMAGE_BYTES", 100)
    _refused(tmp_path, {"type": "image", "path": "photo.jpg"}, "larger than 0 MiB")


def test_image_pixels_are_capped_before_decoding(tmp_path, monkeypatch):
    _photo(tmp_path)
    monkeypatch.setattr(compose, "MAX_IMAGE_PIXELS", 1000)
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("decoded a refused image"))
    _refused(tmp_path, {"type": "image", "path": "photo.jpg"}, "300x400 pixels")


def test_number_of_images_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(compose, "MAX_IMAGES", 1)
    _photo(tmp_path, "a.jpg"), _photo(tmp_path, "b.png")
    with pytest.raises(PrinterError, match="2 images, max 1"):
        _parse({"blocks": [{"type": "image", "path": "a.jpg"}, {"type": "image", "path": "b.png"}]}, tmp_path)


def _count_decodes(monkeypatch):
    decoded = []
    real = compose.load_image
    monkeypatch.setattr(compose, "load_image", lambda path, where: decoded.append(path) or real(path, where))
    return decoded


def test_too_many_images_are_refused_before_any_is_decoded(tmp_path, monkeypatch):
    monkeypatch.setattr(compose, "MAX_IMAGES", 2)
    decoded = _count_decodes(monkeypatch)
    for n in range(4):
        _photo(tmp_path, f"{n}.jpg")
    images = [{"type": "image", "path": f"{n}.jpg"} for n in range(3)]
    page = {"blocks": [images[0], {"type": "columns", "columns": [[images[1]], [images[2]]]}]}
    with pytest.raises(PrinterError, match="3 images, max 2") as exc:
        _parse(page, tmp_path)
    assert exc.value.exit_code == 3 and decoded == []


def test_an_image_file_swapped_in_after_counting_is_not_decoded_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(compose, "MAX_IMAGES", 1)
    monkeypatch.setattr(compose, "_image_paths", lambda blocks, base: [])  # as if counted before the swap
    decoded = _count_decodes(monkeypatch)
    _photo(tmp_path, "a.jpg"), _photo(tmp_path, "b.jpg")
    with pytest.raises(PrinterError, match="2 images, max 1"):
        _parse({"blocks": [{"type": "image", "path": "a.jpg"}, {"type": "image", "path": "b.jpg"}]}, tmp_path)
    assert len(decoded) == 1


def test_one_file_named_many_ways_is_decoded_once_and_counts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(compose, "MAX_IMAGES", 1)
    decoded = _count_decodes(monkeypatch)
    photo = _photo(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "link.jpg").symlink_to(photo)
    spellings = ["photo.jpg", "./photo.jpg", "sub/../photo.jpg", "sub/.././photo.jpg", str(photo), "link.jpg"]
    page = _parse({"blocks": [{"type": "image", "path": p} for p in spellings]}, tmp_path)
    assert len(decoded) == 1 and page.inputs == [photo]
    assert all(block["image"] is page.blocks[0]["image"] for block in page.blocks)


def test_images_are_kept_no_larger_than_they_print(tmp_path):
    _photo(tmp_path, size=(4000, 3000))
    img = _parse({"blocks": [{"type": "image", "path": "photo.jpg"}]}, tmp_path).blocks[0]["image"]
    assert img.width <= compose.CONTENT_W and img.height <= compose.CONTENT_H


def test_transparent_image_prints_on_white(tmp_path):
    Image.new("RGBA", (40, 40), (255, 0, 0, 0)).save(tmp_path / "clear.png")
    img = _parse({"blocks": [{"type": "image", "path": "clear.png"}]}, tmp_path).blocks[0]["image"]
    assert img.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize("content, message", [
    (b"{", "is not valid JSON"),
    (b'{"blocks": [{"type": "qr", "data": "d", "size": NaN}]}', "is not valid JSON"),
    ("{}".encode("utf-16"), "is not UTF-8 text"),
    (b"[" * 100000 + b"]" * 100000, "is not valid JSON: nested too deeply"),
])
def test_unreadable_page_files_are_refused(content, message):
    with pytest.raises(PrinterError, match=message) as exc:
        compose.parse(content, "page.json")
    assert exc.value.exit_code == 3


@pytest.mark.parametrize("template", ['{"blocks": %s}', '{"title": %s, "blocks": []}',
                                      '{"blocks": [{"type": %s}]}', '{"blocks": [{"type": "text", "text": %s}]}',
                                      '{"blocks": [{"type": "heading", "text": "H", "level": %s}]}',
                                      '{"blocks": [{"type": "columns", "columns": [[%s], []]}]}'])
def test_deeply_nested_values_are_refused_by_the_schema(template):
    nested = "[" * 5000 + "]" * 5000
    with pytest.raises(PrinterError) as exc:
        compose.parse((template % nested).encode(), "page.json")
    assert exc.value.exit_code == 3


@pytest.mark.parametrize("template", ['{"title": %s, "blocks": []}', '{"blocks": [{"type": %s}]}',
                                      '{"blocks": [{"type": "text", "text": %s}]}'])
def test_nesting_near_the_recursion_limit_is_refused_cleanly(template):
    limit = sys.getrecursionlimit()
    for depth in range(limit - 60, limit + 1):
        nested = "[" * depth + "]" * depth
        with pytest.raises(PrinterError) as exc:  # refused by the schema or as too deep, never another error
            compose.parse((template % nested).encode(), "page.json")
        assert exc.value.exit_code == 3


def test_checker_names_a_nested_value_without_printing_it(monkeypatch):
    monkeypatch.setattr(compose.json, "dumps", lambda *a, **k: pytest.fail("serialised the nested value"))
    deep = []
    for _ in range(100_000):
        deep = [deep]
    assert compose.problem({"blocks": [{"type": "text", "text": deep}]}) == "blocks[0].text must be text, not a list"
    assert compose.problem({"blocks": [{"type": {"a": deep}}]}) == "blocks[0] has an unknown type an object (use one " \
        "of heading, text, image, qr, columns, rule)"


@pytest.mark.parametrize("page, where", [
    (b'{"blocks": [{"type": "qr", "data": "\\ud800"}]}', "blocks[0].data"),
    (b'{"blocks": [{"type": "columns", "columns": [[{"type": "qr", "data": "a\\udfffb"}], []]}]}',
     "blocks[0].columns[0][0].data"),
    (b'{"title": "\\ud800", "blocks": [{"type": "rule"}]}', "title"),
])
def test_a_lone_surrogate_is_refused_before_layout(page, where):
    with pytest.raises(PrinterError) as exc:
        compose.parse(page, "page.json")
    assert exc.value.exit_code == 3 and str(exc.value) == f"page.json: {where} is not valid text"


def test_load_refuses_missing_and_oversized_files(tmp_path, monkeypatch):
    with pytest.raises(PrinterError, match="page file not found"):
        compose.load(tmp_path / "nope.json")
    monkeypatch.setattr(compose, "MAX_PAGE_FILE", 10)
    with pytest.raises(PrinterError, match="larger than 0 KiB"):
        compose.load(_page_file(tmp_path, {"blocks": [{"type": "rule"}]}))


# --- layout --------------------------------------------------------------------------------------------


def _ink(page, box=None):
    """How many pixels of ``page`` (or of ``box`` on it) are not white."""
    region = page.crop(box) if box else page
    return sum(region.convert("L").histogram()[:250])


@pytest.mark.parametrize("block", EVERY_BLOCK["blocks"], ids=lambda b: b["type"])
def test_every_block_prints(tmp_path, block):
    _photo(tmp_path)
    pages = compose.compose_pages(_parse({"blocks": [block]}, tmp_path))
    assert len(pages) == 1 and pages[0].size == (1240, 1754) and _ink(pages[0]) > 0
    assert _ink(pages[0], (0, 0, 1240, compose.TOP)) == 0  # nothing in the top margin


def test_blocks_go_down_the_page_in_order(tmp_path):
    page = _parse({"blocks": [{"type": "heading", "text": "A"}, {"type": "rule"}, {"type": "text", "text": "B"}]})
    (sheet,) = compose.layout(page)
    ys = [y for y, _ in sheet]
    assert ys[0] == compose.TOP and ys == sorted(ys) and len(ys) == 3
    assert ys[1] == ys[0] + sheet[0][1].height + compose.GAP


def test_long_text_runs_on_to_more_pages_without_losing_a_line():
    words = " ".join(f"word{n}" for n in range(2000))
    page = _parse({"blocks": [{"type": "text", "text": words}]})
    sheets = compose.layout(page)
    assert len(sheets) >= 2
    lines = compose._wrap(words, compose._font(compose.TEXT_SIZES["normal"]), compose.CONTENT_W)
    assert sum(len(sheet) for sheet in sheets) == len(lines)
    for sheet in sheets:
        assert sheet[0][0] == compose.TOP and all(y + p.height <= compose.BOTTOM for y, p in sheet)


def test_paragraph_gap_is_dropped_at_the_top_of_a_page():
    lead = compose._lead(compose.TEXT_SIZES["normal"])
    rows = compose.CONTENT_H // lead
    paragraphs = "\n".join(["line"] * (rows + 3))
    sheets = compose.layout(_parse({"blocks": [{"type": "text", "text": paragraphs}]}))
    assert all(not piece.space for sheet in sheets for _, piece in sheet[:1])


def test_a_heading_is_kept_with_what_follows_it():
    moved = 0
    for n in range(45, 70):  # some of these fillers leave room for the heading but not for the text after it
        page = _parse({"blocks": [{"type": "text", "text": " ".join(["filler"] * 12 * n)},
                                  {"type": "heading", "text": "Next"}, {"type": "text", "text": "After it."}]})
        sheets = compose.layout(page)
        assert not sheets[0][-1][1].keep_with_next, "a heading ended a page"
        if len(sheets) == 2 and sheets[1][0][1].keep_with_next and len(sheets[1]) == 2:
            moved += 1
    assert moved  # at least one filler pushed the heading over with its text


def test_a_tall_image_is_shrunk_to_fit_a_page(tmp_path):
    _photo(tmp_path, size=(100, 2000))
    page = _parse({"blocks": [{"type": "image", "path": "photo.jpg", "height": 260, "caption": "c"}]}, tmp_path)
    (sheet,) = compose.layout(page)
    assert sheet[0][1].height <= compose.CONTENT_H


def test_image_height_is_honoured(tmp_path):
    _photo(tmp_path, size=(1000, 1000))
    page = _parse({"blocks": [{"type": "image", "path": "photo.jpg", "height": 50}]}, tmp_path)
    assert compose.layout(page)[0][0][1].height == round(50 * compose.PX_PER_MM)


def test_too_many_pages_are_refused(monkeypatch):
    monkeypatch.setattr(compose, "MAX_PAGES", 2)
    page = _parse({"blocks": [{"type": "text", "text": "\n".join(["line"] * 200)}]})
    with pytest.raises(PrinterError, match="needs more than 2 pages") as exc:
        compose.layout(page)
    assert exc.value.exit_code == 3


def test_columns_taller_than_a_page_are_refused():
    tall = {"type": "text", "text": "\n".join(["line"] * 80)}
    with pytest.raises(PrinterError, match=r"blocks\[0\]: the columns block is taller than a page"):
        compose.layout(_parse({"blocks": [{"type": "columns", "columns": [[tall], []]}]}))


def test_columns_sit_side_by_side():
    page = _parse({"blocks": [{"type": "columns", "columns": [[{"type": "rule"}], [], [{"type": "rule"}]]}]})
    img = compose.compose_pages(page)[0]
    cell = compose._col_width(compose.CONTENT_W, 3, compose.GUTTER)
    y = compose.TOP
    x0, x2 = compose.MARGIN, compose.MARGIN + 2 * (cell + compose.GUTTER)
    assert img.getpixel((x0 + 5, y))[0] < 50 and img.getpixel((x2 + 5, y))[0] < 50
    assert img.getpixel((x0 + cell + compose.GUTTER + 5, y)) == (255, 255, 255)  # the empty middle column


def test_qr_code_has_whole_modules_and_a_quiet_zone():
    import segno
    img = compose.qr_image("https://example.com/", 300, "blocks[0]")
    modules = segno.make("https://example.com/", error="m", micro=False).symbol_size(scale=1, border=4)[0]
    scale = img.width // modules
    assert img.width == img.height == modules * scale <= 300 and scale >= compose.QR_MIN_MODULE
    assert img.getpixel((2, 2)) == (255, 255, 255)  # quiet zone
    assert img.getpixel((4 * scale + 1, 4 * scale + 1)) == (0, 0, 0)  # the finder pattern's corner


def test_qr_code_too_dense_for_its_room_is_refused():
    with pytest.raises(PrinterError, match="too much data for a QR code") as exc:
        compose.layout(_parse({"blocks": [{"type": "columns", "columns": [
            [{"type": "qr", "data": "🎉" * compose.MAX_QR_DATA}], [], [], []]}]}))
    assert exc.value.exit_code == 3


def test_longest_qr_data_fits_a_full_width_code():
    data = "🎉" * compose.MAX_QR_DATA  # 4 bytes each in UTF-8
    pages = compose.compose_pages(_parse({"blocks": [{"type": "qr", "data": data, "size": 150}]}))
    assert len(pages) == 1


def _count_bitmaps(monkeypatch):
    """Count image resizes and QR codes made and rasterised, from here on."""
    import segno
    made = SimpleNamespace(resized=0, codes=0, qr_bitmaps=0)
    real_resize, real_make, real_bitmap = Image.Image.resize, segno.make, compose._qr_bitmap

    def resize(self, *args, **kwargs):
        made.resized += 1
        return real_resize(self, *args, **kwargs)

    def make(*args, **kwargs):
        made.codes += 1
        return real_make(*args, **kwargs)

    def bitmap(*args):
        made.qr_bitmaps += 1
        return real_bitmap(*args)
    monkeypatch.setattr(Image.Image, "resize", resize)
    monkeypatch.setattr(segno, "make", make)
    monkeypatch.setattr(compose, "_qr_bitmap", bitmap)
    return made


def test_many_images_needing_too_many_pages_are_refused_without_resizing_them(tmp_path, monkeypatch):
    _photo(tmp_path, "tall.png", size=(100, 2000))
    page = _parse({"blocks": [{"type": "image", "path": "tall.png", "height": 260}] * compose.MAX_BLOCKS}, tmp_path)
    made = _count_bitmaps(monkeypatch)
    with pytest.raises(PrinterError, match=f"needs more than {compose.MAX_PAGES} pages") as exc:
        compose.compose_pages(page)
    assert exc.value.exit_code == 3 and made.resized == 0


def test_many_qr_codes_needing_too_many_pages_are_refused_without_drawing_them(monkeypatch):
    page = _parse({"blocks": [{"type": "qr", "data": "x" * (compose.MAX_QR_DATA - 3) + f"{n:03}", "size": 150}
                              for n in range(compose.MAX_BLOCKS)]})
    made = _count_bitmaps(monkeypatch)
    with pytest.raises(PrinterError, match=f"needs more than {compose.MAX_PAGES} pages"):
        compose.compose_pages(page)
    assert made.qr_bitmaps == 0 and made.resized == 0
    assert made.codes <= 3 * (compose.MAX_PAGES + 1)  # measuring stopped at the page that did not fit


def test_a_columns_block_taller_than_a_page_stops_measuring(monkeypatch):
    cell = [{"type": "qr", "data": f"code {n}", "size": 15} for n in range(compose.MAX_CELL_BLOCKS)]
    page = _parse({"blocks": [{"type": "columns", "columns": [cell, cell]}]})
    made = _count_bitmaps(monkeypatch)
    with pytest.raises(PrinterError, match="taller than a page"):
        compose.layout(page)
    assert made.qr_bitmaps == 0 and made.codes < compose.MAX_CELL_BLOCKS


def test_a_qr_code_too_dense_is_refused_without_drawing_it(monkeypatch):
    page = _parse({"blocks": [{"type": "qr", "data": "🎉" * compose.MAX_QR_DATA, "size": 15}]})
    made = _count_bitmaps(monkeypatch)
    with pytest.raises(PrinterError, match="too much data for a QR code 15 mm wide"):
        compose.layout(page)
    assert made.qr_bitmaps == 0


def test_bitmaps_are_made_only_when_a_page_is_drawn(tmp_path, monkeypatch):
    _photo(tmp_path)
    page = _parse(EVERY_BLOCK, tmp_path)
    made = _count_bitmaps(monkeypatch)
    sheets = compose.layout(page)
    assert made.resized == 0 and made.qr_bitmaps == 0
    (img,) = compose.compose_pages(page)
    assert img.size == (1240, 1754) and made.qr_bitmaps == 2 and made.resized >= 4  # 2 photos, 2 QR codes
    for y, piece in sheets[0]:  # every block left ink where it was laid out
        assert piece.space or _ink(img, (compose.MARGIN, y, compose.MARGIN + compose.CONTENT_W, y + max(piece.height, 1))) > 0


# --- prt compose ---------------------------------------------------------------------------------------


def _every_block_file(tmp_path):
    _photo(tmp_path)
    return _page_file(tmp_path, EVERY_BLOCK)


def test_preview_writes_the_pages_and_prints_nothing(capsys, tmp_path, printer):
    out_file = tmp_path / "out.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "compose", str(_every_block_file(tmp_path)),
                         "--preview", str(out_file))
    assert code == 0, err
    data = json.loads(out)
    assert data["estimate"]["pages"] == 1 and data["estimate"]["colour"] is True
    assert without_estimate(out) == {"action": "compose", "title": "Everything", "pages": 1,
                                     "files": [{"file": str(out_file), "format": "png",
                                                "bytes": out_file.stat().st_size}]}
    assert Image.open(out_file).size == (1240, 1754)
    assert printer.made == [] and printer.jobs() == []


def test_preview_of_several_pages_numbers_the_files(capsys, tmp_path, printer):
    page = _page_file(tmp_path, {"blocks": [{"type": "text", "text": " ".join(["word"] * 800)}]})
    code, out, err = run(capsys, "-f", "json", "compose", str(page), "-o", str(tmp_path / "p.jpg"))
    assert code == 0, err
    files = [f["file"] for f in json.loads(out)["files"]]
    assert files == [str(tmp_path / f"p-{n}.jpg") for n in (1, 2)] and "2 pages of 'page' rendered" in err
    assert all((tmp_path / f"p-{n}.jpg").is_file() for n in (1, 2)) and not (tmp_path / "p.jpg").exists()


def test_print_sends_one_job_per_page_after_saving_copies(capsys, tmp_path, printer):
    from prt.booth import to_jpeg
    page_file = _every_block_file(tmp_path)
    copy = tmp_path / "copy.pdf"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "compose", str(page_file), "--save", str(copy),
                         "--yes")
    assert code == 0, err
    expected = [to_jpeg(p) for p in compose.compose_pages(compose.load(page_file))]
    assert printer.jobs() == expected
    assert without_estimate(out) == {"action": "compose", "title": "Everything", "pages": 1, "job_ids": [101],
                                     "saved": [{"file": str(copy), "format": "pdf", "bytes": copy.stat().st_size}]}
    assert copy.read_bytes().startswith(b"%PDF") and "1 page queued as job 101" in err


def test_print_asks_first(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or False)
    code, _, err = run(capsys, "-p", "demo", "compose", str(_every_block_file(tmp_path)))
    assert code == 1 and asked == ["Print 1 page of 'Everything' on demo?"] and printer.jobs() == []


def test_without_a_terminal_print_needs_yes(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "compose", str(_every_block_file(tmp_path)))
    assert code == 2 and _error(err)["kind"] == "confirmation_required" and printer.jobs() == []


def test_page_from_stdin_reads_images_from_the_current_folder(capsys, tmp_path, printer, stdin, monkeypatch):
    _photo(tmp_path)
    monkeypatch.chdir(tmp_path)
    stdin(json.dumps({"blocks": [{"type": "image", "path": "photo.jpg"}]}).encode())
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "compose", "-", "--yes")
    assert code == 0, err
    assert json.loads(out)["job_ids"] == [101] and len(printer.jobs()) == 1


@pytest.mark.parametrize("page, kind_of_problem", [
    ({"blocks": [{"type": "text", "text": "T", "colour": "red"}]}, 'unknown field "colour"'),
    ({"blocks": [{"type": "image", "path": "missing.png"}]}, "image not found"),
    ({"blocks": [{"type": "image", "path": "https://example.com/a.png"}]}, "is a web link"),
    ({"blocks": [{"type": "heading", "text": "Party 🎉"}]}, "cannot print"),
])
def test_an_invalid_page_file_is_refused_before_anything_is_printed(capsys, tmp_path, printer, page,
                                                                    kind_of_problem):
    copy, preview = tmp_path / "copy.pdf", tmp_path / "preview.png"
    for extra in (["--yes", "--save", str(copy)], ["--preview", str(preview)]):
        code, out, err = run(capsys, "-p", "demo", "-f", "json", "compose", str(_page_file(tmp_path, page)), *extra)
        assert code == 3 and out == ""
        assert _error(err)["kind"] == "invalid_input" and kind_of_problem in _error(err)["message"]
    assert printer.made == [] and printer.jobs() == [] and not copy.exists() and not preview.exists()


def test_qr_data_that_is_not_text_is_refused_by_the_cli(capsys, printer, stdin):
    stdin(b'{"blocks": [{"type": "qr", "data": "\\ud800"}]}')
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "compose", "-", "--yes")
    assert code == 3 and out == "" and printer.made == []
    assert _error(err)["kind"] == "invalid_input" and "blocks[0].data is not valid text" in _error(err)["message"]


def test_bad_stdin_prints_nothing(capsys, printer, stdin):
    stdin(b"not json")
    code, out, err = run(capsys, "-p", "demo", "compose", "-", "--yes")
    assert code == 3 and out == "" and "page on stdin is not valid JSON" in err and printer.made == []


def test_output_that_would_overwrite_an_input_is_refused(capsys, tmp_path, printer):
    _photo(tmp_path)
    page_file = _page_file(tmp_path, EVERY_BLOCK, name="layout.png")  # any name will do for a page file
    before = (tmp_path / "photo.jpg").read_bytes()
    for args in (["-o", "photo.jpg"], ["-o", "layout.png"], ["--save", "photo.jpg", "--yes"]):
        code, _, err = run(capsys, "-f", "json", "compose", str(page_file), *[str(tmp_path / a) if "." in a else a
                                                                             for a in args])
        assert code == 2 and "would overwrite input" in _error(err)["message"]
    assert (tmp_path / "photo.jpg").read_bytes() == before and json.loads(page_file.read_text()) == EVERY_BLOCK
    assert printer.jobs() == []


def test_unknown_output_type_is_a_usage_error(capsys, tmp_path, printer):
    code, _, err = run(capsys, "compose", str(_every_block_file(tmp_path)), "--save", "page.docx", "--yes")
    assert code == 2 and "unknown file type .docx" in err and printer.jobs() == []


def test_compose_help(capsys):
    code, out, _ = run(capsys, "compose", "--help")
    assert code == 0 and "heading, text, image, QR code, columns, rule" in " ".join(out.split())


@pytest.mark.parametrize("doc, after", [("README.md", "## Compose a page"), ("AGENTS.md", "prt compose page.json")])
def test_documented_page_examples_pass_the_schema(doc, after):
    text = (ROOT / doc).read_text().split(after, 1)[1]
    example = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    assert compose.problem(example) is None
