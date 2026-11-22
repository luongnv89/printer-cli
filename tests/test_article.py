import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from PIL import Image, ImageDraw

from prt import __version__, article
from prt.article import Article
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.transport import ipp

A4_150DPI = (1240, 1754)
STYLES = list(article.ARTICLE_STYLES)
REAL_FETCH = article.fetch
URL = "https://blog.example/2026/paper"


@pytest.fixture(autouse=True)
def web(monkeypatch):
    """The only "internet" a test sees: the responses it puts in ``routes``; anything else fails the test."""
    routes, seen = {}, []

    def handler(request):
        seen.append(request)
        reply = routes.get(str(request.url))
        if reply is None:
            raise AssertionError(f"unexpected request to {request.url}")
        return reply(request) if callable(reply) else reply

    monkeypatch.setattr(article, "fetch", lambda url, timeout, transport=None:
                        REAL_FETCH(url, timeout, transport=httpx.MockTransport(handler)))
    return SimpleNamespace(routes=routes, seen=seen)


def _html(body, head=""):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                          content=f"<html><head>{head}</head><body>{body}</body></html>".encode())


BLOG = """
<html><head>
  <title>Why Paper Still Matters | The Long Read</title>
  <meta property="og:title" content="Why Paper Still Matters">
  <meta property="og:site_name" content="The Long Read">
  <meta name="author" content="Jo Writer">
  <script>var tracking = "SCRIPT TEXT";</script><style>p { color: red }</style>
</head><body>
  <div id="cookie-consent"><p>We use cookies to improve your experience.</p></div>
  <header><nav><ul><li><a href="/">Home</a></li><li>Sections</li></ul></nav><h1>The Long Read</h1></header>
  <div class="ad-slot"><p>BUY NOW: 50% OFF EVERYTHING</p></div>
  <main>
    <article class="post has-sidebar">
      <h1>Why Paper Still Matters</h1>
      <div class="share-bar"><p>Share on X</p></div>
      <p>Reading on paper is <em>slower</em>, and that is the point.</p>
      <p>People remember more of what they read on a printed page.
         Studies keep finding the same thing.</p>
      <h2>A calmer habit</h2>
      <p>A printed article has an end.</p>
      <ul><li>No notifications</li><li>No autoplay</li></ul>
      <section class="newsletter"><form><p>Subscribe to our newsletter</p></form></section>
      <section id="comments"><p>First comment!</p></section>
    </article>
    <aside><p>Related: ten more articles</p></aside>
  </main>
  <footer><p>Copyright 2026 The Long Read</p></footer>
</body></html>"""
BLOG_BODY = ("Reading on paper is slower, and that is the point.",
             "People remember more of what they read on a printed page. Studies keep finding the same thing.",
             "A calmer habit", "A printed article has an end.", "- No notifications", "- No autoplay")
CLUTTER = ("cookies", "Home", "Sections", "BUY NOW", "Share on X", "Subscribe", "First comment", "Related",
           "Copyright", "SCRIPT TEXT", "color: red")


# --- extracting web pages --------------------------------------------------------------------

def test_blog_post_keeps_title_author_site_and_body_and_drops_clutter(web):
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html"}, content=BLOG.encode())
    got = article.load(URL, 5)
    assert got == Article(title="Why Paper Still Matters", body=BLOG_BODY, author="Jo Writer",
                          source="The Long Read", url=URL)
    text = " ".join(got.body)
    assert [word for word in CLUTTER if word in text] == []


def test_void_and_unclosed_tags_inside_dropped_parts_do_not_swallow_the_page():
    page = ("<div class='ad'><p>Ad one<br><img src='a.png'><p>Ad two</div>"
            "<nav><li>Menu item<li>Another</nav>"
            "<div><p>Kept first<br>Kept second<p>Kept third</div>")
    assert article.from_html(page).body == ("Kept first", "Kept second", "Kept third")


def test_page_furniture_inside_the_article_is_dropped():
    page = ("<article><header><p>Posted in News</p></header><nav><p>Previous | Next</p></nav>"
            "<p>The story itself.</p><aside><p>Pull quote box</p></aside><figure><p>Photo credit</p></figure>"
            "<form><p>Sign up</p></form><footer><p>Tags: paper</p></footer></article>")
    assert article.from_html(page).body == ("The story itself.",)


@pytest.mark.parametrize("attrs, kept", [
    ('class="ad-slot"', False), ('id="ads"', False), ('class="post sidebar"', False),
    ('class="related_posts"', False), ('id="comment-list"', False), ('class="social-share"', False),
    ('class="header-wrap"', True), ('class="shadow"', True), ('class="thread"', True), ('class="lead"', True),
    ('class="shared-content"', True), ('id="reader"', True)])
def test_clutter_is_matched_on_whole_words_of_class_and_id(attrs, kept):
    page = f"<article><p>Main text of the article.</p><div {attrs}><p>Boxed text.</p></div></article>"
    body = article.from_html(page).body
    assert ("Boxed text." in body) is kept and "Main text of the article." in body


def test_article_and_main_are_never_dropped_for_their_class():
    page = '<article class="has-sidebar ad"><p>Inside the article.</p></article>'
    assert article.from_html(page).body == ("Inside the article.",)


def test_body_comes_from_the_largest_article_then_main_then_the_densest_block():
    teasers = "<article><p>Short teaser.</p></article>"
    story = "<article><p>The real story, much longer than any teaser.</p><p>Second paragraph.</p></article>"
    assert article.from_html(teasers + story + teasers).body == (
        "The real story, much longer than any teaser.", "Second paragraph.")
    assert article.from_html("<div><p>Outside.</p></div><main><p>Inside main.</p></main>").body == (
        "Inside main.",)
    dense = "<div><p>Menu-ish line.</p></div><div><p>Long body text one.</p><p>Long body text two.</p></div>"
    assert article.from_html(dense).body == ("Long body text one.", "Long body text two.")


def test_title_author_and_site_fall_back_when_metadata_is_missing():
    page = ('<header><h1>Heading Title</h1><a rel="author" href="/jo">Jo Writer</a></header>'
            '<article><p>Body.</p></article>')
    got = article.from_html(page, host="blog.example")
    assert (got.title, got.author, got.source) == ("Heading Title", "Jo Writer", "blog.example")
    got = article.from_html('<html><head><title>Page Title</title><meta property="article:author" '
                            'content="https://blog.example/jo"></head><body><p>Body.</p></body></html>')
    assert (got.title, got.author) == ("Page Title", "")  # an author that is only a link is no name


def test_web_text_keeps_quotes_entities_and_accents(web):
    page = ("<p>&ldquo;Caf&eacute; &amp; cr&egrave;me&rdquo; &mdash; it&rsquo;s 5&nbsp;&lt;b&gt;bold&lt;/b&gt;"
            "&hellip; &bull; ok &rarr; on.</p>")
    web.routes[URL] = _html(page, '<meta property="og:title" content="Zo&euml;&rsquo;s &lsquo;Note&rsquo;">')
    got = article.load(URL, 5)
    assert got.title == "Zoe’s ‘Note’"
    assert got.body == ("“Cafe & creme” - it’s 5 <b>bold</b>… - ok -> on.",)


def test_page_charset_is_honoured(web):
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html; charset=iso-8859-1"},
                                     content="<p>Déjà vu à Paris.</p>".encode("latin-1"))
    assert article.load(URL, 5).body == ("Deja vu a Paris.",)
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html"},
                                     content='<meta charset="windows-1252"><p>Na\xefve</p>'.encode("latin-1"))
    assert article.load(URL, 5).body == ("Naive",)


def test_plain_text_page_uses_its_first_line_as_the_title(web):
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/plain"},
                                     content=b"A Plain Title\n\nFirst para\nstill first.\n\nSecond.")
    got = article.load(URL, 5)
    assert (got.title, got.body, got.source) == ("A Plain Title", ("First para still first.", "Second."),
                                                 "blog.example")


# --- X posts ---------------------------------------------------------------------------------

POST = "https://x.com/someone/status/1234567890"
OEMBED_HTML = ('<blockquote class="twitter-tweet"><p lang="en" dir="ltr">Printing my reading list today.'
               '<br><br>Paper wins &amp; my eyes agree <a href="https://t.co/x">pic.twitter.com/x</a></p>'
               '&mdash; Some One (@someone) <a href="https://twitter.com/someone/status/1234567890">'
               'June 1, 2026</a></blockquote>')


def _oembed_url(post=POST):
    return str(httpx.URL(f"{article.OEMBED}?" + urlencode({"url": post, "omit_script": "true", "dnt": "true"})))


def test_x_post_is_read_through_oembed(web):
    web.routes[_oembed_url()] = httpx.Response(200, json={
        "url": POST, "author_name": "Some One", "author_url": "https://twitter.com/someone", "html": OEMBED_HTML})
    got = article.load(POST, 5)
    assert got == Article(title="Some One on X", author="Some One (@someone)", source="X", url=POST,
                          body=("Printing my reading list today.", "Paper wins & my eyes agree pic.twitter.com/x"))
    [request] = web.seen
    assert request.url.host == "publish.twitter.com" and parse_qs(request.url.query.decode())["url"] == [POST]


@pytest.mark.parametrize("url", ["https://x.com/a/status/1", "https://www.x.com/a/status/1",
                                 "https://mobile.twitter.com/a/status/1", "https://twitter.com/a/status/1",
                                 "https://X.COM/a/status/1", "https://mobile.x.com/a/status/1"])
def test_x_hosts_are_recognised(url):
    assert article.is_x_post(url)


@pytest.mark.parametrize("url", ["https://netflix.com/a/status/1", "https://notx.com/a/status/1",
                                 "https://x.com.evil.example/a/status/1", "https://twitter.com.example.org/s/1",
                                 "https://blog.example/x.com/status/1", "https://xx.com/a/status/1"])
def test_other_hosts_are_not_x(url):
    assert not article.is_x_post(url)


def test_an_x_link_that_is_not_a_post_is_refused(web):
    with pytest.raises(PrinterError, match="not a link to an X post") as err:
        article.load("https://x.com/someone", 5)
    assert err.value.exit_code == 3 and web.seen == []


@pytest.mark.parametrize("reply, message", [
    (httpx.Response(404), "X did not return the post"),
    (httpx.Response(200, content=b"<html>not json"), "unreadable answer"),
    (httpx.Response(200, json={"author_name": "A"}), "no post text"),
    (httpx.Response(200, json={"html": "<blockquote>&mdash; A (@a)</blockquote>"}), "no article text found")])
def test_x_failures_say_what_went_wrong(web, reply, message):
    web.routes[_oembed_url()] = reply
    with pytest.raises(PrinterError, match=message) as err:
        article.load(POST, 5)
    assert err.value.exit_code == 3 and err.value.hint


# --- fetching --------------------------------------------------------------------------------

@pytest.mark.parametrize("link", ["ftp://blog.example/post", "file:///etc/passwd", "gopher://x.com/a/status/1"])
def test_only_http_and_https_links_are_fetched(web, link):
    with pytest.raises(PrinterError, match="only http:// and https:// links") as err:
        article.load(link, 5)
    assert err.value.exit_code == 3 and web.seen == []


def test_body_size_cap_is_enforced_while_streaming(web, monkeypatch):
    monkeypatch.setattr(article, "MAX_BYTES", 1024 * 1024)
    pulled = []

    def endless():
        while True:
            pulled.append(1)
            yield b"x" * (256 * 1024)
    web.routes[URL] = lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=endless())
    with pytest.raises(PrinterError, match="larger than 1 MiB") as err:
        article.load(URL, 5)
    assert err.value.exit_code == 3 and len(pulled) == 5  # stopped at the first chunk over the cap


def test_a_declared_size_over_the_cap_is_refused_before_reading(web, monkeypatch):
    monkeypatch.setattr(article, "MAX_BYTES", 1024 * 1024)
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html", "content-length": "1048577"},
                                     content=b"<p>small</p>")
    with pytest.raises(PrinterError, match="larger than 1 MiB"):
        article.load(URL, 5)
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + b"x" * 1024 + b"</p>")
    assert article.load(URL, 5).body == ("x" * 1024,)  # under the cap: fine


def test_redirects_are_followed_up_to_the_cap(web):
    hops = [f"https://blog.example/hop{i}" for i in range(7)]
    for here, there in zip(hops, hops[1:]):
        web.routes[here] = httpx.Response(302, headers={"location": there})
    web.routes[hops[-1]] = _html("<p>Arrived.</p>")
    assert article.load(hops[-1 - article.MAX_REDIRECTS], 5).body == ("Arrived.",)
    with pytest.raises(PrinterError, match=f"more than {article.MAX_REDIRECTS} redirects"):
        article.load(hops[-2 - article.MAX_REDIRECTS], 5)


def test_requests_name_the_tool_and_nothing_personal(web):
    web.routes[URL] = _html("<p>Hi.</p>")
    article.load(URL, 5)
    assert web.seen[0].headers["user-agent"] == f"printer-cli/{__version__}"


@pytest.mark.parametrize("reply, message", [
    (httpx.Response(500), "answered HTTP 500"),
    (httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4"), "not a web page"),
    (httpx.Response(200, headers={"content-type": "text/html"}, content=b"<script>app()</script>"),
     "no article text found"),
    (httpx.Response(200, headers={"content-type": "text/html"}, content="<p>日本語の記事です。</p>".encode()),
     "mostly in characters the page font cannot print")])
def test_unusable_pages_are_refused_with_exit_code_3(web, reply, message):
    web.routes[URL] = reply
    with pytest.raises(PrinterError, match=message) as err:
        article.load(URL, 5)
    assert err.value.exit_code == 3 and err.value.hint


@pytest.mark.parametrize("error, message", [(httpx.ConnectError, "cannot fetch"),
                                            (httpx.ReadTimeout, "no answer within 5 s")])
def test_network_failures_are_refused_with_exit_code_3(web, error, message):
    def fail(request):
        raise error("network down", request=request)
    web.routes[URL] = fail
    with pytest.raises(PrinterError, match=message) as err:
        article.load(URL, 5)
    assert err.value.exit_code == 3


# --- files -----------------------------------------------------------------------------------

def test_json_file_round_trips_unusual_text(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"title": "Zoë “quotes” <b>", "author": "Jo", "source": "Blog",
                                "url": "https://blog.example", "body": ["Café — first.", "Second."]}))
    assert article.load(str(path), 5) == Article(title="Zoe “quotes” <b>", author="Jo", source="Blog",
                                                 url="https://blog.example", body=("Cafe - first.", "Second."))
    path.write_text(json.dumps({"title": "T", "body": "One.\n\nTwo."}))
    assert article.load(str(path), 5).body == ("One.", "Two.")


def test_html_and_text_files(tmp_path):
    (tmp_path / "saved.html").write_text(BLOG)
    assert article.load(str(tmp_path / "saved.html"), 5).body == BLOG_BODY
    (tmp_path / "notes.txt").write_text("Title line\n\nBody text.")
    got = article.load(str(tmp_path / "notes.txt"), 5)
    assert (got.title, got.body) == ("Title line", ("Body text.",))


@pytest.mark.parametrize("name, content, message", [
    ("a.json", "{oops", "not valid JSON"),
    ("a.json", "[1]", "must hold a JSON object"),
    ("a.json", '{"title": "T", "body": "B", "date": "x"}', "unknown field"),
    ("a.json", '{"body": "B"}', "missing title"),
    ("a.json", '{"title": "T", "body": [1]}', "must be text: body"),
    ("a.txt", b"\xff\xfe bad", "not UTF-8"),
    ("a.json", '{"title": "T", "body": ""}', "no article text found")])
def test_bad_files_are_refused_with_exit_code_3(tmp_path, name, content, message):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(PrinterError, match=message) as err:
        article.load(str(path), 5)
    assert err.value.exit_code == 3


def test_missing_and_oversized_files_are_refused(tmp_path, monkeypatch):
    with pytest.raises(PrinterError, match="file not found") as err:
        article.load(str(tmp_path / "nope.json"), 5)
    assert err.value.exit_code == 3 and "https://" in err.value.hint
    monkeypatch.setattr(article, "MAX_BYTES", 10)
    (tmp_path / "big.txt").write_text("T\n\n" + "x" * 10)
    with pytest.raises(PrinterError, match="larger than"):
        article.load(str(tmp_path / "big.txt"), 5)


# --- normalising -----------------------------------------------------------------------------

def test_symbols_print_as_ascii_and_emoji_are_left_out():
    got = article.normalise(Article(title="Hi 👋🏽", body=("• one → two ® ×3\u00a0end 🎉",)))
    assert got.title == "Hi" and got.body == ("- one -> two (R) x3 end",)


def test_text_mostly_in_another_script_is_refused():
    with pytest.raises(PrinterError, match=r"cannot print \(2 of 3\)") as err:
        article.normalise(Article(title="T", body=("a日本",)))
    assert err.value.exit_code == 3


def test_text_half_in_another_script_still_prints():
    assert article.normalise(Article(title="T", body=("ab日本",))).body == ("ab??",)


def test_an_article_without_body_text_is_refused():
    with pytest.raises(PrinterError, match="no article text found") as err:
        article.normalise(Article(title="T", body=("🎉🎉", "  ")))
    assert err.value.exit_code == 3


# --- rendering -------------------------------------------------------------------------------

LONG = Article(title="Why Paper Still Matters", author="Jo Writer", source="The Long Read", url=URL,
               body=tuple(f"Paragraph {n}: " + " ".join(f"word{n}x{i}" for i in range(90)) for n in range(40)))


def _drawn(monkeypatch):
    """Every string drawn on a page, with its type size."""
    seen = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        seen.append((text, kwargs["font"].size))
        return real(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return seen


@pytest.mark.parametrize("style", STYLES)
def test_a_long_article_runs_over_several_pages_with_every_word_printed(monkeypatch, style):
    drawn = _drawn(monkeypatch)
    pages = article.compose_pages(LONG, style)
    assert len(pages) > 2 and all(page.size == A4_150DPI for page in pages)
    body_size = article.ARTICLE_STYLES[style].body
    printed = "".join("".join(text for text, size in drawn if size >= body_size and text != LONG.title).split())
    expected = "".join("".join(LONG.body).split())
    assert expected in printed  # every word once, in order (the drop cap is the first letter)
    assert printed.count("word0x0") == 1 and printed.count("word39x89") == 1


@pytest.mark.parametrize("style", STYLES)
def test_title_author_and_source_are_printed(monkeypatch, style):
    seen = _drawn(monkeypatch)
    article.compose_pages(LONG, style)
    drawn = " ".join(text for text, _ in seen)
    assert "Why Paper Still" in drawn and "Jo Writer" in drawn
    assert "The Long Read" in drawn or "T H E · L O N G · R E A D" in drawn


@pytest.mark.parametrize("style", STYLES)
def test_pages_stay_inside_the_printable_margins(style):
    w, h = A4_150DPI
    for page in article.compose_pages(LONG, style):
        for band in [(0, 0, w, 80), (0, h - 80, w, h), (0, 0, 80, h), (w - 80, 0, w, h)]:
            assert page.crop(band).getextrema() == ((255, 255),) * 3, band


def _share(page, test):
    small = page.reduce(4)
    return sum(n for n, p in small.getcolors(small.width * small.height) if test(*p)) / (small.width * small.height)


def test_each_style_looks_different():
    short = Article(title="A Short Piece", author="Jo", source="Blog", body=("Some text. " * 40,) * 3)
    magazine, classic = (article.compose_pages(short, style)[0] for style in ("magazine", "classic-magazine"))

    def blue(r, g, b):
        return b > 150 and r < 60 and b - g > 40

    def ivory(r, g, b):
        return (r, g, b) == article.IVORY

    def white(r, g, b):
        return (r, g, b) == (255, 255, 255)
    assert _share(magazine, blue) > 0.004 and _share(classic, blue) == 0  # accent bar and rules
    assert _share(classic, ivory) > 0.5 and _share(magazine, ivory) == 0  # tinted page
    assert _share(magazine, white) > 0.6
    # magazine sets its text in three columns, classic in one centred column
    assert magazine.crop((900, 600, 1100, 1300)).getextrema() != ((255, 255),) * 3
    assert classic.crop((1080, 600, 1100, 1300)).getextrema() == (article.IVORY[0:1] * 2, article.IVORY[1:2] * 2,
                                                                  article.IVORY[2:3] * 2)


def test_a_short_article_is_one_page():
    assert len(article.compose_pages(Article(title="T", body=("Short.",)), "magazine")) == 1


def test_articles_longer_than_the_page_limit_are_refused(monkeypatch):
    three = len(article.compose_pages(LONG, "magazine"))
    monkeypatch.setattr(article, "MAX_PAGES", three)
    assert len(article.compose_pages(LONG, "magazine")) == three
    monkeypatch.setattr(article, "MAX_PAGES", three - 1)
    with pytest.raises(PrinterError, match=f"more than {three - 1} pages") as err:
        article.compose_pages(LONG, "magazine")
    assert err.value.exit_code == 3


def test_page_files_are_one_pdf_or_numbered_images(tmp_path):
    pages = [Image.new("RGB", A4_150DPI, colour) for colour in ("white", "red", "blue")]
    [(path, pdf)] = article.page_files(pages, tmp_path / "a.pdf", "pdf")
    assert path == tmp_path / "a.pdf" and pdf.startswith(b"%PDF-") and b"/Count 3" in pdf
    files = article.page_files(pages, tmp_path / "a.png", "png")
    assert [p.name for p, _ in files] == ["a-1.png", "a-2.png", "a-3.png"]
    assert Image.open(io.BytesIO(files[1][1])).getpixel((5, 5)) == (255, 0, 0)
    assert [p.name for p, _ in article.page_files(pages[:1], tmp_path / "one.jpg", "jpeg")] == ["one.jpg"]


# --- the command -----------------------------------------------------------------------------

def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; `made` counts how often a driver was created at all."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    jobs = lambda: [data for op, _, _, data in fake.client.requests if op == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=jobs)


def _article_file(tmp_path, art=LONG):
    path = tmp_path / "article.json"
    path.write_text(json.dumps({"title": art.title, "author": art.author, "source": art.source, "url": art.url,
                                "body": list(art.body)}))
    return path


@pytest.mark.parametrize("style", STYLES)
def test_cli_prints_one_job_per_page(capsys, tmp_path, printer, style):
    from prt.booth import to_jpeg
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "article", str(_article_file(tmp_path)),
                         "--style", style, "--yes")
    expected = [to_jpeg(page) for page in article.compose_pages(LONG, style)]
    assert code == 0 and len(expected) > 2
    assert printer.jobs() == expected
    assert json.loads(out) == {"action": "article", "style": style, "title": LONG.title, "pages": len(expected),
                               "job_ids": list(range(101, 101 + len(expected)))}
    assert f"{len(expected)} pages queued" in err


def test_cli_fetches_a_link_and_asks_before_printing(capsys, tmp_path, monkeypatch, printer, web):
    web.routes[URL] = httpx.Response(200, headers={"content-type": "text/html"}, content=BLOG.encode())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, _, _ = run(capsys, "-p", "demo", "article", URL)
    assert code == 0 and len(printer.jobs()) == 1
    assert asked == ["Print 1 page of 'Why Paper Still Matters' as a magazine article on demo?"]


def test_cli_declined_prompt_prints_nothing(capsys, tmp_path, monkeypatch, printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda q, **kw: False)
    code, _, err = run(capsys, "-p", "demo", "article", str(_article_file(tmp_path)), "-s", "classic-magazine")
    assert code == 1 and "nothing was sent" in err and printer.jobs() == []


def test_cli_needs_yes_without_a_terminal(capsys, tmp_path, monkeypatch, printer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "-p", "demo", "article", str(_article_file(tmp_path)))
    assert code == 2 and "--yes" in err and printer.jobs() == []


def test_cli_preview_never_creates_a_driver(capsys, tmp_path, printer):
    out_file = tmp_path / "page.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "article", str(_article_file(tmp_path)), "-o",
                         str(out_file))
    pages = len(article.compose_pages(LONG, "magazine"))
    files = [tmp_path / f"page-{n}.png" for n in range(1, pages + 1)]
    assert code == 0 and "not printed" in err and printer.made == []
    assert json.loads(out) == {"action": "article", "style": "magazine", "title": LONG.title, "pages": pages,
                               "files": [{"file": str(f), "format": "png", "bytes": f.stat().st_size}
                                         for f in files]}
    assert all(Image.open(f).size == A4_150DPI for f in files) and not out_file.exists()


def test_cli_save_keeps_a_pdf_and_prints(capsys, tmp_path, printer):
    copy = tmp_path / "copy.pdf"
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "article", str(_article_file(tmp_path)), "--save",
                       str(copy), "--yes")
    pages = len(article.compose_pages(LONG, "magazine"))
    assert code == 0 and len(printer.jobs()) == pages
    assert f"/Count {pages}".encode() in copy.read_bytes()
    assert json.loads(out)["saved"] == [{"file": str(copy), "format": "pdf", "bytes": copy.stat().st_size}]


def test_cli_untrusted_title_cannot_inject_terminal_codes(capsys, tmp_path, monkeypatch, printer):
    evil = Article(title="[bold]Hi\x1b[31m there", body=("Body text.",))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("typer.confirm", lambda q, **kw: asked.append(q) or True)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "article", str(_article_file(tmp_path, evil)))
    assert code == 0 and "\x1b" not in asked[0] + out + err
    assert json.loads(out)["title"] == "[bold]Hi[31m there"


def _refused(request):
    raise httpx.ConnectError("connection refused", request=request)


FAILURES = {
    "http error": (lambda request: httpx.Response(404), "answered HTTP 404"),
    "network error": (_refused, "connection refused"),
    "not html": (lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"),
                 "not a web page"),
    "empty page": (lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b""),
                   "no article text found"),
    "no body": (lambda request: httpx.Response(200, headers={"content-type": "text/html"},
                                               content=b"<nav><p>Home</p></nav><div class='ad'><p>Buy</p></div>"),
                "no article text found"),
}


@pytest.mark.parametrize("failure", FAILURES)
def test_cli_fetch_or_convert_failure_sends_nothing(capsys, printer, web, failure):
    reply, message = FAILURES[failure]
    web.routes[URL] = reply
    code, out, err = run(capsys, "-p", "demo", "article", URL, "--yes")
    assert code == 3 and out == ""
    assert "error:" in err and message in err and "hint:" in err
    assert printer.made == [] and printer.jobs() == []


@pytest.mark.parametrize("source, message", [("ftp://blog.example/post", "only http:// and https:// links"),
                                             ("missing.json", "file not found")])
def test_cli_bad_source_sends_nothing(capsys, tmp_path, monkeypatch, printer, web, source, message):
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "-p", "demo", "article", source, "--yes")
    assert code == 3 and message in err and printer.made == [] and printer.jobs() == [] and web.seen == []


def test_cli_bad_output_name_is_refused_before_fetching(capsys, printer, web):
    code, _, err = run(capsys, "-p", "demo", "article", URL, "--save", "copy.docx", "--yes")
    assert code == 2 and "unknown file type .docx" in err and web.seen == [] and printer.made == []


def test_cli_unknown_style_is_a_usage_error(capsys, tmp_path, printer):
    code, _, err = run(capsys, "-p", "demo", "article", str(_article_file(tmp_path)), "-s", "tabloid", "--yes")
    assert code == 2 and "tabloid" in err and printer.jobs() == []


def test_article_style_choices_match_renderer():
    from prt.cli import ArticleStyle
    assert {s.value for s in ArticleStyle} == set(article.ARTICLE_STYLES) == {"magazine", "classic-magazine"}


def test_the_link_is_fetched_with_the_global_timeout(capsys, tmp_path, monkeypatch, printer):
    calls = []
    monkeypatch.setattr(article, "fetch", lambda url, timeout, transport=None: calls.append(timeout) or
                        article.Fetched(url, "text/html", None, b"<p>Hi there.</p>"))
    assert run(capsys, "-p", "demo", "--timeout", "12", "article", URL, "-o", str(tmp_path / "a.pdf"))[0] == 0
    assert run(capsys, "-p", "demo", "article", URL, "-o", str(tmp_path / "b.pdf"))[0] == 0
    assert calls == [12.0, 5.0]
