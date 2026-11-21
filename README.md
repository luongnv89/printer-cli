# prt

**Your printer's dashboard, in the terminal.** Ink levels, paper, alerts, nozzle checks, head cleaning, printing and scanning for network printers, in a scriptable CLI and a full-screen TUI.

Built because the Epson XP-6000's web dashboard stopped working. prt talks to the printer directly over standard protocols (IPP, eSCL, SNMP, plus Epson's ESC/P2 remote mode for maintenance). It works with most AirPrint/Mopria printers from any brand, and you can manage several printers from one place.

```
╭─┐EPSON XP-6000 Series · office┌──────────────────────────────────────────╮
│ ● idle    firmware 20.50.NU30Q6 · up 4h02m                               │
├──────────────────────────────────────────────────────────────────────────┤
│ ink                                                                      │
│ Photo Black  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■····  92%               │
│ Cyan         ········································   1%  ✗ replace    │
│ Magenta      ········································   1%  ✗ replace    │
│ Yellow       ■■■■····································  12%  ▲ low        │
│ Black        ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■·····  89%               │
├──────────────────────────────────────────────────────────────────────────┤
│ paper   main A4 ✓   photo 4×6 ✓   disc Disc ✓   (levels not reported)    │
│ waste   ▲ almost full — service soon                                     │
╰─┘4 alerts · checked 17:42└───────────────────────────────────────────────╯
```

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:luongnv89/printer-epson-6000.git && cd printer-epson-6000
uv tool install .          # puts `prt` on your PATH
prt --install-completion   # optional: tab completion for your shell
```

## Quick start

```bash
prt discover                                        # find printers on the LAN
prt printers add office EPSON721655.local --default # save one under a short name
prt status                                          # ink, paper, alerts
prt tui                                             # full-screen dashboard
```

No printer handy? Every command works against a built-in replay of a real XP-6000: `prt -p demo status`, `prt -p demo tui`.

## Commands

| Command | What it does |
|---|---|
| `prt status [--watch N]` | State, alerts, ink, paper, waste pad. `--watch` prints one line per sample |
| `prt ink` | Ink levels only |
| `prt overview` | System, software and hardware: model, serial, firmware, uptime, page count, formats, media, scanner, capabilities |
| `prt discover` | mDNS scan for printers and scanners |
| `prt printers list/add/remove/default` | Manage saved printers |
| `prt maintain nozzle-check` | Print the nozzle check pattern |
| `prt maintain clean [--force]` | Head cleaning, with a safety check first (see below) |
| `prt maintain test-page` | Colour test page: ink bars, gradients, grey steps, fine lines |
| `prt maintain identify` | Flash the printer's panel |
| `prt print FILE… [-n 2] [--media 4x6] [--tray photo] [--quality high] [--mono] [--duplex]` | Print |
| `prt jobs [--all]` / `prt jobs cancel ID` | List or cancel jobs |
| `prt scan [-o file.pdf] [--type jpeg] [--dpi 600] [--mode gray] [--area full]` | Flatbed scan (`-o -` writes to stdout) |
| `prt tui` | Full-screen dashboard |

**Global options**, which can go before or after the command:
- `-p/--printer NAME|HOST` picks the printer.
- `-f/--format json` prints machine-readable output.
- `--no-color` turns colour off.
- `-q` hides notes and prints only results.
- `--timeout S` sets the network timeout in seconds.
- `--config PATH` uses a different config file.

### Scripting

```bash
prt ink -f json | jq -r '.inks[] | select(.state != "ok") | "\(.name): \(.level)%"'
prt status | grep '^alert'            # piped output is tab-separated, no colour
prt scan -o - --type jpeg > page.jpg
```

Exit codes: `0` ok · `1` runtime/network error (also when only cached status is shown) · `2` usage error · `3` bad input file · `130` Ctrl-C.

## The TUI

`prt tui` opens a keyboard-driven dashboard. Inks are drawn as vertical tanks in their real cartridge colours, alongside system info, paper and alerts. Tabs: **Dashboard · Maintain · Print · Scan · Jobs**.

| Key | Action |
|---|---|
| `r` | Refresh (auto-refreshes every 10 s) |
| `n` / `c` / `i` | Nozzle check / head cleaning / identify (each asks first) |
| `p` / `s` / `j` | Print, Scan, Jobs tabs |
| `1`–`9` | Switch between saved printers |
| `q` | Quit |

## Safety

- **Head cleaning is refused** while any ink is at or below its low level, or while the waste ink pad is flagged. Cleaning flushes ink into the pad, and a full pad locks the printer until it's serviced. `--force` overrides this, and the TUI offers "Clean anyway".
- Every maintenance action asks for confirmation. In scripts, pass `--yes`.
- prt never resets the waste ink counter or writes to the printer's EEPROM.

## How it talks to printers

| Feature | Protocol | Notes |
|---|---|---|
| Status, ink, info, jobs, identify | IPP (631) | Small IPP codec built in, no `ipptool` needed |
| Printing | IPP Print-Job | JPEG/PWG/URF go direct; PNG etc. are converted to JPEG; **PDF and other files go through the local CUPS queue** for the same printer, matched by UUID (the XP-6000 doesn't accept PDF) |
| Scanning | eSCL / AirScan (443) | Certificate checks are skipped for the scanner host only, because printers ship self-signed certificates |
| Page counter | SNMP v1 | Printer-MIB lifetime count |
| Nozzle check, cleaning | ESC/P2 remote mode on 9100 | Byte sequences from gutenprint's `escputil` (Epson only) |

When the printer is asleep, `status` and the TUI show the **last known status** from `~/.cache/prt/`, marked as cached.

## Configuration

`~/.config/prt/config.toml`. Override the location with `PRT_CONFIG` or `--config`. Priority order: **command-line flags > environment variables (`PRT_PRINTER`, `PRT_TIMEOUT`) > config file > defaults**.

```toml
default = "office"

[printers.office]
host = "EPSON721655.local"

[printers.pantum]
host = "192.168.1.80"
driver = "ipp"
```

## Adding a printer brand

Drivers live in `src/prt/drivers/`:
- `IPPDriver` covers any IPP Everywhere / AirPrint printer.
- `EpsonDriver` adds maintenance and counters on top of it.
- `FakeDriver` replays a captured fixture, for tests and the demo.

To add a vendor, subclass `IPPDriver`, declare its `capabilities`, override what it can do, and register it in `drivers/registry.py`. The CLI and TUI hide actions a driver doesn't support.

## Development

```bash
uv sync
uv run pytest            # 79 tests, no printer needed
uv run prt -p demo tui
```

## Troubleshooting

- **"cannot reach printer"**: the printer is probably asleep. Press any button on it, or check `prt discover`.
- **Shell completion isn't detected**: write the script out manually, e.g. `_PRT_COMPLETE=source_zsh prt > ~/.zfunc/_prt`.
- **PDF won't print**: add the printer in System Settings → Printers so a CUPS queue exists, or print a JPEG.
