# AGENTS.md

printer-cli (`prt`) is a CLI and TUI for network printers: status, ink, maintenance, printing and scanning over IPP, eSCL, SNMP and Epson ESC/P2. It can also print a photo as a newspaper front page, card or poster (`prt booth`), a web article as magazine pages (`prt article`), photos as a vintage newspaper (`prt gazette`) and any page described as blocks of headings, text, images, QR codes, columns and rules (`prt compose`). This file is for AI agents. The first part covers using `prt`, the second covers working on this repository. The [README](README.md) is the full user guide.

## Using prt

### Install

```bash
uv tool install printer-cli            # or: pipx install printer-cli
uv tool install 'printer-cli[mcp]'     # adds the MCP SDK that `prt mcp` needs
```

Add `-p demo` to any command to use the built-in demo printer, which needs no hardware. `-p NAME|HOST` (or `PRT_PRINTER`) picks a real printer, and `prt discover` finds and saves the printers on the network.

### Read the output as data

- Add `-f json` anywhere on the command line (`prt ink -f json` is the same as `prt -f json ink`). Results go to stdout as JSON, and notes go to stderr. `-q` hides the notes.
- With `-f json`, a failing command writes one JSON object as the last line of stderr: `{"error": {"code": 3, "kind": "fetch_failed", "message": "…", "hint": "…"}}`. `code` is the exit code, `kind` is a stable name to branch on, and `hint` may be `null`. The README lists every kind under *Scripting*.
- Exit codes: `0` ok, `1` runtime or printer error, `2` usage error, `3` bad input (file, link or story), `130` interrupted.

### Find out what can be printed

- `prt styles -f json` lists every booth, article and gazette style: the command and `args` that pick it, its fields with `required` and `max_length`, how many photos it takes, `max_pages`, its input `schema` and a `sample` image.
- `prt schema story`, `prt schema article`, `prt schema gazette` and `prt schema page` print the JSON Schema (draft 2020-12) of the booth story file, the article JSON file, the gazette story file and the `prt compose` page file. A file that passes the schema is accepted, except for text in a script the page font cannot print, such as emoji or Chinese (and, for a page file, image files that cannot be read or a page longer than 10 pages).

### Make a page no other command makes

`prt compose page.json` prints a page described as JSON blocks, laid out top to bottom on A4 and run on to more pages as needed (up to 10):

```json
{"title": "Wi-Fi", "blocks": [
  {"type": "heading", "text": "Guest Wi-Fi", "align": "center"},
  {"type": "qr", "data": "WIFI:S:home;T:WPA;P:secret;;", "size": 60, "caption": "Scan to join"},
  {"type": "rule"},
  {"type": "columns", "columns": [[{"type": "text", "text": "Network: home"}], [{"type": "text", "text": "Password: secret"}]]}
]}
```

- Block types: `heading` (`text`, `level` 1-3), `text` (`text`, `size` small/normal/large), `image` (`path`, `height` in mm, `caption`), `qr` (`data`, `size` in mm, `caption`), `columns` (2-4 lists of blocks) and `rule` (`weight` thin/thick). Most take `align` (left, center, right). `prt schema page` has every field and limit.
- An invalid page file is refused before anything is drawn, written or printed (exit `3`, kind `invalid_input`); the message says where, e.g. `blocks[2].level must be one of 1, 2, 3, not 4`.
- `image` paths are local files only; a relative path is read from the page file's folder. Web links are refused, nothing is downloaded.
- `prt compose -` reads the page from stdin (images relative to the current folder). `--preview`, `--save`, `--yes` and the `estimate` work as for the other page commands.

### Pass text without writing a file

- `prt booth PHOTO --story-file -` and `prt gazette PHOTO… --story-file -` read the story JSON from stdin.
- `prt article -` reads the article from stdin: JSON when it starts with `{`, HTML when it starts with `<`, plain text otherwise.
- Bad input on stdin fails like a bad file (exit `3`), and nothing is printed.

### Preview, then print

1. Render without printing: `prt booth photo.jpg --story-file story.json --preview page.jpg -f json` (or `prt compose page.json --preview page.pdf -f json`). `--preview` (also `-o`) writes `.pdf`, `.png`, `.jpg` or `.jpeg` and sends nothing to the printer.
2. Check the fit. For `booth` and `gazette`, the JSON line has `"fits"` and `"cut"`. When text was cut with `…`, `cut` gives each field's `max_fit` (characters printed) and `given`. Shorten each field to `max_fit` and preview again. `"fits": true, "cut": {}` means everything fitted. Article text is never cut, because it runs on over more pages.
3. Check the cost. With `-f json`, `booth`, `article`, `gazette` and `compose` add an `estimate`, e.g. `{"pages": 3, "colour": true, "ink": {…}, "ink_percent": 3.8, "summary": "~3 pages, colour, est. ink 4%"}`.
4. Print with `--yes`. Without a terminal, a command that would ask for confirmation fails with kind `confirmation_required` instead.

### Guardrails

- Run agents with `PRT_AGENT=1`. Every command that prints or uses ink (`print`, `booth`, `article`, `gazette`, `compose`, `maintain test-page`, `maintain nozzle-check`, `maintain clean`) then refuses to run without `--yes`, even at a terminal (exit `2`, kind `agent_confirmation_required`). `--preview` never needs `--yes`.
- Page caps: `max_pages_per_run` and `max_pages_per_day` at the top of `~/.config/prt/config.toml`. In agent mode the per-run cap defaults to 10. A print over a cap is refused whole before anything is sent (exit `1`, kind `page_cap_exceeded` or `daily_page_cap_exceeded`). Tell the user instead of retrying, and never raise a cap yourself.
- Head cleaning is refused while ink is low or the waste pad is nearly full. Do not pass `--force` unless the user asks for it.

### Follow a print job

- `prt print FILE --wait` returns once each job has finished. The JSON gives each `{"file", "job_id", "state"}`, and a job that was aborted or cancelled exits `1` (kind `job_failed` or `job_canceled`).
- `prt jobs wait ID` waits for one job. `--wait-timeout SECONDS` sets how long to wait.
- `--wait` follows only images sent straight to the printer. PDFs go through CUPS, and `--wait` refuses them.

### MCP server

`prt mcp` runs printer-cli as an MCP server over stdio. It needs the `printer-cli[mcp]` extra, and without it the command exits `2` with kind `missing_dependency`. Client config:

```json
{"mcpServers": {"printer": {"command": "prt", "args": ["-p", "demo", "mcp"]}}}
```

The tools are `status`, `ink`, `list_styles`, `preview_booth`, `preview_article` and `print_file`. A preview prints nothing. It returns the rendered page images, the fit report, the `estimate` and a single-use `token` that is valid for 30 minutes. `print_file` prints only with that token or with `confirm: true`. The page caps and agent mode apply to it too.

### Skills and the Claude Code plugin

Two agent skills come with prt: `photo-booth` (write a story for a photo and print it with `prt booth`) and `article-print` (print an article, fetching the text yourself when `prt article` cannot). They live in [`skills/`](skills).

```bash
prt skills list                     # the bundled skills
prt skills install --claude         # into ~/.claude/skills (the default)
prt skills install --agents         # into ~/.agents/skills, for Codex and other agents
prt skills install --project        # into ./.claude/skills of the current folder (with --agents: ./.agents/skills)
prt skills install --dir PATH       # into any skills folder
```

`prt skills install` never replaces an installed file that differs from the bundled one. It refuses with kind `skill_exists`, writes nothing, and `--force` replaces the file. Reinstalling unchanged skills does nothing.

In Claude Code the repository is also a plugin, with both skills and the `prt mcp` server:

```text
/plugin marketplace add luongnv89/printer-cli
/plugin install printer-cli@printer-cli
```

The plugin runs `prt mcp` with `PRT_AGENT=1` and needs `printer-cli[mcp]` on the PATH.

## Working on this repository

### Setup and tests

```bash
uv sync                    # Python 3.11+, installs the dev group
uv run pytest              # the whole suite; no printer or network needed
uv run prt -p demo status  # run the CLI from the checkout
```

The tests use the demo printer (`FakeDriver` replaying `src/prt/fixtures/epson-xp6000.json`) and a temporary config, cache and home folder (`tests/conftest.py`). A test must never touch the real `~/.config/prt`, `~/.cache/prt`, `~/.claude` or `~/.agents`.

### Layout

| Path | What it holds |
|---|---|
| `src/prt/cli.py` | Every command (Typer), the `-f json` output and the JSON error object |
| `src/prt/drivers/` | The driver contract (`base.py`, with `PrinterError` and its `kind`), the IPP, Epson and fake drivers, and the registry |
| `src/prt/transport/` | The IPP codec, the eSCL scanner client and the SNMP client |
| `src/prt/booth.py`, `article.py`, `vintage.py`, `layout.py`, `render.py` | Page layout for booth, article and gazette, and the render steps the CLI and the MCP server share |
| `src/prt/compose.py` | `prt compose`: the page file's schema, the checker that validates page files against that same schema, image loading and the block layout |
| `src/prt/catalog.py` | The styles and JSON Schemas behind `prt styles` and `prt schema` |
| `src/prt/guard.py` | Agent mode, the page caps, the daily page ledger and the ink estimate |
| `src/prt/mcp_server.py` | `prt mcp`. `Tools` holds the tools without the SDK |
| `src/prt/skills.py` | `prt skills list` and `prt skills install` |
| `src/prt/tui/` | The Textual dashboard (`prt tui`) |
| `skills/` | The agent skills. The wheel carries a copy as `prt/bundled_skills` (`force-include` in `pyproject.toml`) |
| `.claude-plugin/` | The Claude Code plugin manifest and the marketplace that lists it |

### Conventions

- Every user-visible change gets an entry under `## [Unreleased]` in `CHANGELOG.md`, and the README section that describes the behaviour is updated with it.
- Errors are `PrinterError(message, hint, exit_code, kind)`. A new failure that a script could branch on gets its own `kind`, added to the kinds table in the README.
- Every command that prints goes through `guard.check` / `guard.reserve` (see `_authorize` in `cli.py`).
- A change to `prt booth`, `prt article` or their flags is also a change to the matching skill in `skills/`. Keep the skill's examples true.
- `version` in `.claude-plugin/plugin.json` must match `pyproject.toml` (a test checks this), because the plugin version pins plugin users to it.
- Commit messages follow Conventional Commits: `feat(scope): description (#issue)`.
