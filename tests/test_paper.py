"""prt.paper: the sheet a page is laid out on decides its size, how it is sent and what its ink costs (#27, #28)."""

import io
import re

import pytest
from PIL import Image

from prt import guard, media, render, testpage
from prt.drivers.fake import FakeDriver
from prt.paper import A4, DISC, PAPERS, PHOTO_4X6, Paper
from prt.transport import ipp


def _sent(fake):
    """The media-col of every job the fake printer received, as {name: value} without the IPP tags."""
    out = []
    for op, _, job_attrs, _ in fake.client.requests:
        if op != ipp.OP_PRINT_JOB:
            continue
        col = next((a.value for a in job_attrs if a.name == "media-col"), {})
        out.append({k: (v[1] if k != "media-size" else {d: x[1] for d, x in v[1].items()}) for k, v in col.items()})
    return out


def _send(paper, pages=1):
    fake = FakeDriver()
    ticket = guard.Ticket(pages)
    jobs = list(render.send_pages(fake, [b"jpeg"] * pages, [f"job {n}" for n in range(pages)], ticket, paper))
    return fake, jobs, ticket


def test_a4_is_the_page_every_a4_command_draws_on():
    assert A4.size == (testpage.W, testpage.H) and A4.dpi == 150
    assert PAPERS["a4"] is A4 and A4.tray is None and A4.media == "a4"


def test_4x6_photo_paper_is_drawn_at_300_dpi_for_the_photo_tray():
    assert PHOTO_4X6.size == (1200, 1800) and PHOTO_4X6.landscape_size == (1800, 1200)
    assert PHOTO_4X6.tray == "photo" and PHOTO_4X6.media_type == "photographic"
    assert PHOTO_4X6.mm(25.4) == 300 and PAPERS["4x6"] is PHOTO_4X6
    assert PHOTO_4X6.loaded == "4×6 paper in the photo tray" and A4.loaded == "A4 paper"


def test_send_pages_asks_for_the_4x6_size_photo_tray_and_photo_media():
    fake, jobs, ticket = _send(PHOTO_4X6)
    assert len(jobs) == 1 and ticket.attempted == 1
    assert _sent(fake) == [
        {
            "media-size": {"x-dimension": 10160, "y-dimension": 15240},
            "media-source": "photo",
            "media-type": "photographic",
        }
    ]


def test_send_pages_on_a4_is_unchanged_no_tray_and_no_media_type():
    fake, _, _ = _send(A4, pages=2)
    assert _sent(fake) == [{"media-size": {"x-dimension": 21000, "y-dimension": 29700}}] * 2


def test_another_sheet_is_one_more_paper():
    """A later format (an A6 postcard, a disc) needs no change to the send path."""
    a6 = Paper("a6", "A6", 105, 148, "a6", tray="main", media_type="stationery")
    assert a6.size == (620, 874)
    fake, _, _ = _send(a6)
    assert _sent(fake) == [
        {"media-size": {"x-dimension": 10500, "y-dimension": 14800}, "media-source": "main", "media-type": "stationery"}
    ]


def test_a_disc_is_the_120mm_square_round_it_from_the_disc_tray():
    assert DISC.size == (1417, 1417) and DISC.dpi == 300 and PAPERS["disc"] is DISC
    assert DISC.tray == "disc" and DISC.media_type == "disc" and media.resolve_alias(DISC.media) == "disc_12cm_18x120mm"
    assert DISC.to_dict() == {"size": "disc", "media": "disc", "tray": "disc"}
    assert DISC.loaded == "a printable CD/DVD in the disc tray, printable side up"
    assert DISC.ink_scale == pytest.approx(120 * 120 / (210 * 297))
    square = Image.new("RGB", DISC.size, "white")
    assert DISC.for_printer(square) is square


def test_a_disc_media_name_gives_the_disc_size_not_its_hub_and_diameter():
    """'disc_12cm_18x120mm' names an 18 mm hub on a 120 mm disc; the printer reports the disc as 120 x 120 mm."""
    assert media.size_of("disc_12cm_18x120mm") == (12000, 12000)
    assert media.size_of(media.resolve_alias("disc")) == (12000, 12000)
    assert media.size_of("iso_a4_210x297mm") == (21000, 29700) and media.size_of("disc_odd") is None


def test_send_pages_on_a_disc_asks_for_the_disc_size_tray_and_media():
    fake, jobs, _ = _send(DISC)
    assert len(jobs) == 1
    assert _sent(fake) == [
        {"media-size": {"x-dimension": 12000, "y-dimension": 12000}, "media-source": "disc", "media-type": "disc"}
    ]


def test_estimate_counts_the_ink_of_a_small_sheet_by_its_area():
    page = Image.new("RGB", (600, 900), (200, 30, 30))
    a4, small = guard.estimate([page]), guard.estimate([page], PHOTO_4X6)
    assert small["pages"] == a4["pages"] == 1 and small["colour"] is True
    for ink in guard.INKS:
        assert small["ink"][ink] == pytest.approx(a4["ink"][ink] * PHOTO_4X6.ink_scale, abs=0.06)
    assert PHOTO_4X6.ink_scale == pytest.approx(101.6 * 152.4 / (210 * 297))
    assert small["summary"].startswith("~1 page (4×6), colour")
    assert guard.estimate([page], A4) == a4 and "(" not in a4["summary"]


def test_a_landscape_page_is_turned_upright_for_the_printer():
    wide = Image.new("RGB", (1800, 1200), "white")
    wide.putpixel((0, 0), (255, 0, 0))  # top left of the card ends up bottom left of the sheet
    upright = PHOTO_4X6.for_printer(wide)
    assert upright.size == (1200, 1800) and upright.getpixel((0, 1799)) == (255, 0, 0)
    tall = Image.new("RGB", (1200, 1800))
    assert PHOTO_4X6.for_printer(tall) is tall
    assert Image.open(io.BytesIO(PHOTO_4X6.to_jpeg(wide))).size == (1200, 1800)


def test_saved_files_carry_the_paper_resolution():
    page = Image.new("RGB", PHOTO_4X6.size, "white")
    pdf = PHOTO_4X6.to_bytes(page, "pdf")
    box = [float(n) for n in re.search(rb"/MediaBox \[([^\]]+)\]", pdf).group(1).split()]
    assert box[2:] == [288.0, 432.0]  # 4 x 6 in, in points
    assert Image.open(io.BytesIO(PHOTO_4X6.to_bytes(page, "png"))).info["dpi"] == pytest.approx((300, 300), abs=0.01)
    with pytest.raises(ValueError):
        PHOTO_4X6.to_bytes(page, "gif")
