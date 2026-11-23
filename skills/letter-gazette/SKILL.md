---
name: letter-gazette
description: "Turn a handwritten letter into a printed vintage newspaper edition: scan it with `prt scan`, transcribe it, and print the scan and the transcription as a front page with `prt gazette`. Use when asked to scan a letter, note or card and print it as a newspaper, a gazette or a letters-page edition. Don't use for plain copies (scan and `prt print`), photos of people (use the photo-booth skill), typed documents, or translating the letter."
license: MIT
metadata:
  version: 0.1.0
  author: "Luong NGUYEN"
---

# Letter gazette

Scan a handwritten letter, read it and type it out, then print an old-style newspaper front page with `prt gazette`: the scanned letter as the lead picture and your transcription as the story, under a headline you write for it. The command needs printer-cli installed (`prt` on the PATH) and a printer with a flatbed scanner. Add `-p NAME` to pick a saved printer, or `-p demo` to try the whole workflow without one: the demo printer's scanner returns a sample letter.

## Workflow

### 1. Scan the letter

Ask the user to put the letter face down on the glass and close the lid, then scan it as a JPEG:

```bash
prt scan --format jpeg -o letter.jpg -f json
```

The output is `{"file": "letter.jpg", "bytes": N}`. `prt gazette` needs an image, so always pass `--format jpeg` (the default is PDF). The defaults, colour at 300 dpi over an A4 area, read handwriting well; add `--area letter` for US Letter paper, and `--mode gray` for pencil or faint ink. If the user already has a photo or scan of the letter, skip this step and use that file.

If the scan fails (exit `1`), the hint asks to check that the lid is closed and the scanner is idle. Fix that and scan again; do not print anything yet.

### 2. Read and transcribe it

Open `letter.jpg` and type out the letter.

- Copy the words as written, in their order, with the writer's own spelling. Keep each paragraph, and the greeting and sign-off, as its own paragraph.
- Do not add, improve or summarise anything. Write `[illegible]` for a word you cannot read, and tell the user how many there were.
- The page prints Latin letters only. Accents print without their marks, and other scripts and emoji are refused. If the letter is in another script, tell the user rather than translating it.
- A letter is personal. If it holds an address, a phone number, health or money details, or the names of other people, ask the user what to leave out before anything is printed, and replace what they leave out with `[…]`.

Show the user the transcription and fix anything they correct.

### 3. Write `story.json`

```json
{
  "kicker": "Letters to the editor",
  "headline": "Bake Sale Raises 212 Euros for Library Roof",
  "byline": "From a letter by Margot Lane",
  "caption": "The letter as it reached our desk.",
  "story": "Dear Editor,\nOur street held its first bake sale last Saturday. ...\nYours sincerely,\nMargot Lane"
}
```

| Field | Required | Max characters | What to put in it |
|---|---|---|---|
| `headline` | yes | 120 | A short headline about what the letter says, about 4–8 words. This is the one line you write yourself; keep it true to the letter |
| `story` | yes | 2500 | The transcription. Each newline starts a paragraph |
| `kicker` | no | 40 | A label above the headline, e.g. "Letters to the editor", "From the post bag", "Family news" |
| `byline` | no | 80 | Who wrote it, as signed, e.g. "From a letter by Margot Lane"; leave it out if the letter is unsigned or the user prefers |
| `caption` | no | 200 | The line under the scan, e.g. "The letter as it reached our desk." |

All values are strings, and other fields are rejected. `prt schema gazette` prints these rules as a JSON Schema.

When the scan is the only image, the front page holds the whole 2500 characters: a long story is set in smaller type. Each extra photo or page scan takes room from the story: with two more images the front page holds only about 1350 characters. A letter longer than the room it has does not fit in one edition. Tell the user, and either print the first part ending with `[continued]` or print it in two editions, one per part (each with its own page scan).

If you cannot write files, pass `--story-file -` and pipe the same JSON in on stdin, e.g. `printf '%s' '{"headline": "...", "story": "..."}' | prt gazette letter.jpg --story-file - --yes`. A bad story on stdin fails exactly like a bad file, and nothing is printed.

### 4. Preview without printing

```bash
prt gazette letter.jpg --story-file story.json --preview edition.pdf -f json
```

`--preview` (also `-o`) writes the page and sends nothing to the printer: `.pdf`, or `.png`, `.jpg`, `.jpeg` for an image. Open it and check that the letter is readable on the page and the text is right. The JSON line reports the fit and the cost (the `estimate` is shortened here):

```json
{"action": "gazette", "photos": 1, "pages": 1, "fits": true, "cut": {},
 "estimate": {"pages": 1, "colour": true, "ink_percent": 0.6, "summary": "~1 page, colour, est. ink 1%"},
 "files": [{"file": "edition.pdf", "format": "pdf", "bytes": 123456}]}
```

When `"fits"` is false, `cut` names each field that was cut with `…`, with how many characters were printed (`max_fit`) and how many you gave (`given`). Shorten the headline, kicker, byline or caption to `max_fit` and preview again. When `cut` names `story`, the transcription did not fit: never shorten it without asking the user. Take out the extra images, or split the letter into two editions as in step 3. `"fits": true, "cut": {}` means everything fitted.

A letter written across a wide sheet may look better with `--landscape`, which puts the scan beside the story.

### 5. Print

```bash
prt gazette letter.jpg --story-file story.json --yes -f json
```

`--yes` skips the confirmation prompt, which is required when there is no terminal and always when `PRT_AGENT=1` is set. The output is the same JSON line with `"job_ids": [N]` in place of `files`. Add `--save edition.pdf` to keep a copy of the printed page. If the owner's page caps refuse the print (exit `1`, kind `page_cap_exceeded` or `daily_page_cap_exceeded`), nothing was printed: tell the user rather than retrying.

If the letter came with photos, scan them too and add them after the letter: `prt gazette letter.jpg photo-1.jpg photo-2.jpg --story-file story.json`. The front page shows the first two after the letter as numbered plates, and later pages hold six more each. The plates leave less room for the story (step 3), so preview again and check `cut`. For a letter of several pages, print one edition per page instead.

## Try it with the demo printer

The demo printer needs no hardware, and its scanner returns a sample letter, so every step above runs as written:

```bash
prt -p demo scan --format jpeg -o letter.jpg
# read letter.jpg, transcribe it and write story.json
prt -p demo gazette letter.jpg --story-file story.json --preview edition.pdf -f json
prt -p demo gazette letter.jpg --story-file story.json --yes -f json
```

## Errors

Nothing is sent to the printer when an error occurs; the message and a hint go to stderr, and with `-f json` the last line of stderr is `{"error": {"code": …, "kind": …, "message": …, "hint": …}}`.

| Exit | Cause | Fix |
|---|---|---|
| `1` | The printer or scanner is unreachable or busy, the scan failed, a page cap refused the print, or an output file cannot be written | Wake the printer (`prt status`), close the lid, or tell the user about the cap |
| `2` | A bad flag, an output name that would overwrite the scan, or no `--yes` without a terminal or with `PRT_AGENT=1` | Correct the flag, or add `--yes` once the user has agreed to print |
| `3` | The scan is not a readable image (for example a PDF scan), or `story.json` is invalid: missing `headline` or `story`, unknown fields, text over its maximum, or characters the font cannot print | Scan again with `--format jpeg`, or fix the field the message names |
