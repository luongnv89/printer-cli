# Sample pages

These images are the sample pages shown in the main README. They were rendered with `--preview`, so nothing was printed, then scaled down for the web.

| File | Command |
|---|---|
| `booth-metro.jpg` | `prt booth astronaut.jpg --story-file story.json -o booth-metro.png` |
| `booth-daily.jpg` | `prt booth astronaut.jpg --style daily --story-file story.json -o booth-daily.png` |
| `booth-wizard.jpg` | `prt booth astronaut.jpg --style wizard -o booth-wizard.png` |
| `booth-wanted.jpg` | `prt booth astronaut.jpg --style wanted -o booth-wanted.png` |
| `booth-certificate.jpg` | `prt booth astronaut.jpg --style certificate -o booth-certificate.png` |
| `booth-sports.jpg` | `prt booth astronaut.jpg --style sports -o booth-sports.png` |
| `booth-missing.jpg` | `prt booth astronaut.jpg --style missing -o booth-missing.png` |
| `booth-movie.jpg` | `prt booth astronaut.jpg --style movie -o booth-movie.png` |
| `card.jpg` | `prt card astronaut.jpg -o card.png` |
| `comic.jpg` | `prt comic astronaut.jpg launch.jpg earthrise.jpg bluemarble.jpg --comic-file comic.json -o comic.png` |
| `strip.jpg` | `prt strip astronaut.jpg launch.jpg earthrise.jpg bluemarble.jpg --caption "Apollo 11 · July 1969" -o strip.png` |
| `instant.jpg` | `prt instant earthrise.jpg --caption "Earthrise, Christmas Eve 1968" -o instant.png` |
| `colour-in.jpg` | `prt colour-in astronaut.jpg -o colour-in.png` |
| `postcard.jpg` | `prt postcard earthrise.jpg --postcard-file postcard.json -o postcard.png`: `postcard-front.png` above `postcard-back.png` |
| `disc.jpg` | `prt disc bluemarble.jpg --disc-file disc.json -o disc.png` |
| `recipe.jpg` | `prt recipe recipe.json -o recipe.png` |
| `bingo.jpg` | `prt bingo --words-file bingo.json --seed 1969 -o bingo.png` |
| `puzzle-sudoku.jpg` | `prt puzzle sudoku --seed 1969 -o puzzle-sudoku.png` |
| `puzzle-wordsearch.jpg` | `prt puzzle wordsearch --theme space --seed 1969 -o puzzle-wordsearch.png` |
| `puzzle-crossword.jpg` | `prt puzzle crossword --crossword-file crossword.json -o puzzle-crossword.png` |
| `puzzle-maze.jpg` | `prt puzzle maze --seed 1969 -o puzzle-maze.png` |
| `article-magazine.jpg` | `prt article article.json -o article-magazine.png` |
| `article-classic.jpg` | `prt article article.json --style classic-magazine -o article-classic.png` (page 1) |
| `gazette.jpg` | `prt gazette astronaut.jpg launch.jpg earthrise.jpg --story-file story.json -o gazette.png` |
| `gazette-landscape.jpg` | the same with `--landscape` |
| `gazette-morning.jpg` | `prt gazette --edition morning --edition-file morning.json -o gazette-morning.png` (the `morning` schema example) |
| `commit-receipt.jpg` | `prt receipt . --period week -o commit-receipt.png` in this repository |
| `tui.jpg` | `prt -p demo tui`, saved as a Textual screenshot at 120×36 |

`story.json`, `article.json`, `recipe.json`, `postcard.json`, `disc.json`, `comic.json`, `bingo.json` and `crossword.json` are the inputs used above. The article text, the recipe, the postcard, the disc's track list, the comic's dialogue, the bingo words and the crossword clues were written for these samples (the track titles are made up).

## Photo credits

The source photos are not included here. All are public domain (works of NASA) and can be downloaded from Wikimedia Commons:

- `astronaut.jpg`: [Aldrin Apollo 11 original.jpg](https://commons.wikimedia.org/wiki/File:Aldrin_Apollo_11_original.jpg), Neil Armstrong / NASA
- `launch.jpg`: [Apollo 11 Saturn V lifting off on July 16, 1969.jpg](https://commons.wikimedia.org/wiki/File:Apollo_11_Saturn_V_lifting_off_on_July_16,_1969.jpg), NASA
- `bluemarble.jpg`: [The Earth seen from Apollo 17.jpg](https://commons.wikimedia.org/wiki/File:The_Earth_seen_from_Apollo_17.jpg), Apollo 17 crew / NASA
- `earthrise.jpg`: [NASA-Apollo8-Dec24-Earthrise.jpg](https://commons.wikimedia.org/wiki/File:NASA-Apollo8-Dec24-Earthrise.jpg), Bill Anders / NASA
