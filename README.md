<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/luongnv89/printer-cli/main/assets/logo/logo-white.svg"><img src="https://raw.githubusercontent.com/luongnv89/printer-cli/main/assets/logo/logo-black.svg" alt="printer-cli" width="360"></picture></p>

<p align="center"><a href="https://pypi.org/project/printer-cli/"><img src="https://img.shields.io/pypi/v/printer-cli" alt="PyPI"></a> <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"> <a href="https://github.com/luongnv89/printer-cli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a></p>

Check ink, paper and alerts, run nozzle checks and head cleaning, print and scan on network printers, from a scriptable CLI or a full-screen TUI.

## Install

```bash
uv tool install printer-cli
```

Requires Python 3.11+. Puts `prt` on your PATH (`printer-cli` works too). Other methods: [Install options](#install-options).

## Quick start

Check a printer's ink, paper and alerts. No printer handy? The built-in `demo` printer replays a real Epson XP-6000:

```bash
prt -p demo status
```

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

Next: [Use your own printer](#use-your-own-printer) · [Commands](#commands) · [The TUI](#the-tui) · [Photo booth](#photo-booth) · [Safety](#safety)

## Why printer-cli

It started because the Epson XP-6000's web dashboard stopped working. printer-cli talks to the printer directly over standard protocols (IPP, eSCL, SNMP, plus Epson's ESC/P2 remote mode for maintenance). It works with most AirPrint/Mopria printers from any brand, and you can manage several printers from one place.

## Use your own printer

```bash
prt discover                                        # find printers on the LAN
prt printers add office EPSON721655.local --default # save one under a short name
prt status                                          # ink, paper, alerts
prt tui                                             # full-screen dashboard
```

Every command also works against the built-in replay: `prt -p demo status`, `prt -p demo tui`.

## Status

| | Verified against the real XP-6000 | Covered by tests (replay of its data) |
|---|---|---|
| Ink, state, alerts, paper, firmware (IPP) | ✅ read live during development | ✅ |
| Page counter (SNMP) | ✅ | ✅ |
| Scanner capabilities (eSCL) | ✅ | ✅ |
| Scanning a page | ⏳ not yet | ✅ (mocked HTTP) |
| Printing (IPP / CUPS) | ⏳ not yet | ✅ (fake transport) |
| Nozzle check / head cleaning (ESC/P2 on 9100) | ⏳ not yet: bytes match gutenprint `escputil`, port 9100 is open | ✅ (byte-exact) |

## Commands

| Command | What it does |
|---|---|
| `prt status [--watch N]` | State, alerts, ink, paper, waste pad. `--watch` prints one line per sample |
| `prt ink` | Ink levels only |
| `prt overview` | System, software and hardware: model, serial, firmware, uptime, page count, formats, media, scanner, capabilities |
| `prt discover` | mDNS scan for printers and scanners |
| `prt printers list/add/remove/default` | Manage saved printers |
| `prt maintain nozzle-check` | Print the nozzle check pattern |
| `prt maintain clean [--force]` | Head cleaning, with a safety check first (see [Safety](#safety)) |
| `prt maintain test-page` | Colour test page: ink bars, gradients, grey steps, fine lines |
| `prt maintain identify` | Flash the printer's panel |
| `prt print FILE… [-n 2] [--media 4x6] [--tray photo] [--quality high] [--mono] [--duplex]` | Print |
| `prt booth PHOTO [--style metro\|daily] [--story-file story.json] [--preview page.jpg]` | Photo booth: print the photo as an A4 newspaper front page with a short story (see [Photo booth](#photo-booth)) |
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
prt booth me.jpg --style daily --story-file story.json --preview page.jpg   # render only, nothing printed
```

Exit codes: `0` ok · `1` runtime/network error (also when only cached status is shown) · `2` usage error · `3` bad input file · `130` Ctrl-C.

## The TUI

`prt tui` opens a keyboard-driven dashboard. Inks are drawn as vertical tanks in their real cartridge colours, alongside system info, paper and alerts. Tabs: **Dashboard · Maintain · Print · Scan · Jobs**.

| Key | Action |
|---|---|
| `r` | Refresh (auto-refreshes every 10 s) |
| `n` / `c` / `i` | Nozzle check / head cleaning (both ask first) / identify |
| `p` / `s` / `j` | Print, Scan, Jobs tabs |
| `1`–`9` | Switch between saved printers |
| `q` | Quit |

## Photo booth

`prt booth photo.jpg` prints the photo as a newspaper front page with a short news story. With no story it uses a built-in one.

- `--style metro` (default) is a colour tabloid with two columns. `--style daily` is a black-and-white broadsheet with three columns.
- Write your own story as JSON: `headline` and `story` are required, `kicker`, `caption` and `byline` are optional. Pass it with `--story-file story.json`. `--headline`, `--story`, `--kicker`, `--caption` and `--byline` override single fields.
- The page uses the font that ships with Pillow, which covers basic Latin only. Accents print without their marks, and other scripts and emoji are refused with an error.
- `--preview page.jpg` saves the page without printing.
- The length limits (headline 120, story 2500, kicker 40, caption 200, byline 80 characters) are what the command accepts, not what always fits. `metro` has the least room: about 70 characters of headline on two full-size lines, about 100 on three smaller ones, and about 100 characters of caption under a portrait photo.
- Text too long for its space is cut with `…` after the last whole word that fits, and `prt booth` warns on stderr naming the fields that were cut. Transparent parts of a PNG print as white paper.
- The photo and story are checked before anything is sent, so an unreadable image or a bad story file exits with an error and prints nothing.

To let an AI agent look at the photo and write a story that fits it, give it the [photo-booth skill](https://github.com/luongnv89/printer-cli/blob/main/skills/photo-booth/SKILL.md).

## Safety

- **Head cleaning is refused** while any ink is at or below its low level, or while the waste ink pad is flagged. Cleaning flushes ink into the pad, and a full pad locks the printer until it's serviced. `--force` overrides this, and the TUI offers "Clean anyway".
- Nozzle check, cleaning, the test page and the photo booth ask for confirmation first. In scripts, pass `--yes`.
- printer-cli never resets the waste ink counter or writes to the printer's EEPROM.

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

## Install options

With pipx:

```bash
pipx install printer-cli
```

From source (add `--editable` to track local changes):

```bash
git clone https://github.com/luongnv89/printer-cli.git && cd printer-cli
uv tool install .
```

Optional tab completion for your shell:

```bash
prt --install-completion
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
uv run pytest            # 160 tests, no printer needed
uv run prt -p demo tui
```

## Troubleshooting

- **"cannot reach printer"**: the printer is probably asleep. Press any button on it, or check `prt discover`.
- **Shell completion isn't detected**: write the script out manually, e.g. `_PRT_COMPLETE=source_zsh prt > ~/.zfunc/_prt`.
- **PDF won't print**: add the printer in System Settings → Printers so a CUPS queue exists, or print a JPEG.

## License

[MIT](https://github.com/luongnv89/printer-cli/blob/main/LICENSE) © Luong NGUYEN
