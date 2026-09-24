"""A floating table-of-contents panel over the current PDF page.

The page stays visible outside the panel; the caller restores it on exit.
"""

import re

# Keys: e/j/down move down, u/k/up move up, E/U big jumps, g/G ends,
# +/- adjusts visible chapters, Enter jumps, q/Esc closes, Ctrl-C quits.
# Counts multiply moves and size changes.

_BIG_JUMP = 10

_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
    (0xFE30, 0xFE6F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)


def _dw(text):
    """Rough display width (CJK counted as 2 cells)."""
    total = 0
    for ch in text:
        code = ord(ch)
        total += 2 if any(lo <= code <= hi for lo, hi in _WIDE_RANGES) else 1
    return total


def _fit(text, width):
    """Truncate to `width` display cells."""
    out = []
    used = 0
    for ch in text:
        w = 2 if any(lo <= ord(ch) <= hi for lo, hi in _WIDE_RANGES) else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _clamp_sel(toc, sel):
    return max(0, min(len(toc) - 1, sel))


def _initial_sel(toc, page0):
    """Last chapter whose start page is at or before the current page."""
    sel = 0
    for i, (_lvl, _title, page) in enumerate(toc):
        if page - 1 <= page0:
            sel = i
        else:
            break
    return sel


# Tokyo Night: night background, blue selection rail, subdued borders.
# The terminal's monospace face keeps outline numbers and page indices aligned.
_BG = "\x1b[48;2;26;27;38m"          # #1a1b26
_SELECTED = "\x1b[48;2;41;46;66m"    # #292e42
_PAPER = "\x1b[38;2;192;202;245m"   # #c0caf5
_MUTED = "\x1b[38;2;169;177;214m"   # #a9b1d6
_BORDER = "\x1b[38;2;86;95;137m"    # #565f89
_ACCENT = "\x1b[38;2;122;162;247m"  # #7aa2f7
_RESET = "\x1b[0m"


def _pad(text, width):
    """Fit and fill exactly width cells, including wide chapter titles."""
    text = _fit(text, max(0, width))
    return text + " " * max(0, width - _dw(text))


def _safe_title(title):
    """PDF outlines are untrusted: never print embedded terminal controls."""
    title = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", title)
    return "".join(ch if ch.isprintable() else " " for ch in title)


def _panel_size(term, toc, requested):
    """Clamp visible entries to the outline and the terminal's available rows."""
    visible = max(1, min(requested, len(toc), term.rows - 10))
    return visible, visible + 6


def _stale_rows(term, old_height, new_height):
    """Erase only rows the newly drawn panel no longer covers."""
    width = min(66, term.cols - 4)
    x = (term.cols - width) // 2 + 1
    old_y = (term.rows - old_height) // 2 + 1
    new_y = (term.rows - new_height) // 2 + 1
    return "".join(f"\x1b[{row};{x}H{_RESET}{' ' * width}"
                   for row in range(old_y, old_y + old_height)
                   if not new_y <= row < new_y + new_height)


def _draw(term, toc, sel, top, requested=15, previous_height=None):
    cols, rows = term.cols, term.rows
    if cols < 30 or rows < 12:
        # Tiny terminals have room for only one useful line, not a frame.
        line = _pad(f" {sel + 1}/{len(toc)}  {_safe_title(toc[sel][1])}"
                    f"  p.{toc[sel][2]}", max(0, cols - 1))
        term.write(f"\x1b[{max(1, rows // 2)};1H{_BG}{_PAPER}{line}{_RESET}")
        return sel

    width = min(66, cols - 4)
    visible, height = _panel_size(term, toc, requested)
    x = (cols - width) // 2 + 1
    y = (rows - height) // 2 + 1
    inner = width - 2
    content = inner - 4
    if sel < top:
        top = sel
    if sel >= top + visible:
        top = sel - visible + 1
    top = max(0, min(top, max(0, len(toc) - visible)))

    def at(row, text, foreground=_PAPER, background=_BG):
        return (f"\x1b[{y + row};{x}H{background}{foreground}"
                f"{text}{_RESET}")

    def body(text, foreground=_PAPER, background=_BG, rail=_BORDER):
        return (f"{rail}│{foreground}{background}"
                f"{_pad('  ' + text, inner)}{_BG}{_BORDER}│")

    position = f"{sel + 1:02d} / {len(toc):02d}  ·  {min(visible, len(toc))} shown"
    heading = "CONTENTS" + " " * max(1, content - 8 - _dw(position)) + position
    parts = [at(0, "╭" + "─" * inner + "╮", _BORDER),
             at(1, body(heading, _PAPER)),
             at(2, "├" + "─" * inner + "┤", _BORDER)]

    for offset in range(visible):
        i = top + offset
        if i >= len(toc):
            parts.append(at(3 + offset, body("")))
            continue
        level, title, page = toc[i]
        active = i == sel
        marker = "▏" if active else " "
        prefix = f"{marker} {i + 1:02d}  " + "  " * min(max(level - 1, 0), 4)
        suffix = f"  {page}"
        title_width = max(0, content - _dw(prefix) - _dw(suffix))
        name = _pad(_safe_title(title), title_width)
        text = _pad(prefix + name + suffix, content)
        fg = _ACCENT if active else _PAPER
        bg = _SELECTED if active else _BG
        parts.append(at(3 + offset, body(text, fg, bg,
                                         _ACCENT if active else _BORDER)))

    parts.extend([at(height - 3, "├" + "─" * inner + "┤", _BORDER),
                  at(height - 2, body("↑↓ move   +/- size   Enter open   Esc close",
                                      _MUTED)),
                  at(height - 1, "╰" + "─" * inner + "╯", _BORDER)])
    # Kitty's synchronized-update mode keeps the whole resize offscreen until
    # the final frame. Never expose an empty card over the PDF in between.
    cleanup = (_stale_rows(term, previous_height, height)
               if previous_height is not None else "")
    term.write("\x1b[?2026h" + "".join(parts) + cleanup + "\x1b[?2026l")
    return top


def toc_loop(term, toc, page0):
    """Run the overlay.

    Returns ("jump", page0_based), ("cancel",) or ("eof",).
    """
    if not toc:
        return ("cancel", None)
    sel = _initial_sel(toc, page0)
    top = 0
    visible_rows = max(1, min(15, len(toc), term.rows - 10))
    top = _draw(term, toc, sel, top, visible_rows)
    count = ""
    while True:
        event = term.read_key(timeout=0.25)
        # The caller re-lays out the page on exit. Closing on resize avoids
        # leaving a stale panel or a displaced image behind the new geometry.
        if term.take_resize():
            return ("cancel", None)
        if event is None:
            continue
        kind, val = event
        if kind == "eof":
            return ("eof", None)
        n = int(count) if count else 1

        if kind == "char":
            if val.isdigit():
                count += val
                continue
            if val in ("+", "=", "-", "_"):
                delta = n if val in ("+", "=") else -n
                limit = max(1, min(len(toc), term.rows - 10))
                new_rows = max(1, min(limit, visible_rows + delta))
                count = ""
                if new_rows != visible_rows:
                    old_height = _panel_size(term, toc, visible_rows)[1]
                    visible_rows = new_rows
                    top = _draw(term, toc, sel, top, visible_rows,
                                previous_height=old_height)
                continue
            if val in ("e", "j"):
                sel = _clamp_sel(toc, sel + n)
            elif val in ("u", "k"):
                sel = _clamp_sel(toc, sel - n)
            elif val == "E":
                sel = _clamp_sel(toc, sel + _BIG_JUMP * n)
            elif val == "U":
                sel = _clamp_sel(toc, sel - _BIG_JUMP * n)
            elif val == "g":
                sel = 0
            elif val == "G":
                sel = len(toc) - 1
            elif val == " ":
                sel = _clamp_sel(toc, sel + n)
            elif val == "q":
                # NB: 't' deliberately does NOT close the overlay: the key
                # that opens a mode must not also close it, or holding it
                # (key repeat) oscillates open/close forever.
                return ("cancel", None)
            else:
                count = ""
                continue
        elif kind == "key":
            if val == "down":
                sel = _clamp_sel(toc, sel + n)
            elif val == "up":
                sel = _clamp_sel(toc, sel - n)
            elif val == "home":
                sel = 0
            elif val == "end":
                sel = len(toc) - 1
            else:
                count = ""
                continue
        elif kind == "enter":
            return ("jump", toc[sel][2] - 1)
        elif kind == "esc":
            return ("cancel", None)
        elif kind == "ctrl" and val == "C":
            return ("eof", None)
        else:
            count = ""
            continue
        count = ""
        top = _draw(term, toc, sel, top, visible_rows)
