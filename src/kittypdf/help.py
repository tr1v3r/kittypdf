"""Scrollable keyboard reference, styled like the contents panel."""

import textwrap

from .toc import _ACCENT, _BG, _BORDER, _MUTED, _PAPER, _RESET, _pad, _panel_size, _panel_width

_READER_KEYS = (
    ("READING / PAGING", ""),
    ("e / j / Down / Space / Enter", "Next page (two pages in a spread)"),
    ("u / k / b / Up", "Previous page (two pages in a spread)"),
    ("PgDn / PgUp", "Next / previous page"),
    ("E / U", "Forward / back 10 steps"),
    ("gg / Home", "First page"),
    ("G / End", "Last page"),
    ("[count] + movement", "Repeat movement; 10e = 10 steps, 3E = 30"),
    ("[page]G / [page]gg", "Jump to page number (1-based)"),
    ("READING / DISPLAY", ""),
    ("t", "Open table of contents"),
    ("i", "Toggle inverted colors"),
    ("a", "Toggle transparent unpainted paper"),
    ("c", "Toggle margin autocrop"),
    ("d", "Cycle layout: auto -> single -> dual"),
    ("r / R / Ctrl-L", "Redraw page"),
    ("?", "Open keyboard shortcuts"),
    ("q / Q / Ctrl-C", "Quit and save progress"),
)
_TOC_KEYS = (
    ("TABLE OF CONTENTS", ""),
    ("e / j / Down / Space", "Next chapter"),
    ("u / k / Up", "Previous chapter"),
    ("E / U", "Forward / back 10 chapters"),
    ("g / Home / G / End", "First / last chapter (g/Home, G/End)"),
    ("+ / =", "Show more chapters"),
    ("- / _", "Show fewer chapters"),
    ("[count] + move / size", "Repeat movement or size change"),
    ("Enter", "Open selected chapter"),
    ("?", "Open keyboard shortcuts"),
    ("t / q / Esc", "Close contents"),
    ("Ctrl-C", "Quit and save progress"),
)
_HELP_KEYS = (
    ("KEYBOARD HELP", ""),
    ("e / j / Down / Space / Enter", "Scroll down"),
    ("u / k / Up", "Scroll up"),
    ("E / U / PgDn / PgUp", "Scroll down / up one screen"),
    ("g / Home / G / End", "First / last line (g/Home, G/End)"),
    ("q / Esc", "Close help; ? stays open"),
    ("Ctrl-C", "Quit and save progress"),
)


def _lines(width, context):
    """Wrap rather than truncate, so every binding is reachable at any width."""
    sections = ((_TOC_KEYS, _READER_KEYS) if context == "toc"
                else (_READER_KEYS, _TOC_KEYS))
    lines = []
    for section in (*sections, _HELP_KEYS):
        if lines:
            lines.append(("", False))
        for keys, action in section:
            text = f"{keys:<29}  {action}" if action else keys
            for line in textwrap.wrap(text, width=max(1, width),
                                      subsequent_indent="  " if width > 2 else ""):
                lines.append((line, not action))
    return lines


def _draw(term, lines, top):
    """Draw a bounded card, with a minimal unframed fallback for tiny windows."""
    cols, rows = max(1, term.cols), max(1, term.rows)
    framed = cols >= 30 and rows >= 10
    width = _panel_width(term) if framed else max(1, cols - 1)
    if framed:
        visible, height = _panel_size(term)
    else:
        visible = max(1, rows - 2)
        height = min(rows, visible + 2)
    top = max(0, min(top, max(0, len(lines) - visible)))
    x = (cols - width) // 2 + 1 if framed else 1
    y = (rows - height) // 2 + 1 if framed else 1
    inner = width - 2 if framed else width
    parts = []

    def at(row, text, color=_PAPER):
        parts.append(f"\x1b[{y + row};{x}H{_BG}{color}{text}{_RESET}")

    def body(text, color=_PAPER):
        return f"{_BORDER}│{color}{_pad(' ' + text, inner)}{_BORDER}│"

    title = f"SHORTCUTS  {top + 1}-{min(top + visible, len(lines))}/{len(lines)}"
    if framed:
        at(0, "╭" + "─" * inner + "╮", _BORDER)
        at(1, body(title, _ACCENT))
        at(2, "├" + "─" * inner + "┤", _BORDER)
    elif rows >= 2:
        at(0, _pad(title, width), _ACCENT)
    for offset in range(visible):
        i = top + offset
        text, heading = lines[i] if i < len(lines) else ("", False)
        row = offset + (3 if framed else int(rows >= 2))
        if row >= height:
            break
        color = _ACCENT if heading else _PAPER
        at(row, body(text, color) if framed else _pad(text, width), color)
    footer = "e/u scroll  PgDn/PgUp page  q/Esc close"
    if framed:
        at(height - 3, "├" + "─" * inner + "┤", _BORDER)
        at(height - 2, body(footer, _MUTED))
        at(height - 1, "╰" + "─" * inner + "╯", _BORDER)
    elif rows >= 3:
        at(height - 1, _pad(footer, width), _MUTED)
    term.write("\x1b[?2026h" + "".join(parts) + "\x1b[?2026l")
    return top, visible


def help_loop(term, context="reader"):
    """Return cancel, resize, or eof; the caller restores the underlying view."""
    framed = term.cols >= 30 and term.rows >= 10
    width = _panel_width(term) - 3 if framed else max(1, term.cols - 1)
    lines = _lines(width, context)
    # Like contents, paint only the card's cells. Keep the PDF image and all
    # cells outside the card intact so the page remains visible around it.
    top, visible = _draw(term, lines, 0)
    while True:
        event = term.read_key(timeout=0.25)
        if term.take_resize():
            return "resize"
        if event is None:
            continue
        kind, val = event
        if kind == "eof" or (kind == "ctrl" and val == "C"):
            return "eof"
        if kind == "esc" or (kind == "char" and val == "q"):
            return "cancel"
        if ((kind == "char" and val in ("e", "j", " "))
                or (kind == "key" and val == "down") or kind == "enter"):
            top += 1
        elif ((kind == "char" and val in ("u", "k"))
              or (kind == "key" and val == "up")):
            top -= 1
        elif (kind, val) in (("char", "E"), ("key", "pgdn")):
            top += visible
        elif (kind, val) in (("char", "U"), ("key", "pgup")):
            top -= visible
        elif (kind, val) in (("char", "g"), ("key", "home")):
            top = 0
        elif (kind, val) in (("char", "G"), ("key", "end")):
            top = len(lines)
        else:
            continue  # '?' repeats never oscillate the overlay open/closed.
        top, visible = _draw(term, lines, top)
