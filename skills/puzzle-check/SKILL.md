---
name: puzzle-check
description: "Print a sudoku, word search, crossword or maze with `prt puzzle` and keep its answers, then scan the filled-in sheet with `prt scan` and check what was written against the solution. Use when asked to print a puzzle for a child or a class and mark it afterwards, or to check a finished puzzle prt printed. Don't use for puzzles prt did not print (there is no solution to check against), for marking free-text homework, or for solving a puzzle for someone."
license: MIT
metadata:
  version: 0.1.0
  author: "Luong NGUYEN"
---

# Puzzle check

Print a puzzle with `prt puzzle`, keep its solution, and once it has been filled in, scan the sheet with `prt scan`, read what was written and check it against the solution. The commands need printer-cli installed (`prt` on the PATH) and, for the check, a printer with a flatbed scanner. Add `-p NAME` to pick a saved printer, or `-p demo` to try the commands without one.

## Workflow

### 1. Make the puzzle and look at it

Pick the kind and the difficulty with the user, then render the page without printing:

```bash
prt puzzle sudoku --difficulty easy --preview sudoku.png -f json
prt puzzle wordsearch --theme zoo --difficulty easy --preview search.png -f json
prt puzzle crossword --crossword-file crossword.json --preview crossword.png -f json
prt puzzle maze --difficulty medium --preview maze.png -f json
```

- **Sudoku**: `--difficulty` `easy`, `medium` or `hard`. Rows are labelled A to I and columns 1 to 9.
- **Word search**: `--theme` is `road trip`, `beach`, `zoo`, `birthday` or `space`. For any other topic, write 3 to 20 words of 3 to 15 letters yourself and pass them with `--words-file words.json`: `{"title": "Our Holiday", "words": ["Beach", "Sandcastle", "Ice cream"]}`.
- **Crossword**: write the answers and clues yourself, about the topic and for the solver's age, and pass them with `--crossword-file crossword.json`. Use 2 to 20 entries, each answer 2 to 15 letters. Answers that share letters make a tighter grid. If an answer cannot cross the others, prt refuses (exit `3`) and names it: swap it for another answer, or add one that shares a letter with it.
- `--title` sets the title, up to 60 characters.

```json
{"title": "Ocean Crossword", "entries": [
  {"answer": "Whale", "clue": "The largest animal in the sea"},
  {"answer": "Coral", "clue": "Reefs are built from it"},
  {"answer": "Octopus", "clue": "It has eight arms"},
  {"answer": "Seahorse", "clue": "A fish that swims upright, with a head like a horse"}
]}
```

Open the preview and check it with the user. The JSON line holds `kind`, `title`, `seed`, `difficulty` and `solution`.

### 2. Print it and keep the answers

The sudoku, word search and maze are random, so print with the `seed` the preview reported. That way the printed page is the one you looked at. Save the answers at the same time:

```bash
prt puzzle sudoku --difficulty easy --seed 4821 --answers sudoku-answers.json --yes -f json
prt puzzle crossword --crossword-file crossword.json --answers crossword-answers.json --yes -f json
```

`--answers FILE.json` writes the solution before anything is printed. Keep the file and its name, because the check usually happens in a later session. `--yes` is required when there is no terminal, and always when `PRT_AGENT=1` is set. If the owner's page caps refuse the print (exit `1`, kind `page_cap_exceeded` or `daily_page_cap_exceeded`), nothing was printed: tell the user rather than retrying.

**If the answers file is lost**, the foot of the page says which puzzle it is, e.g. `sudoku · easy · seed 4821`. Run the same command with that seed and `--preview` to get the solution again: `prt puzzle sudoku --difficulty easy --seed 4821 --preview again.png -f json`. A themed word search names its theme at the foot too (`word search · zoo · easy · seed 17`): pass the same `--theme`. A crossword or a word search from a words file also needs the same file.

### 3. Scan the filled-in sheet

Ask the user to put the sheet face down on the glass and close the lid, then scan it as a JPEG:

```bash
prt scan --format jpeg -o filled.jpg -f json
```

The output is `{"file": "filled.jpg", "bytes": N}`. Use `--mode gray` for pencil. If the scan fails (exit `1`), check that the lid is closed and the scanner is idle, then scan again. A photo of the sheet works just as well, if the user has one.

### 4. Read it and check it

Open `filled.jpg` and the answers file, and compare what was written with `solution`. Read each square or word on its own, and never guess a mark you cannot read: count it as unclear and say so.

| Kind | What to compare |
|---|---|
| sudoku | `solution.puzzle` holds the given digits row by row (A to I, `.` for a blank) and `solution.grid` the full solution. Check only the blank squares, by name (`C7` is row C, column 7) |
| wordsearch | `solution.words` gives each word's start `row` and `col` (from 1), `direction` and `cells`. A word is found when its letters are circled or marked. Note words marked in the wrong place |
| crossword | `solution.across` and `solution.down` list each answer by `number`, with its `row`, `col` and `length`. Read the letters in each numbered answer's squares and compare them with `answer`, which has capital letters only, with spaces and hyphens left out |
| maze | `solution.path` is the way through, square by square from `start` to `finish` as [row, col]. Check that the drawn line runs from Start to Finish without crossing a wall |

### 5. Report

Tell the user what is right, what is wrong and what is still blank, e.g. "52 of 55 squares right; C7 should be 4, not 9; F2 and H8 are empty". Be encouraging, since the solver is often a child. Point out the mistakes rather than handing out the whole solution, unless the user asks for it.

## Try it with the demo printer

The demo printer needs no hardware. Every print and preview command above runs as written:

```bash
prt -p demo puzzle sudoku --difficulty easy --preview sudoku.png -f json
prt -p demo puzzle sudoku --difficulty easy --seed 4821 --answers sudoku-answers.json --yes -f json
prt -p demo scan --format jpeg -o filled.jpg
```

The demo scanner always returns a sample handwritten letter, not your puzzle. Use it to try the scan step only, and to check a sheet for real, scan it on a real printer or use a photo of it.

## Errors

When an error occurs, nothing is sent to the printer. The message and a hint go to stderr, and with `-f json` the last line of stderr is `{"error": {"code": …, "kind": …, "message": …, "hint": …}}`.

| Exit | Cause | Fix |
|---|---|---|
| `1` | The printer or scanner is unreachable or busy, the scan failed, a page cap refused the print, or a file cannot be written | Wake the printer (`prt status`), close the lid, or tell the user about the cap |
| `2` | A bad flag, an unknown `--theme` (kind `unknown_theme`), an `--answers` name not ending in `.json`, an output name that would overwrite an input, or no `--yes` without a terminal or with `PRT_AGENT=1` | Correct the flag, or add `--yes` once the user has agreed to print |
| `3` | The words or crossword file is invalid: missing fields, too few or too many entries, an answer or word with characters other than letters, a repeat, or a crossword answer that cannot cross the others | Fix the entry the message names |
