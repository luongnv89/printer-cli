"""prt skills list / install, the Claude Code plugin manifests, and the agent docs at the repository root.

Every install goes into the temporary home folder that conftest sets up, or into a --dir under tmp_path: never
into the real ~/.claude or ~/.agents.
"""

import json
import os
import re
import tomllib
from pathlib import Path

import pytest

from prt import __version__, skills
from prt.cli import main

from conftest import ROOT

NAMES = ["article-print", "photo-booth"]


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def home(tmp_path):
    path = Path(os.environ["HOME"])
    assert path == tmp_path / "home" == Path.home()  # conftest points HOME at the test's own folder
    return path


def bundled_bytes(name: str) -> bytes:
    return (ROOT / "skills" / name / "SKILL.md").read_bytes()


# --- the bundled skills ------------------------------------------------------------------------


def test_bundled_skills_are_the_repository_skills():
    found = skills.bundled()
    assert [s.name for s in found] == NAMES
    for s in found:
        assert s.path == ROOT / "skills" / s.name and s.version and s.description.startswith(("Print", "Turn"))
        assert [f.as_posix() for f in s.files()] == ["SKILL.md"]


def test_wheel_carries_the_skills_where_prt_looks_for_them():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"] == {"skills": f"prt/{skills.BUNDLE}"}
    assert "skills" not in data["tool"]["hatch"]["build"]["targets"]["sdist"].get("exclude", [])


def test_installed_copy_wins_over_the_repository(tmp_path, monkeypatch):
    pkg = tmp_path / "site" / "prt"
    (pkg / skills.BUNDLE / "demo-skill").mkdir(parents=True)
    (pkg / skills.BUNDLE / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo.\n---\n")
    monkeypatch.setattr(skills, "__file__", str(pkg / "skills.py"))
    assert skills.source_dir() == pkg / skills.BUNDLE
    assert [s.name for s in skills.bundled()] == ["demo-skill"]


def test_missing_skills_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(skills, "__file__", str(tmp_path / "site" / "prt" / "skills.py"))
    code, out, err = run(capsys, "-f", "json", "skills", "list")
    assert code == 1 and out == ""
    assert json.loads(err)["error"]["kind"] == "skills_missing"


def test_front_matter_parsing(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "SKILL.md").write_text('---\nname: my-skill\ndescription: "Say \\"hi\\": politely."\nlicense: MIT\n'
                                     "metadata:\n  version: 1.2.3\n  author: 'Me'\n---\n\n# Body\nname: not this\n")
    s = skills._read_skill(folder)
    assert (s.name, s.description, s.version) == ("my-skill", 'Say "hi": politely.', "1.2.3")


# --- prt skills list -------------------------------------------------------------------------


def test_skills_list_json(capsys):
    code, out, err = run(capsys, "skills", "list", "-f", "json")
    assert code == 0
    items = json.loads(out)
    assert [i["name"] for i in items] == NAMES
    assert all(set(i) == {"name", "version", "description", "files"} and i["files"] == ["SKILL.md"] for i in items)


def test_skills_list_plain(capsys):
    code, out, err = run(capsys, "skills", "list")
    assert code == 0
    assert [line.split("\t")[0] for line in out.splitlines()] == NAMES


# --- prt skills install -------------------------------------------------------------------------


def test_install_claude(home, capsys):
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--claude")
    assert code == 0
    result = json.loads(out)
    assert result["action"] == "skills_install"
    assert [(s["name"], s["target"], s["status"]) for s in result["skills"]] == [
        ("article-print", "claude", "installed"), ("photo-booth", "claude", "installed")]
    for name in NAMES:
        installed = home / ".claude" / "skills" / name / "SKILL.md"
        assert installed.read_bytes() == bundled_bytes(name)
        assert result["skills"][NAMES.index(name)]["path"] == str(installed.parent)
    assert not (home / ".agents").exists()
    assert "photo-booth installed" in err


def test_install_defaults_to_claude(home, capsys):
    assert run(capsys, "skills", "install")[0] == 0
    assert sorted(p.name for p in (home / ".claude" / "skills").iterdir()) == NAMES


def test_install_agents(home, capsys):
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--agents")
    assert code == 0
    assert {s["target"] for s in json.loads(out)["skills"]} == {"agents"}
    assert (home / ".agents" / "skills" / "article-print" / "SKILL.md").read_bytes() == bundled_bytes("article-print")
    assert not (home / ".claude").exists()


def test_install_both_and_one_skill(home, capsys):
    code, out, err = run(capsys, "-f", "json", "skills", "install", "photo-booth", "--claude", "--agents")
    assert code == 0
    assert [(s["name"], s["target"]) for s in json.loads(out)["skills"]] == [("photo-booth", "claude"),
                                                                             ("photo-booth", "agents")]
    assert [p.name for p in (home / ".claude" / "skills").iterdir()] == ["photo-booth"]
    assert [p.name for p in (home / ".agents" / "skills").iterdir()] == ["photo-booth"]


def test_install_project(home, tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert run(capsys, "skills", "install", "--project", "--agents")[0] == 0
    assert sorted(p.name for p in (project / ".agents" / "skills").iterdir()) == NAMES
    assert not (home / ".agents").exists()


def test_install_dir(tmp_path, home, capsys):
    target = tmp_path / "my skills"
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--dir", str(target))
    assert code == 0
    assert {s["target"] for s in json.loads(out)["skills"]} == {"dir"}
    assert (target / "photo-booth" / "SKILL.md").read_bytes() == bundled_bytes("photo-booth")
    assert not (home / ".claude").exists()


@pytest.mark.parametrize("extra", [["--claude"], ["--agents"], ["--project"]])
def test_dir_excludes_other_targets(tmp_path, capsys, extra):
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--dir", str(tmp_path / "s"), *extra)
    assert code == 2 and json.loads(err)["error"]["kind"] == "usage"
    assert not (tmp_path / "s").exists()


def test_unknown_skill(home, capsys):
    code, out, err = run(capsys, "-f", "json", "skills", "install", "photo-booth", "nope")
    assert code == 2
    error = json.loads(err)["error"]
    assert error["kind"] == "usage" and "'nope'" in error["message"] and "photo-booth" in error["hint"]
    assert not (home / ".claude").exists()


def test_reinstall_is_unchanged(home, capsys):
    run(capsys, "skills", "install")
    installed = home / ".claude" / "skills" / "photo-booth" / "SKILL.md"
    before = installed.stat().st_mtime_ns
    code, out, err = run(capsys, "-f", "json", "skills", "install")
    assert code == 0
    assert {s["status"] for s in json.loads(out)["skills"]} == {"unchanged"}
    assert installed.stat().st_mtime_ns == before


def test_changed_file_is_never_replaced_without_force(home, capsys):
    run(capsys, "skills", "install", "--claude", "--agents")
    mine = home / ".claude" / "skills" / "photo-booth" / "SKILL.md"
    mine.write_text("my own edits\n")
    (home / ".agents" / "skills" / "article-print" / "SKILL.md").unlink()  # would be reinstalled
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--claude", "--agents")
    assert code == 1 and out == ""
    error = json.loads(err)["error"]
    assert error["kind"] == "skill_exists" and str(mine) in error["message"] and "--force" in error["hint"]
    assert mine.read_text() == "my own edits\n"
    assert not (home / ".agents" / "skills" / "article-print" / "SKILL.md").exists()  # nothing was written


def test_force_replaces_changed_files(home, capsys):
    run(capsys, "skills", "install")
    mine = home / ".claude" / "skills" / "photo-booth" / "SKILL.md"
    mine.write_text("my own edits\n")
    (home / ".claude" / "skills" / "photo-booth" / "notes.txt").write_text("kept\n")
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--force")
    assert code == 0
    assert [s["status"] for s in json.loads(out)["skills"]] == ["unchanged", "replaced"]
    assert mine.read_bytes() == bundled_bytes("photo-booth")
    assert (mine.parent / "notes.txt").read_text() == "kept\n"  # files prt does not ship are left alone


def test_partial_install_is_updated(tmp_path):
    source = tmp_path / "src" / "two-files"
    (source / "refs").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: two-files\ndescription: Two.\n---\n")
    (source / "refs" / "more.md").write_text("more\n")
    skill = skills._read_skill(source)
    dest = tmp_path / "dest"
    assert [i.status for i in skills.install([skill], [("dir", dest)])] == ["installed"]
    (dest / "two-files" / "refs" / "more.md").unlink()
    assert [i.status for i in skills.install([skill], [("dir", dest)])] == ["updated"]
    assert (dest / "two-files" / "refs" / "more.md").read_text() == "more\n"
    assert [i.status for i in skills.install([skill], [("dir", dest)])] == ["unchanged"]


def test_symlink_is_replaced_not_written_through(home, tmp_path, capsys):
    outside = tmp_path / "outside.md"
    outside.write_text("someone else's file\n")
    folder = home / ".claude" / "skills" / "photo-booth"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").symlink_to(outside)
    assert run(capsys, "skills", "install", "photo-booth")[0] == 1  # differs: refused
    assert run(capsys, "skills", "install", "photo-booth", "--force")[0] == 0
    assert outside.read_text() == "someone else's file\n"
    assert not (folder / "SKILL.md").is_symlink()
    assert (folder / "SKILL.md").read_bytes() == bundled_bytes("photo-booth")


def test_file_in_the_way(home, capsys):
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "photo-booth").write_text("not a folder\n")
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--force")
    assert code == 1 and json.loads(err)["error"]["kind"] == "skill_exists"
    assert (skills_dir / "photo-booth").read_text() == "not a folder\n"
    assert not (skills_dir / "article-print").exists()


def test_unwritable_destination(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code, out, err = run(capsys, "-f", "json", "skills", "install", "--dir", str(blocker / "skills"))
    assert code == 1 and json.loads(err)["error"]["message"].startswith("cannot write")


# --- Claude Code plugin and marketplace -------------------------------------------------------------


def manifest(name: str) -> dict:
    return json.loads((ROOT / ".claude-plugin" / name).read_text())


def test_plugin_manifest():
    plugin = manifest("plugin.json")
    assert plugin["name"] == "printer-cli"
    assert plugin["version"] == __version__  # bump it with the package: setting it pins plugin users to it
    assert plugin["mcpServers"] == {"printer": {"command": "prt", "args": ["mcp"], "env": {"PRT_AGENT": "1"}}}
    assert (ROOT / plugin["icon"]).is_file()
    assert "skills" not in plugin  # the default skills/ folder at the plugin root is scanned
    assert sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")) == NAMES


def test_marketplace_lists_the_repository_as_the_plugin():
    market = manifest("marketplace.json")
    assert market["name"] == "printer-cli" and market["owner"]["name"]
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("printer-cli", "./")]


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__ == manifest("plugin.json")["version"]


# --- AGENTS.md and llms.txt ------------------------------------------------------------------------


def test_agents_md_names_the_agent_features():
    text = (ROOT / "AGENTS.md").read_text()
    for needle in ["-f json", '{"error"', "--story-file -", "prt article -", "--wait", "prt jobs wait",
                   "prt styles", "prt schema", "PRT_AGENT=1", "--yes", "max_pages_per_run", "max_pages_per_day",
                   "estimate", "prt mcp", "printer-cli[mcp]", "prt skills install", "uv run pytest"]:
        assert needle in text, needle


def test_llms_txt_follows_the_format():
    lines = (ROOT / "llms.txt").read_text().splitlines()
    assert lines[0] == "# printer-cli"
    assert next(line for line in lines[1:] if line.strip()).startswith("> ")
    links = re.findall(r"^- \[[^]]+\]\((https://[^)]+)\)", "\n".join(lines), re.M)
    assert links and all(link.startswith("https://raw.githubusercontent.com/luongnv89/printer-cli/main/")
                         for link in links)
    for link in links:  # every linked file exists in the repository
        assert (ROOT / link.split("/main/", 1)[1]).is_file(), link


@pytest.mark.parametrize("argv", [["skills", "--help"], ["skills", "list", "--help"], ["skills", "install", "--help"]])
def test_skills_help(capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 0 and "Usage" in out
