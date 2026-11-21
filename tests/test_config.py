import pytest

from prt.config import Config
from prt.drivers.base import PrinterError
from prt.models import Target


def test_save_load_round_trip(tmp_path):
    cfg = Config(path=tmp_path / "c.toml")
    cfg.printers["office"] = Target("office", "EPSON721655.local", driver="epson")
    cfg.printers["my pantum"] = Target("my pantum", "10.0.0.9", ipp_tls=True)
    cfg.default = "office"
    cfg.save()
    back = Config.load(tmp_path / "c.toml")
    assert back.default == "office"
    assert back.printers["office"].driver == "epson"
    assert back.printers["my pantum"].ipp_tls is True


def test_precedence(tmp_path, monkeypatch):
    cfg = Config(path=tmp_path / "c.toml")
    cfg.printers = {"a": Target("a", "a.local"), "b": Target("b", "b.local")}
    cfg.default = "a"
    assert cfg.resolve().name == "a"
    monkeypatch.setenv("PRT_PRINTER", "b")
    assert cfg.resolve().name == "b"
    assert cfg.resolve("a").name == "a"  # CLI beats env
    assert cfg.resolve("192.168.1.70").host == "192.168.1.70"
    assert cfg.resolve("demo").driver == "fake"
    with pytest.raises(PrinterError):
        cfg.resolve("unknown-name")


def test_single_saved_printer_is_implicit_default(tmp_path):
    cfg = Config(path=tmp_path / "c.toml", printers={"only": Target("only", "o.local")})
    assert cfg.resolve().name == "only"


def test_bad_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("default = [unclosed")
    with pytest.raises(PrinterError) as exc:
        Config.load(p)
    assert exc.value.exit_code == 3


def test_timeout_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRT_TIMEOUT", "9")
    assert Config.load(tmp_path / "none.toml").timeout == 9.0
