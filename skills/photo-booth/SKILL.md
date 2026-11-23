---
name: photo-booth
description: "Turn a photo of a person into a printed newspaper front page, wizard trading card or wanted poster with short text written for that photo, using printer-cli's `prt booth`. Use when asked for a photo-booth print, a newspaper clipping, a front page, a themed card or a wanted poster made from someone's picture. Don't use for plain photo printing (use `prt print`), editing the photo itself, or real news reporting."
license: MIT
metadata:
  version: 0.2.0
  author: "Luong NGUYEN"
---

# Photo booth

Write a short, light-hearted news story (or card lore, or a wanted notice) that fits the photo you were given, then print it with `prt booth`, which lays out the photo and text as an A4 newspaper front page, a wizard trading card or a wanted poster. The command needs printer-cli installed (`prt` on the PATH). Add `-p NAME` to pick a saved printer, or `-p demo` to try everything without a printer.

## Workflow

### 1. Look at the photo

Open the image and note only what is visible: the setting, clothing, expression, pose, what the person is doing, and any props or animals.

- Do not name or identify the person, and do not guess age, ethnicity, health, religion or other personal traits.
- Keep it kind and playful. The page is an obvious fun news item, so no claims that could embarrass or harm a real person.

### 2. Pick a style

| `--style` | Look | Full-size body text up to |
|---|---|---|
| `metro` (default) | Colour tabloid: black masthead, red accents, upper-case headline, photo in a thick black frame, 2 columns | About 800 characters: keep it short and punchy |
| `daily` | Black-and-white broadsheet: centred masthead, double rules, grey-tone photo with a mat, 3 ruled columns | About 1200 characters: calmer and longer |
| `wizard` | Ornate trading card: dark blue card with a gold double border and cut marks, the photo in an arched gold frame, a moon-and-star emblem, the name on a gold ribbon, lore in a cream box | About 350 characters of lore |
| `wanted` | Weathered poster: torn parchment, huge WANTED lettering, the photo in a heavy dark frame, a "dead or alive" line, the name, a reward line | About 350 characters of description |

A longer story still fits: the body type gets smaller, and only text that cannot fit at the smallest size is cut. A short story gets a bigger photo on `metro`, `daily` and `wanted`, and a story of up to four lines stays in the first column.

Without any text, each style prints its own built-in example: a news story on `metro` and `daily`, "The Spellbound Guest" on `wizard`, and "The Grinning Captain" with a reward of 1,000,000 gold on `wanted`.

### 3. Write `story.json`

```json
{
  "kicker": "Local news",
  "headline": "Commuter Spotted Smiling Before 9 a.m.",
  "byline": "By the booth desk",
  "caption": "Pictured: a rare morning grin, captured at the booth.",
  "story": "First paragraph.\nSecond paragraph starts after a newline."
}
```

| Field | Required | Max characters | Shown as |
|---|---|---|---|
| `headline` | yes | 120 | Big headline |
| `story` | yes | 2500 | Body text; each newline starts a paragraph (blank lines add no extra space) |
| `kicker` | no | 40 | Small label above the headline |
| `caption` | no | 200 | Line under the photo |
| `byline` | no | 80 | Author line: above the story (metro) or under the headline (daily) |

`prt schema story` prints these rules as a JSON Schema, and `prt styles -f json` lists each style with its fields and limits, so you can check a story before printing it.

On the themed styles the same fields fill the card or poster:

| Field | `wizard` card | `wanted` poster |
|---|---|---|
| `headline` | Name on the gold ribbon (about 70 characters on two lines) | Name in large capitals (about 40 characters on two lines) |
| `kicker` | Card type above the portrait, e.g. "Legendary Wizard" | Line under the photo, e.g. "Dead or Alive" |
| `caption` | Subtitle under the ribbon, e.g. "Keeper of the Photo Booth" | Reward line, e.g. "Reward 1,000,000 Gold" (about 70 characters) |
| `story` | Lore in the text box | Description of the "crimes" |
| `byline` | Small footer, e.g. "Booth wizard series · card No. 1" | Small footer, e.g. "By order of the Harbour Watch" |

Keep themed text in the same playful spirit, and invent your own names, places and titles rather than borrowing characters or places from books, films or games.

All values are strings, and other fields are rejected. Use Latin letters: accents print without their marks, and other scripts or emoji are refused. A headline of about 4–8 words looks best.

The maximums are what the command accepts, not what always fits. The `metro` page has the least room: a headline of about 70 characters fits on two full-size lines and about 100 on three smaller ones, and under a portrait photo a caption holds about 100. Longer text is cut with `…` after the last whole word that fits.

If you cannot write files (a sandbox, a read-only folder), pass `--story-file -` and pipe the same JSON in on stdin instead, e.g. `printf '%s' '{"headline": "...", "story": "..."}' | prt booth photo.jpg --story-file - --yes`. A bad story on stdin fails exactly like a bad file, and nothing is printed.

### 4. Preview without printing

```bash
prt booth photo.jpg --style metro --story-file story.json --preview page.jpg
```

`--preview` (also `-o` or `--output`) writes the finished page and sends nothing to the printer. The file name picks the type: `.jpg` or `.jpeg` for a JPEG, `.png` for a lossless image, `.pdf` for a one-page A4 PDF. Open `page.jpg` and check it. When text had to be cut to fit, its last line ends in `…` and the command warns on stderr, naming the fields, for example `caption cut to fit the metro page`. Shorten those fields and preview again.

With `-f json` you do not have to guess how much to cut: the output says `"fits": false` and gives, for each cut field, how many characters were printed (`max_fit`) and how many you gave, for example `{"action": "booth", "style": "metro", "file": "page.jpg", "bytes": N, "fits": false, "cut": {"caption": {"max_fit": 96, "given": 140}}}`. Rewrite each field in `cut` to at most `max_fit` characters and preview again; `"fits": true` with `"cut": {}` means everything fitted.

### 5. Print

```bash
prt booth photo.jpg --style metro --story-file story.json --yes
```

`--yes` skips the confirmation prompt, which is required when there is no terminal. The cut-to-fit warning also appears before printing, but the page is still printed. Add `-f json` to get `{"action": "booth", "style": "metro", "job_id": N, "fits": true, "cut": {}}` on stdout.

To keep a digital copy of the printed page, add `--save` with a `.pdf`, `.png`, `.jpg` or `.jpeg` file name (repeat it for more files):

```bash
prt booth photo.jpg --style metro --story-file story.json --save page.pdf --yes
```

The copies are written before the printer is contacted, so they are kept even when printing is declined or the printer cannot be reached. With `-f json` the output gains a `saved` list: `{"action": "booth", "style": "metro", "job_id": N, "fits": true, "cut": {}, "saved": [{"file": "page.pdf", "format": "pdf", "bytes": N}]}`. Combined with `--preview`, `--save` writes every file and prints nothing.

For a one-off tweak, override any field from the command line: `--headline`, `--story`, `--kicker`, `--caption`, `--byline`. These flags win over the story file. With no story at all, `prt booth photo.jpg` prints the style's built-in default text, so a themed style can be tried with nothing but the photo:

```bash
prt booth photo.jpg --style wanted --preview page.jpg
```

## Errors

Nothing is sent to the printer when an error occurs; the message and a hint go to stderr.

| Exit | Cause | Fix |
|---|---|---|
| `2` | Bad flag value: unknown `--style`; an empty, too long or unprintable `--headline`, `--story`, `--kicker`, `--caption` or `--byline`; a `--preview` or `--save` file name that does not end in `.pdf`, `.png`, `.jpg` or `.jpeg`; or no `--yes` without a terminal | Correct the flag |
| `3` | The photo is missing or not a readable image, or `story.json` is missing, not valid JSON, or has unknown, missing or non-string fields, an empty `headline` or `story`, text that is too long, or characters the font cannot print | Fix the file the message names |
| `1` | Printer unreachable, or a `--preview` or `--save` file cannot be written | Wake the printer (`prt status`) or pick another output path |
