---
name: greeting-card
description: "Turn a photo into a printed greeting card with a verse written for that photo and the occasion, using printer-cli's `prt card`: one A4 sheet that folds into a card. Use when asked for a birthday, thank-you, congratulations, get-well, holiday or other greeting card made from a photo. Don't use for plain photo printing (use `prt print`), newspaper pages or posters (use the photo-booth skill), invitations or flyers with many details (use `prt compose`), or editing the photo itself."
license: MIT
metadata:
  version: 0.1.0
  author: "Luong NGUYEN"
---

# Greeting card

Write a greeting and a short verse that suit the photo you were given and the occasion, then print them with `prt card`. It lays the photo and text out on one A4 sheet as four panels that fold into an A6 card: the photo and greeting on the front, an optional note inside left, the verse and signature inside right, and a small line on the back. The command needs printer-cli installed (`prt` on the PATH). Add `-p NAME` to pick a saved printer, or `-p demo` to try everything without a printer.

## Workflow

### 1. Find the occasion and look at the photo

Ask for the occasion (birthday, thank you, congratulations, get well, a holiday) and who the card is for and from, unless the user already said. Then open the image and note only what is visible: the setting, the mood, colours, what the person or pet is doing, any props.

- Do not name or identify anyone in the photo, and do not guess age, ethnicity, health, religion or other personal traits.
- Keep it warm and kind. Use only the names the user gives you, for the greeting, the note or the signature.

### 2. Write `card.json`

```json
{
  "greeting": "Happy Birthday!",
  "verse": "Another year of sunny days,\nof laughter in a hundred ways.\n\nSo here's a card to say it true:\nthe world is brighter, thanks to you.",
  "message": "To Grandma,",
  "signature": "With love, Jo and Sam",
  "back": "Printed with love at home"
}
```

| Field | Required | Max characters | Printed |
|---|---|---|---|
| `greeting` | yes | 60 | In large letters on the front, under the photo. Two to five words look best |
| `verse` | yes | 600 | Inside right, centred. Each newline starts a new line; a blank line starts a new stanza |
| `message` | no | 300 | Inside left, centred; a newline starts a new paragraph. Leave it out to keep that panel blank for a handwritten note |
| `signature` | no | 80 | Under the verse, e.g. "With love, Jo" |
| `back` | no | 80 | A small line on the back |

Write the verse for this photo and this occasion: two stanzas of two to four short lines each read best, with lines of up to about 35 characters, so each stays on one line. A verse of up to about 12 lines prints at full size; longer lines or more of them are set in smaller type. Rhyme if it comes naturally, and never borrow lines from published poems or songs.

All values are strings, and other fields are rejected. Use Latin letters: accents print without their marks, and other scripts or emoji are refused. `prt schema card` prints these rules as a JSON Schema.

If you cannot write files, pass `--card-file -` and pipe the same JSON in on stdin: `printf '%s' '{"greeting": "...", "verse": "..."}' | prt card photo.jpg --card-file - --yes`. A bad card on stdin fails exactly like a bad file, and nothing is printed.

### 3. Preview without printing

```bash
prt card photo.jpg --card-file card.json --preview card.png -f json
```

`--preview` (also `-o`) writes the sheet and sends nothing to the printer. The file name picks the type: `.jpg`, `.jpeg`, `.png` or `.pdf`. Open the file and check it. The top half of the sheet is printed upside down on purpose: it reads the right way up once the sheet is folded.

The JSON line says whether everything fitted, for example `{"action": "card", "file": "card.png", "bytes": N, "fits": true, "cut": {}, "estimate": {…}, "fold": "Fold the sheet in half …"}`. Text too long for its panel is cut with `…`; then `"fits"` is `false` and `cut` gives, for each cut field, how many characters were printed (`max_fit`) and how many you gave. Shorten each field in `cut` to at most `max_fit` characters, or rewrite it shorter, and preview again.

### 4. Print

```bash
prt card photo.jpg --card-file card.json --yes -f json
```

`--yes` skips the confirmation prompt, which is required when there is no terminal and always when `PRT_AGENT=1` is set. The card is one sheet; the JSON output carries an `estimate` of its ink use. If the owner's page caps refuse the print (exit code `1`, kind `page_cap_exceeded` or `daily_page_cap_exceeded`), nothing was printed: tell the user rather than retrying. Add `--save card.pdf` to keep a copy of the sheet as well.

Then tell the user how to fold it, as the `fold` field says: fold the sheet in half from top to bottom with the printed side out, then in half again with the photo on the front.

For a one-off change, override any field from the command line: `--greeting`, `--verse`, `--message`, `--signature`, `--back`. These flags win over the card file. With no text at all, `prt card photo.jpg` prints a built-in "Best Wishes" card.

## Errors

Nothing is sent to the printer when an error occurs; the message and a hint go to stderr (with `-f json`, a JSON `error` object on its last line).

| Exit | Cause | Fix |
|---|---|---|
| `2` | An empty, too long or unprintable `--greeting`, `--verse`, `--message`, `--signature` or `--back`; a `--preview` or `--save` name that does not end in `.pdf`, `.png`, `.jpg` or `.jpeg`, or that would overwrite the photo or the card file; or no `--yes` without a terminal or in agent mode | Correct the flag |
| `3` | The photo is missing or not a readable image, or `card.json` is missing, not valid JSON, or has unknown, missing or non-string fields, an empty `greeting` or `verse`, text that is too long, or characters the font cannot print | Fix the file the message names |
| `1` | Printer unreachable, a page cap refused the print, or a `--preview` or `--save` file cannot be written | Wake the printer (`prt status`), tell the user about the cap, or pick another output path |
