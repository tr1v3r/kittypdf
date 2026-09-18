"""Table-of-contents overlay: list chapters, move, jump.

The overlay is plain text on an otherwise cleared screen; the page image
stays transmitted in terminal memory, so closing the overlay only needs a
re-place, not a re-render (the caller invalidates anyway for simplicity).
"""

# Keys: e/j/down move down, u/k/up move up, E/U big jumps, gg/G ends,
# Enter jumps, t/q/Esc closes, Ctrl-C quits the app. Counts multiply moves.

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


def _draw(term, toc, sel, top):
    term.write("\x1b[H\x1b[2J")
    header = _fit(f" Table of Contents ({len(toc)})", term.cols - 1)
    term.write(f"\x1b[7m{header}\x1b[27m")
    visible = max(1, term.rows - 2)
    if sel < top:
        top = sel
    if sel >= top + visible:
        top = sel - visible + 1
    top = max(0, min(top, max(0, len(toc) - visible)))
    for i in range(top, min(top + visible, len(toc))):
        level, title, page = toc[i]
        right = f"{page}"
        budget = max(3, term.cols - len(right) - 4)
        body = _fit(f"{'❯' if i == sel else ' '} {'  ' * min(level - 1, 8)}"
                    f"{title}", budget)
        line = body + " " * max(1, budget - _dw(body)) + right + " "
        if i == sel:
            term.write(f"\x1b[{i - top + 2};1H\x1b[7m{line}\x1b[27m")
        else:
            term.write(f"\x1b[{i - top + 2};1H{line}")
    hint = _fit(" e/u move · Enter jump · t/q close", term.cols - 1)
    term.write(f"\x1b[{term.rows};1H{hint}"[:term.cols + 8])
    return top


def toc_loop(term, toc, page0):
    """Run the overlay.

    Returns ("jump", page0_based), ("cancel",) or ("eof",).
    """
    if not toc:
        return ("cancel", None)
    sel = _initial_sel(toc, page0)
    top = 0
    top = _draw(term, toc, sel, top)
    count = ""
    while True:
        event = term.read_key(timeout=None)
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
            elif val in ("t", "q"):
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
        top = _draw(term, toc, sel, top)
