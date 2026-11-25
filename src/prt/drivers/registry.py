"""Driver registry: pick a driver from the config or by asking the printer who it is.

To add a vendor: write `drivers/<vendor>.py` subclassing IPPDriver (or Driver), then add it
to DRIVERS and, if it can be auto-detected, to MATCHERS.
"""

from __future__ import annotations

from prt.drivers.base import Driver, PrinterError
from prt.drivers.epson import EpsonDriver
from prt.drivers.ipp import IPPDriver
from prt.models import Target

DRIVERS: dict[str, type[Driver]] = {
    "ipp": IPPDriver,
    "epson": EpsonDriver,
}

# (substring of make-and-model / MFG, lowercased) → driver name
MATCHERS: list[tuple[str, str]] = [
    ("epson", "epson"),
]


def driver_names() -> list[str]:
    return ["auto", *DRIVERS, "fake"]


def detect(make_model: str) -> str:
    mm = make_model.lower()
    for needle, name in MATCHERS:
        if needle in mm:
            return name
    return "ipp"


def make_driver(target: Target, timeout: float = 5.0) -> Driver:
    if target.driver == "fake" or target.host == "demo":
        from prt.drivers.fake import FakeDriver

        return FakeDriver(target, timeout)
    if target.driver != "auto":
        cls = DRIVERS.get(target.driver)
        if cls is None:
            raise PrinterError(
                f"unknown driver '{target.driver}'", hint="choose one of: " + ", ".join(driver_names()), exit_code=2
            )
        return cls(target, timeout)
    # Auto: one IPP round-trip tells us the make; reuse the fetched attributes.
    probe = IPPDriver(target, timeout)
    attrs = probe.attributes()
    name = detect(str(attrs.get("printer-make-and-model") or attrs.get("printer-device-id") or ""))
    driver = DRIVERS[name](target, timeout, client=probe.client)
    driver._attrs_cache = attrs
    driver._fresh = True
    return driver
