# kittypdf

A minimal PDF reader that lives inside the [kitty](https://sw.kovidgoyal.net/kitty/)
terminal. Pages are rendered by [PyMuPDF](https://pymupdf.readthedocs.io/) and
drawn as real pixels through kitty's graphics protocol — no ASCII art.

Written from scratch; inspired by the (dormant) `termpdf.py`.

## Usage

```sh
kittypdf book.pdf              # or the `pdf` alias
kittypdf -p 20 book.pdf        # open at page 20 (1-based)
kittypdf -m dual book.pdf       # start layout: auto | single | dual
kittypdf --transparent book.pdf # show unpainted paper over kitty's background
kittypdf --no-transparent book.pdf # explicitly restore opaque paper
kittypdf -V                     # version
```

## Keys

Movement follows the colemak diamond (`u` = up, `e` = down), as in a colemak
vim setup. `i` — the diamond's right slot — stays on invert colors: a reader
has no horizontal motion, so nothing competes for it.

**Paging**

| Key | Action |
|---|---|
| `e` / `↓` / `Space` / `Enter` | next page |
| `u` / `↑` | previous page |
| `E` / `U` | big jump: 10 pages forward / back |
| `PgDn` / `PgUp` | next / previous page |
| `j` / `k` / `b` | qwerty aliases (`j` next, `k` / `b` back) |

**Jumping**

| Key | Action |
|---|---|
| `gg` | first page |
| `G` | last page |
| `Home` / `End` | first / last page |
| `t` | table of contents overlay |

**Display**

| Key | Action |
|---|---|
| `i` | invert colors (dark reading) |
| `a` | toggle transparent unpainted paper (alpha mode) |
| `c` | toggle autocrop (trim page margins) |
| `d` | cycle page layout: auto → single → dual |
| `r` / `Ctrl-L` | redraw current page |

**Quitting**

| Key | Action |
|---|---|
| `q` / `Q` / `Ctrl-C` | quit (saves progress) |

**Counts** work like vim: `10e` ten pages forward, `3E` thirty, `12G` page 12,
`5gg` page 5. Any unrelated key clears the pending count.

**In a spread** `e` / `u` step two pages at a time.

## Table of contents

`t` opens a floating chapter list over the PDF page. Move with `e` / `u`
(or `j` / `k`, arrows), `E` / `U` jump ten, `g` / `G` go to the ends, `Enter`
jumps to the selected chapter. `+` / `-` changes the number of visible
chapters (`=` also enlarges; counts work, e.g. `5+`), up to the terminal's
available height. `q` / `Esc` closes the panel. `t` deliberately does **not**
close it: the key that opens a mode must not also close it, or holding it
oscillates the overlay open and shut.

## Dual-page spreads

By default the layout is **auto**: kittypdf shows two pages side by side
whenever the window is wide enough that a single page would only be
limited by height (e.g. a portrait PDF in a full-screen wide terminal),
and falls back to a single page otherwise. Resizing re-evaluates it live.
Press `d` to cycle auto → single → dual, or start with `-m/--mode
{auto,single,dual}`. In a spread, `e`/`u` move by two pages and the status
bar shows a range like `[3-4/120]`.

## Status bar

`[3-4/120]-cad` reads as: pages 3–4 of 120, `-` invert is on, `c` autocrop
is on, and `a` transparent paper is on. A trailing mode flag (`s` single / `d`
dual) appears only when the layout was forced with `d` or `-m`; auto shows no
flag.

## Transparent paper

Press `a` or start with `--transparent` to let kitty's background show through
parts of the PDF page that contain no painted content. This preserves text and
images at full opacity. It deliberately does **not** remove white pixels:
explicit white page backgrounds, white areas in figures, and full-page scans
remain opaque. Use `i` as well when dark text needs inverting for a dark kitty
background. Kitty's `background_opacity` controls how much of the desktop is
visible behind the terminal.

## Progress memory

The last-read page, invert, crop, and transparent-paper settings are remembered
per file (`~/.cache/kittypdf/`). `--transparent` and `--no-transparent` choose
the initial alpha setting; the final setting is remembered on exit. Autocrop
probes each page for its content bounding box at low resolution; pages whose
paper is darker than the
threshold (dense scans) simply stay uncropped.

## Install

```sh
uv tool install --system-certs git+https://github.com/tr1v3r/kittypdf
kittypdf book.pdf
```

Requires kitty (graphics protocol) and a terminal that reports pixel cell
sizes (kitty does).
