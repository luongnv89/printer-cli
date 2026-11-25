import pytest

from prt.drivers import epson
from prt.drivers.base import Driver, PrinterError, Unsupported
from prt.drivers.fake import FakeDriver
from prt.drivers.ipp import IPPDriver, parse_keyvals, parse_reason
from prt.drivers.registry import detect, make_driver
from prt.maintenance import head_clean_preflight, nozzle_check_preflight
from prt.models import Capability, PrintOptions, Severity, Status, Target


@pytest.fixture
def fake():
    return FakeDriver()


def test_status_from_real_capture(fake):
    st = fake.status()
    assert st.state == "idle"
    assert [(i.short, i.level, i.state) for i in st.inks] == [
        ("PB", 92, "ok"),
        ("C", 1, "replace"),
        ("M", 1, "replace"),
        ("Y", 12, "low"),
        ("BK", 89, "ok"),
    ]
    assert st.waste == "almost-full"
    assert [(t.source, t.media) for t in st.trays] == [("main", "A4"), ("photo", "4×6"), ("disc", "Disc")]
    assert all(t.level is None for t in st.trays)  # printer reports -2 = unknown
    assert st.alerts[0].severity == Severity.ERROR
    assert not any(a.code.startswith("marker-supply-low") for a in st.alerts)  # replaced by named inks


def test_info_from_real_capture(fake):
    info = fake.info()
    assert info.make_model == "EPSON XP-6000 Series"
    assert info.firmware == "20.50.NU30Q6"
    assert info.page_count == 1538
    assert info.serial == "X00000000"
    assert info.duplex is True and info.color is True
    assert "image/jpeg" in info.formats and "application/pdf" not in info.formats
    assert "head_clean" in info.capabilities


def test_serial_with_non_printable_byte_keeps_raw():
    d = IPPDriver(Target("x", "x"))
    assert d._serial({"printer-device-id": "SN:583132333435363786;"}) == "X1234567 (raw 583132333435363786)"


def test_unparseable_low_level_falls_back_to_the_default_15():
    d = IPPDriver(Target("x", "x"))
    inks = d.parse_inks(
        {
            "marker-names": ["Black", "Cyan", "Magenta"],
            "marker-levels": [10, 10, 10],
            "marker-low-levels": ["n/a", 5],  # unparseable, a number, and none given
        }
    )
    assert [ink.low for ink in inks] == [15, 5, 15]
    assert [ink.state for ink in inks] == ["low", "ok", "low"]


def test_reason_parsing():
    assert parse_reason("none") is None
    a = parse_reason("media-jam-error")
    assert a.severity == Severity.ERROR and a.message == "paper jam"
    assert parse_reason("marker-waste-almost-full-report").severity == Severity.WARNING
    assert parse_reason("cover-open").severity == Severity.ERROR  # no suffix = error
    assert parse_reason("weird-thing-warning").message == "weird thing"


def test_keyvals_escapes():
    kv = parse_keyvals("type=other;level=-2;name=Sheet\\ feeder\\ bin\\ 1;")
    assert kv == {"type": "other", "level": "-2", "name": "Sheet feeder bin 1"}


def test_epson_payloads_match_escputil():
    # gutenprint escputil.c: exit_packet_mode_old + start_remote_sequence + cmd + end + do_print_cmd trailer
    head = b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL     \n\x1b@" + b"\x1b@\x1b(R\x08\x00\x00REMOTE1"
    tail = b"\x1b\x00\x00\x00\x1b\x00" + b"\x0c\x1b\x00\x1b\x00"
    assert epson.head_clean_payload() == head + b"CH\x02\x00\x00\x00" + tail
    assert epson.nozzle_check_payload() == head + b"VI\x02\x00\x00\x00NC\x02\x00\x00\x10NC\x02\x00\x00\x00" + tail


def test_maintenance_goes_to_raw_channel(fake):
    fake.nozzle_check()
    fake.head_clean()
    assert fake.raw_sent == [epson.nozzle_check_payload(), epson.head_clean_payload()]


def test_head_clean_preflight_blocks_on_low_ink_and_waste(fake):
    pf = head_clean_preflight(fake.status())
    assert not pf.ok
    assert any("Cyan" in b for b in pf.blockers) and any("waste" in b for b in pf.blockers)
    from prt.models import Ink

    assert head_clean_preflight(Status(state="idle", inks=[Ink("Black ink", "#000000", 80)])).ok
    assert not head_clean_preflight(Status(state="unknown")).ok  # fails closed without data
    assert not head_clean_preflight(Status(state="idle", inks=[Ink("Black ink", "#000000", None)])).ok
    assert not nozzle_check_preflight(Status(state="offline")).ok


def test_submit_builds_print_job(fake):
    job = fake.submit(
        b"\xff\xd8jpeg", "image/jpeg", PrintOptions(copies=2, media="4x6", tray="photo", quality="high", mono=True)
    )
    assert job == 101
    op, op_attrs, job_attrs, data = fake.client.requests[-1]
    names = {a.name: a.value for a in job_attrs}
    assert names["copies"] == 2 and names["print-quality"] == 5
    assert names["print-color-mode"] == "monochrome"
    assert names["media-col"]["media-source"][1] == "photo"
    assert names["media-col"]["media-size"][1]["x-dimension"][1] == 10160
    assert data == b"\xff\xd8jpeg"


def test_jobs(fake):
    assert fake.jobs() == []
    done = fake.jobs(include_completed=True)
    assert [j.id for j in done] == [41, 42] and done[0].state == "completed"


def test_jobs_all_merges_active_and_completed(fake):
    fake.fixture["jobs"].append({"job-id": 43, "job-name": "printing-now.jpg", "job-state": 5})
    assert [j.id for j in fake.jobs()] == [43]
    assert [j.id for j in fake.jobs(include_completed=True)] == [41, 42, 43]


def test_generic_driver_lacks_maintenance():
    d = IPPDriver(Target("pantum", "pantum.local"))
    assert not d.supports(Capability.HEAD_CLEAN)
    with pytest.raises(Unsupported) as exc:
        d.head_clean()
    assert "head clean" in str(exc.value)


def test_a_driver_without_ipp_refuses_rendered_pages():
    class Bare(Driver):
        def info(self):
            raise AssertionError("not called")

        def status(self):
            raise AssertionError("not called")

    with pytest.raises(Unsupported) as exc:
        Bare(Target("bare", "bare.local")).submit(b"page", "image/jpeg", PrintOptions())
    assert "(capability: print, driver: base)" in str(exc.value)


def test_registry():
    assert detect("EPSON XP-6000 Series") == "epson"
    assert detect("Pantum M7100DW Series") == "ipp"
    assert isinstance(make_driver(Target("demo", "demo")), FakeDriver)
    assert isinstance(make_driver(Target("x", "x", driver="epson")), epson.EpsonDriver)
    with pytest.raises(PrinterError):
        make_driver(Target("x", "x", driver="nope"))


def test_unreachable_maps_to_printer_error():
    d = IPPDriver(Target("ghost", "127.0.0.1", ipp_port=9), timeout=0.5)
    with pytest.raises(PrinterError) as exc:
        d.status()
    assert "cannot reach" in str(exc.value) and exc.value.hint


def test_test_page_renders_jpeg(fake):
    job = fake.test_page()
    assert job > 0
    _, _, _, data = fake.client.requests[-1]
    assert data[:2] == b"\xff\xd8"


def test_trays_dedupe_margin_variants():
    """HP DeskJet 3630 reports one A4 tray three times, differing only in margins."""

    def col(m):
        return {
            "media-size": {"x-dimension": 21000, "y-dimension": 29700},
            "media-source": "main",
            "media-type": "stationery",
            **{f"media-{s}-margin": m for s in ("top", "bottom", "left", "right")},
        }

    a = {
        "media-col-ready": [col(296), col(0), col(296)],
        "printer-input-tray": ["type=sheetFeedAutoNonRemovable;maxcapacity=100;level=40;name=InputTray1"],
    }
    trays = IPPDriver(Target("x", "x")).parse_trays(a)
    assert [(t.source, t.media, t.media_type, t.level) for t in trays] == [("main", "A4", "stationery", 40)]
