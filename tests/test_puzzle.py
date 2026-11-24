"""prt puzzle: sudoku, word search, crossword and maze pages, and the solution an agent checks a scan against
(#35, #38)."""

import json
import re
import shlex
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from prt import catalog, guard, puzzle, render
from prt.cli import main
from prt.drivers.base import PrinterError
from prt.drivers.fake import FakeDriver
from prt.paper import A4
from prt.puzzle import CrosswordFile, Entry, WordList
from prt.transport import ipp

from conftest import ROOT


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def _error(err):
    return json.loads(err.splitlines()[-1])["error"]


def _file(tmp_path, data, name="input.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def printer(monkeypatch):
    """One FakeDriver handed to the CLI; ``jobs()`` are the JPEGs sent over IPP."""
    fake = FakeDriver()
    made = []
    monkeypatch.setattr("prt.cli.make_driver", lambda target, timeout=5.0: made.append(target) or fake)
    requests = lambda: [r for r in fake.client.requests if r[0] == ipp.OP_PRINT_JOB]  # noqa: E731
    return SimpleNamespace(driver=fake, made=made, jobs=lambda: [data for _, _, _, data in requests()])


# --- sudoku ----------------------------------------------------------------------------------------------------


def _grid(rows):
    return [[0 if ch == "." else int(ch) for ch in row] for row in rows]


@pytest.mark.parametrize("difficulty", puzzle.DIFFICULTIES)
def test_sudoku_has_exactly_one_solution_and_the_solution_fits_the_givens(difficulty):
    for seed in range(5):
        p = puzzle.sudoku(seed, difficulty)
        given, full = _grid(p.solution["puzzle"]), _grid(p.solution["grid"])
        for k in range(9):
            assert sorted(full[k]) == list(range(1, 10))  # every row
            assert sorted(row[k] for row in full) == list(range(1, 10))  # every column
            box = [full[3 * (k // 3) + i][3 * (k % 3) + j] for i in range(3) for j in range(3)]
            assert sorted(box) == list(range(1, 10))
        assert all(g in (0, f) for grow, frow in zip(given, full) for g, f in zip(grow, frow))
        flat = [v for row in given for v in row]
        assert puzzle._solve(list(flat), 2) == 1
        assert 81 - flat.count(0) == 81 - p.solution["blanks"] >= puzzle.SUDOKU_GIVENS[difficulty]
        assert p.layout.givens == tuple(flat)


def test_sudoku_difficulty_sets_how_many_digits_are_given():
    given = {d: 81 - puzzle.sudoku(3, d).solution["blanks"] for d in puzzle.DIFFICULTIES}
    assert given["easy"] > given["medium"] > given["hard"]
    assert given["easy"] == 38 and given["medium"] == 31


def test_the_same_seed_makes_the_same_puzzle_and_another_seed_another():
    for make in (lambda s: puzzle.sudoku(s, "hard"), lambda s: puzzle.wordsearch(s, "hard", theme="zoo"),
                 lambda s: puzzle.maze(s, "easy")):
        assert make(42).answers() == make(42).answers()
        assert make(42).solution != make(43).solution
    a, b = puzzle.compose(puzzle.sudoku(42)), puzzle.compose(puzzle.sudoku(42))
    assert a[0].tobytes() == b[0].tobytes()


# --- word search -----------------------------------------------------------------------------------------------


def _read(grid, cells):
    return "".join(grid[r - 1][c - 1] for r, c in cells)


@pytest.mark.parametrize("difficulty", puzzle.DIFFICULTIES)
def test_every_word_is_in_the_grid_where_the_solution_says(difficulty):
    p = puzzle.wordsearch(7, difficulty, theme="road trip")
    grid, size = p.solution["grid"], p.solution["size"]
    assert size == puzzle.SEARCH_SIZE[difficulty] and len(grid) == size and all(len(row) == size for row in grid)
    assert all(re.fullmatch(r"[A-Z]+", row) for row in grid)
    words = p.solution["words"]
    assert len(words) == puzzle.SEARCH_WORDS[difficulty]
    for w in words:
        assert w["direction"] in puzzle.SEARCH_DIRECTIONS[difficulty]
        assert w["cells"][0] == [w["row"], w["col"]] and len(w["cells"]) == len(w["letters"])
        assert _read(grid, w["cells"]) == w["letters"] == puzzle.letters_of(w["word"])
    assert sorted(p.layout.words, key=str.casefold) == sorted((w["word"] for w in words), key=str.casefold)


def test_words_file_hides_every_word_as_written():
    words = puzzle.parse_words(json.dumps({"title": "Our Holiday", "words": ["Ice cream", "Sandcastle", "Café",
                                                                           "Flip-flops", "Rock pool"]}))
    p = puzzle.wordsearch(1, "easy", words)
    assert p.title == "Our Holiday"
    found = {w["word"]: w["letters"] for w in p.solution["words"]}
    assert found == {"Ice cream": "ICECREAM", "Sandcastle": "SANDCASTLE", "Café": "CAFE", "Flip-flops": "FLIPFLOPS",
                     "Rock pool": "ROCKPOOL"}
    for w in p.solution["words"]:
        assert _read(p.solution["grid"], w["cells"]) == w["letters"]
    # A theme with a words file only names the puzzle, when the file has no title.
    assert puzzle.wordsearch(1, "easy", WordList(words.words), theme="holiday").title == "Holiday Word Search"


def test_a_long_word_makes_the_grid_bigger_and_twenty_long_words_still_fit():
    p = puzzle.wordsearch(2, "easy", WordList(("Constellation", "Moon", "Star")))
    assert p.solution["size"] == 13
    long = WordList(tuple(f"Word{chr(65 + i)}abcdefghij" for i in range(20)))  # 15 letters each, 300 letters
    with pytest.raises(PrinterError, match="too many") as exc:
        puzzle.wordsearch(2, "hard", long)
    assert exc.value.exit_code == 3
    fits = WordList(tuple(f"{chr(65 + i)}{chr(65 + i)}{'xyz'[i % 3]}abcde" for i in range(12)))  # 96 letters
    p = puzzle.wordsearch(2, "hard", fits)
    assert len(p.solution["words"]) == 12


@pytest.mark.parametrize("data, message", [
    ({"words": ["Moon", "Star"]}, "has 2 words"),
    ({"words": ["Moon", "Star", "M2"]}, r"words\[2\] \('M2'\) has characters other than letters"),
    ({"words": ["Moon", "Star", "Go"]}, r"words\[2\] \('Go'\) has 2 letters"),
    ({"words": ["Moon", "Star", "MOON"]}, r"words\[2\] repeats words\[0\]"),
    ({"words": ["Moon", "Star", "Sun"], "theme": "x"}, "unknown field"),
    ({"title": 3, "words": ["Moon", "Star", "Sun"]}, "title in words file must be text"),
    ({"words": "Moon"}, "must be a list of texts"),
    ({}, "missing words"),
])
def test_bad_words_file_is_refused_with_exit_code_3(data, message):
    with pytest.raises(PrinterError, match=message) as exc:
        puzzle.parse_words(json.dumps(data))
    assert exc.value.exit_code == 3


# --- crossword -------------------------------------------------------------------------------------------------


def _check_crossword(p):
    grid = p.solution["grid"]
    assert len(grid) == p.solution["rows"] <= puzzle.MAX_CROSSWORD
    assert all(len(row) == p.solution["cols"] <= puzzle.MAX_CROSSWORD for row in grid)
    cells = {}
    for way, (dr, dc) in (("across", (0, 1)), ("down", (1, 0))):
        for a in p.solution[way]:
            read = "".join(grid[a["row"] - 1 + dr * k][a["col"] - 1 + dc * k] for k in range(a["length"]))
            assert read == a["answer"] == puzzle.letters_of(a["written"])
            assert p.layout.numbers[(a["row"] - 1, a["col"] - 1)] == a["number"]
            for k in range(a["length"]):
                cells.setdefault((a["row"] - 1 + dr * k, a["col"] - 1 + dc * k), []).append(a["number"])
    # Every letter square belongs to an answer, and every run of two or more letters is one of the answers.
    letters = {(r, c) for r, row in enumerate(grid) for c, ch in enumerate(row) if ch != "."}
    assert letters == set(cells)
    runs = set()
    for r, row in enumerate(grid):
        for m in re.finditer(r"[A-Z]{2,}", row):
            runs.add(("across", r, m.start()))
    for c in range(len(grid[0])):
        column = "".join(row[c] for row in grid)
        for m in re.finditer(r"[A-Z]{2,}", column):
            runs.add(("down", m.start(), c))
    assert runs == {(way, a["row"] - 1, a["col"] - 1) for way in ("across", "down") for a in p.solution[way]}
    numbers = [a["number"] for way in ("across", "down") for a in p.solution[way]]
    assert sorted(set(numbers)) == list(range(1, len(set(numbers)) + 1))


def test_crossword_places_every_answer_so_that_it_crosses():
    for file in (puzzle.DEFAULT_CROSSWORD,
                 puzzle.load_crossword(ROOT / "assets" / "samples" / "crossword.json")):
        p = puzzle.crossword(file)
        _check_crossword(p)
        assert len(p.solution["across"]) + len(p.solution["down"]) == len(file.entries)
        assert p.seed is None and p.difficulty is None


def test_crossword_layout_depends_on_the_entries_alone():
    file = puzzle.load_crossword(ROOT / "assets" / "samples" / "crossword.json")
    assert puzzle.crossword(file).answers() == puzzle.crossword(file).answers()
    assert puzzle.crossword(file).title == "Apollo Crossword"
    assert puzzle.crossword(file, "Moon Quiz").title == "Moon Quiz"


def test_crossword_shows_letter_counts_and_drops_spaces_from_the_grid():
    file = puzzle.check_crossword(CrosswordFile((Entry("Buzz Aldrin", "Second on the Moon"),
                                                 Entry("Ice-cream", "A cold treat"),
                                                 Entry("Zebra", "A striped horse"))), "test", 3)
    p = puzzle.crossword(file)
    _check_crossword(p)
    shown = {clue: count for way in ("across", "down") for _, clue, count in p.layout.clues[way]}
    assert shown == {"Second on the Moon": "(4,6)", "A cold treat": "(3-5)", "A striped horse": "(5)"}


def test_an_answer_that_cannot_cross_is_refused_by_name():
    file = puzzle.check_crossword(CrosswordFile((Entry("Moon", "a"), Entry("Mars", "b"), Entry("Tux", "c"))),
                                  "test", 3)
    with pytest.raises(PrinterError, match=r"entries\[2\] \('Tux'\) cannot cross the other answers") as exc:
        puzzle.crossword(file)
    assert exc.value.exit_code == 3


def test_the_largest_crossword_fits_its_page():
    words = ["Astronaut", "Telescope", "Satellite", "Spaceship", "Asteroid", "Starlight", "Planetary", "Moonbeam",
             "Lunar", "Solar", "Comet", "Orbit", "Rocket", "Galaxy", "Nebula", "Meteor", "Crater", "Saturn",
             "Jupiter", "Neptune"]
    assert len(words) == puzzle.MAX_ENTRIES
    file = puzzle.check_crossword(CrosswordFile(tuple(Entry(w, "x" * puzzle.MAX_CLUE) for w in words)), "t", 3)
    p = puzzle.crossword(file)
    _check_crossword(p)
    cell, size, blocks, flow = puzzle._crossword_fit(p.layout)
    room = puzzle.FOOT - 70 - (puzzle.BODY_TOP + 10 + cell * p.layout.rows + 50)
    assert puzzle._flow(blocks, round(size * 1.3), room, 2) == flow  # it fits, not the overflow fallback
    page, cut = puzzle.compose(p)
    assert cut == {} and page.size == A4.size
    # Nothing is drawn over the label line at the foot: the clues end above it.
    crop = page.convert("L").crop((0, puzzle.FOOT - 30, page.width, puzzle.FOOT - 22))
    assert crop.getextrema() == (255, 255)


@pytest.mark.parametrize("data, message", [
    ({"entries": [{"answer": "Moon", "clue": "a"}]}, "has 1 entries"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Apollo 11", "clue": "b"}]},
     r"entries\[1\]\.answer \('Apollo 11'\) has characters other than letters"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "M", "clue": "b"}]}, "has 1 letters"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "moon", "clue": "b"}]},
     r"entries\[1\]\.answer repeats entries\[0\]\.answer"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Mars", "clue": " "}]}, r"entries\[1\]\.clue is empty"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Mars", "clue": "x" * 121}]}, "too long"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Mars", "clue": "b", "hint": "c"}]},
     r"unknown field\(s\) in entries\[1\]: hint"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Mars"}]}, r"entries\[1\]\.clue must be text"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, "Mars"]}, r"entries\[1\] must be an object"),
    ({"entries": [{"answer": "Moon", "clue": "a"}, {"answer": "Mars", "clue": "😀"}]}, "cannot print"),
    ({"title": "x"}, "missing entries"),
])
def test_bad_crossword_file_is_refused_with_exit_code_3(data, message):
    with pytest.raises(PrinterError, match=message) as exc:
        puzzle.parse_crossword(json.dumps(data))
    assert exc.value.exit_code == 3


# --- maze ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("difficulty", puzzle.DIFFICULTIES)
def test_maze_is_perfect_and_the_path_runs_from_start_to_finish(difficulty):
    p = puzzle.maze(11, difficulty)
    cols, rows = puzzle.MAZE_SIZE[difficulty]
    assert (p.solution["cols"], p.solution["rows"]) == (cols, rows)
    # A perfect maze over every square is a spanning tree: one opening fewer than squares, all reachable.
    assert len(p.layout.open) == cols * rows - 1
    path = [tuple(cell) for cell in p.solution["path"]]
    assert path[0] == (1, 1) == tuple(p.solution["start"]) and path[-1] == (rows, cols) == tuple(p.solution["finish"])
    assert len(set(path)) == len(path)
    for (r1, c1), (r2, c2) in zip(path, path[1:]):
        assert abs(r1 - r2) + abs(c1 - c2) == 1
        assert tuple(sorted(((r1 - 1, c1 - 1), (r2 - 1, c2 - 1)))) in p.layout.open


# --- the page --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [lambda: puzzle.sudoku(1), lambda: puzzle.wordsearch(1), puzzle.crossword,
                                  lambda: puzzle.maze(1)])
def test_each_kind_renders_one_a4_page(make):
    p = make()
    rendered = render.puzzle_page(p)
    assert len(rendered.pages) == 1 and rendered.pages[0].size == A4.size and rendered.paper == A4
    assert rendered.fit() == {"fits": True, "cut": {}}
    assert rendered.pages[0].convert("L").getextrema()[0] < 80  # something dark is drawn


def test_the_foot_of_the_page_says_which_puzzle_it_is():
    assert puzzle.sudoku(4821, "hard").label() == "sudoku · hard · seed 4821"
    assert puzzle.maze(7, "easy").label() == "maze · easy · seed 7"
    assert puzzle.crossword().label() == f"crossword · {len(puzzle.DEFAULT_CROSSWORD.entries)} clues"


def test_a_long_title_is_cut_and_reported():
    p = puzzle.sudoku(1, title="A very long title for a sudoku that runs on and on, past the edge")
    assert list(render.puzzle_page(p).cut) == ["title"]


# --- prt puzzle --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, args", [("sudoku", ["--seed", "5"]), ("wordsearch", ["--seed", "5"]),
                                        ("crossword", []), ("maze", ["--seed", "5"])])
def test_preview_saves_the_page_and_prints_nothing(capsys, tmp_path, printer, kind, args):
    out_file = tmp_path / f"{kind}.png"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", kind, *args, "--preview", str(out_file))
    assert code == 0, err
    line = json.loads(out)
    assert line["action"] == "puzzle" and line["kind"] == kind and line["file"] == str(out_file)
    assert line["paper"]["size"] == "a4" and line["fits"] is True and line["estimate"]["pages"] == 1
    assert Image.open(out_file).size == A4.size
    assert printer.made == [] and printer.jobs() == [] and guard.used_today() == 0


def test_json_line_and_answers_file_carry_the_same_solution(capsys, tmp_path, printer):
    answers = tmp_path / "key.json"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", "sudoku", "-d", "hard", "--seed", "4821",
                         "--answers", str(answers), "-o", str(tmp_path / "s.pdf"))
    assert code == 0, err
    line = json.loads(out)
    saved = json.loads(answers.read_text(encoding="utf-8"))
    assert saved == {k: line[k] for k in ("kind", "title", "seed", "difficulty", "solution")}
    assert saved == puzzle.sudoku(4821, "hard").answers()
    assert line["answers"] == str(answers) and saved["seed"] == 4821 and saved["difficulty"] == "hard"


def test_a_printed_sheet_and_its_preview_with_the_same_seed_are_the_same_puzzle(capsys, tmp_path, printer):
    code, out, _ = run(capsys, "-p", "demo", "-f", "json", "puzzle", "wordsearch", "--theme", "space",
                       "-o", str(tmp_path / "w.png"))
    preview = json.loads(out)
    assert isinstance(preview["seed"], int)
    key = tmp_path / "key.json"
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", "wordsearch", "--theme", "space",
                         "--seed", str(preview["seed"]), "--answers", str(key), "--yes")
    assert code == 0, err
    printed = json.loads(out)
    assert printed["job_id"] and printed["solution"] == preview["solution"]
    assert json.loads(key.read_text())["solution"] == preview["solution"]
    assert len(printer.jobs()) == 1 and guard.used_today() == 1


def test_print_sends_one_a4_page(capsys, printer):
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", "maze", "--seed", "3", "--yes")
    assert code == 0, err
    assert json.loads(out)["job_id"] and len(printer.jobs()) == 1
    assert Image.open(__import__("io").BytesIO(printer.jobs()[0])).size == A4.size
    assert "--seed 3 makes the same puzzle again" in err


def test_crossword_file_and_stdin(capsys, tmp_path, printer, stdin):
    data = json.loads((ROOT / "assets" / "samples" / "crossword.json").read_text())
    path = _file(tmp_path, data)
    code, out, err = run(capsys, "-f", "json", "puzzle", "crossword", "--crossword-file", str(path),
                         "-o", str(tmp_path / "x.png"))
    assert code == 0, err
    from_file = json.loads(out)
    assert from_file["title"] == "Apollo Crossword" and from_file["seed"] is None
    stdin(json.dumps(data).encode())
    code, out, err = run(capsys, "-f", "json", "puzzle", "crossword", "--crossword-file", "-",
                         "-o", str(tmp_path / "y.png"))
    assert code == 0, err
    assert json.loads(out)["solution"] == from_file["solution"]


def test_words_file_from_stdin(capsys, tmp_path, printer, stdin):
    stdin(json.dumps({"title": "Pets", "words": ["Cat", "Dog", "Rabbit", "Hamster"]}).encode())
    code, out, err = run(capsys, "-f", "json", "puzzle", "wordsearch", "--words-file", "-", "--seed", "1",
                         "-o", str(tmp_path / "w.png"))
    assert code == 0, err
    line = json.loads(out)
    assert line["title"] == "Pets" and {w["word"] for w in line["solution"]["words"]} == {"Cat", "Dog", "Rabbit",
                                                                                         "Hamster"}


@pytest.mark.parametrize("args, code_, kind, message", [
    (["wordsearch", "--theme", "mars"], 2, "unknown_theme", "unknown word search theme"),
    (["sudoku", "--answers", "key.txt"], 2, "usage", "must end in .json"),
    (["sudoku", "--title", "x" * 61], 2, "usage", "title is too long"),
    (["maze", "--difficulty", "extreme"], 2, "usage", "extreme"),
    (["crossword", "--crossword-file", "missing.json"], 3, "invalid_input", "crossword file not found"),
])
def test_bad_options_are_refused_before_anything_is_written(capsys, tmp_path, printer, monkeypatch, args, code_,
                                                            kind, message):
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", *args, "-o", "page.png")
    assert code == code_ and out == ""
    assert _error(err)["kind"] == kind and message in _error(err)["message"]
    assert list(tmp_path.iterdir()) == [] and printer.made == []


def test_an_output_on_top_of_the_input_file_is_refused(capsys, tmp_path, printer):
    path = _file(tmp_path, {"words": ["Cat", "Dog", "Rabbit"]}, "words.json")
    before = path.read_bytes()
    code, _, err = run(capsys, "-f", "json", "puzzle", "wordsearch", "--words-file", str(path),
                       "--answers", str(path), "-o", str(tmp_path / "w.png"))
    assert code == 2 and "would overwrite" in _error(err)["message"]
    assert path.read_bytes() == before and not (tmp_path / "w.png").exists()


def test_agent_mode_needs_yes_but_preview_and_answers_do_not(capsys, tmp_path, printer, monkeypatch):
    monkeypatch.setenv("PRT_AGENT", "1")
    code, _, err = run(capsys, "-p", "demo", "-f", "json", "puzzle", "sudoku", "--seed", "1")
    assert code == 2 and _error(err)["kind"] == "agent_confirmation_required" and printer.jobs() == []
    code, _, _ = run(capsys, "-p", "demo", "puzzle", "sudoku", "--seed", "1", "--answers",
                     str(tmp_path / "k.json"), "-o", str(tmp_path / "s.png"))
    assert code == 0 and (tmp_path / "k.json").is_file() and printer.jobs() == [] and guard.used_today() == 0


# --- catalog and schemas -----------------------------------------------------------------------------------------


def test_schemas_accept_what_the_commands_accept():
    crossword = json.loads((ROOT / "assets" / "samples" / "crossword.json").read_text())
    assert Draft202012Validator(catalog.schema("crossword")).is_valid(crossword)
    puzzle.parse_crossword(json.dumps(crossword))
    for example in catalog.schema("crossword")["examples"]:
        puzzle.crossword(puzzle.parse_crossword(json.dumps(example)))
    for example in catalog.schema("wordsearch")["examples"]:
        puzzle.wordsearch(1, "medium", puzzle.parse_words(json.dumps(example)))
    too_few = {"entries": [{"answer": "Moon", "clue": "a"}]}
    assert not Draft202012Validator(catalog.schema("crossword")).is_valid(too_few)
    assert not Draft202012Validator(catalog.schema("wordsearch")).is_valid({"words": ["a", "b"]})


def test_styles_list_the_four_puzzles(capsys):
    code, out, _ = run(capsys, "styles", "-f", "json")
    styles = {s["name"]: s for s in json.loads(out)}
    for kind in puzzle.KINDS:
        s = styles[kind]
        assert s["command"] == "puzzle" and s["args"] == [kind] and s["max_pages"] == 1
        assert s["sample"].endswith(f"puzzle-{kind}.jpg") and s["paper"]["size"] == "a4"


# --- the puzzle-check skill (#38) ----------------------------------------------------------------------------------

SKILL = ROOT / "skills" / "puzzle-check" / "SKILL.md"


def test_puzzle_check_skill_matches_the_cli(capsys, tmp_path, monkeypatch, printer):
    """Every flag the skill names exists, and its example crossword and commands run as written."""
    skill = SKILL.read_text()
    help_text = "".join(run(capsys, "puzzle", kind, "--help")[1] for kind in puzzle.KINDS)
    help_text += run(capsys, "scan", "--help")[1] + run(capsys, "--help")[1]
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", skill))
    assert {"--answers", "--seed", "--difficulty", "--crossword-file", "--words-file", "--preview", "--yes",
            "--format"} <= flags
    assert [f for f in sorted(flags) if f not in help_text] == []
    monkeypatch.chdir(tmp_path)
    (tmp_path / "crossword.json").write_text(re.search(r"```json\n(.*?)```", skill, re.S).group(1))
    puzzle.crossword(puzzle.load_crossword(tmp_path / "crossword.json"))  # the example lays out
    commands = [shlex.split(line) for line in re.findall(r"^prt (?:-p demo )?(?:puzzle|scan) .*$", skill, re.M)]
    assert len(commands) >= 8
    for argv in commands:
        argv = [a for a in argv[1:] if a not in ("-p", "demo")]
        if argv[0] == "puzzle" and "--words-file" in argv:
            continue  # the words file is the user's own
        assert run(capsys, "-p", "demo", *argv)[0] == 0, argv
    key = json.loads((tmp_path / "sudoku-answers.json").read_text())
    assert key["solution"] == puzzle.sudoku(4821, "easy").solution
    assert (tmp_path / "filled.jpg").read_bytes()[:2] == b"\xff\xd8"


def test_puzzle_check_skill_describes_every_solution_field():
    skill = SKILL.read_text()
    shapes = {"sudoku": puzzle.sudoku(1).solution, "wordsearch": puzzle.wordsearch(1).solution,
              "crossword": puzzle.crossword().solution, "maze": puzzle.maze(1).solution}
    for kind, solution in shapes.items():
        row = next(line for line in skill.splitlines() if line.startswith(f"| {kind} |"))
        for name in re.findall(r"solution\.(\w+)", row):
            assert name in solution, (kind, name)
    for key in ("puzzle", "grid"):
        assert f"solution.{key}" in skill
    assert "row" in puzzle.wordsearch(1).solution["words"][0] and "number" in puzzle.crossword().solution["across"][0]
