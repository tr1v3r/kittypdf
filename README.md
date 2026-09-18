# kittypdf

A minimal PDF reader that lives inside the [kitty](https://sw.kovidgoyal.net/kitty/)
terminal. Pages are rendered by [PyMuPDF](https://pymupdf.readthedocs.io/) and
drawn as real pixels through kitty's graphics protocol — no ASCII art.

Written from scratch; inspired by the (dormant) `termpdf.py`.

## Keys

Movement follows the colemak diamond (`u` = up, `e` = down), as in a colemak
vim setup. `i` — the diamond's right slot — stays on invert colors: a reader
has no horizontal motion, so nothing competes for it.

| Key | Action |
|---|---|
| `e` / `↓` / `Space` | next page (accepts a count: `10e`) |
| `u` / `↑` | previous page |
| `E` / `U` | big jump: 10 pages forward / back (`3E` → 30 pages) |
| `gg` | first page |
| `G` | last page (`12G` jumps to page 12) |
| `i` | invert colors (dark reading) |
| `t` | table of contents overlay (`e`/`u` pick, `Enter` jumps, `q`/`Esc` closes) |
| `c` | toggle autocrop (trim page margins) |
| `d` | cycle page layout: auto → single → dual |
| `r` / `Ctrl-L` | redraw current page |
| `q` / `Q` | quit |
| `j` / `k` / `b` | qwerty aliases: `j` next page, `k` / `b` previous page |

## Dual-page spreads

By default the layout is **auto**: kittypdf shows two pages side by side
whenever the window is wide enough that a single page would only be
limited by height (e.g. a portrait PDF in a full-screen wide terminal),
and falls back to a single page otherwise. Resizing re-evaluates it live.
Press `d` to cycle auto → single → dual, or start with `-m/--mode
{auto,single,dual}`. In a spread, `e`/`u` move by two pages and the status
bar shows a range like `[3-4/120]`.

The last-read page, invert and crop settings are remembered per file
(~/.cache/kittypdf/). Autocrop probes each page for its content bounding
box at low resolution; pages whose paper is darker than the threshold
(dense scans) simply stay uncropped.

## Install

```sh
uv tool install --system-certs git+https://github.com/tr1v3r/kittypdf
kittypdf book.pdf
```

Requires kitty (graphics protocol) and a terminal that reports pixel cell
sizes (kitty does).
