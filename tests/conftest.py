import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the user's real config/cache; neutral terminal environment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for var in ("PRT_PRINTER", "PRT_CONFIG", "PRT_TIMEOUT", "FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE",
                "COLORTERM", "COLUMNS", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "C.UTF-8")
    yield


def clean_env(**extra):
    """Subprocess environment per the btop-style verify rules."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE", "COLORTERM", "COLUMNS", "LC_ALL", "LC_CTYPE")}
    env.update(TERM="xterm-256color", LANG="C.UTF-8", PYTHONIOENCODING="utf-8")
    env.update({k: str(v) for k, v in extra.items()})
    return env


PRT = [sys.executable, "-m", "prt"]
