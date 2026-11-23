import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the user's real config, cache or home folder (skills); neutral terminal environment."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for var in ("PRT_PRINTER", "PRT_CONFIG", "PRT_TIMEOUT", "FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE",
                "COLORTERM", "COLUMNS", "LC_ALL", "LC_CTYPE", "PRT_AGENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "C.UTF-8")
    yield


@pytest.fixture
def stdin(monkeypatch):
    """Feed bytes to the command's standard input: ``stdin(b'{"headline": ...}')``."""
    def feed(data: bytes) -> None:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
    return feed


def clean_env(**extra):
    """Subprocess environment per the btop-style verify rules."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE", "COLORTERM", "COLUMNS", "LC_ALL", "LC_CTYPE")}
    env.update(TERM="xterm-256color", LANG="C.UTF-8", PYTHONIOENCODING="utf-8")
    env.update({k: str(v) for k, v in extra.items()})
    return env


PRT = [sys.executable, "-m", "prt"]


def without_estimate(text: str) -> dict:
    """A booth/article/gazette JSON line minus its cost ``estimate``, after checking the estimate's shape."""
    import json
    data = json.loads(text)
    est = data.pop("estimate")
    assert set(est) == {"pages", "colour", "ink", "ink_percent", "summary"}
    assert est["pages"] == data.get("pages", 1) and set(est["ink"]) == {"cyan", "magenta", "yellow", "black"}
    return data
