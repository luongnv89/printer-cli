"""The agent skills that come with prt, and ``prt skills install``, which copies them where agents look for them.

The skills are kept once, in the repository's ``skills/`` folder: one folder per skill, holding its ``SKILL.md``.
The wheel carries a copy of that folder as ``prt/bundled_skills`` (see ``force-include`` in ``pyproject.toml``), and
a source checkout reads ``skills/`` itself, so an installed ``prt`` and one run from the repository install the same
files.

Installing never replaces a file that differs from the bundled one unless ``force`` is given, and checks every
skill before it writes any: a refused install changes nothing. Files are written to a temporary name and renamed
into place, so a symlink at the destination is replaced, never written through.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prt.drivers.base import PrinterError

BUNDLE = "bundled_skills"  # the skills folder inside the installed package
SKILL_FILE = "SKILL.md"

# Where each agent reads skills from, under the home folder (or the project folder with --project).
TARGETS = {
    "claude": Path(".claude") / "skills",   # Claude Code
    "agents": Path(".agents") / "skills",   # agents that follow the shared .agents/skills folder (e.g. Codex)
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str | None
    path: Path  # the skill's own folder

    def files(self) -> list[Path]:
        """Every file of the skill, relative to its folder, in a stable order."""
        return sorted(p.relative_to(self.path) for p in self.path.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "description": self.description,
                "files": [f.as_posix() for f in self.files()]}


@dataclass(frozen=True)
class Installed:
    skill: str
    target: str  # claude, agents or dir
    path: Path   # the skill's folder at the destination
    status: str  # installed, updated, unchanged or replaced

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.skill, "target": self.target, "path": str(self.path), "status": self.status}


def source_dir() -> Path:
    """The folder that holds the bundled skills: the wheel's copy, or ``skills/`` in a source checkout."""
    here = Path(__file__).resolve().parent
    bundled = here / BUNDLE
    if _has_skills(bundled):
        return bundled
    repo = here.parent.parent  # src/prt -> the repository root
    if (repo / "pyproject.toml").is_file() and _has_skills(repo / "skills"):
        return repo / "skills"
    raise PrinterError("the agent skills are missing from this prt install",
                       hint="reinstall printer-cli, e.g. `uv tool install --reinstall printer-cli`",
                       kind="skills_missing")


def _has_skills(folder: Path) -> bool:
    return folder.is_dir() and any(folder.glob(f"*/{SKILL_FILE}"))


def bundled() -> list[Skill]:
    """The bundled skills, by name."""
    root = source_dir()
    return [_read_skill(p.parent) for p in sorted(root.glob(f"*/{SKILL_FILE}"))]


_FIELD = re.compile(r"^(\s*)([A-Za-z_][\w-]*):\s*(.*)$")


def _read_skill(folder: Path) -> Skill:
    """Read the name, description and ``metadata.version`` from a SKILL.md front matter (a small YAML subset)."""
    text = (folder / SKILL_FILE).read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = _FIELD.match(line)
            if m:
                indent, key, value = m.groups()
                fields[("metadata." if indent else "") + key] = _scalar(value)
    return Skill(name=fields.get("name") or folder.name, description=fields.get("description", ""),
                 version=fields.get("metadata.version") or None, path=folder)


def _scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    return value.strip("'")


def select(skills: list[Skill], names: list[str]) -> list[Skill]:
    """The skills named (all of them when ``names`` is empty), refusing an unknown name."""
    if not names:
        return skills
    by_name = {s.name: s for s in skills}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise PrinterError(f"no bundled skill named {', '.join(repr(n) for n in unknown)}",
                           hint=f"bundled skills: {', '.join(by_name)} (see `prt skills list`)", exit_code=2)
    return [by_name[n] for n in dict.fromkeys(names)]


def target_dir(target: str, base: Path) -> Path:
    """The skills folder ``target`` (claude or agents) reads, under ``base`` (the home or project folder)."""
    return base / TARGETS[target]


def _status(skill: Skill, dest: Path) -> tuple[str, list[str]]:
    """What installing ``skill`` into ``dest`` would do, and the installed files that differ from the bundled ones."""
    if dest.exists() and not dest.is_dir():
        raise PrinterError(f"cannot install {skill.name}: {dest} is in the way and is not a folder",
                           hint="move it away and retry", kind="skill_exists")
    differ, missing = [], 0
    for rel in skill.files():
        target = dest / rel
        if not target.exists() and not target.is_symlink():
            missing += 1
        elif not target.is_file() or target.read_bytes() != (skill.path / rel).read_bytes():
            differ.append(rel.as_posix())
    if differ:
        return "replaced", differ
    if missing == len(skill.files()):
        return "installed", []
    return ("updated" if missing else "unchanged"), []


def install(skills: list[Skill], destinations: list[tuple[str, Path]], force: bool = False) -> list[Installed]:
    """Copy each skill into each ``(target, skills folder)`` destination.

    Every destination is checked first: when an installed file differs from the bundled one and ``force`` is
    false, nothing is written and the error names the files. Unchanged skills are left alone.
    """
    plan = []
    conflicts = []
    for target, folder in destinations:
        for skill in skills:
            dest = folder / skill.name
            status, differ = _status(skill, dest)
            plan.append((skill, target, dest, status))
            conflicts += [str(dest / rel) for rel in differ]
    if conflicts and not force:
        shown = ", ".join(conflicts[:3]) + (f" and {len(conflicts) - 3} more" if len(conflicts) > 3 else "")
        raise PrinterError(f"not replacing skill files you have changed or that came from another version: {shown}",
                           hint="nothing was installed; re-run with --force to replace them with the bundled skills",
                           kind="skill_exists")
    done = []
    for skill, target, dest, status in plan:
        if status != "unchanged":
            for rel in skill.files():
                _write(dest / rel, (skill.path / rel).read_bytes())
        done.append(Installed(skill.name, target, dest, status))
    return done


def _write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same folder, so the rename replaces the file
    (or a symlink) at ``path`` instead of writing into whatever it points at."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise PrinterError(f"cannot write {path}: {exc.strerror or exc}",
                           hint="check that the folder exists and is writable") from exc
