import itertools

import pytest
from PIL import Image, ImageDraw

from prt import layout
from prt.testpage import _font

FONT = _font(24)
COL_W = 300
WORDS = ("Printing a long article means the text has to keep going on the next page, "
         "word for word, without dropping or repeating anything along the way.").split()


def _article(paragraphs=12, words=60):
    return [" ".join(WORDS[(p + w) % len(WORDS)] + str(p) for w in range(words)) for p in range(paragraphs)]


def _text(columns_or_pages):
    """All the characters a layout prints, in reading order, without spaces or paragraph gaps."""
    out = []
    for item in columns_or_pages:
        out.append(_text(item) if isinstance(item, list) else item)
    return "".join("".join(out).split())


def _chars(paragraphs):
    return "".join("".join(paragraphs).split())


def test_text_that_fits_comes_back_with_no_leftover():
    cols, left = layout.flow_columns(["One short paragraph.", "And another."], FONT, COL_W, 20, 2)
    assert left == []
    assert cols == [["One short paragraph.", "", "And another."]]  # four lines or fewer stay in one column


def test_text_that_fits_is_balanced_over_the_columns():
    paragraphs = _article(paragraphs=2, words=40)
    cols, left = layout.flow_columns(paragraphs, FONT, COL_W, 100, 3)
    assert left == [] and len(cols) == 3
    assert max(map(len, cols)) - min(map(len, cols)) <= 2


def test_leftover_resumes_exactly_where_the_page_stopped():
    paragraphs = _article()
    cols, left = layout.flow_columns(paragraphs, FONT, COL_W, 10, 2)
    assert len(cols) == 2 and all(len(col) <= 10 for col in cols)
    assert left and len(left) < len(paragraphs)
    assert _text(cols) + _chars(left) == _chars(paragraphs)  # nothing lost, nothing printed twice
    assert left[-1] == paragraphs[-1]  # paragraphs not reached are handed back untouched


def test_a_paragraph_split_by_the_page_continues_mid_paragraph():
    para = " ".join(f"word{i}" for i in range(200))
    cols, left = layout.flow_columns([para], FONT, COL_W, 5, 1)
    printed = " ".join(cols[0]).split()
    assert len(cols[0]) == 5
    assert left == [" ".join(para.split()[len(printed):])]


def test_a_word_wider_than_the_column_is_carried_over_by_characters():
    long_word = "x" * 400
    cols, left = layout.flow_columns([f"go {long_word} end"], FONT, 120, 2, 1)
    assert _text(cols) + _chars(left) == f"go{long_word}end"
    assert left[0].startswith("x") and left[0].endswith(" end")


def test_no_paragraph_gap_at_the_top_of_a_column():
    one_line = ["Short line."] * 6
    cols, left = layout.flow_columns(one_line, FONT, COL_W, 3, 2)
    assert all(col[0] for col in cols)
    assert _text(cols) + _chars(left) == _chars(one_line)


def test_narrow_first_lines_leave_room_for_a_drop_cap():
    para = " ".join(f"word{i}" for i in range(80))
    cols, _ = layout.flow_columns([para], FONT, COL_W, 40, 1, narrow=(3, 150))
    assert all(FONT.getlength(line) <= 150 for line in cols[0][:3])
    assert FONT.getlength(cols[0][3]) > 150  # back to the full width after the drop cap
    assert _text(cols) == _chars([para])


@pytest.mark.parametrize("paragraphs, words", [(1, 10), (12, 60), (40, 120)])
def test_paginate_places_every_word_on_exactly_one_page(paragraphs, words):
    text = _article(paragraphs, words)
    pages = list(layout.paginate(text, FONT, COL_W, 3, 20, 40))
    assert _text(pages) == _chars(text)
    for n, page in enumerate(pages):
        assert len(page) <= 3 and all(len(col) <= (20 if n == 0 else 40) for col in page)
    assert all(any(col for col in page) for page in pages)  # no blank pages


def test_paginate_page_count_follows_the_room_on_each_page():
    lines_of_text = [f"line{i}" for i in range(100)]  # one short line per paragraph: 199 rows with the gaps
    assert len(list(layout.paginate(lines_of_text, FONT, COL_W, 1, 50, 50))) == 4
    assert len(list(layout.paginate(lines_of_text, FONT, COL_W, 1, 10, 100))) == 3
    assert len(list(layout.paginate(lines_of_text, FONT, COL_W, 2, 50, 50))) == 2
    assert len(list(layout.paginate(lines_of_text[:3], FONT, COL_W, 1, 50, 50))) == 1


def test_paginate_with_no_room_on_the_first_page_starts_the_text_on_page_two():
    pages = list(layout.paginate(["Hello there."], FONT, COL_W, 2, 0, 10))
    assert pages == [[], [["Hello there."]]]
    with pytest.raises(ValueError):
        list(layout.paginate(["Hello"], FONT, COL_W, 2, 10, 0))


def test_draw_columns_draws_inside_the_columns():
    page = Image.new("RGB", (800, 400), "white")
    cols = [["left column"], ["right column"]]
    layout.draw_columns(ImageDraw.Draw(page), cols, 20, 20, 300, 40, FONT, 24, (0, 0, 0), rule=(255, 0, 0),
                        indent=(1, 50))
    left = page.crop((20, 20, 70, 60))
    assert left.getextrema() == ((255, 255),) * 3  # the indent leaves the drop-cap corner empty
    assert page.crop((70, 20, 320, 60)).getextrema() != ((255, 255),) * 3
    assert page.getpixel((340, 30)) == (255, 0, 0)  # rule midway in the gutter


def test_paginate_measures_only_the_pages_it_is_asked_for():
    class CountingFont:
        calls = 0

        def getlength(self, text):
            CountingFont.calls += 1
            return FONT.getlength(text)

    huge = " ".join(f"w{i}" for i in range(100_000))  # one paragraph of about 200 pages
    first, second = itertools.islice(layout.paginate([huge], CountingFont(), COL_W, 3, 20, 40), 2)
    assert len(first) == 3 and len(second) == 3
    assert CountingFont.calls < 20 * 3 * 40  # a few measurements per printed line, not per word of the text
