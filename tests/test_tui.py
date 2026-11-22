import pytest
from textual.widgets import Button, DataTable, RichLog, Static, TabbedContent

from prt.config import Config
from prt.drivers.fake import FakeDriver
from prt.models import Target
from prt.tui.app import ConfirmScreen, PrtApp, build_targets
from prt.tui.widgets import render_alerts, render_tanks

DEMO = [Target(name="demo", host="demo", driver="fake"), Target(name="demo2", host="demo", driver="fake")]


async def settle(pilot, app):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_tanks_render_real_colours():
    st = FakeDriver().status()
    text = render_tanks(st.inks)
    plain = text.plain
    for label in ("PB", "C", "M", "Y", "BK", "92%", "1%", "12%", "✗ out", "▲ low"):
        assert label in plain
    styles = {str(span.style) for span in text.spans}
    assert any("#00FFFF" in s for s in styles) and any("#FF00FF" in s for s in styles)


def test_alerts_all_clear():
    from prt.models import Status
    assert "all clear" in render_alerts(Status(state="idle")).plain


async def test_dashboard_loads_from_driver(tmp_path):
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        top = str(app.query_one("#topbar", Static).render())
        assert "EPSON XP-6000 Series" in top and "idle" in top
        assert "92%" in str(app.query_one("#ink").render())
        assert "20.50.NU30Q6" in str(app.query_one("#system").render())
        assert "waste ink pad" in str(app.query_one("#alerts").render())
        app.save_screenshot(str(tmp_path / "dash.svg"))


async def test_clean_confirm_shows_blockers_and_cancel_sends_nothing():
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "Cyan" in str(app.screen.query_one("#details").render())
        assert "anyway" in str(app.screen.query_one("#confirm", Button).label)
        await pilot.press("escape")
        await settle(pilot, app)
        assert app.drivers["demo"].raw_sent == []


async def test_clean_without_status_fails_closed():
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        app.status = None
        await pilot.press("c")
        await pilot.pause()
        assert "unknown" in str(app.screen.query_one("#details").render())
        assert "anyway" in str(app.screen.query_one("#confirm", Button).label)


async def test_nozzle_check_confirmed_sends_payload():
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("n")
        await pilot.pause()
        await pilot.click("#confirm")
        await settle(pilot, app)
        assert len(app.drivers["demo"].raw_sent) == 1
        assert app.query_one(TabbedContent).active == "maintain"


async def test_switch_printer_and_jobs_tab():
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("2")
        await settle(pilot, app)
        assert app.target.name == "demo2"
        await pilot.press("j")
        await settle(pilot, app)
        assert app.query_one("#jobs-table", DataTable).row_count == 2


async def test_scan_saves_file(tmp_path):
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("s")
        app.query_one("#scan-dir").value = str(tmp_path)
        await pilot.click("#btn-scan")
        await settle(pilot, app)
        assert list(tmp_path.glob("scan-*.pdf"))
        assert "saved" in str(app.query_one("#scan-result", Static).render())


async def test_narrow_terminal_stacks_panels():
    app = PrtApp(DEMO)
    async with app.run_test(size=(80, 40)) as pilot:
        await settle(pilot, app)
        assert app.query_one("#dash").has_class("narrow")


def test_build_targets_falls_back_to_demo(tmp_path, monkeypatch):
    monkeypatch.setattr("prt.discovery.discover", lambda timeout=2.0: [])
    targets, notice = build_targets(Config(path=tmp_path / "c.toml"), None)
    assert targets[0].driver == "fake" and "demo" in notice


def test_tanks_multi_colour_cartridge():
    """HP tri-colour cartridges report marker-colors '#00FFFF#FF00FF#FFFF00' (was a crash)."""
    from prt.models import Ink
    inks = [Ink(name="tri-color ink", color="#00FFFF#FF00FF#FFFF00", level=90, low=1),
            Ink(name="black ink", color="#000000", level=100, low=1),
            Ink(name="odd ink", color="none", level=50)]
    text = render_tanks(inks)
    styles = {str(span.style) for span in text.spans}
    assert {"#00FFFF", "#FF00FF", "#FFFF00"} <= styles
    assert not any(s.count("#") > 1 for s in styles)
    from rich.console import Console
    Console(file=__import__("io").StringIO(), color_system="truecolor").print(text)


def test_ink_style_multi_colour():
    from prt.style import ink_colors, ink_style
    assert ink_colors("#00FFFF#FF00FF#FFFF00") == ["#00FFFF", "#FF00FF", "#FFFF00"]
    assert ink_style("#00FFFF#FF00FF#FFFF00") == "#00FFFF"
    assert ink_style("#000000") == "bold" and ink_style("none") == "bold"


async def test_stray_output_does_not_crash_app():
    """Textual routes captured stdout/stderr to App._print(text, stderr=); a PrtApp method of the
    same name used to shadow it, so any library warning crashed the dashboard."""
    import sys
    app = PrtApp(DEMO)
    async with app.run_test(size=(130, 40)) as pilot:
        await settle(pilot, app)
        print("stray stdout")
        print("stray stderr", file=sys.stderr)
        await pilot.pause()
