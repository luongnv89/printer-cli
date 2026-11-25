"""Collect a repository's recent commits and present them as a text gazette receipt."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from prt.drivers.base import PrinterError
from prt.layout import _clean
from prt.textgazette import Section, TextBlock, TextGazette

MAX_COMMITS = 80
MAX_SUBJECT = 180
MAX_AUTHOR = 48
GIT_TIMEOUT = 10


@dataclass(frozen=True)
class Commit:
    sha: str
    short: str
    authored_at: datetime
    author: str
    subject: str


@dataclass(frozen=True)
class Receipt:
    repository: str
    period: str
    since: datetime
    until: datetime
    commits: tuple[Commit, ...]
    total: int


def _bad(message: str, hint: str | None = None) -> PrinterError:
    return PrinterError(message, hint=hint, exit_code=3)


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run git with a fixed argument vector — never a shell — and turn environment failures into PrinterError."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as e:
        raise PrinterError(
            "git is not installed or not on PATH",
            hint="install git to print a commit receipt",
            exit_code=1,
            kind="git_unavailable",
        ) from e
    except subprocess.TimeoutExpired as e:
        raise PrinterError(f"git did not answer within {GIT_TIMEOUT} seconds", exit_code=1, kind="git_timeout") from e


def _git_ok(repo: Path, args: list[str], what: str) -> str:
    proc = _run_git(repo, args)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise PrinterError(
            f"git could not {what}: {detail[-1] if detail else 'unknown error'}", exit_code=1, kind="git_failed"
        )
    return proc.stdout


def _period_start(period: str, now: datetime) -> datetime:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if period == "day" else midnight - timedelta(days=6)


def collect(repo: Path, period: str = "day", now: datetime | None = None) -> Receipt:
    """Read the repository's commits since local midnight (day) or the last seven days (week).

    The repository is only read through fixed ``git`` argv forms; nothing from the repository is ever run.
    An empty repository, or a period without commits, is a valid empty receipt; a path outside a work tree is
    bad input (exit 3).
    """
    if period not in {"day", "week"}:
        raise _bad(f"period must be day or week, not {period!r}")
    repo = repo.expanduser()
    if not repo.is_dir():
        raise _bad(f"repository {repo} is not a directory")
    now = (now or datetime.now()).astimezone()
    since = _period_start(period, now)
    probe = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode or probe.stdout.strip() != "true":
        raise _bad(f"{repo} is not inside a git work tree", "run prt receipt in a repository, or pass one")
    try:
        root = Path(_git_ok(repo, ["rev-parse", "--show-toplevel"], "find the repository").strip()).resolve()
    except PrinterError:
        root = repo.resolve()
    head = _run_git(repo, ["rev-parse", "--verify", "HEAD"])
    if head.returncode:
        return Receipt(str(root), period, since, now, (), 0)

    since_arg = f"--since={since.isoformat()}"
    count_text = _git_ok(repo, ["rev-list", "--count", "--no-merges", since_arg, "HEAD"], "count commits").strip()
    try:
        total = int(count_text)
    except ValueError:
        total = 0
    fmt = "%H%x1f%h%x1f%aI%x1f%an%x1f%s"
    out = _git_ok(
        repo,
        ["log", "--no-merges", since_arg, f"--max-count={MAX_COMMITS}", f"--pretty=format:{fmt}"],
        "read the commit log",
    )
    commits: list[Commit] = []
    for line in out.splitlines():
        parts = line.split("\x1f", 4)
        if len(parts) != 5:
            continue
        sha, short, authored, author, subject = parts
        try:
            authored_at = datetime.fromisoformat(authored)
        except ValueError:
            continue
        author_text = _clean(author, keep_newlines=False)[:MAX_AUTHOR]
        subject_text = _clean(subject, keep_newlines=False)
        commits.append(Commit(sha, short, authored_at, author_text, subject_text or "(no subject)"))
    return Receipt(str(root), period, since, now, tuple(commits), total)


def _day_title(day) -> str:
    return day.strftime("%A, %B ") + str(day.day)


def document(receipt: Receipt) -> TextGazette:
    """Convert collected commits into the sectioned text gazette they print as."""
    authors = {commit.author for commit in receipt.commits if commit.author}
    period_name = "day" if receipt.period == "day" else "week"
    noun = "commit" if receipt.total == 1 else "commits"
    if receipt.total:
        summary = f"{receipt.total} {noun} by {len(authors) or 1} author{'s' if len(authors) != 1 else ''} in the last {period_name}."
        if receipt.total > len(receipt.commits):
            summary += f" The newest {len(receipt.commits)} are shown."
    else:
        summary = f"No commits in the last {period_name}."
    sections = [Section("At a glance", (TextBlock(summary, "summary"),), "summary")]

    days: dict[object, list[Commit]] = {}
    for commit in receipt.commits:
        days.setdefault(commit.authored_at.date(), []).append(commit)
    commit_index = 0
    for day, commits in days.items():
        blocks = []
        for commit in commits:
            author = commit.author
            if len(author) > 24:
                author = author[:23] + "…"
            label = f"{commit.authored_at:%H:%M} {commit.short} {author}: "
            blocks.append(TextBlock(commit.subject, f"commits[{commit_index}].subject", label))
            commit_index += 1
        sections.append(Section(_day_title(day), tuple(blocks), f"commits-{day.isoformat()}"))

    name = Path(receipt.repository).name or receipt.repository
    end = receipt.until.strftime("%Y-%m-%d")
    start = receipt.since.strftime("%Y-%m-%d")
    dateline = start if start == end else f"{start} — {end}"
    return TextGazette(
        masthead="The Commit Receipt",
        headline="Today in commits" if receipt.period == "day" else "The week in commits",
        kicker="Git log",
        byline=f"Repository: {name}",
        dateline=dateline,
        edition="Day edition" if receipt.period == "day" else "Week edition",
        tagline="A printed record of work committed",
        sections=tuple(sections),
    )
