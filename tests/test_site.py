"""The examples site in site/: a static slideshow of every README example, served without a build step.

The slides live as JSON inside site/index.html. These tests hold them to the README (every sample image, the
command that made it, every terminal example copied verbatim), keep the folder self-contained and grey-only,
and check the autoplay controls the slideshow needs to stay pausable.
"""

import json
import re
import tomllib
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
    "prt article https://example.com/blog/post": "article " + USAGE,
    "prt gazette me.jpg": "gazette " + USAGE,
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
    assert len(samples) == 22
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


def test_image_slides_carry_their_real_size():
    from PIL import Image

    for s in IMAGE_SLIDES:
        with Image.open(SITE / s["image"]) as im:
            assert (s["width"], s["height"]) == im.size, s["id"]


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


def colour_sources() -> list[tuple[str, str]]:
    sources = [("styles.css", v) for v in css_values(CSS)]
    html = SLIDES_BLOCK.sub("", INDEX)
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
    return sources


def test_every_colour_is_grey_black_or_white():
    sources = colour_sources()
    assert len(sources) > 50 and any(name == "theme-color" for name, _ in sources)
    assert [(name, value, colour_problems(value)) for name, value in sources if colour_problems(value)] == []


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


def test_autoplay_pauses_and_only_the_toggle_resumes_it():
    for needle in ("prefers-reduced-motion", "visibilitychange", "let userPaused = reduceMotion.matches",
                   "hoverPaused", "focusPaused"):
        assert needle in JS, needle
    writes = set(re.findall(r"\buserPaused\s*=(?!=)\s*([^;]+);", JS))
    assert writes == {"reduceMotion.matches", "true", "!userPaused"}
    space = re.search(r'key === " "\)(.*?)\} else', JS, re.S).group(1)
    assert re.search(r"target\.closest\(\"button, a, input", space) and space.index("return") < space.index("toggleAutoplay()")
