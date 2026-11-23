---
name: article-print
description: "Print a blog post, news article or X post as A4 magazine pages with printer-cli's `prt article`, fetching the text yourself when the tool cannot (login walls, JavaScript-only pages, long X threads). Use when asked to print an article, a blog post, a tweet or an X thread in a magazine layout. Don't use for printing PDFs or photos (use `prt print`), photo-booth pages (use the photo-booth skill), or summarising an article."
license: MIT
metadata:
  version: 0.2.0
  author: "Luong NGUYEN"
---

# Article print

Print an article as A4 magazine pages with `prt article`. The command fetches a link itself; when it cannot get the text (the page needs a login or JavaScript, or the X post is private, long or part of a thread), you read the article and hand it over as a JSON file. The command needs printer-cli installed (`prt` on the PATH). Add `-p NAME` to pick a saved printer, or `-p demo` to try everything without a printer.

## Workflow

### 1. Try the link first

```bash
prt article https://example.com/blog/post --preview page.pdf
```

`prt article` accepts `http://` and `https://` links to blog posts and news articles, and links to single X posts (`https://x.com/<user>/status/<id>`, also `twitter.com`). It keeps the title, author, site name and the full body text, and leaves out navigation, ads, share bars, comments, newsletter boxes and other page clutter. X posts come from X's public oEmbed service, which only returns the text of one public post.

`--preview` (also `-o` or `--output`) writes the pages and sends nothing to the printer. Open the file and check that the text is the article and nothing else. If the command fails with exit code 3 (see [Errors](#errors)), or the preview is missing text or shows the wrong text, go to step 2.

### 2. Fall back: write `article.json` yourself

Open the page with your own tools (a browser session that is logged in, a reader view, or the text the user pasted) and copy the article as it is. Do not summarise, rewrite or add to it; for an X thread, put each post in order as its own paragraph.

```json
{
  "title": "Why Paper Still Matters",
  "author": "Jo Writer",
  "source": "The Long Read",
  "url": "https://example.com/blog/paper",
  "body": [
    "First paragraph of the article.",
    "Second paragraph, copied as written."
  ]
}
```

| Field | Required | Shown as |
|---|---|---|
| `title` | yes | Headline |
| `body` | yes | The text: a list of paragraphs, or one string where each newline starts a paragraph |
| `author` | no | Byline ("By …" or "by …") |
| `source` | no | Site or publication name (the section label on `magazine`, the letter-spaced line on `classic-magazine`) |
| `url` | no | Link in the page footer (`magazine`) |

All values are text and other fields are rejected. The file may be at most 5 MiB. Saved `.html` pages and plain `.txt` files also work: in a text file the first line is the title and blank lines separate paragraphs.

### 3. Pick a style

| `--style` | Look |
|---|---|
| `magazine` (default) | White page, blue accent bar and section label, big bold headline, byline, heavy rule, three columns of text with hairline rules, page numbers in a blue footer |
| `classic-magazine` | Ivory page in a double-rule frame, letter-spaced publication name, centred headline between fleurons, one centred column with a rust-red drop cap, page numbers between diamonds |

Long articles run on over as many pages as they need, up to 30, and nothing is cut. Pages are numbered; on `magazine` each page that does not end the article also says where it continues. The built-in font covers Latin letters only: accents print without their marks, curly quotes and dashes are kept or replaced by plain ones, emoji are left out, and an article mostly in another script is refused.

### 4. Preview the file

```bash
prt article article.json --style classic-magazine --preview page.pdf
```

A `.pdf` holds every page. A `.png`, `.jpg` or `.jpeg` name saves one image per page, numbered `page-1.png`, `page-2.png` and so on when there is more than one.

### 5. Print

```bash
prt article article.json --style magazine --yes
```

The command prints one job per page. `--yes` skips the confirmation prompt ("Print 3 pages of '…' as a magazine article on …?"), which is required when there is no terminal. Add `-f json` to get `{"action": "article", "style": "magazine", "title": "…", "pages": N, "job_ids": [...]}` on stdout.

To keep a digital copy of the printed pages, add `--save` with a `.pdf`, `.png`, `.jpg` or `.jpeg` file name (repeat it for more files):

```bash
prt article article.json --style magazine --save page.pdf --yes
```

The copies are written before the printer is contacted, so they are kept even when printing is declined or the printer cannot be reached. With `-f json` the output gains a `saved` list of `{"file", "format", "bytes"}`.

## Errors

Nothing is sent to the printer when an error occurs; the message and a hint go to stderr.

| Exit | Cause | Fix |
|---|---|---|
| `2` | Bad flag value: unknown `--style`, a `--preview` or `--save` file name that does not end in `.pdf`, `.png`, `.jpg` or `.jpeg`, or no `--yes` without a terminal | Correct the flag |
| `3` | The link is not `http://` or `https://`, the site answered with an error or did not answer in time (`--timeout` allows longer), the page is not HTML or text or is larger than 5 MiB, no article text was found (JavaScript or login pages), X did not return the post, the text is mostly in a script the font cannot print, the article needs more than 30 pages, or the file is missing, not UTF-8, or not valid article JSON | Use the fallback in step 2, or fix the file the message names |
| `1` | Printer unreachable, or a `--preview` or `--save` file cannot be written | Wake the printer (`prt status`) or pick another output path |
