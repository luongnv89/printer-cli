"""The examples site in site/: a static slideshow of every README example, served without a build step.

The slides live as JSON inside site/index.html. These tests hold them to the README (every sample image, the
command that made it, every terminal example copied verbatim), keep the folder self-contained and grey-only,
and check the autoplay controls the slideshow needs to stay pausable.
"""

import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import pytest

from conftest import ROOT

SITE = ROOT / "site"
README = (ROOT / "README.md").read_text(encoding="utf-8")
SAMPLES_README = (ROOT / "assets" / "samples" / "README.md").read_text(encoding="utf-8")
INDEX = (SITE / "index.html").read_text(encoding="utf-8")
CSS = (SITE / "styles.css").read_text(encoding="utf-8")
JS = (SITE / "app.js").read_text(encoding="utf-8")

SLIDES_BLOCK = re.compile(r'<script type="application/json" id="slides">(.*?)</script>', re.S)
SLIDES = json.loads(SLIDES_BLOCK.search(INDEX).group(1))
IMAGE_SLIDES = [s for s in SLIDES if s["kind"] == "image"]
TERMINAL_SLIDES = {s["id"]: s for s in SLIDES if s["kind"] == "terminal"}

EXPECTED_TERMINAL = {"quick-start", "ink", "status-watch", "overview", "maintenance", "printing", "scanning", "jobs",
                     "letter-gazette", "compose"}

GAZETTE_LANDSCAPE = ("prt gazette astronaut.jpg launch.jpg earthrise.jpg --story-file story.json --landscape "
                     "-o gazette-landscape.png")

USAGE = "usage variants; the result has its own image slide with the command that made its sample"
SETUP = "setup or scripting reference, not an example of what prt makes"
# Every README fence that is not a terminal slide, keyed by a substring of its first line.
EXCLUDED = {
    "uv tool install printer-cli": "install command; " + SETUP,
    "# find printers on the LAN": "printer setup; " + SETUP,
    "prt discover --wait 5": "adding printers by hand; " + SETUP,
    "╭─┐printers┌": "the saved-printers list; " + SETUP,
    "prt ink -f json | jq": "Scripting reference; " + SETUP,
    "$ prt -f json article https://example.com/gone": "the JSON error object; " + SETUP,
    "$ prt styles -f json | jq": "the styles listing for scripts; " + SETUP,
    "prt booth astronaut.jpg --story-file story.json --preview metro.jpg": "photo booth " + USAGE,
    "prt card me.jpg": "greeting card " + USAGE,
    "prt comic a.jpg b.jpg c.jpg d.jpg": "comic " + USAGE,
    '{"title": "One Small Step"': "the comic file format; the comic image slide shows the page it makes",
    "prt strip a.jpg b.jpg c.jpg d.jpg": "strip and instant " + USAGE,
    "prt colour-in dog.jpg": "colouring page " + USAGE,
    "prt postcard beach.jpg": "postcard " + USAGE,
    "prt disc beach.jpg": "disc label " + USAGE,
    "prt recipe https://example.com/lemon-cake": "recipe card " + USAGE,
    "prt bingo --theme": "bingo " + USAGE,
    "prt puzzle sudoku --difficulty hard": "puzzle " + USAGE,
    "prt article https://example.com/blog/post": "article " + USAGE,
    "prt gazette me.jpg": "gazette " + USAGE,
    "prt gazette --edition morning --edition-file morning.json --preview": "morning edition " + USAGE,
    "# cron, every morning at 07:30": "operating-system scheduler reference; " + SETUP,
    "# cron, at 10:00 on every third day of the month": "keep-alive scheduler reference; " + SETUP,
    "prt receipt --preview receipt.pdf -f json": "commit receipt " + USAGE,
    "prt compose party.json --preview": "compose usage variants; the compose slide shows the page file and its command",
    "✗ Cyan ink is at 1%": "a refused head cleaning: a safety note, not something prt makes",
    "pipx install 'printer-cli[mcp]'": "installing the MCP extra; " + SETUP,
    '{"mcpServers"': "MCP client config; " + SETUP,
    "prt skills list": "installing the agent skills; " + SETUP,
    "/plugin install printer-cli": "the Claude Code plugin; " + SETUP,
    'default = "office"': "the config file; " + SETUP,
    "pipx install printer-cli": "install option; " + SETUP,
    "git clone": "install from source; " + SETUP,
    "prt --install-completion": "shell completion; " + SETUP,
    "uv sync": "development; " + SETUP,
}

ALLOWED_LINKS = ("https://github.com/luongnv89/printer-cli", "https://pypi.org/project/printer-cli/",
                 "https://commons.wikimedia.org/")
SKILLS = {p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()}


def readme_fences() -> list[str]:
    """The body of every ``` fenced block in README.md, in order."""
    bodies, lines, i = [], README.split("\n"), 0
    while i < len(lines):
        if lines[i].startswith("```"):
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            bodies.append("\n".join(lines[i + 1:j]))
            i = j
        i += 1
    return bodies


def readme_inline_code() -> set[str]:
    prose = re.sub(r"^```.*?^```", "", README, flags=re.S | re.M)
    return set(re.findall(r"`([^`\n]+)`", prose))


def readme_samples() -> list[tuple[str, str]]:
    """(file name, alt text) of every sample image README.md embeds, in order."""
    return re.findall(r'assets/samples/([\w-]+\.jpg)" width="\d+" alt="([^"]*)"', README)


def sample_rows() -> dict[str, str]:
    return dict(re.findall(r"^\| `([\w-]+\.jpg)` \| (.*) \|$", SAMPLES_README, re.M))


def first_prt(cell: str) -> str:
    return next(span for span in re.findall(r"`([^`]*)`", cell) if span.startswith("prt "))


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags() -> list[tuple[str, dict]]:
    parser = Tags()
    parser.feed(SLIDES_BLOCK.sub("", INDEX))
    return parser.tags


# --- slide data -------------------------------------------------------------------------------------


def test_slides_are_json_with_unique_ids_and_known_kinds():
    assert len(SLIDES) == len({s["id"] for s in SLIDES})
    assert {s["kind"] for s in SLIDES} <= {"intro", "image", "terminal"}
    for s in IMAGE_SLIDES:
        assert {"image", "alt", "width", "height"} <= set(s), s["id"]
    for s in TERMINAL_SLIDES.values():
        assert isinstance(s["output"], str), s["id"]


def test_every_readme_sample_has_one_image_slide_in_readme_order():
    samples = readme_samples()
    assert len(samples) == 26
    assert [s["image"] for s in IMAGE_SLIDES] == [f"samples/{name}" for name, _ in samples]
    assert {s["image"]: s["alt"] for s in IMAGE_SLIDES} == {f"samples/{name}": alt for name, alt in samples}


def test_image_slide_commands_are_the_commands_that_made_the_samples():
    rows = sample_rows()
    for s in IMAGE_SLIDES:
        name = s["image"].removeprefix("samples/")
        if name == "gazette-landscape.jpg":
            assert rows[name] == "the same with `--landscape`"
            assert s["command"] == GAZETTE_LANDSCAPE
            assert s["command"] == first_prt(rows["gazette.jpg"]).replace(" -o gazette.png",
                                                                          " --landscape -o gazette-landscape.png")
        else:
            assert s["command"] == first_prt(rows[name]), name


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's start-of-frame segment."""
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    raise ValueError("no start-of-frame segment")


def test_image_slides_carry_their_real_size():
    assert jpeg_size((SITE / "samples" / "recipe.jpg").read_bytes()) == (1200, 800)
    for s in IMAGE_SLIDES:
        assert (s["width"], s["height"]) == jpeg_size((SITE / s["image"]).read_bytes()), s["id"]


def test_terminal_slides_are_the_expected_ones():
    assert set(TERMINAL_SLIDES) == EXPECTED_TERMINAL


def test_every_readme_fence_is_a_terminal_slide_or_excluded():
    fences = readme_fences()
    used = {key: 0 for key in EXCLUDED}
    claimed = {sid: [] for sid in TERMINAL_SLIDES}
    for n, body in enumerate(fences):
        owners = [sid for sid, s in TERMINAL_SLIDES.items() if body in (s["command"], s["output"])]
        keys = [key for key in EXCLUDED if key in body.split("\n")[0]]
        assert len(owners) + len(keys) == 1, (n, body.split("\n")[0], owners, keys)
        for key in keys:
            used[key] += 1
        for sid in owners:
            claimed[sid].append(body)
    assert [key for key, count in used.items() if count != 1] == []
    assert all(EXCLUDED.values())

    inline = readme_inline_code()
    for sid, s in TERMINAL_SLIDES.items():
        assert claimed[sid], f"{sid}: no README block is its command or its output"
        if s["output"]:
            assert s["output"] in claimed[sid], sid
        if s["command"] not in claimed[sid]:  # the command sits in the prose, as inline code
            assert all(line in inline for line in s["command"].split("\n")), sid


def test_terminal_slides_copy_the_readme_verbatim():
    fences = readme_fences()
    assert TERMINAL_SLIDES["quick-start"]["command"] == "prt -p demo status"
    assert TERMINAL_SLIDES["quick-start"]["output"] in fences
    assert TERMINAL_SLIDES["ink"]["output"].startswith("╭─┐ink · demo┌")
    assert TERMINAL_SLIDES["jobs"]["command"] == "prt jobs --all\nprt jobs cancel 42"
    assert TERMINAL_SLIDES["compose"]["command"] == "prt compose page.json"
    assert json.loads(TERMINAL_SLIDES["compose"]["output"])["title"] == "Garden party"
    for sid in ("maintenance", "printing", "scanning", "letter-gazette"):
        assert TERMINAL_SLIDES[sid]["command"] in fences and TERMINAL_SLIDES[sid]["output"] == "", sid


def test_every_slide_has_a_title_a_prt_command_and_an_agent_prompt():
    install = readme_fences()[0]
    assert install == "uv tool install printer-cli"
    for s in SLIDES:
        assert s["title"].strip() and s["group"].strip() and s["blurb"].strip(), s["id"]
        lines = [line for line in s["command"].split("\n") if line.strip()]
        assert lines, s["id"]
        for line in lines:
            assert line.startswith("prt ") or (s["kind"] == "intro" and line == install), (s["id"], line)
        assert re.search(r"\bprt\b|printer-cli|\bthe [\w-]+ skill\b", s["prompt"]), s["id"]
        for skill in re.findall(r"the ([\w-]+) skill", s["prompt"]):
            assert skill in SKILLS, (s["id"], skill)


# --- the folder ---------------------------------------------------------------------------------------


def test_site_copies_are_byte_identical():
    names = {name for name, _ in readme_samples()}
    assert {p.name for p in (SITE / "samples").iterdir()} == names
    for name in names:
        assert (SITE / "samples" / name).read_bytes() == (ROOT / "assets" / "samples" / name).read_bytes(), name
    for logo in ("logo-black.svg", "logo-white.svg"):
        assert (SITE / logo).read_bytes() == (ROOT / "assets" / "logo" / logo).read_bytes(), logo
    assert (SITE / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    fonts = {p.name for p in (SITE / "fonts").iterdir()}
    assert fonts == {"PlayfairDisplay-Variable.ttf", "OldStandard-Regular.ttf", "OldStandard-Bold.ttf",
                     "OFL-PlayfairDisplay.txt", "OFL-OldStandardTT.txt"}
    for font in fonts:
        assert (SITE / "fonts" / font).read_bytes() == (ROOT / "src" / "prt" / "fonts" / font).read_bytes(), font


def test_pyproject_keeps_site_out_of_the_sdist_but_not_the_skills():
    exclude = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "site" in exclude["exclude"] and "skills" not in exclude["exclude"]


# --- grey only ---------------------------------------------------------------------------------------

NAMED_COLOURS = set("""
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood
cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray
darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue
firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew
hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray
lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue
mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred
midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple
rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue
slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white
whitesmoke yellow yellowgreen
accentcolor accentcolortext activetext buttonborder buttonface buttontext canvas canvastext field fieldtext graytext
highlight highlighttext linktext mark marktext selecteditem selecteditemtext visitedtext activeborder activecaption
appworkspace background buttonhighlight buttonshadow captiontext inactiveborder inactivecaption inactivecaptiontext
infobackground infotext menu menutext scrollbar threeddarkshadow threedface threedhighlight threedlightshadow
threedshadow window windowframe windowtext
""".split())
GREY_KEYWORDS = {"black", "white", "gray", "grey", "transparent", "currentcolor", "inherit", "initial", "unset",
                 "none"}
HUE_FUNCTIONS = re.compile(r"(?<![\w-])(hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\(", re.I)


def colour_problems(value: str) -> list[str]:
    """Every colour in a CSS value (or a JS expression that sets one) that is not a neutral grey."""
    value = re.sub(r"url\([^)]*\)|\"[^\"]*\"|'[^']*'|var\(--[\w-]+", " ", value)
    problems = [f"hue function {m.group(1)}()" for m in HUE_FUNCTIONS.finditer(value)]
    for digits in re.findall(r"#([0-9a-fA-F]+)\b", value):
        if len(digits) in (3, 4):
            r, g, b = digits[0], digits[1], digits[2]
        elif len(digits) in (6, 8):
            r, g, b = digits[0:2], digits[2:4], digits[4:6]
        else:
            problems.append(f"#{digits} is not a colour")
            continue
        if not r.lower() == g.lower() == b.lower():
            problems.append(f"#{digits}")
    for args in re.findall(r"(?<![\w-])rgba?\(([^)]*)\)", value, re.I):
        r, g, b = re.split(r"[\s,/]+", args.strip())[:3]
        if not r == g == b:
            problems.append(f"rgb({args})")
    for word in re.findall(r"[a-zA-Z][\w-]*", value):
        if word.lower() in NAMED_COLOURS and word.lower() not in GREY_KEYWORDS:
            problems.append(word)
    return problems


def css_values(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    values = []
    for block in re.findall(r"\{([^{}]*)\}", css):
        for declaration in block.split(";"):
            if ":" in declaration:
                values.append(declaration.split(":", 1)[1])
    return values


PRESENTATION = "fill|stroke|stop-color|color|flood-color|lighting-color"
JS_STYLE_ATTRIBUTE = re.compile(r"""setAttribute\(\s*["'`]style["'`]\s*,\s*(["'`])(.*?)\1""", re.S)


def colour_sources() -> list[tuple[str, str]]:
    sources = [("styles.css", v) for v in css_values(CSS)]
    html = SLIDES_BLOCK.sub("", INDEX)
    # The inline icons: a colour can sit in an SVG presentation attribute, outside any style.
    sources += [("index.html attribute", v) for v in re.findall(rf'\s(?:{PRESENTATION})="([^"]*)"', html)]
    for style in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S):
        sources += [("index.html <style>", v) for v in css_values(style)]
    sources += [("index.html style=", v) for v in re.findall(r'\sstyle="([^"]*)"', html)]
    sources += [("theme-color", attrs["content"]) for tag, attrs in tags()
                if tag == "meta" and attrs.get("name") == "theme-color"]
    for svg in SITE.rglob("*.svg"):
        text = svg.read_text(encoding="utf-8")
        attrs = r'\s(?:fill|stroke|stop-color|color|flood-color|lighting-color|style)="([^"]*)"'
        sources += [(svg.name, v) for v in re.findall(attrs, text)]
        for style in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S):
            sources += [(svg.name, v) for v in css_values(style)]
    # In app.js a colour arrives as a string literal, so the quotes go before the value is read.
    for pattern in (r"\.style\.\w+\s*=\s*([^;\n]+)", r"style\.setProperty\(([^;\n]+)",
                    r"\.style\.cssText\s*\+?=\s*([^;\n]+)"):
        sources += [("app.js", re.sub(r"[\"'`]", " ", v)) for v in re.findall(pattern, JS)]
    # A colour set as an attribute: a presentation attribute's value, or each declaration of a style attribute.
    attribute = rf"""setAttribute\(\s*["'`](?:{PRESENTATION})["'`]\s*,\s*([^;\n]+)"""
    sources += [("app.js setAttribute", re.sub(r"[\"'`]", " ", v)) for v in re.findall(attribute, JS)]
    for _, style in JS_STYLE_ATTRIBUTE.findall(JS):
        sources += [("app.js setAttribute style", v) for v in css_values("{" + style + "}")]
    return sources


def test_every_colour_is_grey_black_or_white():
    sources = colour_sources()
    assert len(sources) > 50 and any(name == "theme-color" for name, _ in sources)
    assert [(name, value, colour_problems(value)) for name, value in sources if colour_problems(value)] == []
    # A style attribute set from a variable could not be read, so every one must be a string literal.
    assert len(re.findall(r"""setAttribute\(\s*["'`]style["'`]""", JS)) == len(JS_STYLE_ATTRIBUTE.findall(JS))
    assert any(name == "index.html attribute" and value == "currentColor" for name, value in sources)


def colour_failures() -> list[tuple[str, str]]:
    return [(name, value) for name, value in colour_sources() if colour_problems(value)]


@pytest.mark.parametrize("name, old, new, source, bad", [
    ("INDEX", 'fill="currentColor"', 'fill="#00FF41"', "index.html attribute", True),
    ("INDEX", 'stroke="currentColor"', 'stroke="tomato"', "index.html attribute", True),
    ("INDEX", 'fill="currentColor"', 'fill="#0a0a0a"', "index.html attribute", False),
    ("JS", 'glyph.setAttribute("aria-hidden", "true");',
     'glyph.setAttribute("style", "background:#00FF41");', "app.js setAttribute style", True),
    ("JS", 'glyph.setAttribute("aria-hidden", "true");',
     'glyph.setAttribute("style", "background:#eeeeee");', "app.js setAttribute style", False),
    ("JS", 'glyph.setAttribute("aria-hidden", "true");',
     'glyph.setAttribute("fill", "#00FF41");', "app.js setAttribute", True),
])
def test_colour_scan_reads_svg_attributes_and_attributes_set_from_the_script(monkeypatch, name, old, new, source, bad):
    module = sys.modules[__name__]
    text = getattr(module, name)
    assert old in text and colour_failures() == []
    monkeypatch.setattr(module, name, text.replace(old, new, 1))
    assert any(found == source for found, _ in colour_sources())
    assert [found for found, _ in colour_failures()] == ([source] if bad else [])


@pytest.mark.parametrize("value, bad", [
    ("#22D3EE", True), ("#0A0A0B", True), ("#0a0a0a29", False), ("#ccc", False), ("#cdc", True),
    ("rgb(10, 10, 10)", False), ("rgba(0 0 0 / 0.4)", False), ("rgb(0, 255, 65)", True), ("hsl(0 0% 50%)", True),
    ("color-mix(in srgb, #000000, #ffffff)", True), ("oklch(0.5 0 0)", True), ("lime", True), ("Highlight", True),
    ("LinkText", True), ("silver", True), ("white", False), ("currentColor", False), ("transparent", False),
    ('"Old Standard TT", Georgia, serif', False), ("var(--mark)", False),
])
def test_colour_check_tells_grey_from_hue(value, bad):
    assert bool(colour_problems(value)) is bad


def test_styles_set_link_focus_selection_and_accent_colours():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    rules = [({sel.strip() for sel in selectors.split(",")}, body) for selectors, body in
             re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

    def body_of(selector):
        return " ".join(body for selectors, body in rules if selector in selectors)

    assert re.search(r"(?<![\w-])color\s*:", body_of("a"))
    assert re.search(r"(?<![\w-])color\s*:", body_of("a:visited"))
    assert "outline-color" in body_of(":focus-visible")
    assert "background-color" in body_of("::selection") and re.search(r"(?<![\w-])color\s*:", body_of("::selection"))
    assert "accent-color" in css


# --- self-contained ------------------------------------------------------------------------------------


def test_no_network_or_module_loading():
    for name, text in (("index.html", INDEX), ("styles.css", CSS), ("app.js", JS)):
        for needle in ("fetch(", "XMLHttpRequest", "import(", "@import", "sendBeacon", "WebSocket", "EventSource",
                       "new Worker"):
            assert needle not in text, (name, needle)
        assert not re.search(r"^\s*import\s.*\sfrom\s|^\s*import\s*[\"']", text, re.M), name


def assert_local(path: str, where: str):
    assert not re.match(r"^[a-z][\w+.-]*:|^/", path, re.I), (where, path)
    target = (SITE / re.split(r"[?#]", path)[0]).resolve()
    assert target.is_relative_to(SITE.resolve()) and target.is_file(), (where, path)


def test_every_reference_is_a_local_file_or_an_allowed_link():
    for tag, attrs in tags():
        for attr in ("src", "href", "srcset"):
            if attr not in attrs:
                continue
            ref = attrs[attr]
            if re.match(r"https?://", ref):
                assert tag == "a" and attr == "href" and ref.startswith(ALLOWED_LINKS), (tag, ref)
            else:
                assert_local(ref, f"<{tag} {attr}>")
    for ref in re.findall(r"url\(\s*[\"']?([^\"')]+)", CSS):
        assert_local(ref, "styles.css")
    for literal in re.findall(r"\"([^\"\n]*)\"|'([^'\n]*)'|`([^`]*)`", JS):
        text = "".join(literal)
        if re.match(r"https?://", text):
            raise AssertionError(f"app.js loads {text}")
        if re.search(r"\.(jpg|svg|ttf|css|js)$", text):
            assert_local(text, "app.js")
    for s in SLIDES:
        for path in [s["image"]] if "image" in s else s.get("stack", []):
            assert_local(path, s["id"])


def test_reference_check_refuses_remote_and_escaping_paths():
    for bad in ("https://cdn.example.com/x.js", "/samples/card.jpg", "../README.md", "samples/missing.jpg",
                "data:image/png;base64,AA"):
        with pytest.raises(AssertionError):
            assert_local(bad, "test")
    assert_local("samples/card.jpg?v=1#top", "test")


# --- page outline, logo and the autoplay controls ------------------------------------------------------


def test_page_outline_and_logo():
    found = tags()
    assert [tag for tag, _ in found].count("h1") == 1
    assert re.search(r"<title>[^<]+</title>", INDEX)
    assert any(tag == "meta" and attrs.get("name") == "description" and attrs.get("content") for tag, attrs in found)
    logos = [attrs.get("src") or attrs.get("srcset") for tag, attrs in found
             if tag in ("img", "source") and "logo" in (attrs.get("src") or attrs.get("srcset") or "")]
    assert sorted(logos) == ["logo-black.svg", "logo-white.svg"]
    source = next(attrs for tag, attrs in found if tag == "source")
    assert source == {"media": "(prefers-color-scheme: dark)", "srcset": "logo-white.svg"}
    img = next(attrs for tag, attrs in found if tag == "img" and attrs.get("src") == "logo-black.svg")
    assert img["alt"] == "printer-cli"


def test_autoplay_toggle_has_a_fixed_name_and_reports_its_state():
    button = next(attrs for tag, attrs in tags() if tag == "button" and attrs.get("id") == "autoplay")
    assert button["aria-label"] == "Autoplay" and button["aria-pressed"] in ("true", "false")
    assert any(attrs.get("aria-live") == "polite" for _, attrs in tags())
    assert 'toggle.setAttribute("aria-pressed", String(!userPaused))' in JS
    assert not re.search(r"toggle\.(textContent|innerText|setAttribute\(\"aria-label\")", JS)


def js_function(name: str) -> list[str]:
    """The statements of a top-level function in app.js, one stripped line each, without comment lines."""
    body = re.search(rf"\n  function {name}\([^)]*\) \{{\n(.*?)\n  \}}\n", JS, re.S).group(1)
    return [line.strip() for line in body.split("\n") if line.strip() and not line.strip().startswith("//")]


def js_listener(target: str, event: str) -> list[str]:
    """The statements of the one listener app.js adds for an event, without comments."""
    found = re.findall(rf'{target}\.addEventListener\("{event}", \((?:event)?\) => \{{(.*?)\}}\);', JS, re.S)
    assert len(found) == 1, (target, event, len(found))
    lines = [re.sub(r"\s*//.*$", "", line).strip() for line in found[0].split("\n")]
    return [line for line in lines if line]


def test_playing_stops_for_every_pause():
    assert re.findall(r"^  const playing = .*$", JS, re.M) == [
        "  const playing = (now) => !(userPaused || hoverPaused || focusPaused() || credits.open || "
        "now < touchUntil || hidden);"]


def test_a_manual_step_stops_autoplay_and_only_the_clock_steps_without_stopping_it():
    assert js_function("go") == [
        "index = (to + slides.length) % slides.length;",
        "elapsed = 0;",
        "if (manual) {",
        "userPaused = true;",
        "announce.textContent = `Slide ${index + 1} of ${slides.length}: ${slides[index].title}`;",
        "}",
        "render();",
        "sync();",
    ]
    calls = re.findall(r"(?<![\w.])go\((.*?), (true|false)\)", JS)
    assert len(calls) == len(re.findall(r"(?<![\w.])go\(", JS)) - 1 == 7  # every call but the definition
    assert [args for args, manual in calls if manual == "false"] == ["index + 1"]


def test_the_clock_advances_the_show_only_while_it_is_playing():
    assert js_function("tick") == [
        "if (playing(now)) {",
        "if (last !== null) elapsed += now - last;",
        "last = now;",
        "if (elapsed >= DURATION) go(index + 1, false);",
        "} else {",
        "last = null;",
        "}",
        "progress.style.transform = `scaleX(${Math.min(elapsed / DURATION, 1)})`;",
        "requestAnimationFrame(tick);",
    ]
    assert re.search(r"const DURATION = \d{4};", JS)
    assert JS.rstrip().endswith("render();\n  sync();\n  requestAnimationFrame(tick);\n})();")


def test_a_hidden_tab_and_a_mouse_over_the_text_pause_only_while_they_last():
    assert js_listener("document", "visibilitychange") == ["hidden = document.hidden;", "last = null;"]
    assert "let hidden = document.hidden;" in JS
    assert js_listener("stage", "pointerover") == [
        'if (event.pointerType === "mouse") hoverPaused = Boolean(event.target.closest(".panel"));']
    assert js_listener("stage", "pointerleave") == ['if (event.pointerType === "mouse") hoverPaused = false;']
    assert sorted(re.findall(r"\bhoverPaused\s*=(?!=)\s*([^;]+);", JS)) == [
        'Boolean(event.target.closest(".panel"))', "false", "false"]  # the declaration, then the two listeners


def test_text_reaches_the_page_as_text_never_as_markup():
    assert js_function("el") == [
        "const node = document.createElement(tag);",
        "if (className) node.className = className;",
        "if (text !== undefined) node.textContent = text;",
        "return node;",
    ]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment",
                 "DOMParser", "srcdoc", "setHTML", "eval(", "new Function"):
        assert sink not in JS, sink


def test_the_heading_a_slide_change_focuses_does_not_pause_the_show():
    # After an arrow key the new slide's heading has focus; Space must then really start the show again.
    assert re.findall(r"^  const holdsFocus = .*$", JS, re.M) == [
        "  const holdsFocus = (node) => (stage.contains(node) && node.tabIndex >= 0) || dots.contains(node);"]
    assert re.findall(r"^  const focusPaused = .*$", JS, re.M) == [
        "  const focusPaused = () => holdsFocus(document.activeElement) && "
        'document.activeElement.matches(":focus-visible");']
    render = "\n".join(js_function("render"))
    assert "heading.tabIndex = -1;\nheading.focus({ preventScroll: true });" in render
    assert js_function("toggleAutoplay") == ["userPaused = !userPaused;", "sync();"]
    # The controls that do pause when the keyboard is on them can all take Tab.
    assert "sheet.tabIndex = 0;" in JS and 'const button = el("button", "copy", "Copy");' in JS


def test_the_ticks_are_one_tab_stop_moved_with_the_arrow_keys():
    assert "dot.tabIndex = i === index ? 0 : -1;" in js_function("sync")
    assert JS.count('addEventListener("keydown"') == 1  # one handler, so an arrow on a tick steps once
    keydown = re.search(r'document\.addEventListener\("keydown", \(event\) => \{(.*?)\n  \}\);', JS, re.S).group(1)
    assert "const onDots = dots.contains(target);" in keydown
    assert re.findall(r"(?<![\w.])go\((.*)\);", keydown) == [
        'key === "Home" ? 0 : slides.length - 1, true', 'index + (key === "ArrowRight" ? 1 : -1), true']
    assert '} else if (onDots && (key === "Home" || key === "End")) {' in keydown
    assert keydown.count("dots.children[index].focus()") == 2
    assert "if (onDots) dots.children[index].focus();" in keydown
    assert 'dot.addEventListener("click", () => go(i, true));' in JS


def test_autoplay_pauses_and_only_the_toggle_resumes_it():
    for needle in ("prefers-reduced-motion", "visibilitychange", "let userPaused = reduceMotion.matches",
                   "hoverPaused", "focusPaused"):
        assert needle in JS, needle
    writes = set(re.findall(r"\buserPaused\s*=(?!=)\s*([^;]+);", JS))
    assert writes == {"reduceMotion.matches", "true", "!userPaused"}
    space = re.search(r'key === " "\)(.*?)\} else', JS, re.S).group(1)
    assert re.search(r"target\.closest\(\"button, a, input", space) and space.index("return") < space.index("toggleAutoplay()")


def test_keyboard_shortcuts_have_no_single_letter_key():
    # WCAG 2.1.4: a one-letter shortcut that cannot be turned off fires on speech input; Space and the button stay.
    assert not re.search(r"key === \"[a-zA-Z]\"", JS)
    assert "(P)" not in INDEX and "P / Space" not in JS


def test_only_keyboard_focus_pauses_and_focus_survives_a_slide_change():
    assert 'document.activeElement.matches(":focus-visible")' in JS
    assert "focusPaused()" in re.search(r"const playing = .*", JS).group(0)
    render = re.search(r"function render\(\) \{(.*?)\n  \}", JS, re.S).group(1)
    assert render.index("const hadFocus = stage.contains(document.activeElement)") < render.index("replaceChildren")
    assert "focus({ preventScroll: true })" in render


def test_a_touch_or_scroll_in_the_text_holds_the_slide():
    assert re.search(r"const TOUCH_HOLD = \d{4};", JS)
    assert "now < touchUntil" in re.search(r"const playing = .*", JS).group(0)
    assert 'closest(".panel, .sheet")' in JS
    for listener in ('addEventListener("touchstart", hold', 'addEventListener("scroll", hold, { capture: true'):
        assert listener in JS, listener


# --- layout ---------------------------------------------------------------------------------------------


def test_sheet_type_has_a_floor_and_scrolls_instead():
    size = re.search(r"\.sheet pre \{[^}]*font-size:\s*([^;]+);", CSS).group(1)
    floor = re.match(r"clamp\((\d+)px,", size)
    assert floor and int(floor.group(1)) >= 10, size
    assert re.search(r"\.sheet \{[^}]*overflow: auto", CSS)


def test_short_screens_put_the_text_beside_the_proof():
    rule = re.search(r"@media \(max-width: 767px\) and \(max-height: 500px\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert "grid-template-columns" in rule and "grid-template-rows: minmax(0, 1fr)" in rule
    narrow = re.search(r"@media \(max-width: 767px\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert re.search(r"\.ctl \{ width: 44px; height: 44px; \}", narrow) and ".copy { min-height: 44px; }" in narrow


def test_arrow_keys_leave_the_slide_alone_behind_the_credits_and_with_a_modifier():
    keydown = re.search(r'document\.addEventListener\("keydown", \(event\) => \{(.*?)\n  \}\);', JS, re.S).group(1)
    statements = [line.strip() for line in keydown.split("\n") if line.strip() and not line.strip().startswith("//")]
    # Shift+arrow extends a text selection; no modified key is a shortcut here.
    assert statements[0] == ("if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) "
                             "return;")
    arrows = keydown.split('} else if (key === "ArrowLeft" || key === "ArrowRight") {')[1]
    guard = 'if (credits.open || target.closest(".sheet, input, textarea, select, .credits")) return;'
    assert guard in arrows and arrows.index(guard) < arrows.index("go(")
    ends = keydown.split('} else if (onDots && (key === "Home" || key === "End")) {')[1].split("} else if")[0]
    assert [line.split("//")[0].strip() for line in ends.strip().split("\n")] == [
        "if (credits.open) return;", "event.preventDefault();", 'go(key === "Home" ? 0 : slides.length - 1, true);',
        "dots.children[index].focus();"]
    assert '<details id="credits" class="credits">' in INDEX  # .credits holds the summary and the panel


def test_a_tick_with_keyboard_focus_holds_the_show():
    # The row recentres on every new slide, so a show that ran on would scroll the focused tick out of sight.
    assert "dots.contains(node)" in re.search(r"const holdsFocus = .*", JS).group(0)
    assert "focusPaused()" in re.search(r"const playing = .*", JS).group(0)
    # Autoplay is never turned on while a tick holds the show: Space on a tick presses it, and that step stops
    # the show; a tick is a button, and buttons keep Space.
    assert 'const dot = el("button", "dot");' in JS
    assert "button" in re.search(r'target\.closest\("(button, a, [^"]*)"\)', JS).group(1).split(", ")
    # Focus is moved to a tick only by a key pressed on the row, never by the clock.
    assert JS.count("dots.children[index].focus()") == 2 and ".focus(" not in "\n".join(js_function("sync"))
    assert ".focus(" not in "\n".join(js_function("tick")) + "\n".join(js_function("go"))


def test_every_footer_link_is_a_sized_target_and_the_credit_sentences_stay_inline():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    foot = re.search(r'<footer class="foot">(.*?)</footer>', INDEX, re.S).group(1)
    # The three links and the summary are the footer's own children, so one rule sizes them all; the licence
    # link takes the whole "MIT © Luong NGUYEN", not the three letters alone.
    assert re.search(r'^\s*<a href="LICENSE">MIT © Luong NGUYEN</a>\s*<a href="[^"]+">GitHub</a>\s*'
                     r'<a href="[^"]+">PyPI</a>\s*<details id="credits" class="credits">\s*<summary>', foot)
    assert "<p" not in foot.split("<details")[0]
    targets = ".foot > a, .credits summary"
    assert re.findall(rf"{re.escape(targets)} \{{ ([^}}]*) \}}", css) == [
        "display: flex; align-items: center; min-width: 24px; min-height: 24px;",
        "min-width: 44px; min-height: 44px;", "min-width: 44px; min-height: 44px;",
        "min-width: 32px; min-height: 32px;"]
    assert not re.search(r"\.credits-panel a\b|\.foot a\b", css)


def test_touch_targets_follow_the_pointer_as_well_as_the_width():
    coarse = re.search(r"@media \(any-pointer: coarse\) \{(.*?)\n\}", CSS, re.S).group(1)
    narrow = re.search(r"@media \(max-width: 767px\) \{(.*?)\n\}", CSS, re.S).group(1)
    for rule in (".ctl { width: 44px; height: 44px; }", ".copy { min-height: 44px; }", ".dot { height: 44px; }",
                 ".foot > a, .credits summary { min-width: 44px; min-height: 44px; }"):
        assert rule in coarse and rule in narrow, rule
    assert CSS.index("@media (any-pointer: coarse)") < CSS.index("@media (max-height: 500px)")
    assert "(pointer: coarse)" not in CSS  # the main pointer alone would miss a touch laptop


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def test_footer_is_a_short_line_and_the_credits_open_from_it():
    foot = re.search(r'<footer class="foot">(.*?)</footer>', INDEX, re.S).group(1)
    details = re.search(r'<details id="credits" class="credits">(.*?)</details>', foot, re.S)
    line, credits = foot.replace(details.group(0), ""), details.group(1)
    hrefs = r'<a href="([^"]+)"'

    assert strip_tags(line) == "MIT © Luong NGUYEN GitHub PyPI"
    assert re.findall(hrefs, line) == ["LICENSE", "https://github.com/luongnv89/printer-cli",
                                       "https://pypi.org/project/printer-cli/"]

    # Every credit the licences need, in the static page (no script needed to read them).
    assert re.search(r"^\s*<summary>Credits</summary>", credits)
    assert strip_tags(credits.split("</summary>")[1]) == (
        "printer-cli and this site are under the MIT licence, © Luong NGUYEN. "
        "The photos in the sample pages are public-domain NASA images from Apollo 8, Apollo 11 and Apollo 17, "
        "via Wikimedia Commons. "
        "The fonts are under the SIL OFL 1.1: Playfair Display and Old Standard TT.")
    assert re.findall(hrefs, credits) == [
        "LICENSE", "https://github.com/luongnv89/printer-cli/tree/main/assets/samples",
        "fonts/OFL-PlayfairDisplay.txt", "fonts/OFL-OldStandardTT.txt"]
    assert '<div class="credits-panel" role="group" aria-label="Credits" tabindex="0">' in credits


def test_credits_need_no_sideways_scrolling_and_stay_inside_the_page():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    foot_rules = re.findall(r"(?<![\w-])\.foot \{([^}]*)\}", css)
    assert len(foot_rules) == 4  # the base rule, narrow screens, touch screens and short screens
    for rule in foot_rules:
        assert "nowrap" not in rule and "overflow" not in rule, rule
    assert "flex-wrap: wrap" in foot_rules[0] and "position: relative" in foot_rules[0]
    panel = re.search(r"\.credits-panel \{([^}]*)\}", css).group(1)
    for declaration in ("position: absolute;", "bottom: 100%;", "width: min(34em, 100% - 2 * var(--gutter));",
                        "max-height: calc(100dvh - 100% - 8px);", "overflow-y: auto;"):
        assert declaration in panel, declaration
    assert "white-space" not in panel and "overflow-x" not in panel


def test_open_credits_pause_the_show_and_close_on_escape_or_a_click_outside():
    assert "credits.open" in re.search(r"const playing = .*", JS).group(0)
    assert 'const credits = byId("credits");' in JS
    keydown = re.search(r'document\.addEventListener\("keydown", \(event\) => \{(.*?)\n  \}\);', JS, re.S).group(1)
    escape = re.search(r'if \(key === "Escape" && credits\.open\) \{(.*?)\n    \} else', keydown, re.S).group(1)
    assert [line.strip() for line in escape.split("\n") if line.strip() and not line.strip().startswith("//")] == [
        "const inside = credits.contains(document.activeElement);",
        "credits.open = false;",
        'if (inside) credits.querySelector("summary").focus();',
    ]
    # The panel never stays open over a control the keyboard has moved to.
    assert js_listener("credits", "focusout") == [
        "if (event.relatedTarget instanceof Node && !credits.contains(event.relatedTarget)) credits.open = false;"]
    # Nor over one that focus reaches without leaving the credits first: Tab from the last link goes out of the
    # page and comes back round to the first control.
    assert js_listener("document", "focusin") == [
        "if (credits.open && !(event.target instanceof Node && credits.contains(event.target))) credits.open = false;"]
    assert 'addEventListener("blur"' not in JS  # a window blur closes nothing
    assert js_listener("document", "click") == [
        "if (credits.open && !(event.target instanceof Node && credits.contains(event.target))) credits.open = false;"]
    # Space on the summary opens the credits and in the panel scrolls them; it must not also toggle Autoplay.
    keeps_space = re.search(r'target\.closest\("(button, a, [^"]*)"\)', JS).group(1).split(", ")
    assert {"summary", ".credits-panel", ".sheet", "button", "a"} <= set(keeps_space)


SVG = "{http://www.w3.org/2000/svg}"


def logo_mark(path) -> tuple[dict, list[tuple[str, dict]]]:
    """The mark of a logo file: its one <g>, as the group's attributes and each shape's tag and attributes."""
    groups = ET.parse(path).getroot().findall(f"{SVG}g")
    assert len(groups) == 1, path
    return dict(groups[0].attrib), [(shape.tag, dict(shape.attrib)) for shape in groups[0]]


def test_favicon_is_the_readme_logo_mark():
    favicon, logo = SITE / "favicon.svg", ROOT / "assets" / "logo" / "logo-black.svg"
    transform, shapes = logo_mark(logo)
    assert transform == {"transform": "translate(4,4)"} and len(shapes) == 6
    assert all(tag == f"{SVG}path" and attrs["d"] for tag, attrs in shapes)
    assert logo_mark(favicon) == (transform, shapes)
    # Besides the mark the favicon draws only a white tile behind it; the logo's wordmark is left out.
    root = ET.parse(favicon).getroot()
    assert root.attrib["viewBox"] == "0 0 72 72"
    assert [child.tag for child in root] == [f"{SVG}rect", f"{SVG}g"]
    assert root[0].attrib == {"x": "0", "y": "0", "width": "72", "height": "72", "rx": "14", "fill": "#FFFFFF"}


def test_selection_shows_on_the_command_box_and_the_sheet():
    assert ".cmd ::selection { background-color: var(--term-text); color: var(--term); }" in CSS
    assert ".sheet ::selection { background-color: var(--paper-ink); color: var(--paper); }" in CSS


# --- prompts ------------------------------------------------------------------------------------------


# Samples prt makes from nothing but a seed (and a bundled theme): their prompt names the seed instead of a file.
MADE_FROM_A_SEED = {"puzzle-sudoku", "puzzle-wordsearch", "puzzle-maze"}


def test_image_slide_prompts_ask_for_the_sample_shown():
    for s in IMAGE_SLIDES:
        command, prompt = s["command"], s["prompt"]
        output = re.search(r"-o (\S+)", command)
        if output:
            assert output.group(1) in prompt, (s["id"], output.group(1))
            inputs = re.findall(r"(?<![\w-])([\w-]+\.(?:jpg|json))\b", command.replace(output.group(0), ""))
            assert inputs or s["id"] in MADE_FROM_A_SEED, s["id"]
            for name in inputs:
                assert name in prompt, (s["id"], name)
        for caption in re.findall(r'--caption "([^"]+)"', command):
            assert caption in prompt, (s["id"], caption)
        for name in re.findall(r"([\w-]+\.json)", command):
            data = json.loads((ROOT / "assets" / "samples" / name).read_text(encoding="utf-8"))
            title = next(data[key] for key in ("title", "headline", "greeting") if key in data)
            assert title in prompt, (s["id"], name, title)
    prompts = {s["id"]: s["prompt"] for s in SLIDES}
    assert "Space Camp" in prompts["bingo"] and "1969" in prompts["bingo"]
    for sid in MADE_FROM_A_SEED:
        assert "seed 1969" in prompts[sid], sid
    assert "Apollo Crossword" in prompts["puzzle-crossword"]
    assert "Earthrise" in prompts["instant"] and "Summer" not in prompts["instant"]
    assert "Blue Marble Mix" in prompts["disc"] and "road-trip" not in prompts["disc"]
    assert "Whiskers" in prompts["booth-missing"] and "built-in text" in prompts["booth-wizard"]
