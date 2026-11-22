---
name: photo-booth
description: "Turn a photo of a person into a printed newspaper front page with a short news story written for that photo, using printer-cli's `prt booth`. Use when asked for a photo-booth print, a newspaper clipping or a front page made from someone's picture. Don't use for plain photo printing (use `prt print`), editing the photo itself, or real news reporting."
license: MIT
metadata:
  version: 0.1.0
  author: "Luong NGUYEN"
---

# Photo booth

Write a short, light-hearted news story that fits the photo you were given, then print it with `prt booth`, which lays out the photo and story as an A4 newspaper front page. The command needs printer-cli installed (`prt` on the PATH). Add `-p NAME` to pick a saved printer, or `-p demo` to try everything without a printer.

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

A longer story still fits: the body type gets smaller, and only text that cannot fit at the smallest size is cut.

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
| `story` | yes | 2500 | Body text; each newline starts a paragraph |
| `kicker` | no | 40 | Small label above the headline |
| `caption` | no | 200 | Line under the photo |
| `byline` | no | 80 | Author line: above the story (metro) or under the headline (daily) |

All values are strings, and other fields are rejected. Use Latin letters: accents print without their marks, and other scripts or emoji are refused. A headline of about 4–8 words looks best.

The maximums are what the command accepts, not what always fits. The `metro` page has the least room: it holds a headline of about 70 characters, and under a portrait photo a caption of about 100. Longer text is cut with `…`.

### 4. Preview without printing

```bash
prt booth photo.jpg --style metro --story-file story.json --preview page.jpg
```

`--preview` (also `-o` or `--output`) writes the finished page as a JPEG and sends nothing to the printer. Open `page.jpg` and check it. When text had to be cut to fit, its last line ends in `…` and the command warns on stderr, naming the fields, for example `caption cut to fit the metro page`. Shorten those fields and preview again.

### 5. Print

```bash
prt booth photo.jpg --style metro --story-file story.json --yes
```

`--yes` skips the confirmation prompt, which is required when there is no terminal. The cut-to-fit warning also appears before printing, but the page is still printed. Add `-f json` to get `{"action": "booth", "style": "metro", "job_id": N}` on stdout.

For a one-off tweak, override any field from the command line: `--headline`, `--story`, `--kicker`, `--caption`, `--byline`. These flags win over the story file. With no story at all, `prt booth photo.jpg` prints a built-in default story.

## Errors

Nothing is sent to the printer when an error occurs; the message and a hint go to stderr.

| Exit | Cause | Fix |
|---|---|---|
| `2` | Bad flag value: unknown `--style`; an empty, too long or unprintable `--headline`, `--story`, `--kicker`, `--caption` or `--byline`; or no `--yes` without a terminal | Correct the flag |
| `3` | The photo is missing or not a readable image, or `story.json` is missing, not valid JSON, or has unknown, missing or non-string fields, an empty `headline` or `story`, text that is too long, or characters the font cannot print | Fix the file the message names |
| `1` | Printer unreachable, or the preview file cannot be written | Wake the printer (`prt status`) or pick another output path |
