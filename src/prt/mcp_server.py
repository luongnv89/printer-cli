"""``prt mcp``: printer-cli as an MCP server over stdio, so any MCP client can check the printer, preview pages and
print without shelling out or knowing flags.

Tools: ``status``, ``ink``, ``list_styles``, ``preview_booth``, ``preview_article`` and ``print_file``. The preview
tools return the rendered pages as images in the tool result, so the agent can look at them in the same call, and
a single-use ``token`` that prints exactly what was previewed. ``print_file`` prints only with that token or with
``confirm: true``, and every print goes through the print guard (:func:`prt.guard.authorize`): agent-mode
consent and the page caps hold here as on the command line.

What each tool does is in :class:`Tools`, which needs no MCP SDK; :func:`build_server` wraps it in the SDK's
``MCPServer``. The SDK is an optional dependency (``pip install 'printer-cli[mcp]'``) and is imported only when
the server is built. Failures come back as tool errors carrying the same ``{"error": {...}}`` object
``prt -f json`` prints (``PrinterError.to_dict``), so an agent can branch on ``kind``.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from prt import __version__, cache, catalog, guard, render
from prt.drivers.base import PrinterError, Unreachable
from prt.drivers.registry import make_driver
from prt.models import Capability, PrintOptions, to_dict

if TYPE_CHECKING:
    from PIL import Image

    from prt.config import Config
    from prt.drivers.base import Driver

INSTALL_HINT = "install the MCP extra: pip install 'printer-cli[mcp]' (or uv tool install 'printer-cli[mcp]')"

PREVIEW_LONG_SIDE = 1200  # pixels: preview images are scaled down to this, enough to read the headline and check fit
PREVIEW_QUALITY = 85
DEFAULT_ARTICLE_IMAGES = 3  # pages an article preview shows unless asked for more
TOKEN_TTL = 30 * 60  # seconds a preview token stays valid
MAX_TOKENS = 8  # previews kept at once; the oldest is dropped first

BoothStyleName = Literal["metro", "daily", "wizard", "wanted", "certificate", "sports", "missing", "movie"]
ArticleStyleName = Literal["magazine", "classic-magazine"]
ContentType = Literal["json", "html", "text"]

INSTRUCTIONS = """\
printer-cli (prt) controls a network printer. Read state with `status` and `ink`. To print a page made from a \
photo (newspaper front page, card, certificate, poster) or an article, call `preview_booth` or \
`preview_article` first: they print nothing and return the rendered page images, whether the text fits and an \
ink estimate. Check the images, then pass the returned `token` to `print_file` to print exactly that. \
`print_file` with a `path` prints an existing file and needs `confirm: true`. `list_styles` lists every style \
with its fields and their character limits. Errors carry a machine-readable `kind`.\
"""


@dataclass
class Preview:
    """Pages a preview rendered and a token can print: the JPEG bytes sent to the printer, one job per page."""
    kind: str  # what was previewed, e.g. "wizard card"
    pages: list[bytes]
    names: list[str]  # job name of each page
    created: float


@dataclass
class Result:
    """What a tool returns: ``data`` (JSON) and, for previews, page images (JPEG bytes) to show the agent."""
    data: dict[str, Any]
    images: list[bytes] = field(default_factory=list)


def preview_image(page: Image.Image) -> bytes:
    """A page scaled down to ``PREVIEW_LONG_SIDE`` as JPEG: small enough for a tool result, big enough to read."""
    shown = page.convert("RGB")
    shown.thumbnail((PREVIEW_LONG_SIDE, PREVIEW_LONG_SIDE))
    buf = io.BytesIO()
    shown.save(buf, "JPEG", quality=PREVIEW_QUALITY)
    return buf.getvalue()


def _usage(message: str, hint: str | None = None, kind: str = "usage") -> PrinterError:
    return PrinterError(message, hint=hint, exit_code=2, kind=kind)


class Tools:
    """The MCP tools as plain methods: arguments in, JSON-able data (or a :class:`Result`) out, ``PrinterError``
    on failure. ``printer`` and ``timeout`` are the defaults ``prt mcp`` was started with (``-p``,
    ``--timeout``); a tool call may name another saved printer."""

    def __init__(self, cfg: Config, printer: str | None = None, timeout: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.printer = printer
        self.timeout = timeout
        self.clock = clock
        self._previews: dict[str, Preview] = {}
        self._lock = threading.Lock()

    # --- reading the printer ---------------------------------------------------------------------------

    def _driver(self, printer: str | None) -> Driver:
        return make_driver(self.cfg.resolve(printer or self.printer), self.timeout or self.cfg.timeout)

    def _status(self, printer: str | None):
        """The driver (None when it could not be made) and the live status, or the cached one when the printer
        does not answer."""
        target = self.cfg.resolve(printer or self.printer)
        d = None
        try:
            d = self._driver(printer)
            st = d.status()
        except Unreachable:
            cached = cache.load(target.name)
            if cached is None:
                raise
            return d, target.name, cached
        cache.save(target.name, st)
        return d, target.name, st

    def status(self, printer: str | None = None) -> dict[str, Any]:
        """State, alerts, ink, paper and waste: what ``prt status -f json`` prints."""
        d, name, st = self._status(printer)
        model, pages = name, None
        if d is not None and not st.stale:
            from prt.cli import _quick_facts
            model, _, _, pages = _quick_facts(d)
        return {"printer": name, "model": model, "page_count": pages, **to_dict(st)}

    def ink(self, printer: str | None = None) -> dict[str, Any]:
        """Ink levels: what ``prt ink -f json`` prints."""
        _, name, st = self._status(printer)
        return {"printer": name, "stale": st.stale, "inks": [to_dict(i) for i in st.inks]}

    def list_styles(self) -> dict[str, Any]:
        """Every page style with its fields and limits (``prt styles -f json``) and the input schemas' names."""
        return {"styles": catalog.styles(), "schemas": catalog.schema_summaries()}

    # --- previews ------------------------------------------------------------------------------------

    def _remember(self, preview: Preview) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._expire()
            while len(self._previews) >= MAX_TOKENS:
                self._previews.pop(next(iter(self._previews)))
            self._previews[token] = preview
        return token

    def _expire(self) -> None:
        now = self.clock()
        for token in [t for t, p in self._previews.items() if now - p.created > TOKEN_TTL]:
            del self._previews[token]

    def _preview_result(self, action: str, style: str, rendered: render.Rendered, kind: str, names: list[str],
                        shown: int, **extra: Any) -> Result:
        from prt import booth
        token = self._remember(Preview(kind, [booth.to_jpeg(p) for p in rendered.pages], names, self.clock()))
        images = [preview_image(page) for page in rendered.pages[:shown]]
        data = {"action": action, "style": style, **extra, "pages": len(rendered.pages), **rendered.fit(),
                **rendered.estimate(), "images": len(images), "printed": False, "token": token,
                "token_expires_in": TOKEN_TTL}
        return Result(data, images)

    def preview_booth(self, photo: str, style: str = "metro", story: dict[str, Any] | None = None) -> Result:
        """Render a booth page of ``photo`` (a path) without printing it. ``story`` holds any of headline, story,
        kicker, caption and byline; fields left out keep the style's built-in text."""
        from prt import booth
        if style not in booth.STYLES:
            raise _usage(f"unknown booth style '{style}'", "choose: " + ", ".join(booth.STYLES))
        if story is not None and not isinstance(story, dict):
            raise PrinterError("story must be an object", hint='e.g. {"headline": "...", "story": "..."}',
                               exit_code=3)
        path = Path(photo).expanduser()
        image = booth.load_photo(path)
        # Checked as a story file is: unknown fields, text values, limits; left-out fields keep the style's own.
        text = booth.parse_story(json.dumps({**asdict(booth.default_story(style)), **(story or {})}))
        rendered = render.booth_page(image, text, style)
        return self._preview_result("preview_booth", style, rendered, booth.KINDS[style],
                                    [f"prt booth {path.name}"], 1, photo=str(path))

    def preview_article(self, source: str | None = None, content: str | dict[str, Any] | None = None,
                        content_type: str | None = None, style: str = "magazine",
                        max_images: int = DEFAULT_ARTICLE_IMAGES) -> Result:
        """Render an article as magazine pages without printing it. Give either ``source`` (an http(s) link or a
        saved file) or ``content`` (the article itself: JSON object or text, HTML or plain text)."""
        from prt import article
        if style not in article.ARTICLE_STYLES:
            raise _usage(f"unknown article style '{style}'", "choose: " + ", ".join(article.ARTICLE_STYLES))
        if (source is None) == (content is None):
            raise _usage("give either source (a link or a file) or content (the article itself)")
        if not 1 <= max_images <= article.MAX_PAGES:
            raise _usage(f"max_images must be between 1 and {article.MAX_PAGES}")
        if source is not None:
            if source.strip() == "-":  # stdin carries the MCP protocol itself
                raise _usage("source '-' (stdin) cannot be used by the MCP server", "pass the article as content")
            text = article.load(source, self.timeout or self.cfg.timeout)
        else:
            text = article.normalise(_parse_content(content, content_type))
        rendered = render.article_pages(text, style)
        return self._preview_result("preview_article", style, rendered, article.ARTICLE_STYLES[style].kind,
                                    render.article_job_names(text.title, len(rendered.pages)),
                                    max_images, title=text.title)

    # --- printing ------------------------------------------------------------------------------------

    def print_file(self, path: str | None = None, token: str | None = None, confirm: bool = False,
                   copies: int = 1, media: str | None = None, printer: str | None = None) -> dict[str, Any]:
        """Print what a preview rendered (``token``), or a file (``path``, only with ``confirm``)."""
        if (path is None) == (token is None):
            raise _usage("give either token (from a preview) or path (a file to print)")
        if token is not None:
            if copies != 1 or media is not None:
                raise _usage("copies and media apply to a path; a token prints the previewed pages as they are")
            return self._print_preview(token, printer)
        if not confirm:
            raise PrinterError("print_file prints a file only with confirm: true — nothing was sent",
                               hint="preview the page and print its token, or pass confirm: true for this file",
                               exit_code=2, kind="confirmation_required")
        if not 1 <= copies <= 99:
            raise _usage("copies must be between 1 and 99")
        file = Path(path).expanduser()
        if not file.is_file():
            raise PrinterError(f"file not found: {file}", hint="check the path", exit_code=3)
        d = self._driver(printer)
        d.require(Capability.PRINT)
        pages = guard.job_pages([file], copies)
        with guard.authorize(self.cfg, pages, yes=True) as ticket:
            job = d.print_file(file, PrintOptions(copies=copies, media=media, job_name=file.name),
                               before_send=lambda: ticket.attempt(pages or 0))
        return {"action": "print", "printer": d.target.name, "file": str(file), "copies": copies, "job_id": job}

    def _print_preview(self, token: str, printer: str | None) -> dict[str, Any]:
        with self._lock:
            self._expire()
            preview = self._previews.get(token)
        if preview is None:
            raise _usage("unknown or expired preview token — nothing was sent",
                         "preview again and print with the token it returns (tokens are single-use and last "
                         f"{TOKEN_TTL // 60} minutes)", kind="invalid_token")
        d = self._driver(printer)
        d.require(Capability.PRINT)
        jobs: list[int] = []
        with guard.authorize(self.cfg, len(preview.pages), yes=True) as ticket:
            with self._lock:
                if self._previews.pop(token, None) is None:  # printed by a concurrent call meanwhile
                    raise _usage("this preview token was already used — nothing was sent", kind="invalid_token")
            try:
                jobs.extend(render.send_pages(d, preview.pages, preview.names, ticket))
            except PrinterError as exc:
                if not jobs:
                    raise
                raise PrinterError(f"{exc}; pages already queued as job(s) {', '.join(map(str, jobs))}",
                                   hint=exc.hint, exit_code=exc.exit_code, kind=exc.kind) from exc
        return {"action": "print", "printer": d.target.name, "kind": preview.kind, "pages": len(preview.pages),
                "job_ids": jobs}


def _parse_content(content: str | dict[str, Any] | None, content_type: str | None):
    """An article from tool-call content: a JSON object, or text that is JSON, HTML or plain text (as stdin is
    read when ``content_type`` is not given)."""
    from prt import article
    if isinstance(content, dict):
        if content_type not in (None, "json"):
            raise _usage("content is an object, so content_type can only be json")
        data, kind = json.dumps(content).encode("utf-8"), "json"
    elif isinstance(content, str):
        data = content.encode("utf-8")
        start = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
        kind = content_type or ("json" if start == b"{" else "html" if start == b"<" else "text")
        if kind not in ("json", "html", "text"):
            raise _usage(f"unknown content_type '{kind}'", "choose: json, html or text")
    else:
        raise PrinterError("content must be text or a JSON object", exit_code=3)
    if len(data) > article.MAX_BYTES:
        raise PrinterError(f"the article is larger than {article.MAX_BYTES // (1024 * 1024)} MiB",
                           hint="send just the article", exit_code=3)
    return article.parse(data, kind, "the article")


# --- the MCP SDK side ----------------------------------------------------------------------------------


def _sdk():
    """The MCP SDK modules, or a PrinterError saying how to install them."""
    try:
        import mcp_types
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise PrinterError("prt mcp needs the MCP Python SDK (mcp 2 or later), which is not installed",
                           hint=INSTALL_HINT, exit_code=2, kind="missing_dependency") from exc
    return MCPServer, mcp_types


def build_server(tools: Tools):
    """An ``MCPServer`` exposing ``tools``; raises PrinterError (kind ``missing_dependency``) without the SDK."""
    MCPServer, types = _sdk()
    server = MCPServer("prt", title="printer-cli", instructions=INSTRUCTIONS, version=__version__,
                       log_level="WARNING")
    read_only = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=False)
    prints = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=False)

    # Each tool returns a CallToolResult (text, images, structured content, is_error) built here. Their return
    # types are left unannotated: the SDK then takes the result as it is instead of wrapping it.
    def answer(call: Callable[[], Any]):
        try:
            out = call()
        except PrinterError as exc:
            error = {"error": exc.to_dict()}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(error))],
                                        structured_content=error, is_error=True)
        result = out if isinstance(out, Result) else Result(out)
        content: list[Any] = [types.TextContent(type="text", text=json.dumps(result.data, default=str))]
        content += [types.ImageContent(type="image", data=base64.b64encode(image).decode("ascii"),
                                       mime_type="image/jpeg") for image in result.images]
        return types.CallToolResult(content=content, structured_content=json.loads(json.dumps(result.data,
                                                                                              default=str)))

    @server.tool(annotations=read_only)
    def status(printer: str | None = None):
        """The printer's state, alerts, ink levels, paper and waste pad. `printer` is a saved printer name, host
        or 'demo'; left out, the one prt mcp was started with (or the default) is used. When the printer does
        not answer, its last known status is returned with `stale: true`."""
        return answer(lambda: tools.status(printer))

    @server.tool(annotations=read_only)
    def ink(printer: str | None = None):
        """Ink levels only: each cartridge's name, colour, level (0-100) and state (ok, low, replace)."""
        return answer(lambda: tools.ink(printer))

    @server.tool(annotations=read_only)
    def list_styles():
        """Every page style preview_booth and preview_article can render: what it prints, its fields and their
        character limits, how many photos it takes and a link to a sample page."""
        return answer(tools.list_styles)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                   idempotent_hint=False, open_world_hint=False))
    def preview_booth(photo: str, style: BoothStyleName = "metro",
                      story: dict[str, str] | None = None):
        """Render a photo as an A4 booth page WITHOUT printing it, and return the page image. `photo` is the path
        of a JPEG, PNG, WebP, GIF, BMP or TIFF file. `style`: metro (colour tabloid), daily (black-and-white
        broadsheet), wizard (fantasy trading card), wanted (pirate poster), certificate (award certificate), sports
        (sports trading card), missing (missing-pet poster) or movie (movie poster). `story` may hold headline, story, kicker,
        caption and byline (see list_styles for what each is on a style, and the limits); fields left out keep
        the style's built-in text. The result says whether the text fits (`fits`, `cut`), estimates ink use,
        and gives a `token`: pass it to print_file to print this exact page."""
        return answer(lambda: tools.preview_booth(photo, style, story))

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                   idempotent_hint=False, open_world_hint=True))
    def preview_article(source: str | None = None, content: str | dict[str, Any] | None = None,
                        content_type: ContentType | None = None, style: ArticleStyleName = "magazine",
                        max_images: int = DEFAULT_ARTICLE_IMAGES):
        """Render an article as A4 magazine pages WITHOUT printing them, and return the first `max_images` page
        images. Give `source` (an http(s) link to a blog post, news article or X post, or the path of a saved
        JSON, HTML or text file) or `content` (the article itself: an object {"title", "body", "author"?,
        "source"?, "url"?} with body as text or a list of paragraphs, or HTML or plain text; `content_type`
        says which when it is text). `style`: magazine or classic-magazine. Returns the page count, an ink
        estimate and a `token`: pass it to print_file to print these exact pages."""
        return answer(lambda: tools.preview_article(source, content, content_type, style, max_images))

    @server.tool(annotations=prints)
    def print_file(path: str | None = None, token: str | None = None, confirm: bool = False, copies: int = 1,
                   media: str | None = None, printer: str | None = None):
        """Print on paper. Give `token` from preview_booth or preview_article to print exactly what it showed
        (single use), or `path` of a file (JPEG, PNG; PDF and others via CUPS) together with `confirm: true`.
        Without one of them nothing is printed. `copies` (1-99) and `media` (a4, letter, 4x6, ...) apply to a
        path. Page caps and agent mode (PRT_AGENT=1) are enforced; a refusal says why in `error.kind`."""
        return answer(lambda: tools.print_file(path, token, confirm, copies, media, printer))

    return server


def serve(cfg: Config, printer: str | None = None, timeout: float | None = None) -> None:
    """Run the MCP server on stdin/stdout until the client disconnects."""
    build_server(Tools(cfg, printer, timeout)).run("stdio")
