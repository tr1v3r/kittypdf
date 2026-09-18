# kittypdf

A minimal PDF reader that lives inside the [kitty](https://sw.kovidgoyal.net/kitty/)
terminal. Pages are rendered by [PyMuPDF](https://pymupdf.readthedocs.io/) and
drawn as real pixels through kitty's graphics protocol — no ASCII art.

Written from scratch; inspired by the (dormant) `termpdf.py`.

## Keys

| Key | Action |
|---|---|
| `j` / `↓` / `Space` | next page (accepts a count: `10j`) |
| `k` / `↑` / `b` | previous page |
| `gg` | first page |
| `G` | last page (`12G` jumps to page 12) |
| `i` | invert colors (dark reading) |
| `r` / `Ctrl-L` | redraw current page |
| `q` | quit |

The last-read page is remembered per file (`~/.cache/kittypdf/`).

## Install

```sh
uv tool install --system-certs git+https://github.com/tr1v3r/kittypdf
kittypdf book.pdf
```

Requires kitty (graphics protocol) and a terminal that reports pixel cell
sizes (kitty does).
