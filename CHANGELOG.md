# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `prt booth --story-file -` and `prt gazette --story-file -` read the story JSON from stdin, and `prt article -` reads the article from stdin (JSON when it starts with `{`, HTML when it starts with `<`, else plain text), so an agent can pipe its text in without writing a temporary file. The same size limits (64 KiB for a story, 5 MiB for an article) and checks apply as for a file: bad input on stdin exits with code 3 and nothing is printed.
- With `-f json`, errors are printed as a JSON object on stderr, `{"error": {"code": 3, "kind": "fetch_failed", "message": "…", "hint": "…"}}`, where `code` is the exit code and `kind` a stable name scripts and agents can branch on (`unreachable`, `usage`, `fetch_failed`, `job_failed`, …; see the README). Usage errors, Ctrl-C and a status shown from the cache get one too. Without `-f json` error output is unchanged. In code, `PrinterError` takes a `kind`, which defaults from its class or exit code.
- With `-f json`, `prt booth` and `prt gazette` report text that did not fit: `"fits"` is false when a field was cut with `…`, and `"cut"` gives each cut field, in page order, with how many of its characters were printed and how many were given, e.g. `"cut": {"headline": {"max_fit": 68, "given": 92}}`, so an agent can shorten the text to fit and try again. When nothing is cut the report is `"fits": true, "cut": {}`. The stderr warning is unchanged. In code, `booth.compose()` and `vintage.compose_pages()` return the cut fields as a dict of `layout.Cut` instead of a list of names.
- `prt print --wait` and `prt jobs wait ID`: wait until a job has finished and report its final state; a job that was cancelled or aborted exits with code 1. `--wait-timeout` sets how long to wait. `--wait` refuses files that go through CUPS, whose job ids the printer does not know. The demo printer's jobs now move from pending to processing to completed.

## [0.2.0] - 2026-11-23

### Added
- `prt booth PHOTO`: photo booth that prints a photo as an A4 newspaper front page with a short news story. Two styles, `--style metro` (colour tabloid) and `--style daily` (black-and-white broadsheet); a built-in default story; your own story via `--story-file story.json` or `--headline`/`--story`/`--kicker`/`--caption`/`--byline`; `--preview FILE` to render without printing; a warning names any text cut to fit the page. Unreadable photos and bad story files are rejected before anything is sent to the printer.
- `prt booth --style wizard` (an ornate trading card with an arched gold portrait frame, emblem, name ribbon and lore box) and `--style wanted` (a weathered parchment WANTED poster with a framed photo, name and reward line). Both reuse the story fields (headline as the name, kicker as the top label or "dead or alive" line, caption as the subtitle or reward, story as the lore, byline as the footer) and print their own built-in text when no story is given.
- `skills/photo-booth/SKILL.md`: an agent skill that looks at the photo, writes a fitting story, previews it and prints it with `prt booth`.
- `prt booth --save FILE`: keep a digital copy of the page as a PDF (one A4 page), PNG or JPEG and still print it; repeat it for several files. Copies are written before the printer is contacted, so they need no reachable printer and survive a declined prompt; `-f json` adds a `saved` list. `--preview`/`-o` can also write PDF and PNG.
- `prt article URL|FILE`: print a blog post, news article or X post as A4 magazine pages. `--style magazine` (accent bar, bold headline, three columns) or `--style classic-magazine` (ivory page, centred single column, drop cap, fleurons). Keeps the title, author, site and full text over as many pages as it needs (up to 30), and leaves out navigation, ads and other clutter. X posts are read through X's public oEmbed service; saved article JSON, HTML or text files also work. `--preview`/`--save` write a multi-page PDF or one image per page. An article that cannot be fetched or converted exits with code 3 and nothing is printed.
- `skills/article-print/SKILL.md`: an agent skill that tries `prt article` on the link and, when the page needs a login or JavaScript, reads the article itself and prints it from a JSON file.
- `prt gazette PHOTO...`: print one or more photos as a vintage newspaper after luongnv.com/news: cream paper and one ink, a blackletter masthead, a small-caps dateline, a Playfair Display headline and Old Standard TT text in ruled columns, with the photos toned from ink to paper. The front page holds the story, the lead photo and two plates, and each further page six more plates, so no photo is left out. Up to 57 photos (10 pages); more are refused with exit code 3. Takes the booth story flags and `--story-file`, and `--preview`/`--save` like `prt article`. Unreadable photos and output names that would overwrite a photo are refused before anything is written or sent, and when a page fails after others were queued, their job ids are named. `--landscape` lays the pages out on A4 on its side (the lead photo beside the story, plates three abreast); saved pages stay landscape, and each page is turned upright only for the printer.
- `prt list`: the saved printers and the connection status of each one, the same as `prt printers list`.
- Bundled fonts for the gazette: UnifrakturMaguntia, Playfair Display and Old Standard TT, under the SIL Open Font License 1.1, in `src/prt/fonts` with their licence texts.

### Changed
- `prt discover` now saves each printer it finds under a short name (for example `xp-6000`), so the separate `prt printers add` step is no longer needed. Printers already saved, matched by host name or IP address, are left as they are; a name already in use gets a `-2`, `-3` suffix; scanner-only devices are not saved; the default printer is never set or changed. The config file is rewritten only when a printer was added. The output names the saved printers: an "added" line in the box, two extra fields (saved name, `added`) at the end of each tab-separated line, and `saved_as`/`added` keys on each `-f json` item.
- `prt booth --preview`/`-o` picks the file type from the extension (`.pdf`, `.png`, `.jpg`, `.jpeg`, any case). A file name without one of these extensions is now refused with exit code 2 before anything is written, instead of being saved as JPEG.
- `prt printers list` checks each printer's connection by default and shows it as online or offline; `--no-check` skips the check (it was opt-in with `--check`). The printers are checked in parallel, each within `--timeout` when given, else 3 seconds or the configured timeout if lower. A printer that cannot be reached, or fails in any other way, is still listed and marked offline. The `-f json` shape is unchanged: `reachable` is `online`/`offline`, or `null` with `--no-check`.
- The package licence is now `MIT AND OFL-1.1`, and the fonts' OFL texts are listed as licence files.

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


[0.2.0]: https://github.com/luongnv89/printer-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/luongnv89/printer-cli/releases/tag/v0.1.0
