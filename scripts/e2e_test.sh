#!/usr/bin/env bash
# scripts/e2e_test.sh: run every prt command and subcommand end to end against the
# demo printer (FakeDriver), in a throwaway HOME, config and cache, so no real
# printer, network, ~/.config/prt, ~/.cache/prt, ~/.claude or ~/.agents is touched.
# Page commands save a preview; commands with no preview (print, jobs, maintain,
# keepalive --yes) go to the demo printer, whose jobs last one process only. discover, tui, dash and mcp would listen
# on the network or take over the terminal, so they get --help only.
# Exit 0 = every check passed.
set -uo pipefail

cd "$(dirname "$0")/.."
PY="$(uv run --locked python -c 'import sys; print(sys.executable)')" || exit 1

T="$(mktemp -d "${TMPDIR:-/tmp}/prt-e2e.XXXXXX")"
trap 'rm -rf "$T"' EXIT
export HOME="$T/home" XDG_CONFIG_HOME="$T/config" XDG_CACHE_HOME="$T/cache"
export LANG=C.UTF-8 TERM=xterm-256color NO_COLOR=1
unset PRT_PRINTER PRT_CONFIG PRT_TIMEOUT PRT_AGENT FORCE_COLOR CLICOLOR_FORCE COLUMNS
# Run from a git hook, GIT_DIR, GIT_INDEX_FILE and GIT_AUTHOR_* point at the outer repository.
while IFS= read -r var; do unset "$var"; done < <(compgen -e | grep '^GIT_')
mkdir -p "$HOME"

pass=0
fail=0
# check NAME EXPECTED_EXIT PATTERN -- ARGS...: run prt ARGS, compare the exit code,
# and grep the combined output for PATTERN ('' skips the grep).
check() {
  local name="$1" want="$2" pattern="$3"
  shift 4
  local out code
  out="$("$PY" -m prt "$@" 2>&1)"
  code=$?
  if [[ "$code" != "$want" ]]; then
    printf 'FAIL %-26s exit %s, expected %s\n%s\n' "$name" "$code" "$want" "$(tail -5 <<<"$out")"
    fail=$((fail + 1))
  elif [[ -n "$pattern" ]] && ! grep -qE -- "$pattern" <<<"$out"; then
    printf 'FAIL %-26s output lacks /%s/\n%s\n' "$name" "$pattern" "$(tail -5 <<<"$out")"
    fail=$((fail + 1))
  else
    pass=$((pass + 1))
  fi
}
# exists NAME FILE: the command before it wrote a non-empty FILE.
exists() {
  if [[ -s "$2" ]]; then pass=$((pass + 1)); else echo "FAIL $1: $2 missing or empty"; fail=$((fail + 1)); fi
}

# --- Inputs: six photos, the JSON files, an article, a one-commit git repository ---
"$PY" - "$T" <<'EOF' || exit 1
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw

t = Path(sys.argv[1])
for i in range(6):
    im = Image.new("RGB", (640, 800), (40 * i, 120, 200 - 30 * i))
    d = ImageDraw.Draw(im)
    d.ellipse((170, 150, 470, 500), fill=(240, 200, 160))
    d.rectangle((120, 520, 520, 800), fill=(30, 60, 120))
    im.save(t / f"p{i}.png")
(t / "comic.json").write_text(json.dumps({"title": "E2E", "panels": [
    {"bubbles": [{"text": f"Panel {i}"}], "caption": f"Caption {i}"} for i in range(3)]}))
(t / "recipe.json").write_text(json.dumps({"title": "Toast", "ingredients": ["1 slice of bread"],
    "steps": ["Toast the bread.", "Eat it."]}))
(t / "page.json").write_text(json.dumps({"blocks": [
    {"type": "heading", "text": "End to end"}, {"type": "text", "text": "A composed page."},
    {"type": "rule"}, {"type": "qr", "data": "https://example.com"}]}))
(t / "crossword.json").write_text(json.dumps({"title": "E2E", "entries": [
    {"answer": "PRINTER", "clue": "It puts ink on paper"},
    {"answer": "INK", "clue": "It runs out"},
    {"answer": "PAPER", "clue": "A4, for one"}]}))
(t / "article.txt").write_text("An end-to-end article\n\n" + "This paragraph fills the page. " * 40)
EOF
git init -q "$T/repo" && git -C "$T/repo" -c user.name=e2e -c user.email=e2e@example.invalid \
  commit -q --allow-empty -m "feat: e2e commit" || exit 1
P=("$T/p0.png" "$T/p1.png" "$T/p2.png" "$T/p3.png")

# --- Version, help, error paths -------------------------------------------------
check version               0 '^prt [0-9]'      -- --version
check help                  0 'Commands'        -- --help
check unknown-command       2 'No such command' -- no-such-command
check schema-unknown        2 ''                -- schema no-such-schema
PRT_AGENT=1 check agent-needs-yes 2 'agent_confirmation_required' -- -p demo -f json keepalive
check strip-wrong-count     2 ''                -- -p demo strip "$T/p0.png" -o "$T/x.png"

# --- Printer state ----------------------------------------------------------------
check status                0 '"worst"'         -- -p demo -f json status
check ink                   0 ''                -- -p demo ink
check overview              0 ''                -- -p demo overview
check info                  0 ''                -- -p demo info
check list                  0 ''                -- list --no-check
check styles                0 'booth'           -- styles
for s in story article gazette page card comic recipe postcard disc bingo wordsearch crossword; do
  check "schema-$s"         0 '"properties"'    -- schema "$s"
done

# --- Saved printers ---------------------------------------------------------------
check printers-add          0 ''                -- printers add e2e 127.0.0.1 --driver fake
check printers-default      0 ''                -- printers default e2e
check printers-list         0 'e2e'             -- printers list --no-check
check printers-remove       0 ''                -- printers remove e2e

# --- Pages: saved as previews, nothing printed --------------------------------------
check booth                 0 '"estimate"'      -- -p demo -f json booth "$T/p0.png" --headline E2E --story "A story." -o "$T/booth.png"
exists booth "$T/booth.png"
check card                  0 ''                -- -p demo card "$T/p0.png" --greeting Hello --verse Roses -o "$T/card.pdf"
exists card "$T/card.pdf"
check comic                 0 ''                -- -p demo comic "$T/p0.png" "$T/p1.png" "$T/p2.png" --comic-file "$T/comic.json" -o "$T/comic.png"
exists comic "$T/comic.png"
check strip                 0 ''                -- -p demo strip "${P[@]}" --caption E2E -o "$T/strip.png"
exists strip "$T/strip.png"
check instant               0 ''                -- -p demo instant "$T/p0.png" --caption E2E -o "$T/instant.png"
exists instant "$T/instant.png"
check colour-in             0 ''                -- -p demo colour-in "$T/p0.png" -o "$T/colour.png"
exists colour-in "$T/colour.png"
check recipe                0 ''                -- -p demo recipe "$T/recipe.json" -o "$T/recipe.pdf"
exists recipe "$T/recipe.pdf"
check bingo                 0 '"seed"'          -- -p demo -f json bingo -n 2 --seed 7 -o "$T/bingo.pdf"
exists bingo "$T/bingo.pdf"
check disc                  0 ''                -- -p demo disc "$T/p0.png" --title Mix --track "1. Song" -o "$T/disc.png"
exists disc "$T/disc.png"
check postcard              0 ''                -- -p demo postcard "$T/p0.png" --message Hi -o "$T/postcard.pdf"
exists postcard "$T/postcard.pdf"
check article               0 ''                -- -p demo article "$T/article.txt" -o "$T/article.pdf"
exists article "$T/article.pdf"
check gazette               0 ''                -- -p demo gazette "$T/p0.png" --headline E2E --story News. -o "$T/gazette.pdf"
exists gazette "$T/gazette.pdf"
check receipt               0 ''                -- -p demo receipt "$T/repo" --period week -o "$T/receipt.png"
exists receipt "$T/receipt.png"
check compose               0 ''                -- -p demo compose "$T/page.json" -o "$T/page.pdf"
exists compose "$T/page.pdf"
check keepalive-preview     0 ''                -- -p demo keepalive -o "$T/keepalive.png"
exists keepalive "$T/keepalive.png"
check puzzle-sudoku         0 '"solution"'      -- -p demo -f json puzzle sudoku --seed 1 -o "$T/sudoku.png" --answers "$T/sudoku.json"
exists sudoku-answers "$T/sudoku.json"
check puzzle-wordsearch     0 '"solution"'      -- -p demo -f json puzzle wordsearch --seed 1 -o "$T/words.png"
check puzzle-crossword      0 '"solution"'      -- -p demo -f json puzzle crossword --crossword-file "$T/crossword.json" -o "$T/cross.png"
check puzzle-maze           0 '"solution"'      -- -p demo -f json puzzle maze --seed 1 -d easy -o "$T/maze.png"

# --- Sent to the demo printer: print, jobs, maintenance, keepalive ------------------
check print                 0 'job_id'          -- -p demo -f json print "$T/p0.png" --yes
check keepalive             0 ''                -- -p demo keepalive --yes
check jobs                  0 ''                -- -p demo jobs
check jobs-wait-unknown     1 'has no job'      -- -p demo jobs wait 999 --wait-timeout 5
check jobs-cancel           0 ''                -- -p demo jobs cancel 1
check maintain-nozzle-check 0 ''                -- -p demo maintain nozzle-check --yes
# The demo printer's ink is low: cleaning is refused unless forced.
check maintain-clean-refused 1 'refused for safety' -- -p demo maintain clean --yes
check maintain-clean-forced 0 ''                -- -p demo maintain clean --yes --force
check maintain-test-page    0 ''                -- -p demo maintain test-page --yes
check maintain-identify     0 ''                -- -p demo maintain identify

# --- Scanner ------------------------------------------------------------------------
check scan-pdf              0 ''                -- -p demo scan -o "$T/scan.pdf"
exists scan-pdf "$T/scan.pdf"
check scan-jpeg             0 ''                -- -p demo scan --format jpeg -o "$T/scan.jpg"
exists scan-jpeg "$T/scan.jpg"

# --- Skills -------------------------------------------------------------------------
check skills-list           0 'photo-booth'     -- skills list
check skills-install        0 ''                -- skills install --dir "$T/skills"
exists skills-installed "$T/skills/photo-booth/SKILL.md"
check skills-reinstall      0 ''                -- skills install --dir "$T/skills"

# --- Networked or interactive: help only ---------------------------------------------
for c in discover tui dash mcp; do
  check "$c-help"           0 'Usage'           -- "$c" --help
done

echo "e2e: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
