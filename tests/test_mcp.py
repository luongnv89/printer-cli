"""prt mcp: the MCP server's tools, on the demo (fake) printer only — nothing here reaches a real printer."""

import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import PRT, clean_env
from prt import catalog, cli, mcp_server
from prt.config import Config
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.mcp_server import Tools
from prt.transport import ipp

try:
    import mcp
except ImportError:  # the SDK is an optional extra; the tool logic is tested without it
    mcp = None

needs_sdk = pytest.mark.skipif(mcp is None, reason="needs the MCP SDK (pip install 'printer-cli[mcp]')")
TOOLS = {"status", "ink", "preview_booth", "preview_article", "print_file", "list_styles"}
ARTICLE = {"title": "Paper", "body": ["Words on paper. " * 40] * 3, "author": "Jo"}


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver behind every tool call; ``sent()`` counts the Print-Jobs that reached it."""
    fake = FakeDriver()
    monkeypatch.setattr("prt.mcp_server.make_driver", lambda target, timeout=5.0: fake)

    def sent():
        return [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]
    return SimpleNamespace(driver=fake, sent=sent)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def read(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tools(clock):
    return Tools(Config.load(None), "demo", clock=clock.read)


def _photo(tmp_path, name="photo.jpg"):
    path = tmp_path / name
    Image.new("RGB", (300, 400), (0, 0, 255)).save(path)
    return path


def _kind(call):
    with pytest.raises(PrinterError) as info:
        call()
    return info.value.kind


# --- reading ---------------------------------------------------------------------------------------------


def test_status_and_ink_report_the_printer(tools):
    status = tools.status()
    assert status["printer"] == "demo" and status["model"] == "EPSON XP-6000 Series" and not status["stale"]
    assert status["inks"] and {"state", "alerts", "trays"} <= set(status)
    ink = tools.ink()
    assert ink == {"printer": "demo", "stale": False, "inks": status["inks"]}


def test_list_styles_is_the_catalog(tools):
    out = tools.list_styles()
    assert out["styles"] == catalog.styles()
    assert set(out["schemas"]) == set(catalog.schema_names())


# --- previews --------------------------------------------------------------------------------------------


def test_preview_booth_returns_the_page_image_and_prints_nothing(tools, printer, tmp_path):
    result = tools.preview_booth(str(_photo(tmp_path)), "wizard", {"headline": "Merlin"})
    assert not printer.sent()
    data = result.data
    assert data["action"] == "preview_booth" and data["style"] == "wizard" and data["pages"] == 1
    assert data["fits"] is True and data["cut"] == {} and data["printed"] is False
    assert data["estimate"]["pages"] == 1 and data["token"] and data["images"] == 1
    image = Image.open(io.BytesIO(result.images[0]))
    assert image.format == "JPEG" and max(image.size) == mcp_server.PREVIEW_LONG_SIDE


def test_preview_booth_reports_text_that_does_not_fit(tools, tmp_path):
    data = tools.preview_booth(str(_photo(tmp_path)), "metro", {"headline": "Breaking " * 13}).data
    assert data["fits"] is False and "headline" in data["cut"]


@pytest.mark.parametrize("story, kind", [
    ({"headline": "Hi", "colour": "red"}, "invalid_input"),  # unknown field
    ({"headline": 3}, "invalid_input"),  # not text
    ({"headline": "x" * 500}, "invalid_input"),  # over the limit
    ("not an object", "invalid_input"),
])
def test_preview_booth_checks_the_story_as_a_story_file(tools, tmp_path, story, kind):
    assert _kind(lambda: tools.preview_booth(str(_photo(tmp_path)), "metro", story)) == kind


def test_preview_booth_refuses_a_bad_style_or_photo(tools, tmp_path):
    assert _kind(lambda: tools.preview_booth(str(_photo(tmp_path)), "comic")) == "usage"
    assert _kind(lambda: tools.preview_booth(str(tmp_path / "missing.jpg"))) == "invalid_input"


@pytest.mark.parametrize("content, content_type", [
    (ARTICLE, None),
    (json.dumps(ARTICLE), None),
    ("<html><body><article><h1>Paper</h1><p>" + "Words on paper. " * 60 + "</p></article></body></html>", None),
    ("Paper\n\n" + "Words on paper. " * 60, "text"),
])
def test_preview_article_takes_the_article_as_content(tools, printer, content, content_type):
    result = tools.preview_article(content=content, content_type=content_type, style="classic-magazine")
    assert not printer.sent()
    data = result.data
    assert data["action"] == "preview_article" and data["style"] == "classic-magazine" and data["pages"] >= 1
    assert data["images"] == len(result.images) == min(data["pages"], mcp_server.DEFAULT_ARTICLE_IMAGES)
    assert data["estimate"]["pages"] == data["pages"] and data["fits"] is True


def test_preview_article_reads_a_saved_file(tools, tmp_path):
    path = tmp_path / "article.json"
    path.write_text(json.dumps(ARTICLE))
    assert tools.preview_article(source=str(path)).data["title"] == "Paper"


def test_preview_article_shows_as_many_pages_as_asked(tools):
    long = {"title": "Long", "body": ["Words on paper, many of them. " * 60] * 12}
    result = tools.preview_article(content=long, max_images=1)
    assert result.data["pages"] > 1 and len(result.images) == 1


@pytest.mark.parametrize("kwargs", [
    {},  # neither source nor content
    {"source": "a.json", "content": "text"},  # both
    {"source": "-"},  # stdin is the MCP protocol stream
    {"content": ARTICLE, "content_type": "html"},
    {"content": "text", "content_type": "pdf"},
    {"content": ARTICLE, "style": "tabloid"},
    {"content": ARTICLE, "max_images": 0},
])
def test_preview_article_refuses_bad_arguments(tools, kwargs):
    assert _kind(lambda: tools.preview_article(**kwargs)) == "usage"


# --- printing --------------------------------------------------------------------------------------------


def test_print_file_needs_a_token_or_confirm(tools, printer, tmp_path):
    photo = str(_photo(tmp_path))
    assert _kind(lambda: tools.print_file(path=photo)) == "confirmation_required"
    assert _kind(lambda: tools.print_file()) == "usage"
    assert _kind(lambda: tools.print_file(path=photo, token="abc", confirm=True)) == "usage"
    assert not printer.sent()


def test_print_file_prints_a_file_with_confirm(tools, printer, tmp_path):
    out = tools.print_file(path=str(_photo(tmp_path)), confirm=True, copies=2)
    assert out["action"] == "print" and out["printer"] == "demo" and out["copies"] == 2
    assert isinstance(out["job_id"], int) and len(printer.sent()) == 1


def test_print_file_refuses_a_missing_file(tools, printer, tmp_path):
    assert _kind(lambda: tools.print_file(path=str(tmp_path / "nope.jpg"), confirm=True)) == "invalid_input"
    assert not printer.sent()


def test_a_preview_token_prints_exactly_the_previewed_pages_once(tools, printer, tmp_path):
    token = tools.preview_article(content=ARTICLE).data["token"]
    pages = tools.preview_article(content=ARTICLE).data["pages"]  # a second preview: its own token
    out = tools.print_file(token=token)
    assert out["pages"] == pages and len(out["job_ids"]) == pages == len(printer.sent())
    assert all(data[:2] == b"\xff\xd8" for data in printer.sent())  # full-size JPEG pages
    assert _kind(lambda: tools.print_file(token=token)) == "invalid_token"  # single use
    assert len(printer.sent()) == pages


def test_a_token_prints_the_booth_page(tools, printer, tmp_path):
    token = tools.preview_booth(str(_photo(tmp_path))).data["token"]
    assert tools.print_file(token=token)["kind"] == "metro newspaper page"
    assert len(printer.sent()) == 1


def test_a_token_expires(tools, printer, tmp_path, clock):
    token = tools.preview_booth(str(_photo(tmp_path))).data["token"]
    clock.tick(mcp_server.TOKEN_TTL + 1)
    assert _kind(lambda: tools.print_file(token=token)) == "invalid_token"
    assert not printer.sent()


def test_only_the_latest_previews_keep_their_tokens(tools, printer, tmp_path):
    photo = str(_photo(tmp_path))
    first = tools.preview_booth(photo).data["token"]
    for _ in range(mcp_server.MAX_TOKENS):
        last = tools.preview_booth(photo).data["token"]
    assert _kind(lambda: tools.print_file(token=first)) == "invalid_token"
    assert tools.print_file(token=last)["job_ids"]


def test_unknown_tokens_and_token_options_are_refused(tools, printer, tmp_path):
    assert _kind(lambda: tools.print_file(token="made-up")) == "invalid_token"
    token = tools.preview_booth(str(_photo(tmp_path))).data["token"]
    assert _kind(lambda: tools.print_file(token=token, copies=3)) == "usage"
    assert not printer.sent()
    assert tools.print_file(token=token)["job_ids"]  # a refused call does not use the token up


def test_printing_goes_through_the_page_caps(printer, tmp_path, clock):
    cfg = Config.load(None)
    cfg.max_pages_per_run = 1
    tools = Tools(cfg, "demo", clock=clock.read)
    long = {"title": "Long", "body": ["Words on paper, many of them. " * 60] * 12}
    token = tools.preview_article(content=long).data["token"]
    assert _kind(lambda: tools.print_file(token=token)) == "page_cap_exceeded"
    assert not printer.sent()
    cfg.max_pages_per_run = None
    assert tools.print_file(token=token)["job_ids"]  # a refusal keeps the token


def test_agent_mode_accepts_the_token_or_confirm_as_consent(tools, printer, tmp_path, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    photo = str(_photo(tmp_path))
    assert tools.print_file(token=tools.preview_booth(photo).data["token"])["job_ids"]
    assert tools.print_file(path=photo, confirm=True)["job_id"]
    assert _kind(lambda: tools.print_file(path=photo)) == "confirmation_required"
    assert len(printer.sent()) == 2


def test_a_failure_part_way_names_the_pages_already_queued(tools, printer, monkeypatch):
    long = {"title": "Long", "body": ["Words on paper, many of them. " * 60] * 12}
    token = tools.preview_article(content=long).data["token"]
    real, calls = printer.driver.submit, []

    def second_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise PrinterError("printer went away", kind="unreachable")
        return real(*args, **kwargs)
    monkeypatch.setattr(printer.driver, "submit", second_fails)
    with pytest.raises(PrinterError) as info:
        tools.print_file(token=token)
    assert info.value.kind == "unreachable" and "already queued as job(s)" in str(info.value)


# --- over MCP --------------------------------------------------------------------------------------------


@pytest.fixture
def client(tools):
    return mcp.Client(mcp_server.build_server(tools))


@needs_sdk
async def test_the_server_lists_the_six_tools(client):
    async with client as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    assert set(listed) == TOOLS
    assert listed["preview_booth"].input_schema["properties"]["style"]["enum"] == ["metro", "daily", "wizard",
                                                                                   "wanted"]
    assert listed["status"].annotations.read_only_hint is True
    assert listed["print_file"].annotations.read_only_hint is False


@needs_sdk
async def test_a_preview_returns_the_page_image_in_the_tool_result(client, printer, tmp_path):
    async with client as c:
        result = await c.call_tool("preview_booth", {"photo": str(_photo(tmp_path)), "style": "wanted",
                                                     "story": {"headline": "Long John"}})
        assert not result.is_error
        text, image = result.content
        assert text.type == "text" and json.loads(text.text) == result.structured_content
        assert image.type == "image" and image.mime_type == "image/jpeg"
        assert Image.open(io.BytesIO(base64.b64decode(image.data))).format == "JPEG"
        printed = await c.call_tool("print_file", {"token": result.structured_content["token"]})
    assert not printed.is_error and len(printed.structured_content["job_ids"]) == len(printer.sent()) == 1


@needs_sdk
async def test_a_refusal_is_a_tool_error_with_the_error_object(client, printer, tmp_path):
    async with client as c:
        result = await c.call_tool("print_file", {"path": str(_photo(tmp_path))})
    assert result.is_error
    error = json.loads(result.content[0].text)["error"]
    assert error == result.structured_content["error"]
    assert error["kind"] == "confirmation_required" and error["code"] == 2 and error["hint"]
    assert not printer.sent()


@needs_sdk
async def test_prt_mcp_serves_over_stdio(tmp_path):
    params = mcp.StdioServerParameters(command=PRT[0], args=[*PRT[1:], "-p", "demo", "mcp"],
                                       env=clean_env(XDG_CONFIG_HOME=tmp_path / "config",
                                                     XDG_CACHE_HOME=tmp_path / "cache"),
                                       cwd=str(Path(__file__).resolve().parents[1]))
    async with mcp.Client(params) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOLS
        ink = await c.call_tool("ink", {})
    assert not ink.is_error and ink.structured_content["printer"] == "demo"


# --- without the SDK -------------------------------------------------------------------------------------


def test_prt_mcp_without_the_sdk_says_how_to_install_it(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)  # import fails as if not installed
    assert cli.main(["-f", "json", "mcp"]) == 2
    error = json.loads(capsys.readouterr().err.splitlines()[-1])["error"]
    assert error["kind"] == "missing_dependency" and "printer-cli[mcp]" in error["hint"]


def test_the_tools_work_without_the_sdk(monkeypatch, tools):
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    assert tools.ink()["printer"] == "demo"
    with pytest.raises(PrinterError):
        mcp_server.build_server(tools)
