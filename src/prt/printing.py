"""Printing strategy.

Many printers (the Epson XP-6000 included) accept only JPEG / PWG-raster / URF over IPP —
not PDF. So:
  1. JPEG / PWG / URF  → sent directly with IPP Print-Job.
  2. Other images      → converted to JPEG with Pillow, then sent directly.
  3. Everything else   → handed to the local CUPS queue for the same printer (matched by UUID
                         or host), which converts PDF/text/office output for us.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

from prt.drivers.base import PrinterError
from prt.models import PrintOptions

IMAGE_EXT = {".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}


def find_cups_queue(uuid: str | None, host: str) -> str | None:
    """Return the CUPS queue whose device URI points at this printer."""
    if not shutil.which("lpstat"):
        return None
    try:
        out = subprocess.run(["lpstat", "-v"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return match_queue(out, uuid, host)


def match_queue(lpstat_v: str, uuid: str | None, host: str) -> str | None:
    host_l = host.lower().removesuffix(".local")
    for line in lpstat_v.splitlines():
        m = re.match(r"device for (\S+?):\s+(\S+)", line)
        if not m:
            continue
        queue, uri = m.group(1), m.group(2).lower()
        if uuid and uuid.lower() in uri:
            return queue
        if host_l and host_l in uri:
            return queue
    return None


def cups_print(queue: str, path: Path, options: PrintOptions) -> int:
    cmd = ["lp", "-d", queue, "-n", str(max(1, options.copies)), "-t", options.job_name or path.name]
    if options.media:
        cmd += ["-o", f"media={options.media}"]
    if options.mono:
        cmd += ["-o", "print-color-mode=monochrome"]
    if options.quality:
        cmd += ["-o", f"print-quality={ {'draft': 3, 'normal': 4, 'high': 5}[options.quality] }"]
    if options.duplex:
        cmd += ["-o", "sides=two-sided-long-edge"]
    if options.tray:
        cmd += ["-o", f"media-source={options.tray}"]
    cmd.append(str(path))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise PrinterError(f"CUPS refused the job: {res.stderr.strip() or res.stdout.strip()}")
    m = re.search(r"request id is \S+-(\d+)", res.stdout)
    return int(m.group(1)) if m else 0


def print_file(driver, path: Path, options: PrintOptions) -> int:
    from prt.drivers.ipp import DIRECT_FORMATS

    if not path.is_file():
        raise PrinterError(f"file not found: {path}", hint="check the path", exit_code=3)
    options.job_name = options.job_name or path.name
    ext = path.suffix.lower()
    if ext in DIRECT_FORMATS:
        return driver.submit(path.read_bytes(), DIRECT_FORMATS[ext], options)
    if ext in IMAGE_EXT:
        try:
            from PIL import Image
            with Image.open(path) as img:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=95)
        except Exception as exc:
            raise PrinterError(f"cannot read image {path.name}: {exc}", exit_code=3) from exc
        return driver.submit(buf.getvalue(), "image/jpeg", options)
    if options.direct:
        raise PrinterError(f"{ext or 'this file type'} cannot be sent directly — the printer only "
                           "accepts JPEG, PWG-raster and URF", hint="drop --direct to print via CUPS",
                           exit_code=3)
    info_uuid = None
    try:
        info_uuid = driver.info().uuid
    except PrinterError:
        pass
    queue = find_cups_queue(info_uuid, driver.target.host)
    if not queue:
        raise PrinterError(
            f"the printer cannot take {ext or 'this file'} directly and no CUPS queue was found for it",
            hint="add the printer in System Settings → Printers (or `lpadmin`), or convert to JPEG",
            exit_code=1)
    return cups_print(queue, path, options)
