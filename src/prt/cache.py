"""Last-known-status cache so a sleeping printer still shows something useful."""

from __future__ import annotations

import json
from pathlib import Path

from prt.config import cache_dir
from prt.models import Alert, Ink, Severity, Status, Tray, to_dict


def _file(name: str, base: Path | None = None) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return (base or cache_dir()) / f"status-{safe}.json"


def save(name: str, status: Status, base: Path | None = None) -> None:
    try:
        path = _file(name, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_dict(status)))
    except OSError:
        pass  # the cache is a convenience; never fail a command over it


def load(name: str, base: Path | None = None) -> Status | None:
    try:
        data = json.loads(_file(name, base).read_text())
    except (OSError, ValueError):
        return None
    try:
        return Status(
            state="offline", accepting=False,
            inks=[Ink(name=i["name"], color=i["color"], level=i["level"], low=i.get("low", 15),
                      high=i.get("high", 100), kind=i.get("kind", "ink-cartridge")) for i in data["inks"]],
            trays=[Tray(**t) for t in data.get("trays", [])],
            alerts=[Alert(a["code"], Severity(a["severity"]), a["message"]) for a in data.get("alerts", [])],
            reasons=data.get("reasons", []), waste=data.get("waste"),
            waste_level=data.get("waste_level"), queued_jobs=data.get("queued_jobs", 0),
            checked_at=data.get("checked_at", 0), stale=True,
        )
    except (KeyError, TypeError, ValueError):
        return None
