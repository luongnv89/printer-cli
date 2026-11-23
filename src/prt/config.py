"""Configuration: ~/.config/prt/config.toml, env overrides, and printer resolution.

Precedence: CLI flags > environment (PRT_PRINTER, PRT_CONFIG, PRT_TIMEOUT) > config file > defaults.

``max_pages_per_run`` and ``max_pages_per_day`` cap how many pages a print may use; ``prt.guard`` enforces them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from prt.drivers.base import PrinterError
from prt.models import Target

DEFAULT_TIMEOUT = 5.0


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("PRT_CONFIG"):
        return Path(os.environ["PRT_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "prt" / "config.toml"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "prt"


@dataclass
class Config:
    path: Path
    default: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    printers: dict[str, Target] = field(default_factory=dict)
    max_pages_per_run: int | None = None
    max_pages_per_day: int | None = None

    @classmethod
    def load(cls, explicit: str | Path | None = None) -> "Config":
        path = config_path(explicit)
        cfg = cls(path=path)
        if path.exists():
            try:
                data = tomllib.loads(path.read_text())
            except tomllib.TOMLDecodeError as exc:
                raise PrinterError(f"invalid config file {path}: {exc}",
                                   hint="fix the TOML syntax or delete the file", exit_code=3) from exc
            cfg.default = data.get("default")
            cfg.timeout = float(data.get("timeout", DEFAULT_TIMEOUT))
            cfg.max_pages_per_run = _page_cap(data, "max_pages_per_run", path)
            cfg.max_pages_per_day = _page_cap(data, "max_pages_per_day", path)
            known = {f.name for f in fields(Target)}
            for name, entry in (data.get("printers") or {}).items():
                if not isinstance(entry, dict) or "host" not in entry:
                    raise PrinterError(f"printer '{name}' in {path} has no host", exit_code=3)
                cfg.printers[name] = Target(name=name, **{k: v for k, v in entry.items()
                                                          if k in known and k != "name"})
        if os.environ.get("PRT_TIMEOUT"):
            try:
                cfg.timeout = float(os.environ["PRT_TIMEOUT"])
            except ValueError:
                raise PrinterError("PRT_TIMEOUT must be a number of seconds", exit_code=2)
        return cfg

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# prt configuration — edit freely or use `prt printers add/remove/default`"]
        if self.default:
            lines.append(f"default = {_q(self.default)}")
        if self.timeout != DEFAULT_TIMEOUT:
            lines.append(f"timeout = {self.timeout}")
        for key in ("max_pages_per_run", "max_pages_per_day"):
            if getattr(self, key) is not None:
                lines.append(f"{key} = {getattr(self, key)}")
        defaults = asdict(Target(name="", host=""))
        for name, t in self.printers.items():
            lines.append("")
            lines.append(f"[printers.{_key(name)}]")
            for k, v in asdict(t).items():
                if k == "name" or (k != "host" and v == defaults[k]):
                    continue
                lines.append(f"{k} = {_toml_value(v)}")
        self.path.write_text("\n".join(lines) + "\n")

    def resolve(self, selector: str | None = None) -> Target:
        """Find the printer to talk to. `selector` is a saved name or a host/IP."""
        selector = selector or os.environ.get("PRT_PRINTER") or self.default
        if selector:
            if selector in self.printers:
                return self.printers[selector]
            if selector == "demo":
                return Target(name="demo", host="demo", driver="fake")
            if _looks_like_host(selector):
                return Target(name=selector, host=selector)
            raise PrinterError(f"no saved printer named '{selector}'",
                               hint="run `prt printers list`, or pass a hostname/IP with --printer",
                               exit_code=2)
        if len(self.printers) == 1:
            return next(iter(self.printers.values()))
        from prt.discovery import discover
        found = [p for p in discover(timeout=2.0) if p.ipp]
        if len(found) == 1:
            return found[0].to_target()
        if not found:
            raise PrinterError("no printer configured and none found on the network",
                               hint="wake the printer and run `prt discover`, or `prt printers add NAME HOST`",
                               exit_code=2)
        raise PrinterError(f"{len(found)} printers found; which one?",
                           hint="pick one with --printer, or set a default: `prt printers default NAME`",
                           exit_code=2)


def _page_cap(data: dict, key: str, path: Path) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PrinterError(f"{key} in {path} must be a whole number of pages, 1 or more",
                           hint=f"e.g. {key} = 20, or remove the line for no limit", exit_code=3)
    return value


def _looks_like_host(s: str) -> bool:
    return "." in s or ":" in s or s.endswith(".local") or s == "localhost"


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _key(s: str) -> str:
    return s if all(c.isalnum() or c in "-_" for c in s) else _q(s)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return _q(str(v))
