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
| `strip.jpg` | `prt strip astronaut.jpg launch.jpg earthrise.jpg bluemarble.jpg --caption "Apollo 11 · July 1969" -o strip.png` |
| `instant.jpg` | `prt instant earthrise.jpg --caption "Earthrise, Christmas Eve 1968" -o instant.png` |
| `postcard.jpg` | `prt postcard earthrise.jpg --postcard-file postcard.json -o postcard.png`: `postcard-front.png` above `postcard-back.png` |
| `disc.jpg` | `prt disc bluemarble.jpg --disc-file disc.json -o disc.png` |
| `recipe.jpg` | `prt recipe recipe.json -o recipe.png` |
| `article-magazine.jpg` | `prt article article.json -o article-magazine.png` |
| `article-classic.jpg` | `prt article article.json --style classic-magazine -o article-classic.png` (page 1) |
| `gazette.jpg` | `prt gazette astronaut.jpg launch.jpg earthrise.jpg --story-file story.json -o gazette.png` |
| `gazette-landscape.jpg` | the same with `--landscape` |
| `tui.jpg` | `prt -p demo tui`, saved as a Textual screenshot at 120×36 |

`story.json`, `article.json`, `recipe.json`, `postcard.json` and `disc.json` are the inputs used above. The article text, the recipe, the postcard and the disc's track list were written for these samples (the track titles are made up).

## Photo credits

The source photos are not included here. All are public domain (works of NASA) and can be downloaded from Wikimedia Commons:

- `astronaut.jpg`: [Aldrin Apollo 11 original.jpg](https://commons.wikimedia.org/wiki/File:Aldrin_Apollo_11_original.jpg), Neil Armstrong / NASA
- `launch.jpg`: [Apollo 11 Saturn V lifting off on July 16, 1969.jpg](https://commons.wikimedia.org/wiki/File:Apollo_11_Saturn_V_lifting_off_on_July_16,_1969.jpg), NASA
- `bluemarble.jpg`: [The Earth seen from Apollo 17.jpg](https://commons.wikimedia.org/wiki/File:The_Earth_seen_from_Apollo_17.jpg), Apollo 17 crew / NASA
- `earthrise.jpg`: [NASA-Apollo8-Dec24-Earthrise.jpg](https://commons.wikimedia.org/wiki/File:NASA-Apollo8-Dec24-Earthrise.jpg), Bill Anders / NASA
