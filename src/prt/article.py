"""Article printing: a blog post, news article or X post, laid out over as many A4 pages as it needs.

Sources: an http(s) link, fetched and stripped down to the title, author, site name and body text (an X
post goes through X's public oEmbed endpoint), or a saved file: article JSON, HTML or plain text.
Styles: ``magazine`` (bold headline, three columns, colour accents) and ``classic-magazine`` (cream page,
centred single column, drop cap, double rules and fleurons). Pages are rendered like the photo booth's,
as A4 at 150 dpi, so each one prints as a JPEG over IPP.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from PIL import Image, ImageDraw

from prt import __version__
from prt.booth import INK, RUST, SEPIA, SEPIA_DARK, STDIN, _centred, read_stdin
from prt.drivers.base import PrinterError
from prt.layout import (BOTTOM, CONTENT_W, _clean, _col_width, _ellipsize, _fit, _glyphs, _lead, _text_block,
                        _wrapped, draw_columns, paginate)
from prt.paper import A4, Paper
from prt.testpage import H, MARGIN, W, _font

# --- the article -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    title: str
    body: tuple[str, ...]  # paragraphs
    author: str = ""
    source: str = ""  # site or publication name
    url: str = ""


MAX_BYTES = 5 * 1024 * 1024  # largest web page or file read; far more than any article's text
MAX_REDIRECTS = 5
MAX_PAGES = 30  # longer text is refused rather than sent to the printer by the ream
USER_AGENT = f"printer-cli/{__version__}"
OEMBED = "https://publish.twitter.com/oembed"
FILE_HINT = "save the article as a JSON, HTML or text file and print the file instead (see the article-print skill)"
_LINK = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def load(source: str, timeout: float) -> Article:
    """The article behind a link (``scheme://…``), on stdin (``-``) or in a local file, normalised for the page
    font."""
    if source == STDIN:
        return normalise(load_stdin())
    return normalise(from_link(source, timeout) if _LINK.match(source) else load_file(Path(source)))


# --- fetching --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    url: str  # after redirects
    media_type: str
    charset: str | None
    content: bytes


def check_link(url: str) -> None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:  # e.g. an unclosed [IPv6] host
        raise PrinterError(f"cannot fetch {url}: {exc}", hint="check the link", exit_code=3) from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise PrinterError(f"cannot fetch {url}: only http:// and https:// links are supported",
                           hint="give the article's web link, or the path of a saved file", exit_code=3)
    if not host:
        raise PrinterError(f"cannot fetch {url}: the link has no host name", hint="check the link", exit_code=3)


def fetch(url: str, timeout: float, transport: httpx.BaseTransport | None = None) -> Fetched:
    """GET ``url`` with certificate checks, at most MAX_REDIRECTS redirects and MAX_BYTES of (decoded) body."""
    check_link(url)
    try:
        with httpx.Client(transport=transport, verify=True, timeout=timeout, follow_redirects=True,
                          max_redirects=MAX_REDIRECTS, headers={"User-Agent": USER_AGENT}) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise PrinterError(f"cannot fetch {url}: the site answered HTTP {response.status_code} "
                                       f"{response.reason_phrase}".rstrip(), hint=FILE_HINT, exit_code=3, kind="fetch_failed")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_BYTES:
                    raise _too_large(url)
                chunks, size = [], 0
                for chunk in response.iter_bytes():  # counted as it arrives: a huge page is never held
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise _too_large(url)
                    chunks.append(chunk)
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return Fetched(str(response.url), media_type, response.charset_encoding, b"".join(chunks))
    except httpx.TooManyRedirects as exc:
        raise PrinterError(f"cannot fetch {url}: more than {MAX_REDIRECTS} redirects", hint="check the link",
                           exit_code=3, kind="fetch_failed") from exc
    except httpx.TimeoutException as exc:
        raise PrinterError(f"cannot fetch {url}: no answer within {timeout:g} s",
                           hint="try again, or allow longer with --timeout", exit_code=3, kind="fetch_failed") from exc
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:  # UnicodeError: a host IDNA refuses
        raise PrinterError(f"cannot fetch {url}: {exc}", hint="check the link and your connection",
                           exit_code=3, kind="fetch_failed") from exc


def _too_large(url: str) -> PrinterError:
    return PrinterError(f"cannot fetch {url}: larger than {MAX_BYTES // (1024 * 1024)} MiB",
                        hint="link to the article page itself, or " + FILE_HINT, exit_code=3, kind="fetch_failed")


def from_link(url: str, timeout: float) -> Article:
    check_link(url)
    if is_x_post(url):
        return from_x(url, timeout)
    page = fetch(url, timeout)
    host = _site(page.url)
    if page.media_type in ("text/html", "application/xhtml+xml", ""):
        return from_html(_decode(page.content, page.charset), page.url, host)
    if page.media_type == "text/plain":
        return from_text(_decode(page.content, page.charset), source=host, url=page.url)
    raise PrinterError(f"cannot print {url}: it is not a web page ({page.media_type})",
                       hint="link to the article page itself, or " + FILE_HINT, exit_code=3)


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def _decode(content: bytes, charset: str | None) -> str:
    declared = _META_CHARSET.search(content[:4096])
    for encoding in (charset, declared.group(1).decode("ascii") if declared else None):
        if encoding:
            try:
                return content.decode(encoding, errors="replace")
            except (LookupError, UnicodeError):  # a charset Python does not know, or not a page charset
                continue
    return content.decode("utf-8", errors="replace")


# --- X posts ---------------------------------------------------------------------------------

X_HOSTS = ("x.com", "twitter.com")


def is_x_post(url: str) -> bool:
    """True for links on X (x.com, twitter.com and their subdomains), judged by the host name only."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host in X_HOSTS or host.endswith(tuple("." + h for h in X_HOSTS))


def from_x(url: str, timeout: float) -> Article:
    """An X post's text, through the public oEmbed endpoint (works for public posts only)."""
    if not re.search(r"/status(?:es)?/\d+", urlsplit(url).path):
        raise PrinterError(f"{url} is not a link to an X post",
                           hint="copy the post's own link (it contains /status/)", exit_code=3)
    hint = "X shares public posts only; for a private, deleted or long post, " + FILE_HINT
    try:
        page = fetch(f"{OEMBED}?{urlencode({'url': url, 'omit_script': 'true', 'dnt': 'true'})}", timeout)
    except PrinterError as exc:
        raise PrinterError(f"X did not return the post {url} ({exc})", hint=hint, exit_code=3, kind="fetch_failed") from exc
    try:
        data = json.loads(page.content)
    except ValueError as exc:
        raise PrinterError(f"X sent an unreadable answer for {url}", hint=hint, exit_code=3, kind="fetch_failed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("html"), str):
        raise PrinterError(f"X sent no post text for {url}", hint=hint, exit_code=3, kind="fetch_failed")
    parser = _Extractor()
    parser.feed(data["html"])
    parser.close()

    def text(key: str) -> str:
        return data[key] if isinstance(data.get(key), str) else ""
    name, handle = text("author_name"), urlsplit(text("author_url")).path.strip("/")
    author = f"{name} (@{handle})" if name and handle else name or (f"@{handle}" if handle else "")
    # The embed is the post's <p> followed by "— name (@handle) date": only the <p> is the post.
    return Article(title=f"{name or 'A post'} on X", body=tuple(p.text for p in parser.paragraphs if p.tag == "p"),
                   author=author, source="X", url=text("url") or url)


# --- web pages -------------------------------------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
         "wbr"}
_SKIP = {"nav", "aside", "footer", "header", "script", "style", "form", "noscript", "iframe", "svg", "template",
         "button", "select", "textarea", "canvas", "dialog", "figure"}
_BLOCKS = {"p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "dd"}
_NEVER_CLUTTER = {"html", "body", "article", "main"}
# Page furniture recognised by a whole word of its class or id: "ad" matches "ad-slot", never "header".
_CLUTTER = re.compile(r"(?:^|[\s_-])(?:ads?|advert\w*|sponsor\w*|promo\w*|share|sharing|social|comments?|"
                      r"newsletter|subscribe|subscription|related|recommended|sidebar|cookies?|consent|banner|"
                      r"popup|modal|breadcrumbs?|paywall|menu)(?:$|[\s_-])", re.I)


@dataclass
class _Node:
    tag: str
    id: int
    skip: bool  # furniture by its tag (nav, aside, script…)
    clutter: bool  # furniture by its class or id
    outer_clutter: int = 0  # for <article>/<main>: the clutter count outside it, restored when it closes
    outer_buried: int = 0  # for <article>/<main>: the burial outside it, restored when it closes


@dataclass(frozen=True)
class _Para:
    text: str
    tag: str  # the block it came from: p, li, h2…
    article: int | None  # innermost <article> around it
    main: int | None  # innermost <main> around it
    parent: int  # the element holding the paragraph
    buried: int = 0  # class/id clutter around its <article> elements (not outside a <main>): a comment, a teaser…
    in_main: bool = False  # its innermost <article>/<main> is a <main>: it is not in one of main's articles


class _Extractor(HTMLParser):
    """Collects the page's metadata and every paragraph outside page furniture, with where it sits.

    Elements left open (HTML lets </p>, </td>, </tr>… be omitted) can make the stack deep, so what each
    event needs from it is kept up to date on push and pop instead of being searched for.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Node] = []
        self.open_tags: Counter[str] = Counter()  # tag -> how many are open
        self.blocks: list[int] = []  # stack positions of the open _BLOCKS elements
        self.articles: list[int] = []  # ids of the open <article> elements
        self.mains: list[int] = []  # ids of the open <main> elements
        self.sections: list[str] = []  # the open <article>/<main> tags, outermost first
        self.skipped_tags = 0  # open elements dropped for their tag: dropped wherever they are
        self.clutter = 0  # open elements dropped for their class or id, since the innermost <article>/<main>
        self.buried = 0  # class/id clutter elements around the open <article> elements, since the innermost <main>
        self.serial = 0
        self.buffer: list[str] = []
        self.paragraphs: list[_Para] = []
        self.meta: dict[str, str] = {}
        self.title: list[str] | None = None  # <title> text, while inside it
        self.page_title = ""
        self.h1: list[str] | None = None
        self.first_h1 = ""
        self.byline: list[str] | None = None  # text of a rel="author" link
        self.author_link = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and key not in self.meta and values.get("content", "").strip():
                self.meta[key] = values["content"]
            return
        if tag == "br" and not self.skipping:
            self.buffer.append("\n")
        if tag in _VOID:
            return
        if tag in ("p", "li") and self.stack and self.stack[-1].tag == tag:  # <p>/<li> closed implicitly
            self._pop()
        if tag in _BLOCKS:
            self._flush()
        skip = tag in _SKIP
        clutter = tag not in _NEVER_CLUTTER and any(_CLUTTER.search(values.get(name, "")) for name in ("class", "id"))
        self.serial += 1
        node = _Node(tag, self.serial, skip, clutter)
        if tag in ("article", "main"):  # a sidebar-named wrapper must not hide the article inside it
            node.outer_clutter, self.clutter = self.clutter, 0
            node.outer_buried = self.buried
            # A <main> is the page's main content, never a comment or teaser: clutter around it is layout.
            self.buried = 0 if tag == "main" else self.buried + node.outer_clutter
            (self.articles if tag == "article" else self.mains).append(node.id)
            self.sections.append(tag)
        if tag in _BLOCKS:
            self.blocks.append(len(self.stack))
        self.stack.append(node)
        self.open_tags[tag] += 1
        self.skipped_tags += skip
        self.clutter += clutter
        if tag == "title" and not self.page_title:
            self.title = []
        elif tag == "h1" and not self.first_h1:
            self.h1 = []
        elif tag == "a" and "author" in values.get("rel", "").split() and not self.author_link:
            self.byline = []
        elif tag == "li" and not self.skipping:
            self.buffer.append("- ")

    @property
    def skipping(self) -> bool:
        return bool(self.skipped_tags or self.clutter)

    def handle_endtag(self, tag: str) -> None:
        if self.open_tags[tag]:
            while self._pop().tag != tag:
                pass

    def _pop(self) -> _Node:
        node = self.stack[-1]
        if node.tag in _BLOCKS:
            self._flush()
            self.blocks.pop()
        self.stack.pop()
        self.open_tags[node.tag] -= 1
        self.skipped_tags -= node.skip
        self.clutter -= node.clutter
        if node.tag in ("article", "main"):
            self.clutter, self.buried = node.outer_clutter, node.outer_buried
            (self.articles if node.tag == "article" else self.mains).pop()
            self.sections.pop()
        if node.tag == "title" and self.title is not None:
            self.page_title, self.title = "".join(self.title), None
        elif node.tag == "h1" and self.h1 is not None:
            self.first_h1, self.h1 = "".join(self.h1), None
        elif node.tag == "a" and self.byline is not None:
            self.author_link, self.byline = "".join(self.byline), None
        return node

    def handle_data(self, data: str) -> None:
        # The title, first heading and author link count even inside a dropped <header>.
        for part in (self.title, self.h1, self.byline):
            if part is not None:
                part.append(data)
        if not self.skipping and self.blocks:
            self.buffer.append(data.replace("\r", " ").replace("\n", " "))  # only <br> breaks a line

    def _flush(self) -> None:
        text, self.buffer = "".join(self.buffer), []
        lines = [" ".join(line.split()) for line in text.split("\n")]
        if not any(line.strip("- ") for line in lines):
            return
        at = self.blocks[-1] if self.blocks else len(self.stack)
        tag = self.stack[at].tag if self.blocks else ""
        parent = self.stack[at - 1].id if at else 0
        article = self.articles[-1] if self.articles else None
        main = self.mains[-1] if self.mains else None
        in_main = bool(self.sections) and self.sections[-1] == "main"
        self.paragraphs += [_Para(line, tag, article, main, parent, self.buried, in_main) for line in lines if line]

    def close(self) -> None:
        super().close()
        while self.stack:
            self._pop()
        self._flush()


BURIED = 0.1  # weight of text per level of class/id clutter around its <article>: a comment, a teaser…
BARE = 0.8  # weight of text in a plain block: an <article>/<main> wins unless a block holds clearly more


def _body(paras: list[_Para]) -> list[_Para]:
    """The paragraphs of the candidate holding the most weighted text: each <article>, each <main> (its text
    outside its articles) and each block of paragraphs, every one weighted down by BURIED per level of
    clutter around it, and a plain block by BARE. Ties go to an article, then a main, then a block."""
    groups: dict[tuple[int, int, int], list[_Para]] = {}
    for para in paras:
        for rank, group in ((0, para.article), (1, para.main if para.in_main else None), (2, para.parent)):
            if group is not None:
                groups.setdefault((rank, group, para.buried), []).append(para)

    def weight(item: tuple[tuple[int, int, int], list[_Para]]) -> float:
        (rank, _, buried), group = item
        return sum(len(p.text) for p in group) * BURIED ** buried * (BARE if rank == 2 else 1)
    return max(sorted(groups.items(), key=lambda item: item[0][0]), key=weight, default=(None, []))[1]


def from_html(html: str, url: str = "", host: str = "") -> Article:
    """Title, author, site and body of a web page. Navigation, ads, share bars, comments and the like are left
    out; the body is the article, main or block holding the most text (see ``_body``). An <article> inside
    class/id clutter (a comment, a related-post teaser) counts a tenth as much per level of clutter, so it
    wins only when it holds far more than anything else; clutter around a <main> (a theme's sidebar-named
    wrapper) is ignored."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    meta = parser.meta
    title = (meta.get("og:title") or meta.get("twitter:title") or parser.page_title or parser.first_h1).strip()
    author = next((a for a in (meta.get("author"), meta.get("article:author"), parser.author_link)
                   if a and a.strip() and not _LINK.match(a.strip())), "")
    source = (meta.get("og:site_name") or meta.get("application-name") or host).strip()
    body = [p.text for p in _body(parser.paragraphs) if " ".join(p.text.split()) != " ".join(title.split())]
    return Article(title=title or source, body=tuple(body), author=author.strip(), source=source, url=url)


def from_text(text: str, source: str = "", url: str = "") -> Article:
    """Plain text: the first line is the title; blank lines separate paragraphs."""
    head, _, rest = text.strip().partition("\n")
    body = tuple(" ".join(block.split()) for block in re.split(r"\n\s*\n", rest) if block.strip())
    return Article(title=head.strip(), body=body, source=source, url=url)


# --- files -----------------------------------------------------------------------------------

FIELDS = ("title", "body", "author", "source", "url")
JSON_HINT = 'expected {"title": "...", "body": "first paragraph\\nsecond paragraph", "author": "...", ' \
            '"source": "...", "url": "..."}'


def load_file(path: Path) -> Article:
    """An article saved as JSON (see JSON_HINT), HTML (.html, .htm, .xhtml) or plain text (anything else)."""
    if not path.is_file():
        raise PrinterError(f"file not found: {path}",
                           hint="check the path; to fetch a web page give its full link, starting with https://",
                           exit_code=3)
    if path.stat().st_size > MAX_BYTES:
        raise PrinterError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MiB",
                           hint="save just the article", exit_code=3)
    suffix = path.suffix.lower()
    kind = "html" if suffix in (".html", ".htm", ".xhtml") else "json" if suffix == ".json" else "text"
    return parse(path.read_bytes(), kind, path.name)


def load_stdin() -> Article:
    """An article piped in on stdin: JSON (see JSON_HINT) when it starts with ``{``, HTML when it starts with ``<``,
    else plain text."""
    content = read_stdin(MAX_BYTES)
    if len(content) > MAX_BYTES:
        raise PrinterError(f"the article on stdin is larger than {MAX_BYTES // (1024 * 1024)} MiB",
                           hint="pipe in just the article", exit_code=3)
    start = content.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    kind = "json" if start == b"{" else "html" if start == b"<" else "text"
    return parse(content, kind, "stdin")


def parse(content: bytes, kind: str, name: str = "the article") -> Article:
    """An article from its bytes: ``kind`` is ``json`` (see JSON_HINT), ``html`` or ``text``; ``name`` says where
    it came from in error messages (a file name, or ``stdin``)."""
    if kind == "html":
        return from_html(_decode(content, None))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrinterError(f"{name} is not UTF-8 text", hint="save it as UTF-8", exit_code=3) from exc
    if kind != "json":
        return from_text(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrinterError(f"{name} is not valid JSON: {exc.msg} (line {exc.lineno})", hint=JSON_HINT,
                           exit_code=3) from exc
    if not isinstance(data, dict):
        raise PrinterError(f"{name} must hold a JSON object", hint=JSON_HINT, exit_code=3)
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise PrinterError(f"unknown field(s) in {name}: {', '.join(unknown)}",
                           hint="allowed fields: " + ", ".join(FIELDS), exit_code=3)
    missing = [field for field in ("title", "body") if field not in data]
    if missing:
        raise PrinterError(f"{name} is missing {' and '.join(missing)}", hint=JSON_HINT, exit_code=3)
    body = data["body"]
    if isinstance(body, list) and all(isinstance(p, str) for p in body):
        body = "\n".join(body)
    wrong = sorted(field for field, value in {**data, "body": body}.items() if not isinstance(value, str))
    if wrong:
        raise PrinterError(f"fields of {name} must be text: {', '.join(wrong)}",
                           hint="body may also be a list of paragraphs", exit_code=3)
    return Article(title=data["title"], body=tuple(body.split("\n")), author=data.get("author", ""),
                   source=data.get("source", ""), url=data.get("url", ""))


# --- normalising for the page font -----------------------------------------------------------

# Web text uses more symbols than a booth story; these print as ASCII. Emoji and other pictographs are
# left out, and letters of other scripts become "?".
_WEB_STAND_INS = {"•": "-", "‣": "-", "→": "->", "←": "<-", "⇒": "=>", "®": "(R)", "✓": "v", "✔": "v",
                  "×": "x", "−": "-", "‐": "-", "‑": "-", "′": "'", "″": '"'}


def _printable(text: str) -> tuple[str, int]:
    """``text`` as the page font can draw it, and how many characters became "?"."""
    out, unknown = [], 0
    for c in text:
        if c in _WEB_STAND_INS:
            out.append(_WEB_STAND_INS[c])
        elif (g := _glyphs(c)) is not None:
            out.append(g)
        elif unicodedata.category(c)[0] in "SM":  # symbols (emoji) and stray marks such as U+FE0F
            continue
        else:
            out.append("?")
            unknown += 1
    return "".join(out), unknown


def normalise(article: Article) -> Article:
    """Clean whitespace and control characters, and map the text onto the page font's glyphs.

    Refuses (exit code 3) an article with no body text, or one mostly in a script the font cannot print.
    """
    def line(text: str) -> tuple[str, int]:
        printable, unknown = _printable(_clean(text, keep_newlines=False))
        return " ".join(printable.split()), unknown  # no gap left where an emoji was

    title, author, source = (line(text)[0] for text in (article.title, article.author, article.source))
    paragraphs = [line(para) for para in article.body]
    letters = sum(len(text.replace(" ", "")) for text, _ in paragraphs)
    unknown = sum(bad for _, bad in paragraphs)
    if unknown * 2 > letters:
        raise PrinterError(f"the article is mostly in characters the page font cannot print ({unknown} of "
                           f"{letters})", hint="the built-in font covers Latin letters only", exit_code=3)
    body = [text for text, _ in paragraphs if text.strip("-? ")]
    if not body:
        raise PrinterError("no article text found" + (f" in {article.url}" if article.url else ""),
                           hint="the page may build its text with JavaScript or sit behind a login; "
                                + FILE_HINT, exit_code=3)
    return Article(title=title or source or "Untitled", body=tuple(body), author=author, source=source,
                   url=_clean(article.url, keep_newlines=False))


# --- page styles -----------------------------------------------------------------------------

LEFT, RIGHT = MARGIN, W - MARGIN
GREY = (110, 110, 110)
ACCENT = (0, 112, 186)  # magazine blue
HAIRLINE = (200, 200, 200)
IVORY = (247, 239, 220)  # classic page tint
DROP_LINES = 3  # a drop cap is as tall as this many lines of text


class _Style:
    """One look: the text block (columns, type size, width) and what is drawn around it on each page."""

    kind = ""
    columns, gutter, body, width = 1, 0, 24, CONTENT_W
    ink, rule = INK, None
    text_top, text_bottom = MARGIN, BOTTOM
    drop_cap = False

    def blank(self) -> Image.Image:
        return Image.new("RGB", (W, H), "white")

    def opening(self, page: Image.Image, article: Article) -> int:
        """Draw page one's heading; returns the y where the text starts."""
        raise NotImplementedError

    def running(self, page: Image.Image, article: Article) -> None:
        """Draw the heading of the following pages (the text starts at ``text_top``)."""
        raise NotImplementedError

    def footer(self, page: Image.Image, article: Article, number: int, total: int) -> None:
        raise NotImplementedError


class _Magazine(_Style):
    """Modern magazine: accent bar and section label, big bold headline, heavy rule, three ruled columns."""

    kind = "magazine article"
    columns, gutter, body = 3, 40, 24
    rule = HAIRLINE
    text_top, text_bottom = MARGIN + 80, BOTTOM - 64

    def opening(self, page: Image.Image, article: Article) -> int:
        d = ImageDraw.Draw(page)
        d.rectangle([LEFT, MARGIN, RIGHT, MARGIN + 14], fill=ACCENT)
        y = MARGIN + 40
        label = _fit((article.source or "Feature").upper(), 26, 20, CONTENT_W // 2, 1)
        y = _text_block(d, LEFT, y, label[2], label[0], label[1], ACCENT, bold=1)
        d.line([(LEFT + label[0].getlength(label[2][0]) + 24, y - _lead(label[1]) // 2),
                (RIGHT, y - _lead(label[1]) // 2)], fill=HAIRLINE, width=2)
        y += 16
        font, size, lines, cut = _fit(article.title, 84, 44, CONTENT_W, 3)
        if cut:  # too long for three lines at the smallest size: allow a fourth
            font, size, lines, cut = _fit(article.title, 44, 44, CONTENT_W, 4)
        y = _text_block(d, LEFT, y, lines, font, size, INK, bold=2) + 14
        if article.author:  # the source is already in the section label
            credit = f"By {article.author}" + (f" · {article.source}" if article.source else "")
            font, size, lines, _ = _fit(credit, 28, 20, CONTENT_W, 1)
            y = _text_block(d, LEFT, y, lines, font, size, GREY, bold=1)
        y += 12
        d.rectangle([LEFT, y, RIGHT, y + 6], fill=INK)
        return y + 6 + 34

    def running(self, page: Image.Image, article: Article) -> None:
        d = ImageDraw.Draw(page)
        small = _font(22)
        label = (article.source or "Feature").upper()
        d.text((LEFT, MARGIN), label, font=small, fill=ACCENT, stroke_width=1, stroke_fill=ACCENT)
        room = CONTENT_W - small.getlength(label) - 40
        title = article.title if small.getlength(article.title) <= room else _ellipsize(article.title, small, room)
        d.text((RIGHT - small.getlength(title), MARGIN), title, font=small, fill=GREY)
        d.rectangle([LEFT, MARGIN + 38, RIGHT, MARGIN + 41], fill=ACCENT)

    def footer(self, page: Image.Image, article: Article, number: int, total: int) -> None:
        d = ImageDraw.Draw(page)
        y = BOTTOM - 40
        d.line([(LEFT, y), (RIGHT, y)], fill=ACCENT, width=3)
        font = _font(22)
        mark = f"{number} / {total}"
        if number < total:
            mark = f"continued on page {number + 1}   {mark}"
        d.text((RIGHT - font.getlength(mark), y + 12), mark, font=font, fill=ACCENT)
        where = article.url or article.source
        room = CONTENT_W - font.getlength(mark) - 40
        if where:
            where = where if font.getlength(where) <= room else _ellipsize(where, font, room)
            d.text((LEFT, y + 12), where, font=font, fill=GREY)


class _Classic(_Style):
    """Classic magazine: ivory page in a double frame, centred heading between fleurons, one column, drop cap."""

    kind = "classic magazine article"
    body, width = 28, 840
    ink = SEPIA_DARK
    text_top, text_bottom = MARGIN + 120, BOTTOM - 110
    drop_cap = True

    def blank(self) -> Image.Image:
        page = super().blank()
        d = ImageDraw.Draw(page)
        d.rectangle([LEFT, MARGIN, RIGHT, BOTTOM], fill=IVORY)
        d.rectangle([LEFT + 18, MARGIN + 18, RIGHT - 18, BOTTOM - 18], outline=SEPIA_DARK, width=4)
        d.rectangle([LEFT + 30, MARGIN + 30, RIGHT - 30, BOTTOM - 30], outline=SEPIA_DARK, width=1)
        return page

    def opening(self, page: Image.Image, article: Article) -> int:
        d = ImageDraw.Draw(page)
        y = MARGIN + 80
        if article.source:
            spaced = " · ".join(" ".join(word) for word in article.source.upper().split())  # letter-spaced
            y, _ = _centred(d, y, spaced, 24, SEPIA, min_size=18, width=self.width)
            y += 18
        _fleuron(d, W // 2, y + 10)
        y += 50
        y, _ = _centred(d, y, article.title, 66, SEPIA_DARK, min_size=40, width=self.width, lines=4, bold=1)
        if article.author:
            y, _ = _centred(d, y + 10, f"by {article.author}", 30, SEPIA, min_size=22, width=self.width)
        _fleuron(d, W // 2, y + 30)
        return y + 80

    def running(self, page: Image.Image, article: Article) -> None:
        d = ImageDraw.Draw(page)
        _centred(d, MARGIN + 62, article.title, 22, SEPIA, min_size=22, width=self.width)
        d.line([(W // 2 - 160, MARGIN + 100), (W // 2 + 160, MARGIN + 100)], fill=SEPIA, width=1)

    def footer(self, page: Image.Image, article: Article, number: int, total: int) -> None:
        d = ImageDraw.Draw(page)
        y = BOTTOM - 84
        d.line([(LEFT + 120, y), (RIGHT - 120, y)], fill=SEPIA_DARK, width=2)
        d.line([(LEFT + 120, y + 6), (RIGHT - 120, y + 6)], fill=SEPIA_DARK, width=1)
        font = _font(24)
        mark = str(number)
        x = W // 2 - font.getlength(mark) / 2
        d.text((x, y + 18), mark, font=font, fill=SEPIA_DARK)
        for side in (-1, 1):
            _diamond(d, W // 2 + side * (font.getlength(mark) / 2 + 22), y + 32, 6, RUST)


def _cap_height(size: int) -> int:
    top, bottom = _font(size).getbbox("H")[1::2]
    return bottom - top


def _diamond(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)


def _fleuron(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """A divider drawn from shapes (the page font has no ornaments): rules either side of three diamonds."""
    for side in (-1, 1):
        d.line([(cx + side * 40, cy), (cx + side * 170, cy)], fill=SEPIA_DARK, width=2)
        _diamond(d, cx + side * 24, cy, 5, SEPIA)
    _diamond(d, cx, cy, 10, RUST)


ARTICLE_STYLES: dict[str, _Style] = {"magazine": _Magazine(), "classic-magazine": _Classic()}


def compose_pages(article: Article, style: str = "magazine") -> list[Image.Image]:
    """Lay a normalised article (see ``load``) out over as many A4 pages as its text needs.

    Every word of the body is printed once; a page that does not end the article says where it continues.
    Refuses (exit code 3) an article that needs more than MAX_PAGES pages.
    """
    look = ARTICLE_STYLES[style]
    font, lead = _font(look.body), _lead(look.body)
    col_w = _col_width(look.width, look.columns, look.gutter)
    x0 = (W - look.width) // 2
    first = look.blank()
    top = look.opening(first, article)
    paragraphs = list(article.body)
    first_rows, rows = (look.text_bottom - top) // lead, (look.text_bottom - look.text_top) // lead
    cap, cap_font, cap_w = "", None, 0
    if look.drop_cap and paragraphs and first_rows >= DROP_LINES and paragraphs[0][:1].isalpha():
        # Cap height of the drop cap = from the top of the first line's capitals to the last line's baseline.
        top_of_caps, baseline = font.getbbox("H")[1::2]
        cap_font = _font(round(((DROP_LINES - 1) * lead + baseline - top_of_caps) * 100 / _cap_height(100)))
        cap_w = round(cap_font.getlength(paragraphs[0][0])) + 16
        rest = paragraphs[0][1:].lstrip(" ") if paragraphs[0][1:2] == " " else paragraphs[0][1:]
        if len(list(islice(_wrapped(rest, font, col_w - cap_w), DROP_LINES))) == DROP_LINES:
            cap, paragraphs[0] = paragraphs[0][0], rest
    narrow = (DROP_LINES, col_w - cap_w) if cap else (0, 0)
    layouts = list(islice(paginate(paragraphs, font, col_w, look.columns, first_rows, rows, narrow),
                          MAX_PAGES + 1))
    if len(layouts) > MAX_PAGES:
        raise PrinterError(f"the article is too long to print: more than {MAX_PAGES} pages",
                           hint="save the part you want as a text file and print that file", exit_code=3)
    pages = []
    for number, cols in enumerate(layouts, start=1):
        page = first if number == 1 else look.blank()
        if number > 1:
            look.running(page, article)
        d = ImageDraw.Draw(page)
        y = top if number == 1 else look.text_top
        if number == 1 and cap:
            d.text((x0, y + font.getbbox("H")[1] - cap_font.getbbox("H")[1]), cap, font=cap_font, fill=RUST)
        draw_columns(d, cols, x0, y, col_w, look.gutter, font, look.body, look.ink, look.rule,
                     indent=(DROP_LINES, cap_w) if number == 1 and cap else (0, 0))
        look.footer(page, article, number, len(layouts))
        pages.append(page)
    return pages


# --- saving ----------------------------------------------------------------------------------


def page_files(pages: list[Image.Image], path: Path, fmt: str, paper: Paper = A4) -> list[tuple[Path, bytes]]:
    """The files that hold ``pages`` as ``fmt`` (see ``booth.SAVE_FORMATS``): one PDF with every page, or one
    image per page, numbered ``name-1.png``, ``name-2.png``… when there is more than one. ``paper`` gives the
    resolution, so each page comes out at its size (A4 by default)."""
    if fmt == "pdf":
        buf = io.BytesIO()
        pages[0].save(buf, "PDF", resolution=paper.dpi, quality=92, save_all=True, append_images=pages[1:])
        return [(path, buf.getvalue())]
    if len(pages) == 1:
        return [(path, paper.to_bytes(pages[0], fmt))]
    return [(path.with_name(f"{path.stem}-{n}{path.suffix}"), paper.to_bytes(page, fmt))
            for n, page in enumerate(pages, start=1)]
