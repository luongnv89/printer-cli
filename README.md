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

Next: [Use your own printer](#use-your-own-printer) · [Commands](#commands) · [The TUI](#the-tui) · [Photo booth](#photo-booth) · [Article printing](#article-printing) · [Safety](#safety)

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
| `prt booth PHOTO [--style metro\|daily\|wizard\|wanted] [--story-file story.json] [--preview page.jpg] [--save page.pdf]` | Photo booth: print the photo as an A4 newspaper front page, wizard card or wanted poster (see [Photo booth](#photo-booth)) |
| `prt article URL\|FILE [--style magazine\|classic-magazine] [--preview page.pdf] [--save page.pdf]` | Print a blog post, news article or X post as A4 magazine pages (see [Article printing](#article-printing)) |
| `prt gazette PHOTO… [--story-file story.json] [--preview page.pdf] [--save page.pdf]` | Print one or more photos as a vintage newspaper, over as many A4 pages as the photos need (see [Vintage gazette](#vintage-gazette)) |
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
prt article https://example.com/blog/post -o article.pdf                   # every page in one PDF, nothing printed
```

Exit codes: `0` ok · `1` runtime/network error (also when only cached status is shown) · `2` usage error · `3` bad input file, an article that cannot be fetched or converted, or more photos than the gazette takes · `130` Ctrl-C.

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

`prt booth photo.jpg` prints the photo as a newspaper front page with a short news story, or as a themed card or poster. With no story each style uses its own built-in text.

- `--style metro` (default) is a colour tabloid with two columns. `--style daily` is a black-and-white broadsheet with three columns.
- `--style wizard` is an ornate trading card: a dark card with a gold double border and cut marks, the photo in an arched gold frame, a moon-and-star emblem, a name ribbon and a lore box. `--style wanted` is a weathered parchment poster: huge WANTED lettering, the photo in a heavy frame, a "dead or alive" line, the name and a reward.
- The themed styles reuse the story fields: `headline` is the name, `kicker` the card type (wizard) or the "dead or alive" line (wanted), `caption` the subtitle (wizard) or the reward line (wanted), `story` the lore or description, and `byline` the footer. Without them, `wanted` prints "The Grinning Captain" with a reward of 1,000,000 gold.
- Write your own story as JSON: `headline` and `story` are required, `kicker`, `caption` and `byline` are optional. Pass it with `--story-file story.json`. `--headline`, `--story`, `--kicker`, `--caption` and `--byline` override single fields.
- The page uses the font that ships with Pillow, which covers basic Latin only. Accents print without their marks, and other scripts and emoji are refused with an error.
- `--preview page.jpg` saves the page without printing. The file name picks the type: `.jpg`/`.jpeg` (JPEG), `.png` (lossless image) or `.pdf` (one A4 page); any other name is refused.
- `--save page.pdf` keeps a digital copy and still prints the page. Repeat it for more files, e.g. `--save page.pdf --save page.png`. Copies are written before the printer is contacted, so they are kept even if printing is declined or the printer is asleep.
- The length limits (headline 120, story 2500, kicker 40, caption 200, byline 80 characters) are what the command accepts, not what always fits. `metro` has the least room: about 70 characters of headline on two full-size lines, about 100 on three smaller ones, and about 100 characters of caption under a portrait photo.
- Text too long for its space is cut with `…` after the last whole word that fits, and `prt booth` warns on stderr naming the fields that were cut. Transparent parts of a PNG print as white paper.
- The photo and story are checked before anything is sent, so an unreadable image or a bad story file exits with an error and prints nothing.

To let an AI agent look at the photo and write a story that fits it, give it the [photo-booth skill](https://github.com/luongnv89/printer-cli/blob/main/skills/photo-booth/SKILL.md).

## Article printing

`prt article https://example.com/blog/post` prints a web article as A4 magazine pages. It keeps the title, author, site name and the full text, and leaves out navigation, ads, share bars, comments and other page clutter.

- `--style magazine` (default) is a white page with a blue accent bar and section label, a big bold headline and three ruled columns. `--style classic-magazine` is an ivory page in a double-rule frame with a centred headline between fleurons, one centred column and a drop cap.
- Long articles run on over as many pages as they need, up to 30; nothing is cut. Each page is printed as its own job, after one confirmation prompt (`--yes` skips it).
- Links must start with `http://` or `https://`. Pages are fetched with certificate checks, at most 5 redirects and 5 MiB, within the `--timeout` (default 5 s).
- X posts (`https://x.com/<user>/status/<id>`, also `twitter.com`) are read through X's public oEmbed service, which returns the text of one public post only: no images, no threads, nothing behind a login.
- For pages the command cannot read (a login wall, text built by JavaScript, a long X thread), pass a saved file instead: article JSON (`{"title": "...", "author": "...", "source": "...", "url": "...", "body": ["paragraph", "..."]}`), a saved `.html` page, or a text file whose first line is the title. The [article-print skill](https://github.com/luongnv89/printer-cli/blob/main/skills/article-print/SKILL.md) lets an AI agent fetch the article itself and write that JSON.
- `--preview page.pdf` saves the pages without printing: a `.pdf` holds every page, while `.png`, `.jpg` or `.jpeg` save one image per page (`page-1.png`, `page-2.png`, …). `--save` keeps the same copies and still prints.
- The page font covers Latin letters only: accents print without their marks, emoji are left out, and an article mostly in another script is refused.
- When the article cannot be fetched or converted, `prt article` exits with code 3 and a hint, and nothing is sent to the printer.

## Vintage gazette

`prt gazette photo.jpg` prints a photo as the front page of an old newspaper, after the look of [luongnv.com/news](https://luongnv.com/news): cream paper and one dark ink, a blackletter masthead between double rules, a small-caps dateline, a headline in capitals, the photo toned from ink to paper, and a short story in two ruled columns. With no story it prints its own.

```bash
prt gazette me.jpg --headline "Local Star Steps Out" --preview page.pdf   # one photo, one page, nothing printed
prt gazette trip-1.jpg trip-2.jpg trip-3.jpg trip-4.jpg --save trip.pdf   # four photos on two pages, printed
```

- Give several photos to print them all. The first is the lead picture, and the front page also holds the next two as plates. Each further page holds up to six more in a ruled grid, numbered Fig. 4, Fig. 5 and so on. No photo is left out: 4 photos make 2 pages, 10 make 3.
- One run takes at most 57 photos (10 pages). More are refused with exit code 3 before any photo is read, written or printed, so print them in batches.
- The story works as in `prt booth`: `--story-file story.json` (`headline` and `story` required; `kicker`, `caption` and `byline` optional), with `--headline`, `--story`, `--kicker`, `--caption` and `--byline` to override single fields. The caption goes under the lead photo. A story too long for the front page is set in smaller type, then cut with `…`, and the command warns.
- `--preview`/`-o` and `--save` work as in `prt article`: a `.pdf` holds every page, while `.png`, `.jpg` or `.jpeg` save one image per page (`page-1.png`, `page-2.png`, …). A file name that would overwrite one of the photos is refused with exit code 2.
- Every photo is read before anything is written or sent, so an unreadable or unsupported photo exits with code 3, names the file and prints nothing. Each page prints as its own job after one confirmation (`--yes` skips it). If a later page fails, the jobs already queued are named so you can cancel them with `prt jobs cancel`.
- The type is UnifrakturMaguntia, Playfair Display and Old Standard TT, which ship with printer-cli (see [License](#license)). Letters these fonts lack print without their accents, and other scripts and emoji are refused as in the photo booth.

## Safety

- **Head cleaning is refused** while any ink is at or below its low level, or while the waste ink pad is flagged. Cleaning flushes ink into the pad, and a full pad locks the printer until it's serviced. `--force` overrides this, and the TUI offers "Clean anyway".
- Nozzle check, cleaning, the test page, the photo booth, article printing and the gazette ask for confirmation first. In scripts, pass `--yes`.
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
uv run pytest            # 215 tests, no printer needed
uv run prt -p demo tui
```

## Troubleshooting

- **"cannot reach printer"**: the printer is probably asleep. Press any button on it, or check `prt discover`.
- **Shell completion isn't detected**: write the script out manually, e.g. `_PRT_COMPLETE=source_zsh prt > ~/.zfunc/_prt`.
- **PDF won't print**: add the printer in System Settings → Printers so a CUPS queue exists, or print a JPEG.

## License

[MIT](https://github.com/luongnv89/printer-cli/blob/main/LICENSE) © Luong NGUYEN

The fonts used by `prt gazette`, [UnifrakturMaguntia](https://github.com/google/fonts/tree/main/ofl/unifrakturmaguntia), [Playfair Display](https://github.com/google/fonts/tree/main/ofl/playfairdisplay) and [Old Standard TT](https://github.com/google/fonts/tree/main/ofl/oldstandardtt), are under the SIL Open Font License 1.1. Their licence texts ship with them in [`src/prt/fonts`](https://github.com/luongnv89/printer-cli/tree/main/src/prt/fonts) as `OFL-*.txt`.
