"""The commit receipt: safe git-log collection, rendering and CLI (#42)."""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from prt import receipt
from prt.cli import main
from prt.drivers.base import PrinterError


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def git(repo: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Ada Example")
    git(repo, "config", "user.email", "ada@example.com")
    return repo


def commit(repo: Path, name: str, subject: str, when: datetime) -> None:
    (repo / name).write_text(subject)
    git(repo, "add", name)
    stamp = when.isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    git(repo, "commit", "-m", subject, env=env)


def test_collect_reads_the_day_log_with_fixed_git_argv(tmp_path):
    repo = make_repo(tmp_path)
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    commit(repo, "a.txt", "Add the morning edition", now.replace(hour=9))
    commit(repo, "b.txt", "Fix the receipt layout", now.replace(hour=11))
    data = receipt.collect(repo, "day", now)
    assert data.total == 2 and [c.subject for c in data.commits] == [
        "Fix the receipt layout",
        "Add the morning edition",
    ]
    assert data.commits[0].author == "Ada Example" and len(data.commits[0].sha) == 40


def test_week_includes_older_commits_but_day_does_not(tmp_path):
    repo = make_repo(tmp_path)
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    commit(repo, "old.txt", "Old work", now - timedelta(days=3))
    commit(repo, "new.txt", "New work", now)
    assert receipt.collect(repo, "day", now).total == 1
    assert receipt.collect(repo, "week", now).total == 2


def test_document_groups_commits_by_day(tmp_path):
    repo = make_repo(tmp_path)
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    commit(repo, "a.txt", "Add tests", now)
    data = receipt.collect(repo, "day", now)
    doc = receipt.document(data)
    assert doc.masthead == "The Commit Receipt" and doc.sections[0].key == "summary"
    assert any("Add tests" == block.text for section in doc.sections for block in section.blocks)
    assert any(block.key == "commits[0].subject" for section in doc.sections for block in section.blocks)


def test_empty_repository_is_an_empty_receipt(tmp_path):
    repo = make_repo(tmp_path)
    data = receipt.collect(repo, "day")
    assert data.total == 0 and data.commits == ()
    assert "No commits" in receipt.document(data).sections[0].blocks[0].text


def test_outside_a_work_tree_is_bad_input(tmp_path):
    with pytest.raises(PrinterError) as exc:
        receipt.collect(tmp_path)
    assert exc.value.exit_code == 3 and "not inside a git work tree" in str(exc.value)


def test_git_is_run_without_a_shell(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    calls = []
    real = subprocess.run

    def record(cmd, **kwargs):
        calls.append(cmd)
        return real(cmd, **kwargs)

    monkeypatch.setattr(receipt.subprocess, "run", record)
    receipt.collect(repo)
    assert calls and all(isinstance(cmd, list) and cmd[:2] == ["git", "-C"] for cmd in calls)


def test_receipt_preview_json_and_file(capsys, tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "a.txt", "Add the receipt command", datetime.now().astimezone())
    out_file = tmp_path / "receipt.png"
    code, out, err = run(capsys, "receipt", str(repo), "--period", "day", "-f", "json", "-o", str(out_file))
    assert code == 0, err
    report = json.loads(out)
    assert report["action"] == "receipt" and report["period"] == "day" and report["commits"] == 1
    assert report["estimate"]["pages"] == 1 and out_file.is_file() and out_file.stat().st_size > 10_000


def test_receipt_rejects_an_output_type_before_reading_git(capsys, tmp_path, monkeypatch):
    called = False

    def collect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("git ran before output validation")

    monkeypatch.setattr(receipt, "collect", collect)
    code, _, err = run(capsys, "receipt", str(tmp_path), "-o", str(tmp_path / "receipt.gif"))
    assert code == 2 and "unknown file type .gif" in err and not called
