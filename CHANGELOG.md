# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `prt booth PHOTO`: photo booth that prints a photo as an A4 newspaper front page with a short news story. Two styles, `--style metro` (colour tabloid) and `--style daily` (black-and-white broadsheet); a built-in default story; your own story via `--story-file story.json` or `--headline`/`--story`/`--kicker`/`--caption`/`--byline`; `--preview FILE` to render without printing; a warning names any text cut to fit the page. Unreadable photos and bad story files are rejected before anything is sent to the printer.
- `skills/photo-booth/SKILL.md`: an agent skill that looks at the photo, writes a fitting story, previews it and prints it with `prt booth`.

## [0.1.0] - 2026-11-22

First public release of printer-cli (command: `prt`), a CLI and full-screen TUI for network printers.

### Added
- `prt status`, `prt ink` and `prt overview`: printer state, alerts, ink levels, paper trays, waste pad, firmware, uptime and SNMP page count. `status --watch N` samples continuously.
- `prt discover` (mDNS) and `prt printers list/add/remove/default` to find and save printers under short names.
- `prt maintain nozzle-check | clean | test-page | identify`. Head cleaning is refused while ink is low or the waste pad is flagged (`--force` overrides); nozzle check, cleaning and the test page ask for confirmation first (`--yes` in scripts).
- `prt print` over IPP, with JPEG conversion for images and the local CUPS queue for PDF; `prt jobs` / `prt jobs cancel`.
- `prt scan` over eSCL/AirScan, to PDF or JPEG, including `-o -` for stdout.
- `prt tui`: keyboard-driven dashboard with ink tanks in cartridge colours, Maintain, Print, Scan and Jobs tabs, and switching between saved printers.
- Drivers: generic IPP Everywhere / AirPrint, Epson (ESC/P2 remote mode on port 9100 for maintenance), and a replay driver; `-p demo` runs every command against a captured Epson XP-6000.
- Global options `--format json`, `--no-color`, `-q`, `--timeout`, `--config`, `--verbose` protocol trace; tab-separated output when piped; shell completion.
- Last known status is cached and shown, marked as cached, when the printer is asleep.
- Configuration in `~/.config/prt/config.toml`, with `PRT_CONFIG`, `PRT_PRINTER` and `PRT_TIMEOUT` overrides.
- Multi-colour (tri-colour) cartridges are drawn as striped tanks; duplicate trays that differ only in margins are merged.
- Logo, MIT license, and a `printer-cli` command alias alongside `prt`.


[0.1.0]: https://github.com/luongnv89/printer-cli/releases/tag/v0.1.0
