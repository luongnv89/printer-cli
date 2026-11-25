"""The morning gazette edition: its JSON file, schema, rendering and CLI (#40)."""

import json

import pytest
from jsonschema import Draft202012Validator

from prt import catalog, morning, render
from prt.cli import main
from prt.drivers.base import PrinterError


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


EXAMPLE = {
    "title": "The Morning Paper",
    "weather": {"summary": "Fair and mild, 18C", "details": ["Light wind"]},
    "calendar": {"items": ["09:00 Team standup", "13:00 Dentist"]},
    "todos": {"items": ["Water the plants"]},
    "headlines": {"items": [{"title": "Library reopens", "summary": "After repairs.", "source": "Example News"}]},
}


def _file(tmp_path, data=EXAMPLE):
    path = tmp_path / "morning.json"
    path.write_text(json.dumps(data))
    return path


def _accepted(data) -> bool:
    try:
        morning.parse_morning(json.dumps(data))
    except PrinterError as exc:
        assert exc.exit_code == 3
        return False
    return True


def test_schema_example_is_valid_and_prints(capsys, tmp_path):
    example = catalog.schema("morning")["examples"][0]
    assert Draft202012Validator(catalog.schema("morning")).is_valid(example)
    out_file = tmp_path / "morning.png"
    code, out, err = run(capsys, "gazette", "--edition", "morning", "--edition-file", str(_file(tmp_path, example)),
                         "-f", "json", "-o", str(out_file))
    assert code == 0, err
    report = json.loads(out)
    assert report["action"] == "gazette" and report["edition"] == "morning" and report["photos"] == 0
    assert report["fits"] is True and report["cut"] == {} and report["estimate"]["pages"] == 1
    assert out_file.is_file() and out_file.stat().st_size > 10_000


@pytest.mark.parametrize("data", [
    EXAMPLE,
    {**EXAMPLE, "title": "x" * morning.LIMITS["title"]},
    {**EXAMPLE, "extra": True},
    {k: v for k, v in EXAMPLE.items() if k != "weather"},
    {**EXAMPLE, "weather": {"summary": "Fair", "extra": "x"}},
    {**EXAMPLE, "weather": {"details": []}},
    {**EXAMPLE, "calendar": {"items": "09:00 Standup"}},
    {**EXAMPLE, "todos": {"items": [""]}},
    {**EXAMPLE, "headlines": {"items": [{"summary": "No title"}]}},
    {**EXAMPLE, "headlines": {"items": [{"title": "x" * (morning.LIMITS["headlines.items[].title"] + 1)}]}},
    [], "morning",
])
def test_morning_schema_agrees_with_the_parser(data):
    assert Draft202012Validator(catalog.schema("morning")).is_valid(data) == _accepted(data)


def test_morning_document_has_the_four_required_sections():
    paper = morning.parse_morning(json.dumps(EXAMPLE))
    doc = morning.document(paper)
    assert [s.key for s in doc.sections] == ["weather", "calendar", "todos", "headlines"]
    assert doc.masthead == "The Morning Gazette" and doc.edition == "Morning edition"


def test_a_long_date_is_cut_before_it_overlaps_the_edition_line():
    paper = morning.parse_morning(json.dumps({**EXAMPLE, "date": "D" * morning.LIMITS["date"]}))
    cut = render.morning_page(paper).cut["date"]
    assert cut.given == morning.LIMITS["date"] and 0 < cut.max_fit < cut.given


def test_morning_stdin_matches_the_file(capsys, tmp_path, stdin):
    stdin(json.dumps(EXAMPLE).encode())
    out_file = tmp_path / "morning.png"
    code, out, err = run(capsys, "gazette", "--edition", "morning", "--edition-file", "-", "-o", str(out_file))
    assert code == 0, err
    assert out_file.is_file()


def test_bad_morning_file_fails_before_a_preview_is_written(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"weather": {"summary": "Rain"}}')
    out_file = tmp_path / "morning.png"
    code, out, err = run(capsys, "gazette", "--edition", "morning", "--edition-file", str(bad), "-o", str(out_file))
    assert code == 3 and out == "" and "needs a calendar object" in err and not out_file.exists()


def test_edition_usage_errors(capsys, tmp_path):
    path = _file(tmp_path)
    code, _, err = run(capsys, "gazette", "--edition", "morning")
    assert code == 2 and "needs --edition-file" in err
    code, _, err = run(capsys, "gazette", str(tmp_path / "photo.jpg"), "--edition", "morning", "--edition-file", str(path))
    assert code == 2 and "takes no photos" in err
    code, _, err = run(capsys, "gazette", "--edition-file", str(path))
    assert code == 2 and "needs --edition morning" in err


def test_output_may_not_overwrite_the_morning_file(capsys, tmp_path):
    path = tmp_path / "morning.png"
    path.write_text(json.dumps(EXAMPLE))
    code, _, err = run(capsys, "gazette", "--edition", "morning", "--edition-file", str(path), "-o", str(path))
    assert code == 2 and "would overwrite input morning file" in err
